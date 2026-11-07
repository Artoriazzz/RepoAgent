import json
import pytest
from copy import deepcopy

from repo_agent.core.llm import LLMToolResponse, ToolCall, LLMOutputLimitError
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.query_loop import LoopEventType, _completion_guard_correction_messages
from repo_agent.core.tool import ToolRegistry, ToolResponse, build_tool


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke_with_tools(self, messages, tools):
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        return self.responses.pop(0)


def test_output_limit_persists_explicit_stop_reason(tmp_path):
    class OutputLimitedLLM:
        def invoke_with_tools(self, messages, tools):
            raise LLMOutputLimitError({"reasoning_content": "unfinished"}, None)

    engine = QueryEngine(llm=OutputLimitedLLM(), tools=ToolRegistry(), output_dir=tmp_path)
    with pytest.raises(LLMOutputLimitError):
        engine.chat("repair task")
    session = json.loads((tmp_path / "session.json").read_text())
    assert session["stop_reason"] == "output_limit"
    assert session["completed"] is False


def test_query_engine_runs_tool_loop_and_persists(tmp_path):
    registry = ToolRegistry()
    registry.register_function(lambda answer: answer, name="finish", description="Finish task")
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="finish", arguments=json.dumps({"answer": "完成"}))],
                raw_message={"tool_calls": [{"id": "call_1"}]},
                usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            )
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path)

    answer = engine.chat("做任务")

    assert answer == "完成"
    assert engine.total_tokens == 7
    assert (tmp_path / "messages.json").exists()
    assert (tmp_path / "session.json").exists()
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert LoopEventType.LLM_RESPONSE.value in trace
    assert LoopEventType.TOOL_RESULT.value in trace
    session = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert session["completed"] is True
    assert session["stop_reason"] == "finished"


def test_query_engine_stops_without_tool_calls(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="直接完成",
                tool_calls=[],
                raw_message={"content": "直接完成"},
                usage={},
            )
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path)

    assert engine.chat("hello") == "直接完成"
    assert engine.messages[-1] == {"role": "assistant", "content": "直接完成"}


def test_query_engine_passes_reasoning_content_after_tool_call(tmp_path):
    registry = ToolRegistry()
    registry.register_function(lambda path: "content", name="read_file", description="Read")
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments=json.dumps({"path": "a.py"}))],
                raw_message={
                    "reasoning_content": "先读取目标文件。",
                    "tool_calls": [{"id": "call_1"}],
                },
                usage={},
            ),
            LLMToolResponse(content="完成", tool_calls=[], raw_message={"content": "完成"}, usage={}),
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path)

    assert engine.chat("读取文件") == "完成"

    assistant_message = next(
        message for message in llm.calls[1]["messages"] if message.get("role") == "assistant"
    )
    assert assistant_message["reasoning_content"] == "先读取目标文件。"
    assert assistant_message["tool_calls"][0]["id"] == "call_1"


def test_query_engine_passes_reasoning_content_across_consecutive_tool_calls(tmp_path):
    """连续工具调用场景下，每一轮 reasoning_content 都必须原样带回且不互相覆盖。"""

    registry = ToolRegistry()
    registry.register_function(lambda path: "content", name="read_file", description="Read")
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments=json.dumps({"path": "a.py"}))],
                raw_message={
                    "reasoning_content": "第一步：先读取 a.py。",
                    "tool_calls": [{"id": "call_1"}],
                },
                usage={},
            ),
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_2", name="read_file", arguments=json.dumps({"path": "b.py"}))],
                raw_message={
                    "reasoning_content": "第二步：继续读取 b.py。",
                    "tool_calls": [{"id": "call_2"}],
                },
                usage={},
            ),
            LLMToolResponse(content="完成", tool_calls=[], raw_message={"content": "完成"}, usage={}),
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path)

    assert engine.chat("连续读取") == "完成"

    first_assistant = [m for m in llm.calls[1]["messages"] if m.get("role") == "assistant"][-1]
    second_assistant = [m for m in llm.calls[2]["messages"] if m.get("role") == "assistant"][-1]
    assert first_assistant["reasoning_content"] == "第一步：先读取 a.py。"
    assert first_assistant["tool_calls"][0]["id"] == "call_1"
    assert second_assistant["reasoning_content"] == "第二步：继续读取 b.py。"
    assert second_assistant["tool_calls"][0]["id"] == "call_2"


