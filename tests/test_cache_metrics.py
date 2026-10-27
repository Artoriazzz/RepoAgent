from types import SimpleNamespace

from repo_agent.core.llm import ChatLLM
from repo_agent.core.prompts import prompt_version_for


class Usage:
    def __init__(self, cached_tokens: int | None, prompt_tokens: int = 1000):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = 10
        self.total_tokens = prompt_tokens + 10
        self.prompt_tokens_details = (
            None if cached_tokens is None else SimpleNamespace(cached_tokens=cached_tokens)
        )


class FakeCompletions:
    def __init__(self, usage):
        self.usage = usage
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        usage = {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
        }
        if self.usage.prompt_tokens_details is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": self.usage.prompt_tokens_details.cached_tokens}

        class Stream(list):
            def close(self):
                pass

        return Stream([SimpleNamespace(
            choices=[SimpleNamespace(index=0, delta=SimpleNamespace(content="完成", tool_calls=[]), finish_reason="stop")],
            usage=SimpleNamespace(model_dump=lambda exclude_none: usage),
        )])


def _llm(usage) -> ChatLLM:
    llm = ChatLLM.__new__(ChatLLM)
    llm.model = "glm-5.3-flash"
    llm.temperature = 0.1
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(usage)))
    return llm


def test_cached_tokens_are_reported_from_prompt_tokens_details():
    llm = _llm(Usage(cached_tokens=800))

    response = llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    assert response.usage["cached_tokens"] == 800
    assert response.usage["prompt_tokens"] == 1000
    assert llm.client.chat.completions.requests[0]["stream"] is True


def test_cached_tokens_default_to_zero_when_endpoint_omits_them():
    llm = _llm(Usage(cached_tokens=None))

    response = llm.invoke_with_tools([{"role": "user", "content": "分析"}], [])

    assert response.usage["cached_tokens"] == 0


def test_prompt_versions_are_fixed_and_phase_aware():
    assert prompt_version_for("coordinator") == "repoagent/coordinator/v1"
    assert prompt_version_for("failure_review") == "repoagent/failure_review/v1"
    assert prompt_version_for("experience_curator") == "repoagent/experience_curator/v1"
    # Implement 的 initial 与 corrective 必须是两个独立缓存池。
    assert prompt_version_for("implement", implement_phase="experience_initial") == (
        "repoagent/initial-implement/v1"
    )
    assert prompt_version_for("implement", implement_phase="experience_corrective") == (
        "repoagent/corrective-implement/v1"
    )
