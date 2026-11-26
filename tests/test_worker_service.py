from pathlib import Path
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

from repo_agent.worker.models import (
    AgentRunResult,
    PatchArtifacts,
    RecalledMemory,
    RepairTaskContext,
    RepairTaskMessage,
    RepairPipelineStage,
)
from repo_agent.worker.service import RepairWorkerService
from repo_agent.worker.evaluator import BenchmarkEvaluation
from repo_agent.worker.backend_client import WorkerLease
from repo_agent.core.stage_history import StageConversation


class FakeBackend:
    def __init__(self, context: RepairTaskContext):
        self.context = context
        self.updates: list[tuple[str, dict]] = []
        self.evaluation_updates: list[tuple[str, dict]] = []
        self.events: list[tuple[str, str]] = []
        self.heartbeats: list[tuple[str, object]] = []
        self.lease_token = uuid4()
        self.cancel_requested = False
        self.recalled_memories: tuple[RecalledMemory, ...] = ()
        self.memory_feedback: list[tuple] = []
        self.memory_observations: list[dict] = []
        self.handoffs: list[tuple] = []
        self.handoff_contexts: list[dict] = []
        self.pipeline_completions: list[tuple] = []
        self.memory_searches: list[tuple] = []

    def claim(self, _task_id, _worker_id, _lease_seconds=90, stage=None):
        return self.lease_token

    def renew_lease(self, _task_id, _worker_id, _lease_token, _lease_seconds=90):
        return {}

    def get_control(self, _task_id, _worker_id, _lease_token):
        return {"cancelRequested": self.cancel_requested}

    def report_event(self, _task_id, stage, message, _worker_id, _lease_token, _metadata_json=None):
        self.events.append((stage, message))

    def heartbeat(self, _worker_id, _status, _active_task_id=None, _metadata_json=None):
        self.heartbeats.append((_status, _active_task_id))
        return {}

    def get_context(self, _task_id):
        return self.context

    def update(self, _task_id, status: str, **kwargs):
        self.updates.append((status, kwargs))

    def update_evaluation(self, _task_id, status: str, **kwargs):
        self.evaluation_updates.append((status, kwargs))

    def search_memories(self, _task_id, _query, **_kwargs):
        self.memory_searches.append((_task_id, _query, _kwargs))
        return self.recalled_memories

    def record_memory_feedback(self, memory_id, task_id, revision, **kwargs):
        self.memory_feedback.append((memory_id, task_id, revision, kwargs))
        return {}

    def record_memory_observation(self, _task_id, **kwargs):
        self.memory_observations.append(kwargs)
        return {"id": str(uuid4())}

    def handoff_stage(
        self, task_id, current_stage, next_stage, worker_id, lease_token, message, *, agent_context=None
    ):
        self.handoffs.append(
            (task_id, current_stage, next_stage, worker_id, lease_token, message)
        )
        self.handoff_contexts.append(agent_context)
        return {}

    def complete_pipeline(self, task_id, **kwargs):
        self.pipeline_completions.append((task_id, kwargs))
        return {}


class FakeWorkspace:
    def __init__(self, patch: str):
        self.patch = patch

    def prepare(self, _context):
        return Path("/tmp/worker-worktree")

    def collect_patch(self, _worktree):
        return PatchArtifacts(self.patch, ("src/parser.py",))


class FakeExecutor:
    def __init__(self, exit_code: int = 0, output_tail: str = "agent output"):
        self.exit_code = exit_code
        self.output_tail = output_tail
        self.context = None

    def run(self, context, _worktree, _output_dir, should_cancel=None):
        self.context = context
        return AgentRunResult(self.exit_code, self.output_tail)