def test_query_engine_records_max_steps_stop_reason(tmp_path):
    registry = ToolRegistry()
    registry.register_function(lambda path: "content", name="read_file", description="Read")
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments=json.dumps({"path": "a.py"}))],
                raw_message={"tool_calls": [{"id": "call_1"}]},
                usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            )
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, max_steps=1, output_dir=tmp_path)

    assert engine.chat("读文件") == "达到最大步数，任务未明确完成。"
    assert engine.completed is False
    assert engine.stop_reason == "max_steps"
    session = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert session["completed"] is False
    assert session["stop_reason"] == "max_steps"


def test_query_engine_honors_tool_requested_terminal_state(tmp_path):
    registry = ToolRegistry()
    registry.register(
        build_tool(
            name="terminal_check",
            description="Finish after a one-shot check",
            parameters=[],
            handler=lambda _inp: ToolResponse.success(
                "checked",
                {
                    "terminate_loop": True,
                    "terminal_reason": "verification_attempted",
                    "final_answer": "保留当前 patch 并结束。",
                },
            ),
        )
    )
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_terminal", name="terminal_check", arguments="{}")],
                raw_message={"tool_calls": [{"id": "call_terminal"}]},
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, max_steps=10, output_dir=tmp_path)

    answer = engine.chat("检查后结束")

    assert answer == "保留当前 patch 并结束。"
    assert engine.completed is True
    assert engine.stop_reason == "finished"
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"tool_terminated": true' in trace


def test_query_engine_records_incomplete_tool_terminal_state(tmp_path):
    registry = ToolRegistry()
    registry.register(
        build_tool(
            name="terminal_check",
            description="Stop with incomplete evidence",
            parameters=[],
            handler=lambda _inp: ToolResponse.partial(
                "incomplete",
                {
                    "terminate_loop": True,
                    "terminal_reason": "verification_incomplete",
                    "terminal_completed": False,
                },
            ),
        )
    )
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_terminal", name="terminal_check", arguments="{}")],
                raw_message={"tool_calls": [{"id": "call_terminal"}]},
                usage={},
            )
        ]
    )
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path)

    engine.chat("检查")

    assert engine.completed is False
    assert engine.stop_reason == "verification_incomplete"


def test_completion_guard_rejects_early_natural_language_finish(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMToolResponse(content="done too early", tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(content="blocked_insufficient_edit_scope", tool_calls=[], raw_message={}, usage={}),
        ]
    )
    calls = {"count": 0}

    def guard(answer):
        calls["count"] += 1
        return "dispatch implement" if calls["count"] == 1 else None

    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path, completion_guard=guard)

    answer = engine.chat("修复")

    assert answer == "blocked_insufficient_edit_scope"
    assert any(message.get("content", "").startswith("[Workflow guard]") for message in engine.messages)


def test_completion_guard_gets_no_tool_correction_outside_max_steps(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMToolResponse(content='{"value":"incomplete"}', tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(content='{"value":"complete"}', tool_calls=[], raw_message={}, usage={}),
        ]
    )

    def guard(answer):
        return None if '"complete"' in answer else "Return corrected JSON only."

    engine = QueryEngine(
        llm=llm,
        tools=registry,
        max_steps=1,
        output_dir=tmp_path,
        completion_guard=guard,
        completion_guard_extra_turns=1,
    )

    assert engine.chat("produce JSON") == '{"value":"complete"}'
    assert engine.completed is True
    assert engine.stop_reason == "finished"
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] == []
    history = llm.calls[1]["messages"]
    assert any(m.get("content") == "produce JSON" for m in history)
    assert any(m.get("content") == '{"value":"incomplete"}' for m in history)
    assert any(str(m.get("content", "")).startswith("[Workflow guard]") for m in history)
    assert history[-1]["content"].startswith("[JSON correction]")
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"completion_guard_correction": true' in trace


def test_completion_guard_repairs_reasoning_content_draft(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="",
                tool_calls=[],
                raw_message={"reasoning_content": '{"value":"incomplete"}'},
                usage={},
            ),
            LLMToolResponse(
                content='{"value":"complete"}',
                tool_calls=[],
                raw_message={"content": '{"value":"complete"}'},
                usage={},
            ),
        ]
    )

    def guard(answer):
        return None if '"complete"' in answer else "Return corrected JSON only."

    engine = QueryEngine(
        llm=llm,
        tools=registry,
        max_steps=1,
        output_dir=tmp_path,
        completion_guard=guard,
        completion_guard_extra_turns=1,
    )

    assert engine.chat("produce JSON") == '{"value":"complete"}'
    correction_prompt = json.dumps(llm.calls[1]["messages"])
    assert "incomplete" in correction_prompt


