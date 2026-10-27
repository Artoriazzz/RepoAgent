import os
import platform
from datetime import date

from repo_agent.core.prompts import (
    build_dynamic_context,
    build_runtime_context,
    build_system_prompt,
    load_project_instructions,
)
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, build_tool


class FakeLLM:
    def invoke_with_tools(self, messages, tools):
        raise AssertionError("LLM should not be called in prompt construction tests")


def test_experience_curator_describes_title_and_retrieval_scenario():
    from repo_agent.core.subagent import EXPERIENCE_CURATOR_PROMPT

    assert "标题：" in EXPERIENCE_CURATOR_PROMPT
    assert "problem_description（问题描述）" in EXPERIENCE_CURATOR_PROMPT
    assert "usage_scenario（使用场景）" in EXPERIENCE_CURATOR_PROMPT
    assert "description 保存简洁的 problem_description" in EXPERIENCE_CURATOR_PROMPT
    assert "不从成功 patch 反推" in EXPERIENCE_CURATOR_PROMPT
    assert "不为补字段重复新增经验" in EXPERIENCE_CURATOR_PROMPT


def test_build_system_prompt_contains_standalone_layers(tmp_path):
    prompt = build_system_prompt(tool_rules="读取前先确认路径。")

    assert "维护 GitHub 仓库的代码助手" in prompt
    assert "RepoAgent" not in prompt
    assert "其他角色的隐含上下文" in prompt
    assert "# System" in prompt
    assert "# Doing Tasks" in prompt
    assert "# Executing Actions With Care" in prompt
    assert "# Using Tools" in prompt
    assert "# Tone And Style" in prompt
    assert "# Output Efficiency" in prompt
    assert "读取前先确认路径。" in prompt


def test_system_prompt_is_stable_across_repositories(tmp_path):
    """稳定 system prompt 不得包含任何随任务变化的值，否则缓存前缀会分叉。"""

    first = build_system_prompt()
    second = build_system_prompt()

    assert first == second
    assert build_system_prompt() == first
    # 实际动态值不应出现在稳定前缀里。
    assert date.today().isoformat() not in first
    assert str(tmp_path.resolve()) not in first
    assert platform.machine() not in first
    assert os.environ.get("SHELL", "/bin/sh") not in first
    assert "# Project Instructions" not in first


def test_dynamic_context_carries_repository_and_memory(tmp_path):
    runtime = build_runtime_context(cwd=tmp_path)
    assert str(tmp_path.resolve()) in runtime
    assert "当前日期" in runtime

    dynamic = build_dynamic_context(cwd=tmp_path, memory_section="# Memory Index\n条目 A")
    assert str(tmp_path.resolve()) in dynamic
    assert "条目 A" in dynamic
    # 关闭动态上下文时不产生任何内容，调用方不应追加消息。
    assert build_dynamic_context(cwd=tmp_path, include_dynamic_context=False, memory_section="x") == "x"


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
