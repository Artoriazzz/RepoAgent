from __future__ import annotations

import subprocess
from pathlib import Path

from repo_agent.worker.models import PatchArtifacts, RepairTaskContext


class WorkspaceError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, mirror_root: Path, worktree_root: Path) -> None:
        self._mirror_root = mirror_root
        self._worktree_root = worktree_root

    def prepare(self, context: RepairTaskContext) -> Path:
        mirror = self._mirror_root / str(context.repository_id)
        if not mirror.is_dir():
            raise WorkspaceError(
                f"Repository mirror is missing: {mirror}. "
                "Sync the authorized repository before consuming repair tasks."
            )

        worktree = self._worktree_root / str(context.task_id)
        if worktree.exists():
            raise WorkspaceError(f"Worktree already exists: {worktree}")

        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run(["git", "-C", str(mirror), "worktree", "add", "--detach", str(worktree), context.base_sha])
        self._run(["git", "-C", str(worktree), "switch", "-c", context.branch_name])
        return worktree

    def collect_patch(self, worktree: Path) -> PatchArtifacts:
        patch = self._run(
            ["git", "-C", str(worktree), "diff", "--binary", "--", ".", ":!.env*"],
            allow_failure=False,
        ).stdout
        changed_files = tuple(
            line
            for line in self._run(
                ["git", "-C", str(worktree), "diff", "--name-only", "--", ".", ":!.env*"],
                allow_failure=False,
            ).stdout.splitlines()
            if line
        )
        return PatchArtifacts(patch=patch, changed_files=changed_files)

    @staticmethod
    def _run(command: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
        if result.returncode != 0 and not allow_failure:
            output = result.stdout[-2_000:]
            raise WorkspaceError(f"Git command failed: {' '.join(command)}\n{output}")
        return result
