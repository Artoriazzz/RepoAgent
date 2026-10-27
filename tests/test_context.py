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


def test_context_manager_keeps_history_immutable_below_threshold(tmp_path):
    """低于阈值时不改写任何历史消息，保证请求前缀可以命中缓存。"""

    manager = ConversationContextManager(tmp_path)
    manager.last_input_token_count = int(manager.config.effective_window * 0.3)
    payload = {"status": "success", "text": "x" * 40_000, "data": {"tool": "run_command"}}
    messages = [{"role": "tool", "content": json.dumps(payload), "tool_call_id": "call_1"}]
    original = messages[0]["content"]

    manager.before_model_call(messages)

    assert messages[0]["content"] == original
    assert manager.history_stabilized is False


def test_context_manager_stabilizes_history_only_once(tmp_path):
    """达到阈值后只做一次确定性压缩，之后保持历史不变。"""

    manager = ConversationContextManager(tmp_path)
    manager.last_input_token_count = int(manager.config.effective_window * 0.75)
    payload = {"status": "success", "text": "x" * 40_000, "data": {"tool": "run_command"}}
    messages = [{"role": "tool", "content": json.dumps(payload), "tool_call_id": "call_1"}]

    manager.before_model_call(messages)
    stabilized = messages[0]["content"]
    assert manager.history_stabilized is True

    # 后续轮次即使压力继续升高也不再改写。
    manager.last_input_token_count = int(manager.config.effective_window * 0.95)
    manager.before_model_call(messages)
    assert messages[0]["content"] == stabilized
    assert manager.stabilization_count == 1
