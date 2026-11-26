from types import SimpleNamespace
import json

import httpx
import pytest
from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionChunk

from repo_agent.cli import _build_role_llms, build_parser
from repo_agent.core.llm import ChatLLM, LLMStreamInterruptedError


def test_request_logs_include_retries_without_prompt_or_key(capsys):
    llm = ChatLLM(model="test-model", api_key="secret-api-key", base_url="https://example.test/v1")
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        chunk = {"id": "test", "object": "chat.completion.chunk", "created": 0,
            "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
            "delta": {"role": "assistant", "content": "ok"}}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    llm.client.close()
    llm.max_retries = 1
    llm.client = OpenAI(api_key="secret-api-key", base_url="https://example.test/v1", max_retries=1,
        http_client=httpx.Client(transport=httpx.MockTransport(respond), event_hooks={
            "request": [llm._log_http_attempt], "response": [llm._log_http_response]}))
    try:
        assert llm.invoke_text("private-system", "private-user") == "ok"
    finally:
        llm.client.close()
    output = capsys.readouterr().err
    events = [json.loads(line) for line in output.splitlines()]
    assert events[0]["event"] == "llm_request_start"
    starts = [e for e in events if e["event"] == "llm_attempt_start"]
    assert [e["attempt"] for e in starts] == [1, 2]
    assert starts[1]["retry"] is True
    assert events[-1]["event"] == "llm_request_end"
    assert events[-1]["attempts"] == 2
    assert len({e["request_id"] for e in events}) == 1
    assert all(secret not in output for secret in ["secret-api-key", "private-system", "private-user"])


