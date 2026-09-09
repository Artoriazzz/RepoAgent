from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class ToolCall:
    """模型提出的工具调用意图，还没有被真正执行。

    arguments 保留为 JSON 字符串，是因为参数解析和校验属于 ToolRegistry 的职责。
    LLM 适配层只负责忠实转换协议，不应该越层解释业务参数。
    """

    id: str
    name: str
    arguments: str


@dataclass
class LLMToolResponse:
    """RepoAgent 内部统一使用的模型响应。

    content/tool_calls 是 Agent Loop 所需的稳定字段；raw_message 则保留供应商扩展
    字段，例如 DeepSeek 的 reasoning_content。这样上层循环不用依赖 OpenAI SDK
    的具体响应对象，同时仍能在需要时兼容模型厂商协议。
    """

    content: str | None
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]
    usage: dict[str, int]


class ChatLLM:
    """OpenAI-compatible Chat Completions 的薄适配层。

    这一层刻意不包含 Agent Loop、工具执行和对话历史管理。它只完成两件事：
    组装一次模型请求，以及把供应商响应转换为 LLMToolResponse。
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
        reasoning_effort: str | None = None,
        enable_thinking: bool | None = None,
    ):
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.enable_thinking = enable_thinking
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required.")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _completion_options(self) -> dict[str, Any]:
        """为支持显式思考强度的模型补充请求参数。"""

        if not self.model.lower().startswith("deepseek-v4"):
            return {}
        if getattr(self, "enable_thinking", None) is False:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        effort = getattr(self, "reasoning_effort", None) or "max"
        return {
            "reasoning_effort": effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMToolResponse:
        """执行一次支持 function calling 的同步模型请求。"""

        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        # 无工具回合不要继续声明 tool calling。部分兼容端点即使收到空 tools + auto，
        # 仍会让模型生成伪工具调用，导致 completion guard 的纯 JSON 修正失败。
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        completion_options = self._completion_options()
        request.update(completion_options)
        if not completion_options:
            request["temperature"] = self.temperature
        response = self.client.chat.completions.create(
            **request,
        )
        message = response.choices[0].message
        # SDK 对象在这里被转换成项目自己的数据结构。后续换 HTTP 客户端或其他
        # OpenAI-compatible SDK 时，只需要改这一层，不需要改 Agent Loop。
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
            )
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return LLMToolResponse(
            content=message.content,
            tool_calls=tool_calls,
            raw_message=message.model_dump(exclude_none=True),
            usage=usage,
        )

    def invoke_text(self, system: str, user: str) -> str:
        """执行不带工具的旁路文本请求，供记忆检索等辅助任务使用。"""

        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        completion_options = self._completion_options()
        request.update(completion_options)
        if not completion_options:
            request["temperature"] = 0
        response = self.client.chat.completions.create(**request)
        return response.choices[0].message.content or ""
