import json
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from openai import APITimeoutError

from repo_agent import cli
from repo_agent.core.coordinator import CoordinatorEngine
from repo_agent.core.llm import LLMOutputLimitError, LLMToolResponse, ToolCall
from repo_agent.core.prompts import with_public_evidence_constraint
from repo_agent.core.memory import MemoryStore, MemoryType, RecalledMemory
from repo_agent.core.memory_query import (
    MEMORY_QUERY_SYSTEM_PROMPT,
    MemoryQueryAgent,
    parse_memory_query,
)
from repo_agent.core.tool import ToolRegistry


def response(value, *, calls=None):
    return LLMToolResponse(
        content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
        tool_calls=calls or [], raw_message={},
        usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    )


class FakeLLM:
    model = "query-model"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def invoke_with_tools(self, messages, tools):
        self.requests.append((deepcopy(messages), tools))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_extract_uses_only_public_issue_and_saves_usage_separately(tmp_path):
    llm = FakeLLM([response({
        "problem_description": "先出现维度错误，随后 Line3D 缺少 _verts3d。",
        "usage_scenario": "ax.plot 接收 numpy 数组。",
    })])
    result = MemoryQueryAgent(llm).extract(
        issue_title="Plot fails", issue_body="Array input fails before a missing attribute error.",
        output_dir=tmp_path,
    )
    assert result.status == "success"
    assert result.retrieval_text == (
        "问题描述：先出现维度错误，随后 Line3D 缺少 _verts3d。\n使用场景：ax.plot 接收 numpy 数组。"
    )
    messages, tools = llm.requests[0]
    assert tools == []
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == with_public_evidence_constraint(MEMORY_QUERY_SYSTEM_PROMPT)
    assert json.loads(messages[1]["content"]) == {
        "issue_title": "Plot fails", "issue_body": "Array input fails before a missing attribute error.",
    }
    audit = json.loads((tmp_path / "result.json").read_text())
    assert audit["usage"]["total_tokens"] == 30
    assert audit["model"] == "query-model" and audit["error"] is None
    assert audit["retrieval_text"] == result.retrieval_text
    assert json.loads((tmp_path / "request.json").read_text())["messages"] == messages


def test_each_call_has_fresh_history_and_unknown_scenario_is_allowed():
    llm = FakeLLM([
        response({"problem_description": "FIRST_EXTRACTED", "usage_scenario": ""}),
        response({"problem_description": "SECOND_EXTRACTED", "usage_scenario": ""}),
    ])
    agent = MemoryQueryAgent(llm)
    agent.extract(issue_body="FIRST_ISSUE")
    result = agent.extract(issue_body="SECOND_ISSUE")
    assert result.status == "success" and result.usage_scenario == ""
    second = llm.requests[1][0]
    assert len(second) == 2
    assert "FIRST_ISSUE" not in json.dumps(second)
    assert "FIRST_EXTRACTED" not in json.dumps(second)


@pytest.mark.parametrize("raw", [
    "not json",
    '[]',
    '{"problem_description": "", "usage_scenario": "x"}',
    '{"problem_description": "x"}',
    '{"problem_description": "x", "usage_scenario": null}',
    '{"problem_description": "x", "usage_scenario": "", "root_cause": "guess"}',
    '```json\n{"problem_description": "x", "usage_scenario": ""}\n```',
])
def test_invalid_output_falls_back_after_one_format_repair(raw, tmp_path):
    llm = FakeLLM([response(raw), response(raw)])
    result = MemoryQueryAgent(llm).extract(issue_title="TITLE", issue_body="PUBLIC_BODY", output_dir=tmp_path)
    assert result.status == "fallback_invalid_output"
    assert result.retrieval_text == "TITLE\nPUBLIC_BODY"
    assert result.usage_scenario == ""
    assert len(llm.requests) == 2
    audit = json.loads((tmp_path / "result.json").read_text())
    assert audit["raw_output"] == raw
    assert audit["error"]["reason"] == "invalid_query_output"


