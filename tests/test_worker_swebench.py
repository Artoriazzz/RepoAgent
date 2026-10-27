import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from repo_agent.worker.models import RepairTaskContext
from repo_agent.worker.workspace import WorkspaceError, WorktreeManager


def test_swebench_context_accepts_nullable_github_fields():
    task_id = uuid4()
    issue_id = uuid4()
    repository_id = uuid4()
    context = RepairTaskContext.from_payload(
        {
            "taskId": str(task_id),
            "issueId": str(issue_id),
            "repositoryId": str(repository_id),
            "repositoryProvider": "SWE_BENCH",
            "repositoryExternalId": "pallets/flask",
            "mirrorKey": "pallets__flask",
            "githubRepositoryId": None,
            "repositoryFullName": "pallets/flask",
            "cloneUrl": None,
            "defaultBranch": "main",
            "githubIssueNumber": None,
            "issueExternalId": "pallets__flask-4045",
            "benchmarkDatasetSplit": "lite",
            "issueTitle": "SWE-bench: pallets__flask-4045",
            "issueBody": "Fix the parser.",
            "baseSha": "abc1234",
            "branchName": f"repoagent/benchmark-pallets__flask-4045/run-{task_id}",
        }
    )

    assert context.repository_provider == "SWE_BENCH"
    assert context.github_repository_id is None
    assert context.github_issue_number is None
    assert context.mirror_key == "pallets__flask"


def test_swebench_worktree_uses_named_mirror(tmp_path):
    mirror_root = tmp_path / "swebench-mirrors"
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    source.joinpath("README.md").write_text("base\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    mirror_root.mkdir()
    subprocess.run(
        ["git", "clone", "--mirror", str(source), str(mirror_root / "pallets__flask.git")],
        check=True,
        text=True,
        capture_output=True,
    )

    context = _swebench_context(base_sha)
    manager = WorktreeManager(tmp_path / "github", tmp_path / "worktrees", mirror_root)
    worktree = manager.prepare(context)

    assert worktree.joinpath("README.md").read_text(encoding="utf-8") == "base\n"
    assert _git(worktree, "branch", "--show-current").stdout.strip() == context.branch_name


def test_swebench_worktree_rejects_path_traversal(tmp_path):
    context = _swebench_context("abc1234", mirror_key="../secret")
    manager = WorktreeManager(tmp_path / "github", tmp_path / "worktrees", tmp_path / "mirrors")

    with pytest.raises(WorkspaceError, match="Invalid SWE-bench mirror key"):
        manager.prepare(context)


def _swebench_context(base_sha: str, mirror_key: str = "pallets__flask") -> RepairTaskContext:
    task_id = uuid4()
    return RepairTaskContext(
        task_id=task_id,
        issue_id=uuid4(),
        repository_id=uuid4(),
        repository_provider="SWE_BENCH",
        repository_external_id="pallets/flask",
        mirror_key=mirror_key,
        github_repository_id=None,
        repository_full_name="pallets/flask",
        clone_url=None,
        default_branch="main",
        github_issue_number=None,
        issue_external_id="pallets__flask-4045",
        benchmark_dataset_split="lite",
        issue_title="SWE-bench: pallets__flask-4045",
        issue_body="Fix the parser.",
        base_sha=base_sha,
        branch_name=f"repoagent/benchmark-pallets__flask-4045/run-{task_id}",
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
