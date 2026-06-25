import json

from repo_agent.core.context import ConversationContextManager
from repo_agent.core.tool import ToolResponse


def test_context_manager_persists_large_tool_result(tmp_path):
    manager = ConversationContextManager(tmp_path)
    response = ToolResponse.success("x" * (35 * 1024))

    processed = manager.process_tool_response("run_command", response)

    assert processed.status == "partial"
    assert processed.data["persisted"] is True
    result_path = tmp_path / "tool-results"
    assert any(result_path.iterdir())
    assert "Full output saved" in processed.text


def test_context_manager_budgets_tool_messages(tmp_path):
    manager = ConversationContextManager(tmp_path)
    manager.last_input_token_count = int(manager.config.effective_window * 0.75)
    payload = {"status": "success", "text": "x" * 40_000, "data": {"tool": "run_command"}}
    messages = [{"role": "tool", "content": json.dumps(payload), "tool_call_id": "call_1"}]

    manager.before_model_call(messages)

    updated = json.loads(messages[0]["content"])
    assert updated["data"]["context_budgeted"] is True
    assert len(updated["text"]) < 20_000
