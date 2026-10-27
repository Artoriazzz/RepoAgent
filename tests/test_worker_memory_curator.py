from uuid import uuid4

from repo_agent.worker.evaluator import BenchmarkEvaluation
from repo_agent.worker.memory_curator import SemanticMemoryCurator
from repo_agent.worker.models import PatchArtifacts, RecalledMemory, RepairTaskContext


class FakeLLM:
    def __init__(self, *responses: str):
        self.responses = list(responses)

    def invoke_text(self, _system: str, _user: str) -> str:
        return self.responses.pop(0)


class FakeBackend:
    def __init__(self, candidates=(), responses=()):
        self.candidates = candidates
        self.responses = list(responses)
        self.proposals = []

    def search_memories(self, _task_id, _query, **_kwargs):
        return self.candidates

    def submit_memory_proposal(self, **kwargs):
        self.proposals.append(kwargs)
        return self.responses.pop(0)


def test_curator_updates_only_a_supplied_candidate():
    candidate = RecalledMemory(
        uuid4(), 2, 0.9, "Parser contract", "Keep both sides aligned",
        "Reader and writer share a boundary", "Change the shared boundary",
    )
    backend = FakeBackend(
        (candidate,),
        ({"status": "PENDING", "memoryId": str(candidate.memory_id), "appliedRevision": None},),
    )
    llm = FakeLLM(
        '{"operation":"UPDATE","memoryId":"%s","content":'
        '{"title":"Parser contract","summary":"Keep both sides aligned",'
        '"applicability":"Reader and writer share a boundary",'
        '"repairStrategy":"Change the shared boundary",'
        '"verificationStrategy":"Run round trips","antiPatterns":"One-sided patch"},'
        '"changeReason":"Adds a regression signal"}' % candidate.memory_id
    )

    result = SemanticMemoryCurator(backend, llm).curate(
        _context(), PatchArtifacts("diff --git a/a b/a", ("a",)),
        BenchmarkEvaluation("RESOLVED", fail_to_pass_passed=1), uuid4(),
    )

    assert result.proposal_status == "PENDING"
    assert backend.proposals[0]["memory_id"] == candidate.memory_id
    assert backend.proposals[0]["base_version"] == 2
    assert backend.proposals[0]["content"]["retrievalText"] == "Keep both sides aligned"


def test_curator_does_not_rebase_a_concurrent_update_onto_a_newer_memory():
    candidate = RecalledMemory(uuid4(), 1, 0.8, "Old", "Old", "Old", "Old")
    backend = FakeBackend(
        (candidate,),
        (
            {"status": "STALE", "memoryId": str(candidate.memory_id), "appliedRevision": 2},
        ),
    )
    first = (
        '{"operation":"UPDATE","memoryId":"%s","content":'
        '{"title":"T","summary":"S","applicability":"A","repairStrategy":"R"},'
        '"changeReason":"new evidence"}' % candidate.memory_id
    )
    result = SemanticMemoryCurator(backend, FakeLLM(first)).curate(
        _context(), PatchArtifacts("diff", ("a",)), BenchmarkEvaluation("RESOLVED"), uuid4()
    )

    assert result.proposal_status == "STALE"
    assert len(backend.proposals) == 1
    assert backend.proposals[0]["base_version"] == 1


def _context() -> RepairTaskContext:
    task_id = uuid4()
    return RepairTaskContext(
        task_id=task_id, issue_id=uuid4(), repository_id=uuid4(),
        repository_provider="SWE_BENCH", repository_external_id="owner/repo",
        mirror_key="owner__repo", github_repository_id=None,
        repository_full_name="owner/repo", clone_url=None, default_branch="main",
        github_issue_number=None, issue_external_id="owner__repo-1",
        benchmark_dataset_split="lite", issue_title="Fix parser",
        issue_body="Parser rejects an input", base_sha="abc", branch_name=f"run-{task_id}",
    )
