from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMToolResponse:
    content: str | None
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]
    usage: dict[str, int]


class ChatLLM:
    """Thin OpenAI-compatible Chat Completions client."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
    ):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.temperature = temperature
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required.")
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _completion_options(self) -> dict[str, Any]:
        """为支持显式思考强度的模型补充请求参数。"""

        if self.model.lower().startswith("deepseek-v4"):
            return {
                "reasoning_effort": "max",
                "extra_body": {"thinking": {"type": "enabled"}},
            }
        return {}

    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMToolResponse:
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