def test_json_correction_keeps_full_failure_evidence_and_repairs_reference(tmp_path):
    from repo_agent.core.subagent import _failure_review_completion_guard

    registry = ToolRegistry()
    registry.register_function(lambda: "SOURCE_FACT: app.py:12 escapes the name before both branches",
                               name="read_file", description="Read source")
    payload = {
        "failure_stage": "implementation_semantics", "issue_contract": ["Do not overescape attribute names"],
        "observed_failure": ["Expected name_, got name\\_"], "failure_observation_quality": "expected_actual",
        "root_cause": "Escaping precedes both output branches", "invalidated_assumption": "Only one branch escapes",
        "required_correction": "Cover both attribute branches", "regression_guards": [], "scope_expansion": [],
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "support": "direct", "fact": "Expected name_, got name\\_"},
            {"id": "E2", "source": "public_task", "support": "direct", "fact": "Do not overescape attributes"},
            {"id": "E3", "source": "repository_code", "support": "direct", "fact": "app.py:12 escapes both branches"},
        ],
        "correction_evidence_ids": ["E1", "E3"], "correction_readiness": "actionable",
        "next_evidence_source": "none", "confidence": 0.8,
    }
    repaired = {**payload, "correction_evidence_ids": ["E1", "E2", "E3"]}
    llm = FakeLLM([
        LLMToolResponse(content=None, tool_calls=[ToolCall("read1", "read_file", "{}")],
            raw_message={"role": "assistant", "tool_calls": [{"id": "read1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"}}]}, usage={}),
        LLMToolResponse(content=json.dumps(payload), tool_calls=[], raw_message={}, usage={}),
        LLMToolResponse(content=json.dumps(repaired), tool_calls=[], raw_message={}, usage={}),
    ])
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path, max_steps=2,
        completion_guard=_failure_review_completion_guard, completion_guard_extra_turns=1)
    preparations = []
    prepare = engine.context_manager.before_model_call

    def record_preparation(messages):
        preparations.append(deepcopy(messages))
        return prepare(messages)

    engine.context_manager.before_model_call = record_preparation
    task = "PUBLIC_TASK: do not overescape. FAILED_PATCH: only one branch fixed. FAILURE_LOG: expected name_, got name\\_."
    assert json.loads(engine.chat(task)) == repaired
    assert engine.completed
    assert len(preparations) == 2  # 修正回合不再压缩或截断证据历史。
    correction = llm.calls[-1]
    assert correction["tools"] == []
    assert any(m.get("content") == task for m in correction["messages"])
    source_messages = [m for m in correction["messages"] if m.get("role") == "tool"]
    assert len(source_messages) == 1
    assert "SOURCE_FACT" in source_messages[0]["content"]
    assert source_messages[0]["tool_call_id"] == "read1"
    assert "direct Public Task" in json.dumps(correction["messages"])
    assert "[JSON correction]" in (tmp_path / "messages.json").read_text()


def test_json_correction_history_is_a_deep_copy():
    messages = [{"role": "assistant", "tool_calls": [{"id": "read1"}]}]
    correction = _completion_guard_correction_messages(messages)
    correction[0]["tool_calls"][0]["id"] = "changed"
    assert messages[0]["tool_calls"][0]["id"] == "read1"
    assert len(messages) == 1


def test_json_correction_still_rejects_new_tool_calls(tmp_path):
    registry = ToolRegistry()
    executions = []
    registry.register_function(lambda: executions.append("executed"), name="read_file", description="Read")
    llm = FakeLLM([
        LLMToolResponse(content="invalid JSON", tool_calls=[], raw_message={}, usage={}),
        LLMToolResponse(content=None, tool_calls=[ToolCall("forbidden", "read_file", "{}")],
                        raw_message={}, usage={}),
    ])
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path, max_steps=1,
                         completion_guard=lambda _: "Return JSON", completion_guard_extra_turns=1)
    engine.chat("task")
    assert not engine.completed
    assert engine.stop_reason == "completion_guard_failed"
    assert llm.calls[-1]["tools"] == []
    assert executions == []


