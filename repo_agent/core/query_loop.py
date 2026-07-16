from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable
from typing import Any, AsyncIterator, Protocol

from repo_agent.core.context import ConversationContextManager
from repo_agent.core.llm import LLMToolResponse
from repo_agent.core.tool import ToolRegistry, ToolResponse


class ContinueReason(str, Enum):
    """循环继续的原因。

    当前项目先实现文档里的最小版本：模型调用工具后继续下一轮。
    后续可以继续扩展 prompt_too_long、max_output_tokens 等恢复分支。
    """

    NEXT_TURN = "next_turn"
    COMPLETION_GUARD = "completion_guard"


class LoopEventType(str, Enum):
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONTINUE = "continue"
    FINISH = "finish"
    ERROR = "error"


@dataclass
class LoopEvent:
    type: LoopEventType
    step: int
    payload: dict[str, Any] = field(default_factory=dict)


class SupportsToolCallingLLM(Protocol):
    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMToolResponse: ...


class AbortSignal:
    """轻量中断信号，等价于文档里的 AbortController.signal。"""

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


async def query_loop(
    *,
    messages: list[dict[str, Any]],
    llm: SupportsToolCallingLLM,
    tools: ToolRegistry,
    max_steps: int,
    abort_signal: AbortSignal,
    context_manager: ConversationContextManager | None = None,
    completion_guard: Callable[[str], str | None] | None = None,
    completion_guard_extra_turns: int = 0,
    force_completion_after_max_steps: bool = False,
) -> AsyncIterator[LoopEvent]:
    """单轮级 Agent Loop。

    这个函数只关心一次 query 如何推进：调用 LLM、执行工具、把 tool observation
    写回 messages，然后决定继续或结束。它不关心 CLI、输出目录、会话保存等会话级问题。
    """

    for step in range(1, max_steps + 1):
        if abort_signal.aborted:
            yield LoopEvent(LoopEventType.FINISH, step, {"answer": "任务已中断。", "aborted": True})
            return

        if context_manager:
            context_manager.before_model_call(messages)

        response = await asyncio.to_thread(llm.invoke_with_tools, messages, tools.schemas())
        yield LoopEvent(
            LoopEventType.LLM_RESPONSE,
            step,
            {"response": response.raw_message, "usage": response.usage},
        )

        if not response.tool_calls:
            answer = response.content or ""
            guard_message = completion_guard(answer) if completion_guard else None
            if guard_message:
                messages.append({"role": "assistant", "content": answer})
                messages.append(
                    {
                        "role": "user",
                        "content": f"[Workflow guard]\n{guard_message}",
                    }
                )
                yield LoopEvent(
                    LoopEventType.CONTINUE,
                    step,
                    {
                        "reason": ContinueReason.COMPLETION_GUARD.value,
                        "guard_message": guard_message,
                    },
                )
                if completion_guard_extra_turns > 0:
                    async for event in _completion_guard_correction_turn(
                        messages=messages,
                        llm=llm,
                        completion_guard=completion_guard,
                        abort_signal=abort_signal,
                        context_manager=context_manager,
                        step=step,
                    ):
                        yield event
                    return
                continue
            messages.append({"role": "assistant", "content": answer})
            yield LoopEvent(LoopEventType.FINISH, step, {"answer": answer})
            return

        messages.append(_assistant_tool_call_message(response))

        for tool_call in response.tool_calls:
            if abort_signal.aborted:
                yield LoopEvent(LoopEventType.FINISH, step, {"answer": "任务已中断。", "aborted": True})
                return

            yield LoopEvent(
                LoopEventType.TOOL_CALL,
                step,
                {"tool": tool_call.name, "arguments": tool_call.arguments, "tool_call_id": tool_call.id},
            )

            tool_response = await asyncio.to_thread(
                tools.execute,
                tool_call.name,
                tool_call.arguments,
                context_manager is None,
            )
            if context_manager:
                tool_response = context_manager.process_tool_response(tool_call.name, tool_response)
            yield LoopEvent(
                LoopEventType.TOOL_RESULT,
                step,
                {
                    "tool": tool_call.name,
                    "tool_call_id": tool_call.id,
                    "response": tool_response.to_dict(),
                },
            )

            messages.append(_tool_result_message(tool_call.id, tool_call.name, tool_response))

            if tool_response.data.get("terminate_loop"):
                answer = str(tool_response.data.get("final_answer") or tool_response.text)
                terminal_completed = bool(tool_response.data.get("terminal_completed", True))
                yield LoopEvent(
                    LoopEventType.FINISH,
                    step,
                    {
                        "answer": answer,
                        "tool_terminated": True,
                        "terminal_reason": tool_response.data.get("terminal_reason", "tool_requested"),
                        "terminal_incomplete": not terminal_completed,
                    },
                )
                return

            if tool_call.name == "finish":
                yield LoopEvent(LoopEventType.FINISH, step, {"answer": tool_response.text})
                return

        yield LoopEvent(
            LoopEventType.CONTINUE,
            step,
            {"reason": ContinueReason.NEXT_TURN.value},
        )

    if force_completion_after_max_steps:
        async for event in _max_steps_finalization_turn(
            messages=messages,
            llm=llm,
            completion_guard=completion_guard,
            completion_guard_extra_turns=completion_guard_extra_turns,
            abort_signal=abort_signal,
            context_manager=context_manager,
            step=max_steps,
        ):
            yield event
        return

    yield LoopEvent(
        LoopEventType.FINISH,
        max_steps,
        {"answer": "达到最大步数，任务未明确完成。", "max_steps_reached": True},
    )


