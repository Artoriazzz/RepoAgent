import json
import os
import subprocess
import sys

from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.stage_history import StageConversation, child_environment, prompt_history, snapshot, stage_history
from repo_agent.core.tool import ToolRegistry


def test_archive_keeps_full_tool_result_and_restores_own_history(tmp_path):
    replies = iter([
        LLMToolResponse(None, [ToolCall("read-1", "read_file", "{}")], {}, {}),
        LLMToolResponse("first answer", [], {}, {}),
    ])

    class LLM:
        def invoke_with_tools(self, messages, tools):
            return next(replies)

    tools = ToolRegistry()
    tools.register_function(lambda: "full evidence", name="read_file", description="Read")

    def shorten(_name, result):
        result.text = "preview"
        return result

    with stage_history(None, None):
        engine = QueryEngine(LLM(), tools, output_dir=tmp_path / "first", cwd=tmp_path)
        engine.context_manager.process_tool_response = shorten
        assert engine.chat("first request") == "first answer"
        body = snapshot({"detail": "old material"})
    recorded = body["sessions"][0]["messages"]
    assert "full evidence" in next(msg["content"] for msg in recorded if msg["role"] == "tool")
    assert "preview" in next(msg["content"] for msg in engine.messages if msg["role"] == "tool")

    incoming = {"body": {"material": {"detail": "incoming evidence"}, "sessions": [
        {"scope": ".", "messages": [{"role": "assistant", "content": "other agent's history"}]},
    ]}}
    with stage_history({"body": body}, incoming):
        resumed = QueryEngine(LLM(), tools, output_dir=tmp_path / "second", cwd=tmp_path)
        assert any(msg.get("content") == "first answer" for msg in resumed.messages)
        assert any("incoming evidence" in (msg.get("content") or "") for msg in resumed.messages)
        assert not any(msg.get("content") == "other agent's history" for msg in resumed.messages)
        assert snapshot({})["sessions"] == body["sessions"]


def test_subprocess_writes_to_stage_scope_without_leaking_to_next_task(tmp_path):
    with stage_history(None, None):
        env = child_environment(tmp_path)
        subprocess.run([
            sys.executable, "-c",
            "from pathlib import Path; from repo_agent.core.stage_history import StageConversation;"
            "StageConversation(Path.cwd()).save([{'role':'user','content':'child evidence'}])",
        ], cwd=tmp_path, env=env, check=True)
        assert snapshot({})["sessions"] == [
            {"scope": ".", "messages": [{"role": "user", "content": "child evidence"}]},
        ]
    assert not os.path.exists(env["REPO_AGENT_STAGE_HISTORY_DIR"])
    with stage_history(None, None):
        assert snapshot({})["sessions"] == []
    assert StageConversation().path is None


def test_prompt_budget_does_not_leave_orphaned_tool_messages():
    history = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 1000},
        {"role": "assistant", "tool_calls": [{"id": "unanswered"}]},
        {"role": "assistant", "content": "end"},
    ]
    assert prompt_history(history, budget=150) == [history[-1]]
