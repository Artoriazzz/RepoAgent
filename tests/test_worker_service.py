from pathlib import Path
from uuid import uuid4

from repo_agent.worker.models import AgentRunResult, PatchArtifacts, RepairTaskContext, RepairTaskMessage
from repo_agent.worker.service import RepairWorkerService


class FakeBackend:
    def __init__(self, context: RepairTaskContext):
        self.context = context
        self.updates: list[tuple[str, dict]] = []

    def get_context(self, _task_id):
        return self.context

    def update(self, _task_id, status: str, **kwargs):
        self.updates.append((status, kwargs))


class FakeWorkspace:
    def __init__(self, patch: str):
        self.patch = patch

    def prepare(self, _context):
        return Path("/tmp/worker-worktree")

    def collect_patch(self, _worktree):
        return PatchArtifacts(self.patch, ("src/parser.py",))


class FakeExecutor:
    def __init__(self, exit_code: int = 0):
        self.exit_code = exit_code

    def run(self, _context, _worktree, _output_dir):
        return AgentRunResult(self.exit_code, "agent output")


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


def test_worker_marks_task_failed_when_repo_agent_produces_no_patch(tmp_path):
    context = _context()
    backend = FakeBackend(context)
    service = RepairWorkerService(backend, FakeWorkspace(""), FakeExecutor(), tmp_path)

    assert service.process(_message(context)) is False

    assert backend.updates[-1][0] == "FAILED"
    assert "without a patch" in backend.updates[-1][1]["validation_summary"]


def _context() -> RepairTaskContext:
    task_id = uuid4()
    return RepairTaskContext(
        task_id=task_id,
        issue_id=uuid4(),
        repository_id=uuid4(),
        github_repository_id=101,
        repository_full_name="octo/demo",
        clone_url="https://github.com/octo/demo.git",
        default_branch="main",
        github_issue_number=7,
        issue_title="Fix parser",
        issue_body="Parser rejects valid input.",
        base_sha="abc123",
        branch_name=f"repoagent/issue-7/run-{task_id}",
    )


def _message(context: RepairTaskContext) -> RepairTaskMessage:
    return RepairTaskMessage(
        task_id=context.task_id,
        issue_id=context.issue_id,
        base_sha=context.base_sha,
        branch_name=context.branch_name,
    )