async def _completion_guard_correction_turn(
    *,
    messages: list[dict[str, Any]],
    llm: SupportsToolCallingLLM,
    completion_guard: Callable[[str], str | None],
    abort_signal: AbortSignal,
    context_manager: ConversationContextManager | None,
    step: int,
) -> AsyncIterator[LoopEvent]:
    """执行一次不占普通步数、且不暴露工具的结构化输出修正。"""

    if abort_signal.aborted:
        yield LoopEvent(LoopEventType.FINISH, step, {"answer": "任务已中断。", "aborted": True})
        return
    correction_messages = _completion_guard_correction_messages(messages)
    if context_manager:
        context_manager.before_model_call(correction_messages)

    response = await asyncio.to_thread(llm.invoke_with_tools, correction_messages, [])
    yield LoopEvent(
        LoopEventType.LLM_RESPONSE,
        step,
        {
            "response": response.raw_message,
            "usage": response.usage,
            "completion_guard_correction": True,
        },
    )

    if response.tool_calls:
        yield LoopEvent(
            LoopEventType.FINISH,
            step,
            {
                "answer": "结构化输出修正回合错误地请求了工具。",
                "terminal_incomplete": True,
                "terminal_reason": "completion_guard_failed",
                "completion_guard_correction": True,
            },
        )
        return

    answer = response.content or ""
    messages.append({"role": "assistant", "content": answer})
    guard_message = completion_guard(answer)
    if guard_message:
        yield LoopEvent(
            LoopEventType.FINISH,
            step,
            {
                "answer": answer or "结构化输出修正后仍未通过校验。",
                "terminal_incomplete": True,
                "terminal_reason": "completion_guard_failed",
                "completion_guard_correction": True,
                "guard_message": guard_message,
            },
        )
        return

    yield LoopEvent(
        LoopEventType.FINISH,
        step,
        {"answer": answer, "completion_guard_correction": True},
    )