def test_invalid_json_has_one_format_repair_with_complete_original_issue(tmp_path):
    invalid = '{"problem_description":"bad","usage_scenario":"Plot(["a"])"}'
    llm = FakeLLM([response(invalid), response({"problem_description": "bad", "usage_scenario": 'Plot(["a"])'})])
    result = MemoryQueryAgent(llm).extract(issue_body="FULL_PUBLIC_ISSUE", output_dir=tmp_path)
    assert result.status == "success"
    assert result.usage_scenario == 'Plot(["a"])'
    history, tools = llm.requests[1]
    assert len(history) == 4 and tools == []
    assert json.loads(history[1]['content'])['issue_body'] == 'FULL_PUBLIC_ISSUE'
    assert history[2]['content'] == invalid
    audit = json.loads((tmp_path / 'result.json').read_text())
    assert audit['usage']['total_tokens'] == 60
    assert audit['format_repair']['original_output'] == invalid


def test_timeout_falls_back_and_records_error_type(tmp_path):
    llm = FakeLLM([APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))])
    result = MemoryQueryAgent(llm).extract(issue_body="PUBLIC_BODY", output_dir=tmp_path)
    assert result.status == "fallback_api_error"
    assert result.retrieval_text == "PUBLIC_BODY"
    audit = json.loads((tmp_path / "result.json").read_text())
    assert audit["error"]["type"] == "APITimeoutError"
    assert audit["usage"] == {}


def test_output_limit_keeps_available_usage_and_does_not_use_partial_json(tmp_path):
    llm = FakeLLM([LLMOutputLimitError(
        {"content": '{"problem_description": "unfinished'}, {"total_tokens": 4096}
    )])
    result = MemoryQueryAgent(llm).extract(issue_body="PUBLIC_BODY", output_dir=tmp_path)
    assert result.status == "fallback_output_limit"
    assert result.retrieval_text == "PUBLIC_BODY"
    audit = json.loads((tmp_path / "result.json").read_text())
    assert audit["usage"]["total_tokens"] == 4096
    assert audit["error"]["reason"] == "query_output_limit"


def test_unexpected_tool_call_is_never_executed():
    llm = FakeLLM([response(
        {"problem_description": "x", "usage_scenario": ""},
        calls=[ToolCall("call1", "shell", '{"command": "pwd"}')],
    )])
    assert MemoryQueryAgent(llm).extract(issue_body="PUBLIC_BODY").status == "fallback_invalid_output"
    assert llm.requests[0][1] == []


def test_empty_input_does_not_make_model_request():
    llm = FakeLLM([])
    with pytest.raises(ValueError, match="nonempty"):
        MemoryQueryAgent(llm).extract(issue_body=" \n")
    assert llm.requests == []


