import asyncio
import threading

import pytest

from repo_agent.core.llm import LLMToolResponse
from repo_agent.core.memory import MemoryStore
from repo_agent.core.runtime_pool import (
    AgentRuntime,
    AgentRuntimePool,
    AgentRuntimePoolRegistry,
    AgentSession,
    MAX_ROLE_POOL_CAPACITY,
)
from repo_agent.core.tool import ToolRegistry


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)

    def invoke_with_tools(self, _messages, _tools):
        answer = self.answers.pop(0)
        return LLMToolResponse(answer, [], {"content": answer}, {})


def _runtime(llm=None):
    return AgentRuntime("implement", llm or FakeLLM(["done"]), "system", 2)


def test_runtime_pool_blocks_until_runtime_is_returned():
    pool = AgentRuntimePool("implement", 1, _runtime, acquire_timeout_seconds=1)
    waiting = threading.Event()
    acquired = threading.Event()

    def borrow():
        waiting.set()
        with pool.acquire():
            acquired.set()

    with pool.acquire():
        thread = threading.Thread(target=borrow)
        thread.start()
        assert waiting.wait(0.2)
        assert not acquired.wait(0.05)
    thread.join(timeout=1)

    assert acquired.is_set()
    assert pool.snapshot() == {
        "role": "implement",
        "capacity": 1,
        "available": 1,
        "borrowed": 0,
        "peak_borrowed": 1,
        "wait_count": 1,
    }


def test_runtime_pool_returns_runtime_after_failure():
    pool = AgentRuntimePool("review", 1, lambda: _runtime(), acquire_timeout_seconds=1)

    with pytest.raises(RuntimeError):
        with pool.acquire():
            raise RuntimeError("session failed")

    assert pool.snapshot()["available"] == 1


def test_runtime_pool_supports_async_waiting():
    pool = AgentRuntimePool(
        "research",
        1,
        lambda: AgentRuntime("research", FakeLLM(["done"]), "system", 2),
        acquire_timeout_seconds=1,
    )

    async def borrow():
        async with pool.acquire_async() as runtime:
            return runtime.role

    assert asyncio.run(borrow()) == "research"
    assert pool.snapshot()["available"] == 1


def test_async_role_gate_waits_without_sharing_a_session():
    pool = AgentRuntimePool("implement", 1, _runtime, acquire_timeout_seconds=1)

    async def exercise():
        first_acquired = asyncio.Event()
        release_first = asyncio.Event()
        second_acquired = asyncio.Event()

        async def first():
            async with pool.acquire_async() as runtime:
                first_acquired.set()
                await release_first.wait()
                return runtime

        async def second():
            await first_acquired.wait()
            async with pool.acquire_async() as runtime:
                second_acquired.set()
                return runtime

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_acquired.wait()
        await asyncio.sleep(0.05)
        assert not second_acquired.is_set()
        release_first.set()
        first_runtime, second_runtime = await asyncio.gather(first_task, second_task)
        return first_runtime, second_runtime

    first_runtime, second_runtime = asyncio.run(exercise())

    assert first_runtime is second_runtime
    assert pool.snapshot()["wait_count"] >= 1


def test_registry_reuses_matching_role_runtime_pool():
    llm = FakeLLM(["one"])
    registry = AgentRuntimePoolRegistry({"research": 2})

    first = registry.pool_for(role="research", llm=llm, system_prompt="prompt", max_steps=3)
    second = registry.pool_for(role="research", llm=llm, system_prompt="prompt", max_steps=3)

    assert first is second
    assert first.capacity == 2


def test_role_pool_capacity_is_hard_capped_at_three():
    with pytest.raises(ValueError, match="between 1 and 3"):
        AgentRuntimePool("research", MAX_ROLE_POOL_CAPACITY + 1, _runtime)

    with pytest.raises(ValueError, match="between 1 and 3"):
        AgentRuntimePoolRegistry({"research": MAX_ROLE_POOL_CAPACITY + 1})


def test_agent_session_is_single_use_and_keeps_conversation_local(tmp_path):
    llm = FakeLLM(["first answer", "second answer"])
    runtime = AgentRuntime("research", llm, "shared system", 2)
    memory = MemoryStore(tmp_path / "memory")
    first = AgentSession(
        runtime=runtime,
        tools=ToolRegistry(),
        cwd=tmp_path,
        output_dir=tmp_path / "first",
        memory_store=memory,
        semantic_memory_recall=False,
        excluded_memory_types=set(),
        memory_prompt_enabled=False,
        memory_recall_enabled=False,
        include_dynamic_context=False,
    )
    second = AgentSession(
        runtime=runtime,
        tools=ToolRegistry(),
        cwd=tmp_path,
        output_dir=tmp_path / "second",
        memory_store=memory,
        semantic_memory_recall=False,
        excluded_memory_types=set(),
        memory_prompt_enabled=False,
        memory_recall_enabled=False,
        include_dynamic_context=False,
    )

    first_answer, first_engine = first.execute("task one")
    second_answer, second_engine = second.execute("task two")

    assert first_answer == "first answer"
    assert second_answer == "second answer"
    assert first_engine is not second_engine
    assert all("task two" not in str(message) for message in first_engine.messages)
    with pytest.raises(RuntimeError, match="single-use"):
        first.execute("run again")