class CoordinatorAttributionExecutor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.contexts = []

    def run(self, context, _worktree, output_dir, should_cancel=None):
        self.context = context
        self.contexts.append(context)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir.joinpath("stage_attribution.json").write_text(
            json.dumps({
                "stages": {
                    "reproduction": {
                        "semantic_evidence": {
                            "reproduction_contract": {
                                "trigger": ["a nullable dtype contains a missing value"],
                                "current_behavior": ["the classifier reports unknown"],
                                "expected_behavior": ["use the numeric classification"],
                            }
                        }
                    }
                }
            }),
            encoding="utf-8",
        )
        return AgentRunResult(0, self.output_tail)

    def review_failure(
        self, _context, _worktree, output_dir, _artifacts, _evaluation, should_cancel=None
    ):
        review_dir = output_dir / "failure_review"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_dir.joinpath("failure_feedback.json").write_text(
            json.dumps(
                {
                    "failure_stage": "implementation_semantics",
                    "issue_contract": ["The nullable input must use numeric classification."],
                    "observed_failure": ["The patched result is still unknown."],
                    "failure_observation_quality": "expected_actual",
                    "root_cause": "The first predicate still reads the unnormalized representation.",
                    "invalidated_assumption": "Changing only the later branch was sufficient.",
                    "required_correction": "Normalize before the first classification predicate.",
                    "evidence_ledger": [
                        {
                            "id": "E1",
                            "source": "public_task",
                            "fact": "Nullable input requires numeric classification.",
                            "support": "direct",
                        },
                        {
                            "id": "E2",
                            "source": "failure_log",
                            "fact": "Expected numeric but observed unknown.",
                            "support": "direct",
                        },
                    ],
                    "correction_evidence_ids": ["E1", "E2"],
                    "scope_expansion": [],
                    "regression_guards": ["Preserve existing dense array behavior."],
                    "correction_readiness": "actionable",
                    "next_evidence_source": "none",
                    "confidence": 0.9,
                }
            ),
            encoding="utf-8",
        )
        return AgentRunResult(0, "reviewed")


class FakeEvaluator:
    def evaluate(self, _context, _patch, _output_dir, should_cancel=None):
        return BenchmarkEvaluation(
            status="RESOLVED",
            fail_to_pass_passed=2,
            pass_to_pass_passed=4,
            summary="resolved",
        )


class FakeUnresolvedEvaluator:
    def evaluate(self, _context, _patch, _output_dir, should_cancel=None):
        return BenchmarkEvaluation(
            status="UNRESOLVED",
            fail_to_pass_failed=1,
            pass_to_pass_passed=4,
            summary="unresolved",
        )


class FakeMemoryCurator:
    def __init__(self):
        self.calls = []

    def curate(self, context, artifacts, evaluation, observation_id):
        self.calls.append((context, artifacts, evaluation, observation_id))
        return SimpleNamespace(
            operation="UPDATE", proposal_status="APPLIED", memory_id=str(uuid4()), revision=2
        )


def test_worker_reports_patch_ready_only_after_a_nonempty_patch(tmp_path):
    context = _context()
    backend = FakeBackend(context)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
    )

    assert service.process(_message(context)) is True

    assert [status for status, _ in backend.updates] == [
        "ANALYZING",
        "FIXING",
        "VALIDATING",
        "PATCH_READY",
    ]
    assert (tmp_path / str(context.task_id) / "model.patch").is_file()
    assert backend.updates[-1][1]["patch_artifact_path"].endswith("model.patch")
    assert backend.heartbeats[0] == ("BUSY", context.task_id)
    assert backend.heartbeats[-1] == ("IDLE", None)


def test_worker_delays_each_visible_replay_stage(tmp_path, monkeypatch):
    context = _context()
    backend = FakeBackend(context)
    delays = []
    monkeypatch.setattr("repo_agent.worker.service.time.sleep", delays.append)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
        stage_delay_seconds=10,
    )

    assert service.process(_message(context)) is True

    assert delays == [10, 10, 10, 10]
    assert [stage for stage, _ in backend.events] == [
        "ANALYZING",
        "COORDINATOR",
        "VALIDATING",
        "PATCH_READY",
    ]


