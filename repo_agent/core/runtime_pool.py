from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import threading
import weakref
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, AsyncIterator

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore, MemoryType
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolRegistry


MAX_ROLE_POOL_CAPACITY = 3


# Runtime 是无状态的，可由不同任务复用；会话、工具状态和上下文则始终放在
# AgentSession 中。这里把每个角色的并发硬限制为三个，避免单个角色抢占全部模型
# 配额或让本机同时堆积过多长上下文请求。
DEFAULT_ROLE_POOL_SIZES = {
    "coordinator": 2,
    "research": 3,
    "reproducer": 2,
    "test_expander": 2,
    "plan": 2,
    "implement": 3,
    "patch_ranker": 2,
    "patch_review": 2,
    "verify": 2,
    "review": 2,
    "failure_review": 2,
    "memory_curator": 1,
    "case_curator": 1,
    "experience_curator": 1,
    "general": 2,
}


@dataclass(frozen=True)
class AgentRuntime:
    """可跨任务复用的无状态角色运行时。"""

    role: str
    llm: ChatLLM
    system_prompt: str
    max_steps: int
    # 稳定 Prompt 版本标识，用于按角色与版本分组统计缓存命中率。
    prompt_version: str = ""


@dataclass
class AgentSession:
    """一次性 Agent 会话；所有任务状态都停留在这里。"""

    runtime: AgentRuntime
    tools: ToolRegistry
    cwd: Path
    output_dir: Path
    memory_store: MemoryStore
    semantic_memory_recall: bool
    excluded_memory_types: set[MemoryType]
    memory_prompt_enabled: bool
    memory_recall_enabled: bool
    include_dynamic_context: bool
    completion_guard: Callable[[str], str | None] | None = None
    completion_guard_extra_turns: int = 0
    force_completion_after_max_steps: bool = False
    _used: bool = False

    def execute(self, prompt: str) -> tuple[str, QueryEngine]:
        if self._used:
            raise RuntimeError("AgentSession is single-use")
        self._used = True
        engine = QueryEngine(
            llm=self.runtime.llm,
            tools=self.tools,
            system_prompt=self.runtime.system_prompt,
            max_steps=self.runtime.max_steps,
            output_dir=self.output_dir,
            cwd=self.cwd,
            memory_store=self.memory_store,
            semantic_memory_recall=self.semantic_memory_recall,
            excluded_memory_types=self.excluded_memory_types,
            memory_prompt_enabled=self.memory_prompt_enabled,
            memory_recall_enabled=self.memory_recall_enabled,
            include_dynamic_context=self.include_dynamic_context,
            completion_guard=self.completion_guard,
            completion_guard_extra_turns=self.completion_guard_extra_turns,
            force_completion_after_max_steps=self.force_completion_after_max_steps,
            prompt_version=self.runtime.prompt_version,
        )
        return engine.chat(prompt), engine


class AgentRuntimePool:
    """固定容量 Runtime 池；池耗尽时等待已有 Runtime 归还。"""

    def __init__(
        self,
        role: str,
        capacity: int,
        factory: Callable[[], AgentRuntime],
        *,
        acquire_timeout_seconds: float = 600,
    ) -> None:
        if not 1 <= capacity <= MAX_ROLE_POOL_CAPACITY:
            raise ValueError(
                f"AgentRuntimePool capacity must be between 1 and {MAX_ROLE_POOL_CAPACITY}"
            )
        self.role = role
        self.capacity = capacity
        self._timeout = max(0.1, acquire_timeout_seconds)
        self._available: queue.LifoQueue[AgentRuntime] = queue.LifoQueue(maxsize=capacity)
        for _ in range(capacity):
            self._available.put_nowait(factory())
        self._metrics_lock = threading.Lock()
        self._async_gates_lock = threading.Lock()
        # asyncio 原语与事件循环绑定。弱引用让临时事件循环结束后不残留引用；底层
        # LifoQueue 仍是跨线程/跨事件循环的最终容量边界。
        self._async_gates: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.BoundedSemaphore
        ] = weakref.WeakKeyDictionary()
        self._borrowed = 0
        self._peak_borrowed = 0
        self._wait_count = 0

    @contextmanager
    def acquire(self) -> Iterator[AgentRuntime]:
        runtime = self._take()
        try:
            yield runtime
        finally:
            self._return(runtime)

    @asynccontextmanager
    async def acquire_async(self) -> AsyncIterator[AgentRuntime]:
        """异步角色槽位先限流，再非阻塞地借用 Runtime。

        同一个事件循环内不会为超出角色容量的调用创建等待线程；如果另一个线程或事件
        循环暂时占满了底层池，则以短暂 await 轮询，而不是阻塞当前事件循环。
        """
        gate = self._async_gate_for_running_loop()
        if gate.locked():
            self._record_wait()
        try:
            await asyncio.wait_for(gate.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError as error:
            raise TimeoutError(
                f"Timed out waiting for agent runtime role={self.role}"
            ) from error
        runtime: AgentRuntime | None = None
        try:
            runtime = await self._take_async()
            yield runtime
        finally:
            if runtime is not None:
                self._return(runtime)
            gate.release()

    def snapshot(self) -> dict[str, int | str]:
        with self._metrics_lock:
            return {
                "role": self.role,
                "capacity": self.capacity,
                "available": self._available.qsize(),
                "borrowed": self._borrowed,
                "peak_borrowed": self._peak_borrowed,
                "wait_count": self._wait_count,
            }

    def _take(self) -> AgentRuntime:
        with self._metrics_lock:
            if self._available.empty():
                self._wait_count += 1
        try:
            runtime = self._available.get(timeout=self._timeout)
        except queue.Empty as error:
            raise TimeoutError(
                f"Timed out waiting for agent runtime role={self.role}"
            ) from error
        self._record_borrow()
        return runtime

    async def _take_async(self) -> AgentRuntime:
        deadline = asyncio.get_running_loop().time() + self._timeout
        recorded_wait = False
        while True:
            try:
                runtime = self._available.get_nowait()
            except queue.Empty:
                if not recorded_wait:
                    self._record_wait()
                    recorded_wait = True
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for agent runtime role={self.role}"
                    )
                await asyncio.sleep(min(0.05, remaining))
                continue
            self._record_borrow()
            return runtime

    def _async_gate_for_running_loop(self) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        with self._async_gates_lock:
            gate = self._async_gates.get(loop)
            if gate is None:
                gate = asyncio.BoundedSemaphore(self.capacity)
                self._async_gates[loop] = gate
            return gate

    def _record_borrow(self) -> None:
        with self._metrics_lock:
            self._borrowed += 1
            self._peak_borrowed = max(self._peak_borrowed, self._borrowed)

    def _record_wait(self) -> None:
        with self._metrics_lock:
            self._wait_count += 1

    def _return(self, runtime: AgentRuntime) -> None:
        with self._metrics_lock:
            self._borrowed -= 1
        self._available.put_nowait(runtime)