def test_completion_guard_invalid_extra_turn_is_incomplete(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMToolResponse(content="not json", tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(content="still not json", tool_calls=[], raw_message={}, usage={}),
        ]
    )
    engine = QueryEngine(
        llm=llm,
        tools=registry,
        max_steps=1,
        output_dir=tmp_path,
        completion_guard=lambda _answer: "Return JSON only.",
        completion_guard_extra_turns=1,
    )

    assert engine.chat("produce JSON") == "still not json"
    assert engine.completed is False
    assert engine.stop_reason == "completion_guard_failed"


def test_max_steps_forces_no_tool_finalization_then_guard_correction(tmp_path):
    registry = ToolRegistry()
    registry.register_function(lambda path: path, name="read_file", description="Read")
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="read_1", name="read_file", arguments='{"path":"a.py"}')],
                raw_message={"tool_calls": [{"id": "read_1"}]},
                usage={},
            ),
            LLMToolResponse(content='{"value":"incomplete"}', tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(content='{"value":"complete"}', tool_calls=[], raw_message={}, usage={}),
        ]
    )

    def guard(answer):
        return None if '"complete"' in answer else "Return corrected JSON only."

    finalizer_histories = []

    def finalize(messages):
        finalizer_histories.append(list(messages))
        return llm.invoke_with_tools(messages, [])

    llm.invoke_finalization = finalize
    engine = QueryEngine(
        llm=llm,
        tools=registry,
        max_steps=1,
        output_dir=tmp_path,
        completion_guard=guard,
        completion_guard_extra_turns=1,
        force_completion_after_max_steps=True,
    )

    assert engine.chat("produce JSON after reading") == '{"value":"complete"}'
    assert engine.completed is True
    assert len(llm.calls) == 3
    assert llm.calls[1]["tools"] == []
    assert llm.calls[2]["tools"] == []
    assert len(finalizer_histories) == 1
    assert any(message["role"] == "tool" for message in finalizer_histories[0])
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"max_steps_finalization": true' in trace
    assert '"completion_guard_correction": true' in trace


def test_source_budget_exhaustion_finalizes_with_full_history_before_max_steps(tmp_path):
    registry = ToolRegistry()
    registry.register(build_tool(
        name="read_file", description="Read source", parameters=[],
        handler=lambda inp: ToolResponse.success(
            "SOURCE_FACT: predict returns transform(T)",
            {"remaining_source_queries": 0, "force_final_output": True},
        ),
    ))
    llm = FakeLLM([
        LLMToolResponse(content=None,
            tool_calls=[ToolCall(id="read_last", name="read_file", arguments="{}")],
            raw_message={}, usage={}),
        LLMToolResponse(content='{"status":"insufficient_evidence"}',
            tool_calls=[], raw_message={}, usage={}),
    ])
    engine = QueryEngine(llm=llm, tools=registry, max_steps=6, output_dir=tmp_path,
                         completion_guard=lambda text: None if json.loads(text).get("status") else "Missing status")
    assert engine.chat("FAILURE_FACT: expected ndarray, got DataFrame") == '{"status":"insufficient_evidence"}'
    assert engine.completed
    assert len(llm.calls) == 2
    final = llm.calls[-1]
    assert final["tools"] == []
    history = json.dumps(final["messages"])
    assert "FAILURE_FACT" in history
    assert "SOURCE_FACT" in history
    assert "read_last" in history


def test_max_steps_finalization_keeps_source_and_failure_without_new_session(tmp_path):
    registry = ToolRegistry()
    registry.register_function(lambda: "SOURCE_FACT", name="read_file", description="Read source")
    llm = FakeLLM([
        LLMToolResponse(content=None,
            tool_calls=[ToolCall(id="read1", name="read_file", arguments="{}")],
            raw_message={}, usage={}),
        LLMToolResponse(content='{"status":"done"}', tool_calls=[], raw_message={}, usage={}),
    ])
    engine = QueryEngine(llm=llm, tools=registry, max_steps=1, output_dir=tmp_path,
                         force_completion_after_max_steps=True)
    assert engine.chat("FAILURE_FACT") == '{"status":"done"}'
    assert engine.completed
    assert llm.calls[-1]["tools"] == []
    assert "SOURCE_FACT" in json.dumps(llm.calls[-1]["messages"])
    assert "FAILURE_FACT" in json.dumps(llm.calls[-1]["messages"])