def test_worker_marks_task_failed_when_repo_agent_produces_no_patch(tmp_path):
    context = _context()
    backend = FakeBackend(context)
    service = RepairWorkerService(backend, FakeWorkspace(""), FakeExecutor(), tmp_path)

    assert service.process(_message(context)) is False

    assert backend.updates[-1][0] == "FAILED"
    assert "without a patch" in backend.updates[-1][1]["validation_summary"]
    assert backend.updates[-1][1]["error_code"] == "EMPTY_PATCH"
    assert backend.updates[-1][1]["retryable"] is False


def test_worker_confirms_cancellation_before_starting_agent(tmp_path):
    context = _context()
    backend = FakeBackend(context)
    backend.cancel_requested = True
    service = RepairWorkerService(backend, FakeWorkspace("unused"), FakeExecutor(), tmp_path)

    assert service.process(_message(context)) is False

    assert [status for status, _ in backend.updates] == ["ANALYZING", "CANCELLED"]
    assert "cancelled" in backend.updates[-1][1]["validation_summary"]


def test_worker_evaluates_swebench_patch_after_patch_ready(tmp_path):
    context = replace(
        _context(),
        repository_provider="SWE_BENCH",
        repository_external_id="pallets/flask",
        mirror_key="pallets__flask",
        github_repository_id=None,
        clone_url=None,
        github_issue_number=None,
        issue_external_id="pallets__flask-4045",
    )
    backend = FakeBackend(context)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
        evaluator=FakeEvaluator(),
    )

    assert service.process(_message(context)) is True

    assert [status for status, _ in backend.evaluation_updates] == ["RUNNING", "RESOLVED"]
    assert backend.evaluation_updates[-1][1]["fail_to_pass_passed"] == 2
    assert "RETRYING" not in [stage for stage, _ in backend.events]
    assert "EXPERIENCE_MEMORY" in [stage for stage, _ in backend.events]


def test_unresolved_replay_reports_single_retry_before_experience_memory(tmp_path, monkeypatch):
    context = replace(
        _context(), repository_provider="SWE_BENCH", issue_external_id="demo__repo-1"
    )
    backend = FakeBackend(context)
    delays = []
    monkeypatch.setattr("repo_agent.worker.service.time.sleep", delays.append)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
        evaluator=FakeUnresolvedEvaluator(),
        stage_delay_seconds=10,
    )

    assert service.process(_message(context)) is True

    stages = [stage for stage, _ in backend.events]
    assert stages.index("RETRYING") < stages.index("EXPERIENCE_MEMORY")
    assert backend.evaluation_updates[-1][0] == "UNRESOLVED"
    assert delays[-2:] == [10, 10]


def test_pipeline_workers_handoff_a_resolved_task_between_durable_stage_queues(tmp_path):
    context = replace(
        _context(), repository_provider="SWE_BENCH", issue_external_id="demo__repo-1"
    )
    backend = FakeBackend(context)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
        evaluator=FakeEvaluator(),
    )

    for stage in (
        RepairPipelineStage.COORDINATOR,
        RepairPipelineStage.IMPLEMENT,
        RepairPipelineStage.VALIDATION,
        RepairPipelineStage.EXPERIENCE_MEMORY,
    ):
        assert service.process_stage(_message(context, stage)) is True

    assert [handoff[1:3] for handoff in backend.handoffs] == [
        (RepairPipelineStage.COORDINATOR, RepairPipelineStage.IMPLEMENT),
        (RepairPipelineStage.IMPLEMENT, RepairPipelineStage.VALIDATION),
        (RepairPipelineStage.VALIDATION, RepairPipelineStage.EXPERIENCE_MEMORY),
    ]
    assert [status for status, _ in backend.evaluation_updates] == ["RUNNING"]
    assert len(backend.pipeline_completions) == 1
    assert backend.pipeline_completions[0][1]["evaluation_status"] == "RESOLVED"
    assert [context["material"]["stage"] for context in backend.handoff_contexts] == [
        "COORDINATOR", "IMPLEMENT", "VALIDATION",
    ]
    assert backend.pipeline_completions[0][1]["agent_context"]["material"]["stage"] == "EXPERIENCE_MEMORY"
    assert "RETRYING" not in [stage for stage, _ in backend.events]