class AgentRuntimePoolRegistry:
    """按角色、模型配置和 Prompt 版本惰性创建共享 Runtime 池。"""

    def __init__(
        self,
        capacities: dict[str, int] | None = None,
        *,
        acquire_timeout_seconds: float | None = None,
    ) -> None:
        self._capacities = dict(DEFAULT_ROLE_POOL_SIZES)
        self._capacities.update(_role_pool_sizes_from_environment())
        if capacities:
            self._capacities.update(capacities)
        if any(not 1 <= value <= MAX_ROLE_POOL_CAPACITY for value in self._capacities.values()):
            raise ValueError(
                f"Every agent role pool size must be between 1 and {MAX_ROLE_POOL_CAPACITY}"
            )
        self._timeout = acquire_timeout_seconds or float(
            os.getenv("REPO_AGENT_ROLE_POOL_TIMEOUT_SECONDS", "600")
        )
        self._pools: dict[str, AgentRuntimePool] = {}
        self._lock = threading.Lock()

    def pool_for(
        self,
        *,
        role: str,
        llm: ChatLLM,
        system_prompt: str,
        max_steps: int,
        prompt_version: str = "",
    ) -> AgentRuntimePool:
        key = _runtime_key(role, llm, system_prompt, max_steps, prompt_version)
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                capacity = self._capacities.get(role, 1)
                pool = AgentRuntimePool(
                    role,
                    capacity,
                    lambda: AgentRuntime(role, llm, system_prompt, max_steps, prompt_version),
                    acquire_timeout_seconds=self._timeout,
                )
                self._pools[key] = pool
            return pool

    def snapshots(self) -> list[dict[str, int | str]]:
        with self._lock:
            pools = list(self._pools.values())
        return [pool.snapshot() for pool in pools]


_DEFAULT_REGISTRY: AgentRuntimePoolRegistry | None = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def default_runtime_pool_registry() -> AgentRuntimePoolRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = AgentRuntimePoolRegistry()
        return _DEFAULT_REGISTRY


def _runtime_key(
    role: str,
    llm: ChatLLM,
    system_prompt: str,
    max_steps: int,
    prompt_version: str = "",
) -> str:
    if not isinstance(llm, ChatLLM):
        llm_identity = f"custom:{type(llm).__module__}.{type(llm).__qualname__}:{id(llm)}"
    else:
        credentials = hashlib.sha256(str(llm.api_key).encode()).hexdigest()[:12]
        llm_identity = "|".join(
            str(part)
            for part in (
                llm.model,
                llm.base_url,
                llm.temperature,
                llm.reasoning_effort,
                llm.enable_thinking,
                credentials,
            )
        )
    prompt = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
    parts: tuple[Any, ...] = (
        role,
        llm_identity,
        prompt,
        max_steps,
        prompt_version,
    )
    return "|".join(str(part) for part in parts)


def _role_pool_sizes_from_environment() -> dict[str, int]:
    raw = os.getenv("REPO_AGENT_ROLE_POOL_SIZES", "").strip()
    if not raw:
        return {}
    result: dict[str, int] = {}
    for item in raw.split(","):
        role, separator, value = item.strip().partition("=")
        if not separator or not role.strip():
            raise ValueError(
                "REPO_AGENT_ROLE_POOL_SIZES must use role=size comma-separated entries"
            )
        result[role.strip()] = int(value.strip())
    return result
