import json
from copy import deepcopy

from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.query_loop import LoopEventType
from repo_agent.core.tool import ToolRegistry, ToolResponse, build_tool


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke_with_tools(self, messages, tools):
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        return self.responses.pop(0)


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
    assert len(llm.calls[1]["messages"]) == 2
    assert "Isolated JSON correction mode" in llm.calls[1]["messages"][0]["content"]
    assert "DRAFT_TO_REPAIR" in llm.calls[1]["messages"][1]["content"]
    assert '{"value":"incomplete"}' in llm.calls[1]["messages"][1]["content"]
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
    correction_prompt = llm.calls[1]["messages"][1]["content"]
    assert '{"value":"incomplete"}' in correction_prompt


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
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"max_steps_finalization": true' in trace
    assert '"completion_guard_correction": true' in trace