def test_pipeline_restores_claimed_history_and_submits_new_snapshot(tmp_path):
    context = _context()
    backend = FakeBackend(context)

    class CapturingExecutor(FakeExecutor):
        def run(self, context, worktree, output_dir, should_cancel=None):
            conversation = StageConversation()
            assert conversation.previous == [{"role": "assistant", "content": "old round"}]
            assert conversation.material == {"detail": "current handoff"}
            conversation.save([{"role": "assistant", "content": "new round"}])
            return super().run(context, worktree, output_dir, should_cancel)

    service = RepairWorkerService(
        backend, FakeWorkspace("patch"), CapturingExecutor(), tmp_path
    )
    assert service.process_stage(_message(context, RepairPipelineStage.COORDINATOR))
    previous = {"scope": ".", "messages": [{"role": "assistant", "content": "old round"}]}
    backend.lease_token = WorkerLease(
        uuid4(), 1, 2,
        own_context={"body": {"sessions": [previous]}},
        incoming_context={"body": {"material": {"detail": "current handoff"}}},
    )
    assert service.process_stage(_message(context, RepairPipelineStage.IMPLEMENT))
    assert backend.handoff_contexts[-1]["sessions"] == [
        previous, {"scope": ".", "messages": [{"role": "assistant", "content": "new round"}]},
    ]


def test_pipeline_unresolved_task_runs_failure_review_before_final_memory_stage(tmp_path):
    context = replace(
        _context(), repository_provider="SWE_BENCH", issue_external_id="demo__repo-2"
    )
    backend = FakeBackend(context)
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        FakeExecutor(),
        tmp_path,
        evaluator=FakeUnresolvedEvaluator(),
    )

    for stage in (
        RepairPipelineStage.COORDINATOR,
        RepairPipelineStage.IMPLEMENT,
        RepairPipelineStage.VALIDATION,
        RepairPipelineStage.FAILURE_REVIEW,
        RepairPipelineStage.EXPERIENCE_MEMORY,
    ):
        assert service.process_stage(_message(context, stage)) is True

    handoffs = [handoff[1:3] for handoff in backend.handoffs]
    assert (RepairPipelineStage.VALIDATION, RepairPipelineStage.FAILURE_REVIEW) in handoffs
    assert (RepairPipelineStage.FAILURE_REVIEW, RepairPipelineStage.EXPERIENCE_MEMORY) in handoffs
    assert all(next_stage != RepairPipelineStage.CORRECTIVE_IMPLEMENT for _, next_stage in handoffs)
    assert backend.pipeline_completions[0][1]["evaluation_status"] == "UNRESOLVED"
    assert "RETRYING" in [stage for stage, _ in backend.events]


def test_corrective_implement_does_not_recall_memory_after_failed_validation(tmp_path):
    context = replace(
        _context(), repository_provider="SWE_BENCH", issue_external_id="demo__repo-2"
    )
    backend = FakeBackend(context)
    executor = CoordinatorAttributionExecutor()
    service = RepairWorkerService(
        backend,
        FakeWorkspace("diff --git a/src/parser.py b/src/parser.py\n"),
        executor,
        tmp_path,
        evaluator=FakeUnresolvedEvaluator(),
        memory_mode="recall",
    )

    for stage in (
        RepairPipelineStage.COORDINATOR,
        RepairPipelineStage.IMPLEMENT,
        RepairPipelineStage.VALIDATION,
        RepairPipelineStage.FAILURE_REVIEW,
        RepairPipelineStage.CORRECTIVE_IMPLEMENT,
        RepairPipelineStage.VALIDATION,
        RepairPipelineStage.EXPERIENCE_MEMORY,
    ):
        assert service.process_stage(_message(context, stage)) is True

    assert len(backend.memory_searches) == 1
    assert "MEMORY_QUERY_REFINED" not in [stage for stage, _ in backend.events]
    handoffs = [handoff[1:3] for handoff in backend.handoffs]
    assert handoffs[-1] == (
        RepairPipelineStage.VALIDATION,
        RepairPipelineStage.EXPERIENCE_MEMORY,
    )
    assert sum(
        next_stage == RepairPipelineStage.FAILURE_REVIEW
        for _, next_stage in handoffs
    ) == 1


