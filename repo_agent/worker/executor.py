from __future__ import annotations

import subprocess
from pathlib import Path

from repo_agent.worker.models import AgentRunResult, RepairTaskContext


class RepoAgentExecutor:
    def __init__(self, executable: str, model: str, max_steps: int, timeout_seconds: int) -> None:
        self._executable = executable
        self._model = model
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds

    def run(self, context: RepairTaskContext, worktree: Path, output_dir: Path) -> AgentRunResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self._executable,
            "run",
            "--repo",
            str(worktree),
            "--task",
            self._build_task(context),
            "--model",
            self._model,
            "--coordinator",
            "--max-steps",
            str(self._max_steps),
            "--output-dir",
            str(output_dir),
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or "").strip()
            raise RuntimeError(f"RepoAgent timed out after {self._timeout_seconds}s\n{output[-2_000:]}") from error

        output_dir.joinpath("repo_agent.log").write_text(result.stdout, encoding="utf-8")
        return AgentRunResult(exit_code=result.returncode, output_tail=result.stdout[-2_000:])

    @staticmethod
    def _build_task(context: RepairTaskContext) -> str:
        return (
            f"修复 GitHub Issue #{context.github_issue_number}：{context.issue_title}\n\n"
            f"仓库：{context.repository_full_name}\n"
            f"基线提交：{context.base_sha}\n\n"
            f"Issue 描述：\n{context.issue_body}\n\n"
            "请在当前 worktree 中完成最小修复，保留现有风格，并运行可用的相关验证。"
        )
