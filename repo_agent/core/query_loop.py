from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field
from enum import Enum
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

            if tool_call.name == "finish":
                yield LoopEvent(LoopEventType.FINISH, step, {"answer": tool_response.text})
                return

        yield LoopEvent(
            LoopEventType.CONTINUE,
            step,
            {"reason": ContinueReason.NEXT_TURN.value},
        )

    yield LoopEvent(
        LoopEventType.FINISH,
        max_steps,
        {"answer": "达到最大步数，任务未明确完成。", "max_steps_reached": True},
    )


def _assistant_tool_call_message(response: LLMToolResponse) -> dict[str, Any]:
    return {
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


def _tool_result_message(tool_call_id: str, tool_name: str, response: ToolResponse) -> dict[str, Any]:
    payload = response.to_dict()
    payload.setdefault("data", {})["tool"] = tool_name
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False, indent=2),
    }