def test_coordinator_uses_extracted_query_but_keeps_original_context(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    filename = store.save_memory("relevant", "relevant", "global_experience", "HISTORICAL_RULE")
    header = next(h for h in store.list_memories() if h.filename == filename)
    memory = RecalledMemory(header=header, content=store.read_memory(filename))
    store.recall_experiences = Mock(side_effect=lambda query, *, memory_type, limit: (
        [memory] if memory_type == MemoryType.GLOBAL_EXPERIENCE else []
    ))
    llm = FakeLLM([response({
        "problem_description": "EXTRACTED_PROBLEM", "usage_scenario": "EXTRACTED_SCENARIO",
    })])
    engine = CoordinatorEngine(
        llm=FakeLLM([]), worker_tools=ToolRegistry(), cwd=tmp_path,
        output_dir=tmp_path / "out", memory_store=store,
        memory_query_agent=MemoryQueryAgent(llm), swebench_profile=True, repair_mode="benchmark",
    )
    engine.engine.chat = Mock(return_value="FINAL")
    engine._write_stage_attribution = Mock()
    task = "## Instance\nCASE_ID\n## Problem Statement\nPUBLIC_ISSUE\n## Instructions\nRUNNER_ONLY"
    assert engine.run(task) == "FINAL"
    for call in store.recall_experiences.call_args_list:
        assert call.args[0] == "问题描述：EXTRACTED_PROBLEM\n使用场景：EXTRACTED_SCENARIO"
    coordinator_input = engine.engine.chat.call_args.args[0]
    assert task in coordinator_input and "HISTORICAL_RULE" in coordinator_input
    assert "EXTRACTED_PROBLEM" not in coordinator_input
    assert "EXTRACTED_SCENARIO" not in coordinator_input
    assert MEMORY_QUERY_SYSTEM_PROMPT not in coordinator_input
    sent_issue = json.loads(llm.requests[0][0][1]["content"])
    assert sent_issue["issue_body"] == "PUBLIC_ISSUE"


@pytest.mark.parametrize("mode,recall", [("experience-build", True), ("benchmark", False)])
def test_disabled_recall_does_not_call_extractor(tmp_path, mode, recall):
    query = MemoryQueryAgent(FakeLLM([]))
    query.extract = Mock(side_effect=AssertionError("Extraction must not run"))
    engine = CoordinatorEngine(
        llm=FakeLLM([]), worker_tools=ToolRegistry(), cwd=tmp_path,
        output_dir=tmp_path / "out", memory_query_agent=query,
        swebench_profile=True, repair_mode=mode, repair_memory_recall=recall,
    )
    engine.engine.chat = Mock(return_value="FINAL")
    engine._write_stage_attribution = Mock()
    engine.run("PUBLIC_ISSUE")
    query.extract.assert_not_called()


def test_cli_standalone_extracts_without_constructing_repair_agents(tmp_path, monkeypatch, capsys):
    issue_file = tmp_path / "task.md"
    issue_file.write_text("## Instance\nCASE\n## Problem Statement\nPUBLIC_ISSUE\n## Instructions\nRUNNER")
    llm = FakeLLM([response({"problem_description": "公开问题", "usage_scenario": ""})])
    factory = Mock(return_value=llm)
    monkeypatch.setattr(cli, "ChatLLM", factory)
    monkeypatch.setattr(cli, "_build_role_llms", Mock(side_effect=AssertionError("No repair models")))
    monkeypatch.setattr(cli, "RepoWorkspace", Mock(side_effect=AssertionError("No workspace")))
    cli.main(["extract-memory-query", "--issue-file", str(issue_file), "--model", "small-query-model",
              "--base-url", "https://query.example/v1", "--output-dir", str(tmp_path / "query")])
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert factory.call_args.kwargs["model"] == "small-query-model"
    assert factory.call_args.kwargs["enable_thinking"] is False
    assert factory.call_args.kwargs["output_limit_recovery"] is False
    assert json.loads(llm.requests[0][0][1]["content"])["issue_body"] == "PUBLIC_ISSUE"


def test_separate_provider_and_key_are_supported(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "main-key")
    monkeypatch.setenv("REPO_AGENT_MEMORY_QUERY_API_KEY", "query-key")
    factory = Mock(return_value=FakeLLM([]))
    monkeypatch.setattr(cli, "ChatLLM", factory)
    cli._build_memory_query_agent(model="query-model", base_url="https://query.example/v1", max_output_tokens=1024)
    assert factory.call_args.kwargs["api_key"] == "query-key"
    assert factory.call_args.kwargs["base_url"] == "https://query.example/v1"
    assert factory.call_args.kwargs["max_output_tokens"] == 1024


def test_parser_supports_opt_in_model_configuration(monkeypatch):
    monkeypatch.delenv("REPO_AGENT_MEMORY_QUERY_MODEL", raising=False)
    parser = cli.build_parser()
    assert parser.parse_args(["run", "--repo", ".", "--task", "x"]).memory_query_model is None
    monkeypatch.setenv("REPO_AGENT_MEMORY_QUERY_MODEL", "env-query-model")
    args = cli.build_parser().parse_args(["benchmark", "--repo", ".", "--task", "x"])
    assert args.memory_query_model == "env-query-model"
    assert args.memory_query_max_output_tokens == 4096


def test_parser_accepts_empty_scenario_without_inventing_fields():
    assert parse_memory_query('{"problem_description":"  issue  ","usage_scenario":""}') == ("issue", "")


def test_default_experience_recall_injects_only_one_per_type(tmp_path):
    from repo_agent.core.experience import augment_coordinator_with_experiences

    store = MemoryStore(tmp_path / "memory")
    for kind in (MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE):
        for index in range(2):
            store.save_memory(f"array-{index}", "array", kind.value, f"array rule {index}")
    text, memories = augment_coordinator_with_experiences(task="array", memory_store=store)
    assert len(memories) == 2
    assert [memory.header.type for memory in memories] == [
        MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE,
    ]
    assert text.count("#### 1.") == 2 and "#### 2." not in text


def test_cli_connects_separate_query_model_to_coordinator(tmp_path, monkeypatch, capsys):
    from repo_agent.core.memory_query import MemoryQueryResult

    query_agent = Mock()
    query_agent.extract.return_value = MemoryQueryResult("EXTRACTED", "SCENARIO", "success")
    builder = Mock(return_value=query_agent)
    monkeypatch.setattr(cli, "_build_memory_query_agent", builder)
    monkeypatch.setattr(cli, "_build_role_llms", Mock(return_value=(FakeLLM([]), {})))
    received = {}

    def make_coordinator(**kwargs):
        received.update(kwargs)
        coordinator = Mock()
        coordinator.run.return_value = "FINAL"
        return coordinator

    monkeypatch.setattr(cli, "CoordinatorEngine", make_coordinator)
    cli.main([
        "benchmark", "--repo", str(tmp_path), "--task", "PUBLIC_ISSUE",
        "--output-dir", str(tmp_path / "out"), "--memory-root", str(tmp_path / "memory"),
        "--model", "repair-model", "--memory-query-model", "query-model",
        "--memory-query-base-url", "https://query.example/v1", "--memory-query-max-output-tokens", "1024",
    ])
    assert capsys.readouterr().out.strip() == "FINAL"
    builder.assert_called_once_with(model="query-model", base_url="https://query.example/v1", max_output_tokens=1024)
    assert received["memory_query_agent"] is query_agent


def test_cli_requires_public_issue_and_coordinator_for_automatic_extraction(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO_AGENT_MEMORY_QUERY_MODEL", raising=False)
    for command in (
        ["run", "--task", "PUBLIC_ISSUE"],
        ["fix-issue", "--issue-url", "https://github.com/a/b/issues/1", "--coordinator"],
    ):
        with pytest.raises(SystemExit):
            cli.main([*command, "--repo", str(tmp_path), "--memory-query-model", "query-model"])


def test_coordinator_fallback_retrieves_public_issue_without_api_error_context(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.recall_experiences = Mock(return_value=[])
    query_llm = FakeLLM([APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))])
    engine = CoordinatorEngine(
        llm=FakeLLM([]), worker_tools=ToolRegistry(), cwd=tmp_path,
        output_dir=tmp_path / "out", memory_store=store,
        memory_query_agent=MemoryQueryAgent(query_llm), swebench_profile=True, repair_mode="benchmark",
    )
    engine.engine.chat = Mock(return_value="FINAL")
    engine._write_stage_attribution = Mock()
    task = "## Problem Statement\nPUBLIC_ISSUE\n## Instructions\nRUNNER_ONLY"
    assert engine.run(task) == "FINAL"
    assert all(call.args[0] == "PUBLIC_ISSUE" for call in store.recall_experiences.call_args_list)
    assert engine.engine.chat.call_args.args[0] == task
