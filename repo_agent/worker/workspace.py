from __future__ import annotations

import re
import subprocess
from pathlib import Path

from repo_agent.worker.models import PatchArtifacts, RepairTaskContext


class WorkspaceError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(
        self,
        mirror_root: Path,
        worktree_root: Path,
        swebench_mirror_root: Path | None = None,
    ) -> None:
        self._mirror_root = mirror_root
        self._worktree_root = worktree_root
        self._swebench_mirror_root = swebench_mirror_root or mirror_root

    def prepare(self, context: RepairTaskContext) -> Path:
        mirror = self._resolve_mirror(context)
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

    def prepare_or_reuse(self, context: RepairTaskContext) -> Path:
        """恢复同一 Coordinator 阶段时复用已创建的隔离 worktree。"""
        worktree = self._worktree_root / str(context.task_id)
        if worktree.is_dir():
            probe = self._run(
                ["git", "-C", str(worktree), "rev-parse", "--is-inside-work-tree"],
                allow_failure=True,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "true":
                return worktree
            raise WorkspaceError(f"Existing worktree is invalid: {worktree}")
        return self.prepare(context)

    def _resolve_mirror(self, context: RepairTaskContext) -> Path:
        if context.repository_provider == "GITHUB":
            return self._mirror_root / str(context.repository_id)
        if context.repository_provider != "SWE_BENCH":
            raise WorkspaceError(f"Unsupported repository provider: {context.repository_provider}")
        if not context.mirror_key or not _valid_mirror_key(context.mirror_key):
            raise WorkspaceError("Invalid SWE-bench mirror key")
        mirror = self._swebench_mirror_root / context.mirror_key
        if mirror.is_dir():
            return mirror
        return mirror.with_suffix(".git")

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


def _valid_mirror_key(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", value) is not None