def _completion_guard_correction_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """隔离 JSON 修正上下文，避免模型延续上一轮的工具调用意图。"""

    system = next(
        (str(message.get("content") or "") for message in messages if message.get("role") == "system"),
        "",
    )
    draft = next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if message.get("role") == "assistant" and str(message.get("content") or "").strip()
        ),
        "{}",
    )
    guard = next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if message.get("role") == "user"
            and str(message.get("content") or "").startswith("[Workflow guard]")
        ),
        "[Workflow guard]\nReturn valid JSON.",
    )
    return [
        {
            "role": "system",
            "content": (
                f"{system}\n\n"
                "# Isolated JSON correction mode\n"
                "You have no tools and must not request, describe, or imitate a tool call. "
                "Repair the supplied draft using only its evidence and the validation errors. "
                "Return exactly one literal JSON object. The first non-whitespace character must be `{` and the last "
                "must be `}`. Do not use Markdown, XML/tool-call tags, commentary, or a preamble."
            ),
        },
        {
            "role": "user",
            "content": f"DRAFT_TO_REPAIR:\n{draft}\n\nVALIDATION_ERRORS:\n{guard}",
        },
    ]


async def _max_steps_finalization_turn(
    *,
    messages: list[dict[str, Any]],
    llm: SupportsToolCallingLLM,
    completion_guard: Callable[[str], str | None] | None,
    completion_guard_extra_turns: int,
    abort_signal: AbortSignal,
    context_manager: ConversationContextManager | None,
    step: int,
) -> AsyncIterator[LoopEvent]:
    """普通步数耗尽后强制一次无工具最终输出；该回合不计入 max_steps。"""

    messages.append(
        {
            "role": "user",
            "content": (
                "[Workflow finalizer]\nThe regular tool/step budget is exhausted. "
                "Do not call tools. Return the required final JSON object now."
            ),
        }
    )
    if abort_signal.aborted:
        yield LoopEvent(LoopEventType.FINISH, step, {"answer": "任务已中断。", "aborted": True})
        return
    if context_manager:
        context_manager.before_model_call(messages)

    response = await asyncio.to_thread(llm.invoke_with_tools, messages, [])
    yield LoopEvent(
        LoopEventType.LLM_RESPONSE,
        step,
        {
            "response": response.raw_message,
            "usage": response.usage,
            "max_steps_finalization": True,
        },
    )
    if response.tool_calls:
        yield LoopEvent(
            LoopEventType.FINISH,
            step,
            {
                "answer": "最终输出回合错误地请求了工具。",
                "terminal_incomplete": True,
                "terminal_reason": "max_steps_finalization_failed",
                "max_steps_finalization": True,
            },
        )
        return

    answer = response.content or ""
    guard_message = completion_guard(answer) if completion_guard else None
    if not guard_message:
        messages.append({"role": "assistant", "content": answer})
        yield LoopEvent(
            LoopEventType.FINISH,
            step,
            {"answer": answer, "max_steps_finalization": True},
        )
        return

    messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": f"[Workflow guard]\n{guard_message}"})
    yield LoopEvent(
        LoopEventType.CONTINUE,
        step,
        {
            "reason": ContinueReason.COMPLETION_GUARD.value,
            "guard_message": guard_message,
            "max_steps_finalization": True,
        },
    )
    if completion_guard_extra_turns > 0 and completion_guard is not None:
        async for event in _completion_guard_correction_turn(
            messages=messages,
            llm=llm,
            completion_guard=completion_guard,
            abort_signal=abort_signal,
            context_manager=context_manager,
            step=step,
        ):
            yield event
        return

    yield LoopEvent(
        LoopEventType.FINISH,
        step,
        {
            "answer": answer or "最终输出未通过结构校验。",
            "terminal_incomplete": True,
            "terminal_reason": "completion_guard_failed",
            "max_steps_finalization": True,
            "guard_message": guard_message,
        },
    )


def _assistant_tool_call_message(response: LLMToolResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {"name": tool_call.name, "arguments": tool_call.arguments},
            }
            for tool_call in response.tool_calls
        ],
    }
    # DeepSeek 思考模式要求工具调用后的下一轮原样带回 reasoning_content。
    reasoning_content = response.raw_message.get("reasoning_content")
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return message


def _tool_result_message(tool_call_id: str, tool_name: str, response: ToolResponse) -> dict[str, Any]:
    payload = response.to_dict()
    payload.setdefault("data", {})["tool"] = tool_name
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False, indent=2),
    }
