from types import SimpleNamespace

from repo_agent.cli import _build_role_llms, build_parser
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


def test_deepseek_v4_supports_role_specific_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")
    llm.reasoning_effort = "medium"

    llm.invoke_with_tools([{"role": "user", "content": "review"}], [])

    request = completions.requests[0]
    assert request["reasoning_effort"] == "medium"
    assert "temperature" not in request
    assert "tools" not in request
    assert "tool_choice" not in request


def test_deepseek_v4_can_disable_thinking_for_lightweight_workers():
    llm, completions = _fake_llm("deepseek-v4-flash")
    llm.enable_thinking = False

    llm.invoke_with_tools([{"role": "user", "content": "read only"}], [])

    request = completions.requests[0]
    assert "reasoning_effort" not in request
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "temperature" not in request


def test_tool_schema_enables_automatic_tool_choice():
    llm, completions = _fake_llm("gpt-4.1")
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    llm.invoke_with_tools([{"role": "user", "content": "分析"}], tools)

    request = completions.requests[0]
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"


def test_deepseek_v4_text_call_uses_max_reasoning_effort():
    llm, completions = _fake_llm("deepseek-v4-flash")

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


def test_cli_routes_one_flash_model_with_thinking_only_for_important_roles(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = SimpleNamespace(
        model="deepseek-v4-flash",
        implement_model=None,
        reasoning_effort="high",
        implement_reasoning_effort="max",
    )

    coordinator, overrides = _build_role_llms(args)

    assert coordinator.model == "deepseek-v4-flash"
    assert coordinator.enable_thinking is True
    assert overrides["implement"].model == "deepseek-v4-flash"
    assert overrides["implement"].reasoning_effort == "max"
    assert overrides["failure_review"].enable_thinking is True
    assert overrides["patch_review"].enable_thinking is True
    assert overrides["research"].enable_thinking is False
    assert overrides["verify"].enable_thinking is False
    assert overrides["memory_curator"].enable_thinking is False


def test_cli_defaults_to_flash_and_high_coordinator_reasoning():
    args = build_parser().parse_args(
        ["run", "--repo", "/tmp/repo", "--task", "fix it"]
    )

    assert args.model == "deepseek-v4-flash"
    assert args.reasoning_effort == "high"
    assert args.implement_reasoning_effort == "max"