def test_recall_mode_injects_pinned_memory_and_records_explicit_use(tmp_path):
    context = replace(_context(), repository_provider="SWE_BENCH")
    memory = RecalledMemory(
        memory_id=uuid4(), revision=3, score=0.91, title="Parser boundary",
        summary="Normalize before validation", applicability="Nullable input",
        repair_strategy="Reuse the existing conversion helper",
    )
    backend = FakeBackend(context)
    backend.recalled_memories = (memory,)
    executor = FakeExecutor(output_tail=f"done\nMEMORY_USED: {memory.memory_id}@3")
    service = RepairWorkerService(
        backend, FakeWorkspace("diff --git a/a b/a\n"), executor, tmp_path,
        evaluator=FakeEvaluator(), memory_mode="recall",
    )

    assert service.process(_message(context)) is True

    assert executor.context.recalled_memories == (memory,)
    assert backend.memory_feedback[0][3] == {"used": True, "successful": True}
    assert backend.memory_observations == []


def test_learn_mode_does_not_recall_and_stores_only_resolved_observation(tmp_path):
    context = replace(_context(), repository_provider="SWE_BENCH")
    backend = FakeBackend(context)
    backend.recalled_memories = (
        RecalledMemory(uuid4(), 1, 0.8, "old", "old", "old", "old"),
    )
    executor = FakeExecutor()
    service = RepairWorkerService(
        backend, FakeWorkspace("diff --git a/a b/a\n"), executor, tmp_path,
        evaluator=FakeEvaluator(), memory_mode="learn",
    )

    assert service.process(_message(context)) is True

    assert executor.context.recalled_memories == ()
    assert len(backend.memory_observations) == 1
    assert backend.memory_observations[0]["outcome"] == "RESOLVED"


def test_evolve_mode_recalls_then_curates_after_resolved_evaluation(tmp_path):
    context = replace(_context(), repository_provider="SWE_BENCH")
    memory = RecalledMemory(
        uuid4(), 4, 0.88, "Boundary contract", "Preserve both consumers",
        "Shared producer changes", "Validate every public consumer",
    )
    backend = FakeBackend(context)
    backend.recalled_memories = (memory,)
    curator = FakeMemoryCurator()
    executor = FakeExecutor(output_tail=f"MEMORY_USED: {memory.memory_id}@4")
    service = RepairWorkerService(
        backend, FakeWorkspace("diff --git a/a b/a\n"), executor, tmp_path,
        evaluator=FakeEvaluator(), memory_mode="evolve", memory_curator=curator,
    )

    assert service.process(_message(context)) is True

    assert executor.context.recalled_memories == (memory,)
    assert backend.memory_feedback[0][3] == {"used": True, "successful": True}
    assert len(backend.memory_observations) == 1
    assert len(curator.calls) == 1


def _context() -> RepairTaskContext:
    task_id = uuid4()
    return RepairTaskContext(
        task_id=task_id,
        issue_id=uuid4(),
        repository_id=uuid4(),
        repository_provider="GITHUB",
        repository_external_id="octo/demo",
        mirror_key=None,
        github_repository_id=101,
        repository_full_name="octo/demo",
        clone_url="https://github.com/octo/demo.git",
        default_branch="main",
        github_issue_number=7,
        issue_external_id=None,
        benchmark_dataset_split=None,
        issue_title="Fix parser",
        issue_body="Parser rejects valid input.",
        base_sha="abc123",
        branch_name=f"repoagent/issue-7/run-{task_id}",
    )


def _message(
    context: RepairTaskContext,
    stage: RepairPipelineStage = RepairPipelineStage.COORDINATOR,
) -> RepairTaskMessage:
    return RepairTaskMessage(
        task_id=context.task_id,
        issue_id=context.issue_id,
        base_sha=context.base_sha,
        branch_name=context.branch_name,
        stage=stage,
    )
