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

    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMToolResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
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
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
        return response.choices[0].message.content or ""
