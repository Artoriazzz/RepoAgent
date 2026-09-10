from __future__ import annotations

from pathlib import Path
from typing import Protocol

from repo_agent.worker.backend_client import BackendTransportError
from repo_agent.worker.models import AgentRunResult, PatchArtifacts, RepairTaskContext, RepairTaskMessage


class WorkerBackend(Protocol):
    def get_context(self, task_id): ...

    def update(self, task_id, status: str, **kwargs): ...


class WorkerWorkspace(Protocol):
    def prepare(self, context: RepairTaskContext) -> Path: ...

    def collect_patch(self, worktree: Path) -> PatchArtifacts: ...


class WorkerExecutor(Protocol):
    def run(self, context: RepairTaskContext, worktree: Path, output_dir: Path) -> AgentRunResult: ...


class RepairWorkerService:
    def __init__(
        self,
        backend: WorkerBackend,
        workspace: WorkerWorkspace,
        executor: WorkerExecutor,
        output_root: Path,
    ) -> None:
        self._backend = backend
        self._workspace = workspace
        self._executor = executor
        self._output_root = output_root

    def process(self, message: RepairTaskMessage) -> bool:
        try:
            self._backend.update(message.task_id, "ANALYZING")
            context = self._backend.get_context(message.task_id)
            if context.task_id != message.task_id or context.issue_id != message.issue_id:
                raise RuntimeError("Backend task context does not match RabbitMQ message")

            worktree = self._workspace.prepare(context)
            output_dir = self._output_root / str(message.task_id)
            self._backend.update(message.task_id, "FIXING")
            run_result = self._executor.run(context, worktree, output_dir)
            if run_result.exit_code != 0:
                raise RuntimeError(f"RepoAgent exited with code {run_result.exit_code}: {run_result.output_tail}")

            artifacts = self._workspace.collect_patch(worktree)
            self._backend.update(message.task_id, "VALIDATING")
            if not artifacts.patch.strip():
                raise RuntimeError("RepoAgent completed without a patch")

            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("model.patch").write_text(artifacts.patch, encoding="utf-8")
            self._backend.update(
                message.task_id,
                "PATCH_READY",
                patch_summary=self._patch_summary(artifacts),
                validation_summary=self._validation_summary(run_result),
            )
            return True
        except BackendTransportError:
            raise
        except Exception as error:
            self._report_failure(message, error)
            return False

    def _report_failure(self, message: RepairTaskMessage, error: Exception) -> None:
        try:
            self._backend.update(
                message.task_id,
                "FAILED",
                validation_summary=str(error)[-4_000:],
            )
        except BackendTransportError:
            raise
        except Exception:
            return

    @staticmethod
    def _patch_summary(artifacts: PatchArtifacts) -> str:
        files = ", ".join(artifacts.changed_files) or "unknown files"
        return f"Local patch generated for: {files}. Artifact: model.patch"

    @staticmethod
    def _validation_summary(result: AgentRunResult) -> str:
        return (
            "RepoAgent process completed successfully. The worker did not run an independent "
            "repository test command; inspect the agent trajectory and require CI before publishing.\n"
            f"Agent output tail:\n{result.output_tail[-1_500:]}"
        )
