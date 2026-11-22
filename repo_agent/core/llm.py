from __future__ import annotations

import os
import json
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletion
import httpx


def _request_log(event: str, **fields: Any) -> None:
    line = json.dumps({"event": event, "timestamp": datetime.now(timezone.utc).isoformat(),
                       "pid": os.getpid(), **fields}, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    # 独立落盘，不依赖父进程收集 PIPE；外层超时杀进程后仍保留请求开始记录。
    directory = os.getenv("REPO_AGENT_LLM_LOG_DIR")
    if directory:
        try:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            with (path / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError:
            print("LLM request log file could not be written.", file=sys.stderr, flush=True)


DEFAULT_MODEL = "glm-5.3-flash"

# 单次模型请求的超时与重试。实测出现过供应商长连接挂起十余分钟不返回的情况，
# 没有超时保护会直接耗尽批量实验的单题时间预算。
DEFAULT_LLM_TIMEOUT_SECONDS = 300.0
DEFAULT_LLM_MAX_RETRIES = 2

OUTPUT_LIMIT_RECOVERY_PROMPT = """上一轮已达到输出预算，这是唯一一次收尾请求。停止重复推演，沿用已有事实，不重新展开分析或猜测历史修法。
当前剩余源码查询次数：{remaining_queries}。
本提示不会增加工具权限、查询次数或步骤预算，已有文件授权和流程限制不变。
- 已有充分依据：派发已确定的修改。
- 仍缺关键事实且剩余查询大于 0：只查询能决定修法的具体源码。
- 查询次数为 0，或仍无法确定修法：输出 blocked_insufficient_edit_scope，简述缺失事实和已检查位置。
未提供数值预算时不得假定获得额外查询机会；以现有工具返回的限制为准。
不要重复已完成的推演，不要将定位任务交给实现者。
上一轮未完成的工具调用没有执行，不要把草稿当作工具结果。
"""


class LLMOutputLimitError(RuntimeError):
    def __init__(self, message: dict[str, Any], usage: dict[str, Any] | None):
        super().__init__("Incomplete LLM stream: finish_reason=length")
        self.partial_message = message
        self.usage = usage


class LLMStreamInterruptedError(RuntimeError):
    """连接已建立，但供应商未返回完整响应；未完成的工具调用不能执行。"""


def _model_family(model: str) -> str:
    """按模型名前缀识别供应商协议族。

    同一个 OpenAI-compatible 接口背后可能是不同厂商协议。思考参数、
    reasoning_effort 的支持情况由协议族决定，而不是由 base_url 决定。
    """

    lowered = (model or "").lower()
    if lowered.startswith("deepseek-v4"):
        return "deepseek-v4"
    if lowered.startswith("glm-"):
        return "glm"
    return "openai"


# GLM-5.3-Flash 实测（2026-10-10，真实端点探测）：
# - thinking.type 只接受 enabled；disabled 与 low/high/max 都会返回 400
#   （错误码 1210："该模型始终思考，不支持关闭思考"）。
# - 思考无法关闭，但顶层 reasoning_effort 生效，支持 low/high/max 三档；
#   CLI 的 medium 档（DeepSeek 语义）不被接受，映射到 high。
_GLM_EFFORT_MAP = {"low": "low", "medium": "high", "high": "high", "max": "max"}
_GLM_DEFAULT_EFFORT = "high"


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


def _cached_tokens(usage: Any) -> int:
    """提取供应商报告的缓存命中 token 数。

    缓存输入价格约为普通输入的五分之一，所以这是批量实验里最重要的成本指标。
    不同兼容端点字段名不同（OpenAI / GLM 用 prompt_tokens_details.cached_tokens，
    部分端点直接给 usage.cached_tokens），这里统一归零兜底，避免指标缺失时误判为未命中。
    """

    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is not None:
        value = getattr(details, "cached_tokens", None)
        if value is None and isinstance(details, dict):
            value = details.get("cached_tokens")
        if value is not None:
            return int(value or 0)
    direct = getattr(usage, "cached_tokens", None)
    if direct is None and isinstance(usage, dict):
        direct = usage.get("cached_tokens")
    return int(direct or 0)


class ChatLLM:
    """OpenAI-compatible Chat Completions 的薄适配层。

    这一层刻意不包含 Agent Loop、工具执行和对话历史管理。它只完成两件事：
    组装一次模型请求，以及把供应商响应转换为 LLMToolResponse。

    协议族差异集中在这里处理：

    - deepseek-v4：支持 reasoning_effort 顶层参数；lightweight 角色可以真正关闭思考。
    - glm：思考无法关闭（thinking 是模型固有行为），但顶层 reasoning_effort 支持
      low/high/max 三档。轻量角色传入的 enable_thinking=False 无法关闭思考，会被降档为
      low 以控制成本，并通过 thinking_forced 属性暴露"请求被协议强制覆盖"的事实。
    - 其他模型：不附加思考参数，保持原样。
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
        reasoning_effort: str | None = None,
        enable_thinking: bool | None = None,
        max_output_tokens: int | None = None,
        output_limit_recovery: bool = False,
    ):
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.enable_thinking = enable_thinking
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.max_output_tokens = max_output_tokens
        self.output_limit_recovery = output_limit_recovery
        self.remaining_queries_provider = None
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required.")
        self.timeout = float(
            os.getenv("REPO_AGENT_LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS)
        )
        self.max_retries = int(os.getenv("REPO_AGENT_LLM_MAX_RETRIES", DEFAULT_LLM_MAX_RETRIES))
        self._request_state = threading.local()
        endpoint = urlsplit(self.base_url or "")
        go_headers = {}
        if (endpoint.scheme == "https" and endpoint.hostname == "opencode.ai"
                and endpoint.path.rstrip("/") == "/inference/go/openai/v1"):
            go_headers = {"User-Agent": "RepoAgent/1.0", "x-opencode-session": uuid.uuid4().hex}
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_headers=go_headers,
            http_client=httpx.Client(event_hooks={
                "request": [self._log_http_attempt],
                "response": [self._log_http_response],
            }),
        )

    def _log_http_attempt(self, request: httpx.Request) -> None:
        state = getattr(self._request_state, "current", None)
        if state is None:
            return
        state["attempt"] += 1
        state["attempt_started"] = time.monotonic()
        _request_log("llm_attempt_start", request_id=state["id"], attempt=state["attempt"],
                     retry=state["attempt"] > 1, host=request.url.host,
                     elapsed_seconds=round(time.monotonic() - state["started"], 3))

    def _log_http_response(self, response: httpx.Response) -> None:
        state = getattr(self._request_state, "current", None)
        if state is not None:
            _request_log("llm_response_headers", request_id=state["id"], attempt=state["attempt"],
                         status_code=response.status_code,
                         attempt_seconds=round(time.monotonic() - state["attempt_started"], 3))

    def _create_completion(self, request: dict[str, Any]) -> Any:
        try:
            return self._create_completion_with_stream_retries(request)
        except LLMOutputLimitError as exc:
            if not getattr(self, "output_limit_recovery", False):
                raise
            # 保留完整的原始证据历史；未完成的模型输出只作为草稿，绝不执行其中的工具。
            partial = exc.partial_message
            draft = {key: partial[key] for key in ("content", "tool_calls") if partial.get(key)}
            provider = getattr(self, "remaining_queries_provider", None)
            remaining = provider() if provider else None
            recovery_prompt = OUTPUT_LIMIT_RECOVERY_PROMPT.format(
                remaining_queries=remaining if remaining is not None else "未提供数值预算"
            )
            recovery = {**request, "messages": [*request["messages"],
                {"role": "assistant", "content": partial.get("content") or "上一轮输出未完成。",
                 **({"reasoning_content": partial["reasoning_content"]} if partial.get("reasoning_content") else {})},
                {"role": "user", "content": recovery_prompt
                 + ("\n未执行的输出草稿（仅供参考）：\n" + json.dumps(draft, ensure_ascii=False) if draft else "")} ]}
            # 收尾负责作出下一步决定，不以同一高思考档重做整段推演。
            if _model_family(self.model) == "glm":
                recovery["reasoning_effort"] = "low"
            _request_log("llm_output_limit_recovery", model=self.model, recovery_attempt=1,
                         truncated_usage=exc.usage)
            # 直接调用单次请求，不递归补轮；收尾再次触顶就正常报错。
            response = self._create_completion_with_stream_retries(recovery)
            if exc.usage and response.usage:
                previous = exc.usage
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    setattr(response.usage, key, getattr(response.usage, key) + int(previous.get(key) or 0))
                if response.usage.prompt_tokens_details is not None:
                    response.usage.prompt_tokens_details.cached_tokens = (
                        int(response.usage.prompt_tokens_details.cached_tokens or 0)
                        + _cached_tokens(previous))
            return response

    def _create_completion_with_stream_retries(self, request: dict[str, Any]) -> Any:
        retries = getattr(self, "max_retries", DEFAULT_LLM_MAX_RETRIES)
        for attempt in range(retries + 1):
            try:
                return self._create_completion_once(request)
            except LLMStreamInterruptedError:
                if attempt == retries:
                    raise
                _request_log("llm_stream_retry", model=self.model, retry=attempt + 1,
                             max_retries=retries)
                time.sleep(min(2 ** attempt, 5))

    def _create_completion_once(self, request: dict[str, Any]) -> Any:
        limit = getattr(self, "max_output_tokens", None)
        if limit is not None:
            request = {**request, "max_tokens": limit}
        if not hasattr(self, "_request_state"):
            self._request_state = threading.local()
        state = {"id": uuid.uuid4().hex, "started": time.monotonic(), "attempt": 0}
        self._request_state.current = state
        # 只记录规模和参数，不记录消息正文、工具参数、密钥或完整端点 URL。
        _request_log("llm_request_start", request_id=state["id"], model=self.model,
                     message_count=len(request["messages"]),
                     message_chars=len(json.dumps(request["messages"], ensure_ascii=False)),
                     tool_count=len(request.get("tools", [])),
                     tool_schema_chars=len(json.dumps(request.get("tools", []), ensure_ascii=False)),
                     reasoning_effort=request.get("reasoning_effort"),
                     temperature=request.get("temperature"),
                     stream=True,
                     max_output_tokens=limit,
                     timeout_seconds=getattr(self, "timeout", DEFAULT_LLM_TIMEOUT_SECONDS),
                     max_retries=getattr(self, "max_retries", DEFAULT_LLM_MAX_RETRIES))
        try:
            stream = self.client.chat.completions.create(
                **request, stream=True, stream_options={"include_usage": True})
            try:
                response = self._receive_stream(stream, state)
            except httpx.TimeoutException as exc:
                raise APITimeoutError(request=exc.request) from exc
            except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
                raise LLMStreamInterruptedError("LLM stream transport interrupted") from exc
        except Exception as exc:
            _request_log("llm_request_error", request_id=state["id"], attempts=state["attempt"],
                         elapsed_seconds=round(time.monotonic() - state["started"], 3),
                         error_type=type(exc).__name__,
                         cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None)
            raise
        else:
            _request_log("llm_request_end", request_id=state["id"], attempts=state["attempt"],
                         elapsed_seconds=round(time.monotonic() - state["started"], 3),
                         total_tokens=getattr(getattr(response, "usage", None), "total_tokens", None))
            return response
        finally:
            self._request_state.current = None

    def _receive_stream(self, stream: Any, state: dict[str, Any]) -> ChatCompletion:
        message: dict[str, Any] = {"role": "assistant", "content": None}
        calls: dict[int, dict[str, Any]] = {}
        usage = None
        finish_reason = None
        metadata = {"id": state["id"], "created": 0, "model": self.model}
        count = 0
        last_progress = state["started"]
        total_budget = float(os.getenv("REPO_AGENT_LLM_TOTAL_TIMEOUT_SECONDS", "600"))
        categories = ("content", "reasoning_content", "refusal", "tool_name", "tool_arguments")
        chars = dict.fromkeys(categories, 0)
        fragments = dict.fromkeys(categories, 0)
        tails = dict.fromkeys(categories, "")
        metadata_only_chunks = 0

        def record(category: str, value: str | None) -> None:
            if value:
                chars[category] += len(value)
                fragments[category] += 1
                tails[category] = (tails[category] + value)[-1000:]

        def snapshot(status: str) -> dict[str, Any]:
            data = {"request_id": state["id"], "status": status, "chunks": count,
                    "elapsed_seconds": round(time.monotonic() - state["started"], 3),
                    "character_counts": chars.copy(), "fragment_counts": fragments.copy(),
                    "metadata_only_chunks": metadata_only_chunks, "tail_limit_chars": 1000,
                    "tails": tails.copy(), "finish_reason": finish_reason}
            # 定期覆盖单个有界快照；即使父进程终止请求，也能检查最近输出。
            directory = os.getenv("REPO_AGENT_LLM_LOG_DIR")
            if directory:
                try:
                    path = Path(directory) / f"{os.getpid()}-{state['id']}.snapshot.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(path)
                except OSError:
                    print("LLM stream snapshot could not be written.", file=sys.stderr, flush=True)
            return data

        try:
            for chunk in stream:
                count += 1
                elapsed = time.monotonic() - state["started"]
                meaningful = False
                for key in metadata:
                    value = getattr(chunk, key, None)
                    if value is not None:
                        metadata[key] = value
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump(exclude_none=True)
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    delta = choice.delta
                    for key in ("content", "reasoning_content", "refusal"):
                        value = getattr(delta, key, None)
                        if value:
                            record(key, value)
                            meaningful = True
                            message[key] = (message.get(key) or "") + value
                    # 参数可能在任意字符处分片；必须按 index 拼接后才能交给工具层。
                    for part in delta.tool_calls or []:
                        call = calls.setdefault(part.index, {"id": "", "type": "function",
                                                              "function": {"name": "", "arguments": ""}})
                        if part.id:
                            call["id"] += part.id
                        if part.function:
                            for key in ("name", "arguments"):
                                value = getattr(part.function, key, None)
                                if value:
                                    record("tool_" + key, value)
                                    meaningful = True
                                    call["function"][key] += value
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                metadata_only_chunks += not meaningful
                if count == 1 or time.monotonic() - last_progress >= 30:
                    data = snapshot("receiving")
                    _request_log("llm_stream_first_chunk" if count == 1 else "llm_stream_progress",
                                 **{key: value for key, value in data.items() if key != "tails"})
                    last_progress = time.monotonic()
                if elapsed > total_budget:
                    raise APITimeoutError(request=httpx.Request("POST",
                        (getattr(self, "base_url", None) or "https://api.openai.com/v1").rstrip("/")
                        + "/chat/completions")) from TimeoutError("LLM stream total time budget exceeded")
            if finish_reason == "length":
                if calls:
                    message["tool_calls"] = [calls[index] for index in sorted(calls)]
                raise LLMOutputLimitError(message, usage)
            if finish_reason not in {"stop", "tool_calls"}:
                if finish_reason in {None, "network_error"}:
                    raise LLMStreamInterruptedError(f"Incomplete LLM stream: finish_reason={finish_reason}")
                raise RuntimeError(f"Incomplete LLM stream: finish_reason={finish_reason}")
            if calls:
                message["tool_calls"] = [calls[index] for index in sorted(calls)]
                if any(not call["id"] or not call["function"]["name"] for call in calls.values()):
                    raise RuntimeError("Incomplete LLM stream tool call")
            response = ChatCompletion.model_validate({**metadata, "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}], "usage": usage})
            snapshot("complete")
            return response
        except BaseException as exc:
            data = snapshot("incomplete")
            _request_log("llm_stream_incomplete", error_type=type(exc).__name__,
                         **{key: value for key, value in data.items() if key != "tails"})
            raise
        finally:
            stream.close()

    @property
    def thinking_forced(self) -> bool:
        """请求关闭思考但被供应商协议强制开启时为 True（当前仅 GLM 家族）。"""

        return _model_family(self.model) == "glm" and getattr(self, "enable_thinking", None) is False

    def _completion_options(self) -> dict[str, Any]:
        """按协议族组装思考相关请求参数。"""

        family = _model_family(self.model)
        if family == "deepseek-v4":
            if getattr(self, "enable_thinking", None) is False:
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            effort = getattr(self, "reasoning_effort", None) or "max"
            return {
                "reasoning_effort": effort,
                "extra_body": {"thinking": {"type": "enabled"}},
            }
        if family == "glm":
            # GLM-5.3-Flash 思考无法关闭，但顶层 reasoning_effort 生效（low/high/max）。
            # 轻量角色请求 enable_thinking=False 时降档为 low 控制成本；正常角色按
            # 角色配置映射，未配置时用端点默认档 high。不发送 thinking 参数：
            # 思考是模型固有行为，显式声明是冗余的。
            if getattr(self, "enable_thinking", None) is False:
                return {"reasoning_effort": "low"}
            effort = getattr(self, "reasoning_effort", None)
            return {"reasoning_effort": _GLM_EFFORT_MAP.get(effort, _GLM_DEFAULT_EFFORT)}
        return {}

    def invoke_finalization(self, messages: list[dict[str, Any]]) -> LLMToolResponse:
        return self.invoke_with_tools(messages, [], lightweight_finalization=True)

    def invoke_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, lightweight_finalization: bool = False,
    ) -> LLMToolResponse:
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
        if lightweight_finalization and _model_family(self.model) == "glm":
            completion_options["reasoning_effort"] = "low"
        request.update(completion_options)
        # GLM 同时支持思考档位与温度，不能因开启思考而丢弃采样配置。
        if not completion_options or _model_family(self.model) == "glm":
            request["temperature"] = self.temperature
        response = self._create_completion(request)
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
                "cached_tokens": _cached_tokens(response.usage),
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
        if not completion_options or _model_family(self.model) == "glm":
            request["temperature"] = 0
        response = self._create_completion(request)
        return response.choices[0].message.content or ""
