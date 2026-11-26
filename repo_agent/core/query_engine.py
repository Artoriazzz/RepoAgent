from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repo_agent.core.context import ConversationContextManager
from repo_agent.core.llm import ChatLLM, LLMOutputLimitError
from repo_agent.core.memory import MAX_SESSION_MEMORY_BYTES, MemoryStore, MemoryType, format_memories_for_injection
from repo_agent.core.prompts import build_dynamic_context, build_system_prompt, with_public_evidence_constraint
from repo_agent.core.query_loop import AbortSignal, LoopEvent, LoopEventType, query_loop
from repo_agent.core.session_memory import ShortTermMemory
from repo_agent.core.stage_history import ArchivedMessages, StageConversation
from repo_agent.core.tool import ToolRegistry


class QueryEngine:
    """会话级调度层。

    QueryEngine 负责整个对话生命周期：初始化消息、接收用户输入、累计 token、
    写 trace、自动保存和中断。真正的 LLM→工具循环放在 query_loop 中。
    """

    def __init__(
        self,
        llm: ChatLLM,
        tools: ToolRegistry,
        system_prompt: str | None = None,
        max_steps: int = 20,
        output_dir: Path | str = ".repo_agent_runs/latest",
        cwd: Path | str | None = None,
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
        excluded_memory_types: set[MemoryType] | None = None,
        memory_prompt_enabled: bool = True,
        memory_recall_enabled: bool = True,
        tool_prompt_enabled: bool = True,
        include_dynamic_context: bool = True,
        completion_guard: Callable[[str], str | None] | None = None,
        completion_guard_extra_turns: int = 0,
        force_completion_after_max_steps: bool = False,
        prompt_version: str = "",
    ):
        # 运行依赖：LLM 决定如何推理，ToolRegistry 决定允许执行哪些动作。
        self.llm = llm
        self.tools = tools
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.semantic_memory_recall = semantic_memory_recall
        self.excluded_memory_types = excluded_memory_types or set()
        self.memory_prompt_enabled = memory_prompt_enabled
        self.memory_recall_enabled = memory_recall_enabled
        self.already_surfaced_memories: set[str] = set()
        self.session_memory_bytes = 0
        self.short_memory = ShortTermMemory(repo_path=str(self.cwd))
        self._hydrate_repo_context()
        self.context_manager = ConversationContextManager(self.output_dir)
        # 缓存友好装配：system prompt 只包含稳定规则，工具规则对同一角色也是固定的；
        # 日期、工作目录、Git 状态、项目指令和召回记忆全部后置到第一条 user 消息。
        # 这样同一 Agent 处理不同题目时，请求前缀（system + 工具 schema）可以命中缓存。
        tool_prompt = tools.prompt().strip() if tool_prompt_enabled else ""
        memory_prompt = (
            self.memory_store.build_prompt_section(excluded_types=self.excluded_memory_types)
            if memory_prompt_enabled
            else ""
        )
        self.system_prompt = (
            build_system_prompt(tool_rules=tool_prompt)
            if system_prompt is None
            else self._append_stable_prompt_sections(system_prompt, tool_prompt)
        )
        self.dynamic_context = build_dynamic_context(
            cwd=self.cwd,
            memory_section=memory_prompt,
            include_dynamic_context=include_dynamic_context,
        )
        self.max_steps = max_steps
        self.completion_guard = completion_guard
        self.completion_guard_extra_turns = max(0, completion_guard_extra_turns)
        self.force_completion_after_max_steps = force_completion_after_max_steps
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        # 动态上下文只在会话初始化时快照一次，后续轮次保持不变，使多轮请求也能共享前缀。
        if self.dynamic_context.strip():
            self.messages.append({"role": "user", "content": self.dynamic_context})
        self.stage_conversation = StageConversation(self.output_dir)
        if self.stage_conversation.path is not None:
            self.messages = ArchivedMessages(self.messages)
            self.messages.restore(self.stage_conversation.previous)
            self.stage_conversation.append_material(self.messages)
        # token 和完成状态属于会话级指标，所以由 Engine 持有，而不是塞进纯循环。
        self.prompt_version = prompt_version
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cached_tokens = 0
        self.total_tokens = 0
        self.last_input_token_count = 0
        self.completed = False
        self.stop_reason = "not_started"
        self.abort_signal = AbortSignal()

    def chat(self, user_message: str) -> str:
        """同步入口，便于 CLI 调用。"""
        return asyncio.run(self.achat(user_message))

    async def achat(self, user_message: str) -> str:
        """执行一次用户回合，并把 query_loop 事件归档为会话状态。"""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.abort_signal = AbortSignal()
        self.short_memory.start_turn(user_message)
        self.messages.append({"role": "user", "content": user_message})
        # 长期记忆作为额外 user message 注入，而不是永久拼入 system prompt。
        # already_surfaced_memories 会避免同一会话反复召回同一条记忆。
        self._inject_recalled_memories(user_message)
        self.context_manager.maybe_auto_compact(self.messages, self.short_memory)
        final_answer = ""
        self.completed = False
        self.stop_reason = "running"

        try:
            # query_loop 是异步生成器：它只产生事实事件；Engine 决定如何累计统计、
            # 写 trace、更新短期记忆以及解释最终停止原因。
            async for event in query_loop(
                messages=self.messages,
                llm=self.llm,
                tools=self.tools,
                max_steps=self.max_steps,
                abort_signal=self.abort_signal,
                context_manager=self.context_manager,
                completion_guard=self.completion_guard,
                completion_guard_extra_turns=self.completion_guard_extra_turns,
                force_completion_after_max_steps=self.force_completion_after_max_steps,
            ):
                self._handle_event(event)
                if event.type == LoopEventType.FINISH:
                    final_answer = event.payload.get("answer", "")
                    if event.payload.get("aborted"):
                        self.completed = False
                        self.stop_reason = "aborted"
                    elif event.payload.get("max_steps_reached"):
                        self.completed = False
                        self.stop_reason = "max_steps"
                    elif event.payload.get("terminal_incomplete"):
                        self.completed = False
                        self.stop_reason = str(event.payload.get("terminal_reason") or "incomplete")
                    else:
                        self.completed = True
                        self.stop_reason = "finished"
                    break
        except LLMOutputLimitError:
            self.stop_reason = "output_limit"
            raise
        finally:
            self._auto_save(final_answer)

        return final_answer

    def abort(self) -> None:
        self.abort_signal.abort()

    def _handle_event(self, event: LoopEvent) -> None:
        """把一个循环事件投影到 token、上下文、短期记忆和 trace。"""

        if event.type == LoopEventType.LLM_RESPONSE:
            usage = event.payload.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0)
            cached_tokens = int(usage.get("cached_tokens", 0) or 0)
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_cached_tokens += cached_tokens
            self.total_tokens += total_tokens
            self.last_input_token_count = prompt_tokens
            self.context_manager.after_model_call(prompt_tokens)
            # query_loop 会继续产生后续模型调用；在这里检查，而不是只在本轮开始前检查，
            # 才能让长工具会话真正触发自动压缩。
            self.context_manager.maybe_auto_compact(self.messages, self.short_memory)
        self.short_memory.handle_event(event)
        self._write_trace({"type": event.type.value, "step": event.step, **event.payload})

    def _write_trace(self, event: dict[str, Any]) -> None:
        with (self.output_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _auto_save(self, final_answer: str) -> None:
        """无论成功、异常还是中断都保存可复盘的会话快照。"""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(self.messages, ArchivedMessages):
            self.stage_conversation.save(self.messages.originals)
        (self.output_dir / "messages.json").write_text(
            json.dumps(self.messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "final_answer.md").write_text(final_answer, encoding="utf-8")
        (self.output_dir / "session.json").write_text(
            json.dumps(
                {
                    "prompt_version": self.prompt_version,
                    "total_prompt_tokens": self.total_prompt_tokens,
                    "total_cached_tokens": self.total_cached_tokens,
                    "cache_hit_rate": (
                        round(self.total_cached_tokens / self.total_prompt_tokens, 4)
                        if self.total_prompt_tokens
                        else 0.0
                    ),
                    "total_completion_tokens": self.total_completion_tokens,
                    "total_tokens": self.total_tokens,
                    "last_input_token_count": self.last_input_token_count,
                    "max_steps": self.max_steps,
                    "completion_guard_extra_turns": self.completion_guard_extra_turns,
                    "force_completion_after_max_steps": self.force_completion_after_max_steps,
                    "completed": self.completed,
                    "stop_reason": self.stop_reason,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.short_memory.save(self.output_dir)

    def _inject_recalled_memories(self, user_message: str) -> None:
        if not self.memory_recall_enabled:
            return
        if self.session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
            return
        if len(user_message.strip()) < 4:
            return
        side_query = self._side_query if self.semantic_memory_recall and hasattr(self.llm, "invoke_text") else None
        memories = self.memory_store.recall(
            user_message,
            already_surfaced=self.already_surfaced_memories,
            side_query=side_query,
            excluded_types=self.excluded_memory_types,
        )
        if not memories:
            return
        injection = format_memories_for_injection(memories)
        injection_bytes = len(injection.encode("utf-8"))
        if self.session_memory_bytes + injection_bytes > MAX_SESSION_MEMORY_BYTES:
            return
        self.messages.append({"role": "user", "content": injection})
        self.session_memory_bytes += injection_bytes
        for memory in memories:
            self.already_surfaced_memories.add(str(memory.header.path))

    def _hydrate_repo_context(self) -> None:
        self.short_memory.branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        self.short_memory.initial_status = self._git(["status", "--short"]) or "clean"

    def _git(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    @staticmethod
    def _append_stable_prompt_sections(system_prompt: str, tool_prompt: str) -> str:
        """只追加同一角色稳定的工具规则。记忆和运行时上下文不在这里出现。"""

        sections = [with_public_evidence_constraint(system_prompt.rstrip())]
        if tool_prompt.strip():
            sections.append(f"# Tool-specific Rules\n{tool_prompt.strip()}")
        return "\n\n".join(sections)

    def _side_query(self, system: str, user: str) -> str:
        invoke_text = getattr(self.llm, "invoke_text")
        return invoke_text(system, user)
