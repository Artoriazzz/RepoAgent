import json

from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.query_loop import LoopEventType
from repo_agent.core.tool import ToolRegistry


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def invoke_with_tools(self, messages, tools):
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
