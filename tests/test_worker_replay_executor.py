import json
import subprocess
from pathlib import Path
from uuid import uuid4

from repo_agent.worker.models import RepairTaskContext
from repo_agent.worker.replay_executor import ReplayRepoAgentExecutor


def test_replay_executor_applies_prediction_by_instance_id(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    repo.joinpath("value.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "value.txt")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    repo.joinpath("value.txt").write_text("new\n", encoding="utf-8")
    patch = _git(repo, "diff", "--", "value.txt").stdout
    _git(repo, "restore", "value.txt")

    predictions = tmp_path / "predictions.json"
    predictions.write_text(
        json.dumps([{
            "instance_id": "owner__repo-1",
            "model_patch": patch,
            "model_response": "Historical repair",
            "total_tokens": 321,
        }]),
        encoding="utf-8",
    )
    output = tmp_path / "output"

    result = ReplayRepoAgentExecutor(predictions).run(_context(base_sha), repo, output)

    assert result.exit_code == 0
    assert result.total_tokens == 321
    assert repo.joinpath("value.txt").read_text(encoding="utf-8") == "new\n"
    assert json.loads(output.joinpath("session.json").read_text())["simulation"] is True
    assert "SIMULATED LLM REPLAY" in output.joinpath("repo_agent.log").read_text()


def test_replay_executor_reports_missing_instance_without_modifying_repo(tmp_path):
    predictions = tmp_path / "predictions.json"
    predictions.write_text("[]", encoding="utf-8")

    result = ReplayRepoAgentExecutor(predictions).run(
        _context("abcdef1"), tmp_path, tmp_path / "output"
    )

    assert result.exit_code == 2
    assert "No replay prediction" in result.output_tail


def test_replay_store_accepts_swebench_jsonl(tmp_path):
    predictions = tmp_path / "all_preds.jsonl"
    predictions.write_text(
        json.dumps({"instance_id": "owner__repo-1", "model_patch": ""}) + "\n",
        encoding="utf-8",
    )

    result = ReplayRepoAgentExecutor(predictions).run(
        _context("abcdef1"), tmp_path, tmp_path / "output"
    )

    assert result.exit_code == 0


def _context(base_sha: str) -> RepairTaskContext:
    task_id = uuid4()
    return RepairTaskContext(
        task_id=task_id, issue_id=uuid4(), repository_id=uuid4(),
        repository_provider="SWE_BENCH", repository_external_id="owner/repo",
        mirror_key="owner__repo", github_repository_id=None,
        repository_full_name="owner/repo", clone_url=None, default_branch="main",
        github_issue_number=None, issue_external_id="owner__repo-1",
        benchmark_dataset_split="lite", issue_title="SWE-bench fixture",
        issue_body="Change old to new.", base_sha=base_sha, branch_name=f"run-{task_id}",
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
