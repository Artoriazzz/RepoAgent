from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from repo_agent.core.context import ConversationContextManager
from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MAX_SESSION_MEMORY_BYTES, MemoryStore, format_memories_for_injection
from repo_agent.core.prompts import build_system_prompt
from repo_agent.core.query_loop import AbortSignal, LoopEvent, LoopEventType, query_loop
from repo_agent.core.session_memory import ShortTermMemory
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
    ):
        self.llm = llm
        self.tools = tools
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.semantic_memory_recall = semantic_memory_recall
        self.already_surfaced_memories: set[str] = set()
        self.session_memory_bytes = 0
        self.short_memory = ShortTermMemory(repo_path=str(self.cwd))
        self._hydrate_repo_context()
        self.context_manager = ConversationContextManager(self.output_dir)
        tool_prompt = tools.prompt().strip()
        memory_prompt = self.memory_store.build_prompt_section()
        self.system_prompt = (
            build_system_prompt(cwd=self.cwd, tool_rules=tool_prompt, memory_section=memory_prompt)
            if system_prompt is None
            else self._append_dynamic_prompt_sections(system_prompt, tool_prompt, memory_prompt)
        )
        self.max_steps = max_steps
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.last_input_token_count = 0
        self.abort_signal = AbortSignal()

    def chat(self, user_message: str) -> str:
        """同步入口，便于 CLI 调用。"""
        return asyncio.run(self.achat(user_message))

    async def achat(self, user_message: str) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.abort_signal = AbortSignal()
        self.short_memory.start_turn(user_message)
        self.messages.append({"role": "user", "content": user_message})
        self._inject_recalled_memories(user_message)
        self.context_manager.maybe_auto_compact(self.messages, self.short_memory)
        final_answer = ""

        try:
            async for event in query_loop(
                messages=self.messages,
                llm=self.llm,
                tools=self.tools,
                max_steps=self.max_steps,
                abort_signal=self.abort_signal,
                context_manager=self.context_manager,
            ):
                self._handle_event(event)
                if event.type == LoopEventType.FINISH:
                    final_answer = event.payload.get("answer", "")
                    break
        finally:
            self._auto_save(final_answer)

        return final_answer

    def abort(self) -> None:
        self.abort_signal.abort()

    def _handle_event(self, event: LoopEvent) -> None:
        if event.type == LoopEventType.LLM_RESPONSE:
            usage = event.payload.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0)
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_tokens += total_tokens
            self.last_input_token_count = prompt_tokens
            self.context_manager.after_model_call(prompt_tokens)
        self.short_memory.handle_event(event)
        self._write_trace({"type": event.type.value, "step": event.step, **event.payload})

    def _write_trace(self, event: dict[str, Any]) -> None:
        with (self.output_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _auto_save(self, final_answer: str) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "messages.json").write_text(
            json.dumps(self.messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "final_answer.md").write_text(final_answer, encoding="utf-8")
        (self.output_dir / "session.json").write_text(
            json.dumps(
                {
                    "total_prompt_tokens": self.total_prompt_tokens,
                    "total_completion_tokens": self.total_completion_tokens,
                    "total_tokens": self.total_tokens,
                    "last_input_token_count": self.last_input_token_count,
                    "max_steps": self.max_steps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.short_memory.save(self.output_dir)

    def _inject_recalled_memories(self, user_message: str) -> None:
        if self.session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
            return
        if len(user_message.strip()) < 4:
            return
        side_query = self._side_query if self.semantic_memory_recall and hasattr(self.llm, "invoke_text") else None
        memories = self.memory_store.recall(user_message, already_surfaced=self.already_surfaced_memories, side_query=side_query)
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
    def _append_dynamic_prompt_sections(system_prompt: str, tool_prompt: str, memory_prompt: str) -> str:
        sections = [system_prompt.rstrip()]
        if tool_prompt.strip():
            sections.append(f"# Tool-specific Rules\n{tool_prompt.strip()}")
        if memory_prompt.strip():
            sections.append(memory_prompt.strip())
        return "\n\n".join(sections)

    def _side_query(self, system: str, user: str) -> str:
        invoke_text = getattr(self.llm, "invoke_text")
        return invoke_text(system, user)
