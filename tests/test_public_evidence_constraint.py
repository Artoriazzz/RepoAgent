from types import SimpleNamespace

from repo_agent.core.prompts import PUBLIC_EVIDENCE_CONSTRAINT, build_system_prompt, with_public_evidence_constraint
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.coordinator import coordinator_system_prompt
from repo_agent.core.subagent import WORKER_SPECS
from repo_agent.core.memory_query import MemoryQueryAgent
from repo_agent.core.llm import ChatLLM, LLMToolResponse


def test_constraint_is_stable_and_idempotent():
    prompt = with_public_evidence_constraint("role")
    assert with_public_evidence_constraint(prompt) == prompt
    assert build_system_prompt().count(PUBLIC_EVIDENCE_CONSTRAINT) == 1


def test_all_worker_and_coordinator_prompts_receive_constraint():
    prompts = [s.system_prompt for s in WORKER_SPECS.values()]
    prompts += [coordinator_system_prompt(swebench_profile=True, repair_mode="experience-build"),
                coordinator_system_prompt(swebench_profile=False, repair_mode="benchmark")]
    for prompt in prompts:
        result = QueryEngine._append_stable_prompt_sections(prompt, "tool rules")
        assert result.count(PUBLIC_EVIDENCE_CONSTRAINT) == 1
        assert result.index(PUBLIC_EVIDENCE_CONSTRAINT) < result.index("tool rules")


def test_query_agent_and_side_requests_receive_constraint(tmp_path):
    class LLM:
        def invoke_with_tools(self, messages, tools):
            assert messages[0]["content"].count(PUBLIC_EVIDENCE_CONSTRAINT) == 1
            return LLMToolResponse(content='{"problem_description":"bug","usage_scenario":"call"}',
                                   tool_calls=[], raw_message={}, usage={})

    MemoryQueryAgent(LLM()).extract(issue_body="public issue", output_dir=tmp_path)
    llm = ChatLLM.__new__(ChatLLM)
    llm.model = "test"
    llm._completion_options = lambda: {}

    def completion(request):
        assert request["messages"][0]["content"].count(PUBLIC_EVIDENCE_CONSTRAINT) == 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    llm._create_completion = completion
    assert llm.invoke_text("role", "data") == "ok"
