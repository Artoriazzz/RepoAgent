from repo_agent.core.prompts import build_system_prompt, load_project_instructions
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, build_tool


class FakeLLM:
    def invoke_with_tools(self, messages, tools):
        raise AssertionError("LLM should not be called in prompt construction tests")


def test_build_system_prompt_contains_repo_agent_layers(tmp_path):
    prompt = build_system_prompt(cwd=tmp_path, tool_rules="读取前先确认路径。")

    assert "你是 RepoAgent" in prompt
    assert "# System" in prompt
    assert "# Doing Tasks" in prompt
    assert "# Executing Actions With Care" in prompt
    assert "# Using Tools" in prompt
    assert "# Tone And Style" in prompt
    assert "# Output Efficiency" in prompt
    assert str(tmp_path.resolve()) in prompt
    assert "读取前先确认路径。" in prompt


def test_project_instructions_loads_includes_and_rules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "style.md").write_text("保持小步提交。", encoding="utf-8")
    (repo / "AGENTS.md").write_text("@./style.md\n项目使用 Python。", encoding="utf-8")
    rules_dir = repo / ".repoagent" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "testing.md").write_text("修改后运行测试。", encoding="utf-8")

    instructions = load_project_instructions(repo)

    assert "# Project Instructions" in instructions
    assert "保持小步提交。" in instructions
    assert "项目使用 Python。" in instructions
    assert "修改后运行测试。" in instructions


def test_query_engine_embeds_tool_prompt_rules(tmp_path):
    registry = ToolRegistry()
    registry.register(
        build_tool(
            name="finish",
            description="Finish",
            parameters=[ToolParameter("answer", "string", "Answer")],
            handler=lambda inp: ToolResponse.success(inp["answer"]),
            prompt="完成时必须调用 finish。",
        )
    )

    engine = QueryEngine(llm=FakeLLM(), tools=registry, output_dir=tmp_path, cwd=tmp_path)

    assert "完成时必须调用 finish。" in engine.messages[0]["content"]
    assert "# Tool-specific Rules" in engine.messages[0]["content"]
