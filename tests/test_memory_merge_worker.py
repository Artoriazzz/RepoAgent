from repo_agent.worker.memory_merge_worker import SemanticMemoryMergeAgent


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    def invoke_text(self, _system: str, _user: str) -> str:
        return self.response


def test_merge_agent_normalizes_a_valid_semantic_memory_update():
    agent = SemanticMemoryMergeAgent(
        FakeLLM(
            '{"action":"APPLY","content":{"title":"Parser contract",'
            '"summary":"Keep reader and writer aligned",'
            '"applicability":"Shared parser boundaries",'
            '"repairStrategy":"Change the common contract"},'
            '"changeReason":"Adds a reusable contract boundary"}'
        )
    )

    decision = agent.merge({"operation": "UPDATE", "proposals": []})

    assert decision.action == "APPLY"
    assert decision.content["retrievalText"] == "Keep reader and writer aligned"
    assert decision.change_reason == "Adds a reusable contract boundary"


def test_merge_agent_allows_no_change_without_hallucinating_content():
    decision = SemanticMemoryMergeAgent(FakeLLM('{"action":"NO_CHANGE"}')).merge(
        {"operation": "UPDATE", "proposals": []}
    )

    assert decision.action == "NO_CHANGE"
    assert decision.content is None
