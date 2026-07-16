from types import SimpleNamespace

from repo_agent.core.llm import ChatLLM


class FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        message = SimpleNamespace(
            content="完成",
            tool_calls=[],
            model_dump=lambda exclude_none: {"content": "完成"},
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _fake_llm(model: str) -> tuple[ChatLLM, FakeCompletions]:
    completions = FakeCompletions()
    llm = ChatLLM.__new__(ChatLLM)
    llm.model = model
    llm.temperature = 0.1
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm, completions


def test_deepseek_v4_tool_call_uses_max_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in request
    assert "tools" not in request
    assert "tool_choice" not in request


def test_tool_schema_enables_automatic_tool_choice():
    llm, completions = _fake_llm("gpt-4.1")
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], tools)

    request = completions.requests[0]
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"


def test_deepseek_v4_text_call_uses_max_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-pro")

    assert llm.invoke_text("system", "user") == "完成"

    request = completions.requests[0]
    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in request


def test_other_models_keep_temperature_without_deepseek_options():
    llm, completions = _fake_llm("gpt-4.1")

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    request = completions.requests[0]
    assert request["temperature"] == 0.1
    assert "reasoning_effort" not in request
    assert "extra_body" not in request
