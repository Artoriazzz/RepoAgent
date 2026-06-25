import json

from repo_agent.core.coordinator import CoordinatorEngine
from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.subagent import get_worker_spec
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, build_tool


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tool_names_by_call = []

    def invoke_with_tools(self, messages, tools):
        self.tool_names_by_call.append([tool["function"]["name"] for tool in tools])
        return self.responses.pop(0)


def _base_tools():
    registry = ToolRegistry()
    registry.register(
        build_tool(
            name="read_file",
            description="Read",
            parameters=[ToolParameter("path", "string", "Path")],
            handler=lambda inp: ToolResponse.success("content", {"path": inp["path"]}),
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        build_tool(
            name="write_file",
            description="Write",
            parameters=[
                ToolParameter("path", "string", "Path"),
                ToolParameter("content", "string", "Content"),
            ],
            handler=lambda inp: ToolResponse.success("written", {"path": inp["path"]}),
        )
    )
    registry.register(
        build_tool(
            name="search_repo",
            description="Search",
            parameters=[ToolParameter("query", "string", "Query")],
            handler=lambda inp: ToolResponse.success("matches", {"query": inp["query"]}),
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    return registry


def test_coordinator_only_exposes_agent_and_research_worker_is_read_only(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_agent",
                        name="agent",
                        arguments=json.dumps(
                            {
                                "description": "探索代码",
                                "prompt": "读取 src/app.py 并总结。",
                                "type": "research",
                            }
                        ),
                    )
                ],
                raw_message={"tool_calls": [{"id": "call_agent"}]},
                usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            ),
            LLMToolResponse(
                content="研究完成",
                tool_calls=[],
                raw_message={"content": "研究完成"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="最终结果",
                tool_calls=[],
                raw_message={"content": "最终结果"},
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            ),
        ]
    )
    engine = CoordinatorEngine(llm=llm, worker_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")

    assert engine.run("分析仓库") == "最终结果"
    assert llm.tool_names_by_call[0] == ["agent"]
    assert "read_file" in llm.tool_names_by_call[1]
    assert "search_repo" in llm.tool_names_by_call[1]
    assert "write_file" not in llm.tool_names_by_call[1]
    assert (tmp_path / "out" / "subagents").exists()


def test_worker_spec_aliases_explore_to_research():
    assert get_worker_spec("explore").name == "research"
    assert get_worker_spec("unknown").name == "general"