def test_request_logs_timeout_and_reraises(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_LLM_LOG_DIR", str(tmp_path / "requests"))
    llm, _ = _fake_llm("test-model")

    def fail(**kwargs):
        raise APITimeoutError(request=httpx.Request("POST", "https://example.test"))

    llm.client.chat.completions.create = fail
    with pytest.raises(APITimeoutError):
        llm.invoke_text("system", "user")
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[-1]["event"] == "llm_request_error"
    assert events[-1]["error_type"] == "APITimeoutError"
    assert events[-1]["elapsed_seconds"] >= 0
    persisted = next((tmp_path / "requests").glob("*.jsonl")).read_text().splitlines()
    assert [json.loads(line) for line in persisted] == events


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            yield ChatCompletionChunk.model_validate({"id": "test", "object": "chat.completion.chunk",
                "created": 0, "model": "test-model", **chunk})

    def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return FakeStream([{"choices": [{"index": 0, "delta": {"content": "完成"},
                                          "finish_reason": "stop"}]}])


class NetworkErrorStream(FakeStream):
    def __iter__(self):
        for chunk in super().__iter__():
            # SDK 接收兼容端点扩展字段时不做严格枚举校验。
            chunk.choices[0].finish_reason = "network_error"
            yield chunk


def _fake_llm(model: str) -> tuple[ChatLLM, FakeCompletions]:
    completions = FakeCompletions()
    llm = ChatLLM.__new__(ChatLLM)
    llm.model = model
    llm.temperature = 0.1
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm, completions


def test_stream_merges_parallel_tool_calls_reasoning_and_final_usage():
    llm, completions = _fake_llm("test-model")
    stream = FakeStream([
        {"choices": [{"index": 0, "delta": {"reasoning_content": "先", "tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "read_file", "arguments": '{"path":'}},
            {"index": 0, "id": "a", "function": {"name": "search_repo", "arguments": '{"query":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "查源码", "tool_calls": [
            {"index": 0, "function": {"arguments": '"uniq"}'}},
            {"index": 1, "function": {"arguments": '"a.py"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                                   "prompt_tokens_details": {"cached_tokens": 8}}},
    ])
    completions.create = lambda **kwargs: stream
    response = llm.invoke_with_tools([{"role": "user", "content": "task"}], [])
    assert response.raw_message["reasoning_content"] == "先查源码"
    assert [(c.id, c.name, json.loads(c.arguments)) for c in response.tool_calls] == [
        ("a", "search_repo", {"query": "uniq"}), ("b", "read_file", {"path": "a.py"})]
    assert response.usage["cached_tokens"] == 8
    assert response.usage["total_tokens"] == 15
    assert stream.closed


@pytest.mark.parametrize("reason", [None, "length", "content_filter"])
def test_incomplete_stream_is_rejected_and_closed(reason):
    llm, completions = _fake_llm("test-model")
    stream = FakeStream([{"choices": [{"index": 0, "delta": {"content": "partial"},
                                       "finish_reason": reason}]}])
    completions.create = lambda **kwargs: stream
    with pytest.raises(RuntimeError, match="Incomplete LLM stream"):
        llm.invoke_text("system", "user")
    assert stream.closed


def test_all_requests_enable_streaming():
    llm, completions = _fake_llm("test-model")
    llm.invoke_text("system", "user")
    llm.invoke_with_tools([{"role": "user", "content": "task"}], [])
    assert all(r["stream"] is True and r["stream_options"] == {"include_usage": True}
               for r in completions.requests)


def test_network_stream_retry_preserves_request_and_discards_partial_tools(monkeypatch):
    monkeypatch.setattr("repo_agent.core.llm.time.sleep", lambda seconds: None)
    llm, completions = _fake_llm("test-model")
    llm.max_retries = 1
    streams = [NetworkErrorStream([{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "partial", "function": {"name": "write_file", "arguments": '{"path":'}}]},
        "finish_reason": "stop"}]}]),
        FakeStream([{"choices": [{"index": 0, "delta": {"content": "complete"}, "finish_reason": "stop"}]}])]
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return streams[len(requests)-1]
    completions.create = create
    history = [{"role": "user", "content": "task"}]
    result = llm.invoke_with_tools(history, [])
    assert result.content == "complete"
    assert not result.tool_calls
    assert len(requests) == 2 and requests[0] == requests[1]
    assert history == [{"role": "user", "content": "task"}]
    assert all(stream.closed for stream in streams)


def test_network_stream_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("repo_agent.core.llm.time.sleep", lambda seconds: None)
    llm, completions = _fake_llm("test-model")
    llm.max_retries = 2
    streams = []
    def create(**kwargs):
        stream = NetworkErrorStream([{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        streams.append(stream)
        return stream
    completions.create = create
    with pytest.raises(LLMStreamInterruptedError):
        llm.invoke_text("system", "user")
    assert len(streams) == 3 and all(stream.closed for stream in streams)


def test_midstream_transport_error_retries_and_closes_connections(monkeypatch):
    monkeypatch.setattr("repo_agent.core.llm.time.sleep", lambda seconds: None)
    llm, completions = _fake_llm("test-model")
    llm.max_retries = 1
    class BrokenStream(FakeStream):
        def __iter__(self):
            raise httpx.ReadError("connection reset", request=httpx.Request("POST", "https://example.test"))
            yield
    streams = [BrokenStream([]), FakeStream([{"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}])]
    attempts = []
    def create(**kwargs):
        attempts.append(kwargs)
        return streams[len(attempts)-1]
    completions.create = create
    assert llm.invoke_text("system", "user") == "ok"
    assert len(attempts) == 2 and all(stream.closed for stream in streams)


def test_output_cap_applies_to_text_and_tool_requests():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.max_output_tokens = 12288
    llm.invoke_text("system", "user")
    llm.invoke_with_tools([{"role": "user", "content": "task"}], [])
    assert [r["max_tokens"] for r in completions.requests] == [12288, 12288]


def test_output_limit_recovery_preserves_history_discards_tool_draft_and_counts_usage():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.max_output_tokens = 12288
    llm.output_limit_recovery = True
    llm.reasoning_effort = "high"
    remaining = {"count": 1}
    llm.remaining_queries_provider = lambda: remaining["count"]
    streams = [
        FakeStream([
            {"choices": [{"index": 0, "delta": {"reasoning_content": "已有定位结论", "tool_calls": [
                {"index": 0, "id": "incomplete", "function": {"name": "read_file", "arguments": '{"path":'}}]},
                "finish_reason": "length"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22,
                                       "prompt_tokens_details": {"cached_tokens": 5}}},
        ]),
        FakeStream([
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "complete", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
                "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
                                       "prompt_tokens_details": {"cached_tokens": 7}}},
        ]),
    ]
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        remaining["count"] = 0
        return streams[len(requests) - 1]

    completions.create = create
    history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "source evidence"}]
    response = llm.invoke_with_tools(history, [{"type": "function", "function": {"name": "read_file"}}])
    assert len(requests) == 2
    assert requests[1]["messages"][:len(history)] == history
    assert len(history) == 2
    assert all("tool_calls" not in m for m in requests[1]["messages"])
    assert requests[1]["messages"][-2]["reasoning_content"] == "已有定位结论"
    assert "停止重复推演" in requests[1]["messages"][-1]["content"]
    assert "当前剩余源码查询次数：0" in requests[1]["messages"][-1]["content"]
    assert "本提示不会增加工具权限" in requests[1]["messages"][-1]["content"]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert [r["max_tokens"] for r in requests] == [12288, 12288]
    assert [r["reasoning_effort"] for r in requests] == ["high", "low"]
    assert [c.id for c in response.tool_calls] == ["complete"]
    assert response.usage["total_tokens"] == 45
    assert response.usage["cached_tokens"] == 12
    assert all(s.closed for s in streams)


def test_output_limit_recovery_does_not_repeat_if_second_request_hits_limit():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.output_limit_recovery = True
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return FakeStream([{"choices": [{"index": 0, "delta": {"reasoning_content": "unfinished"},
                                           "finish_reason": "length"}]}])

    completions.create = create
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        llm.invoke_text("system", "user")
    assert len(requests) == 2


def test_cli_coordinator_output_cap_is_role_specific(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("REPO_AGENT_COORDINATOR_MAX_OUTPUT_TOKENS", raising=False)
    args = build_parser().parse_args(["run", "--repo", ".", "--task", "task"])
    coordinator, overrides = _build_role_llms(args)
    assert coordinator.max_output_tokens == 12288
    assert all(llm.max_output_tokens is None for llm in overrides.values())
    args = build_parser().parse_args(["run", "--repo", ".", "--task", "task",
                                     "--coordinator-max-output-tokens", "16384"])
    assert args.coordinator_max_output_tokens == 16384
    monkeypatch.setenv("REPO_AGENT_COORDINATOR_MAX_OUTPUT_TOKENS", "10000")
    args = build_parser().parse_args(["run", "--repo", ".", "--task", "task"])
    assert args.coordinator_max_output_tokens == 10000
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--repo", ".", "--task", "task",
                                  "--coordinator-max-output-tokens", "0"])


def test_review_output_cap_preserves_other_role_budgets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS", "12288")
    args = build_parser().parse_args(["review-failure", "--repo", ".", "--task-file", "task",
                                     "--patch-file", "patch", "--failure-log-file", "failure"])
    _, overrides = _build_role_llms(args)
    assert overrides["failure_review"].max_output_tokens == 12288
    assert overrides["failure_review"].output_limit_recovery is True
    assert overrides["implement"].max_output_tokens is None
    assert overrides["research"].max_output_tokens is None


def test_midstream_read_timeout_is_api_error_and_closes_stream():
    llm, completions = _fake_llm("test-model")

    class BrokenStream(FakeStream):
        def __iter__(self):
            raise httpx.ReadTimeout("idle", request=httpx.Request("POST", "https://example.test"))
            yield

    stream = BrokenStream([])
    completions.create = lambda **kwargs: stream
    with pytest.raises(APITimeoutError):
        llm.invoke_text("system", "user")
    assert stream.closed


def test_failed_stream_persists_bounded_classified_tails(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_LLM_LOG_DIR", str(tmp_path))
    llm, completions = _fake_llm("test-model")

    class InterruptedStream(FakeStream):
        def __iter__(self):
            yield from super().__iter__()
            raise httpx.ReadTimeout("idle", request=httpx.Request("POST", "https://example.test"))

    stream = InterruptedStream([
        {"choices": [{"index": 0, "delta": {"reasoning_content": "r" * 2400}}]},
        {"choices": [{"index": 0, "delta": {"content": "answer", "tool_calls": [
            {"index": 0, "id": "call", "function": {"name": "read_file", "arguments": "a" * 1200}}]}}]},
        {"choices": []},
    ])
    completions.create = lambda **kwargs: stream
    with pytest.raises(APITimeoutError):
        llm.invoke_text("system", "user")
    data = json.loads(next(tmp_path.glob("*.snapshot.json")).read_text())
    assert data["status"] == "incomplete"
    assert data["chunks"] == 3
    assert data["metadata_only_chunks"] == 1
    assert data["character_counts"]["reasoning_content"] == 2400
    assert data["character_counts"]["content"] == 6
    assert data["character_counts"]["tool_arguments"] == 1200
    assert data["fragment_counts"]["reasoning_content"] == 1
    assert data["tails"]["reasoning_content"] == "r" * 1000
    assert data["tails"]["tool_arguments"] == "a" * 1000
    assert stream.closed


def test_deepseek_v4_tool_call_uses_max_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}


def test_deepseek_v4_supports_role_specific_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")
    llm.reasoning_effort = "medium"

    llm.invoke_with_tools([{"role": "user", "content": "review"}], [])

    request = completions.requests[0]
    assert request["reasoning_effort"] == "medium"
    assert "temperature" not in request
    assert "tools" not in request
    assert "tool_choice" not in request


def test_deepseek_v4_can_disable_thinking_for_lightweight_workers():
    llm, completions = _fake_llm("deepseek-v4-flash")
    llm.enable_thinking = False

    llm.invoke_with_tools([{"role": "user", "content": "read only"}], [])

    request = completions.requests[0]
    assert "reasoning_effort" not in request
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "temperature" not in request


def test_tool_schema_enables_automatic_tool_choice():
    llm, completions = _fake_llm("gpt-4.1")
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], tools)

    request = completions.requests[0]
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"


def test_deepseek_v4_text_call_uses_max_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")

    assert llm.invoke_text("system", "user") == "完成"

    request = completions.requests[0]
    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in request


def test_other_models_keep_temperature_without_deepseek_options():
    llm, completions = _fake_llm("gpt-4.1")

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    assert request["temperature"] == 0.1
    assert "reasoning_effort" not in request
    assert "extra_body" not in request


def test_glm_flash_tool_call_sends_mapped_reasoning_effort():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.reasoning_effort = "high"

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    # GLM 思考无法关闭但支持 effort 分档；thinking 参数是冗余的，不上行。
    assert request["reasoning_effort"] == "high"
    assert "extra_body" not in request
    assert request["temperature"] == 0.1
    assert llm.thinking_forced is False


def test_glm_finalization_keeps_history_without_tools_or_shared_effort_changes():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.reasoning_effort = "high"
    history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "evidence"}]

    llm.invoke_finalization(history)
    llm.invoke_with_tools(history, [])

    finalizer, regular = completions.requests
    assert finalizer["messages"] == regular["messages"] == history
    assert "tools" not in finalizer and "tool_choice" not in finalizer
    assert finalizer["reasoning_effort"] == "low"
    assert regular["reasoning_effort"] == llm.reasoning_effort == "high"


def test_glm_flash_unspecified_effort_defaults_to_high():
    llm, completions = _fake_llm("glm-5.3-flash")

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    assert request["reasoning_effort"] == "high"


def test_glm_flash_maps_medium_effort_to_high():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.reasoning_effort = "medium"

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    # GLM 端点不接受 medium（400），映射到最接近的高档。
    assert request["reasoning_effort"] == "high"


def test_glm_flash_lightweight_workers_degrade_to_low_effort():
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.enable_thinking = False

    llm.invoke_with_tools([{"role": "user", "content": "read only"}], [])

    request = completions.requests[0]
    # GLM 无法关闭思考：轻量角色降档为 low 控制成本，而非吃满默认思考。
    assert request["reasoning_effort"] == "low"
    assert "extra_body" not in request
    assert request["temperature"] == 0.1
    assert llm.thinking_forced is True


def test_glm_flash_text_call_sends_reasoning_effort():
    llm, completions = _fake_llm("glm-5.3-flash")

    assert llm.invoke_text("system", "user") == "完成"

    request = completions.requests[0]
    assert request["reasoning_effort"] == "high"
    assert "extra_body" not in request
    assert request["temperature"] == 0


@pytest.mark.parametrize("temperature", [0, 0.2, 1])
def test_glm_reasoning_requests_preserve_configured_temperature(temperature):
    llm, completions = _fake_llm("glm-5.3-flash")
    llm.reasoning_effort = "high"
    llm.temperature = temperature
    llm.invoke_with_tools([{"role": "user", "content": "Return JSON"}], [])
    assert completions.requests[0]["temperature"] == temperature
    assert completions.requests[0]["reasoning_effort"] == "high"


def test_deepseek_v4_lightweight_workers_are_not_thinking_forced():
    llm, _ = _fake_llm("deepseek-v4-flash")
    llm.enable_thinking = False

    assert llm.thinking_forced is False


def test_cli_routes_one_flash_model_with_thinking_only_for_important_roles(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = SimpleNamespace(
        model="deepseek-v4-flash",
        implement_model=None,
        reasoning_effort="high",
        coordinator_reasoning_effort="max",
        implement_reasoning_effort="max",
    )

    coordinator, overrides = _build_role_llms(args)

    assert coordinator.model == "deepseek-v4-flash"
    assert coordinator.enable_thinking is True
    assert coordinator.reasoning_effort == "max"
    assert overrides["implement"].model == "deepseek-v4-flash"
    assert overrides["implement"].reasoning_effort == "max"
    assert overrides["failure_review"].enable_thinking is True
    assert overrides["patch_review"].enable_thinking is True
    assert overrides["research"].enable_thinking is False
    assert overrides["verify"].enable_thinking is False
    assert overrides["memory_curator"].enable_thinking is False
    # DeepSeek 轻量角色可以真正关闭思考，不存在强制覆盖。
    assert overrides["research"].thinking_forced is False
    assert overrides["verify"].thinking_forced is False


def test_cli_roles_report_forced_thinking_for_glm_lightweight_workers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = SimpleNamespace(
        model="glm-5.3-flash",
        implement_model=None,
        reasoning_effort="high",
        coordinator_reasoning_effort="max",
        implement_reasoning_effort="max",
    )

    coordinator, overrides = _build_role_llms(args)

    assert coordinator.model == "glm-5.3-flash"
    assert coordinator.enable_thinking is True
    assert coordinator.thinking_forced is False
    # Coordinator 独立档位默认 max；GLM 下直接透传。
    assert coordinator.reasoning_effort == "max"
    assert overrides["implement"].model == "glm-5.3-flash"
    assert overrides["implement"].reasoning_effort == "max"
    # review workers 继续使用共享的 --reasoning-effort。
    assert overrides["patch_review"].reasoning_effort == "high"
    # 轻量角色仍按 enable_thinking=False 配置，但 GLM 强制思考会覆盖它。
    assert overrides["research"].enable_thinking is False
    assert overrides["research"].thinking_forced is True
    assert overrides["verify"].thinking_forced is True
    assert overrides["memory_curator"].thinking_forced is True


def test_cli_defaults_to_flash_and_high_coordinator_reasoning(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    args = build_parser().parse_args(
        ["run", "--repo", "/tmp/repo", "--task", "fix it"]
    )

    assert args.model == "glm-5.3-flash"
    # Coordinator 默认 max 档，review workers 保持 high，Implement 保持 max。
    assert args.coordinator_reasoning_effort == "max"
    assert args.reasoning_effort == "high"
    assert args.implement_reasoning_effort == "max"


def test_cli_uses_openai_model_environment_default(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "provider-model-from-env")

    args = build_parser().parse_args(
        ["run", "--repo", "/tmp/repo", "--task", "fix it"]
    )

    assert args.model == "provider-model-from-env"


def test_cli_explicit_model_overrides_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "provider-model-from-env")

    args = build_parser().parse_args(
        [
            "run",
            "--repo",
            "/tmp/repo",
            "--task",
            "fix it",
            "--model",
            "explicit-model",
        ]
    )

    assert args.model == "explicit-model"
