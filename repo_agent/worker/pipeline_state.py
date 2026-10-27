from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from repo_agent.worker.evaluator import BenchmarkEvaluation
from repo_agent.worker.models import AgentRunResult, PatchArtifacts, RepairTaskContext


class PipelineStateError(RuntimeError):
    """阶段间状态缺失或损坏；由队列重试或任务恢复流程处理。"""


@dataclass(frozen=True)
class RepairPipelineState:
    context: RepairTaskContext
    worktree: Path
    output_dir: Path
    artifacts: PatchArtifacts | None = None
    run_result: AgentRunResult | None = None
    evaluation: BenchmarkEvaluation | None = None
    failure_feedback: dict | None = None

    def with_context(self, context: RepairTaskContext) -> "RepairPipelineState":
        return RepairPipelineState(
            context, self.worktree, self.output_dir, self.artifacts, self.run_result,
            self.evaluation, self.failure_feedback,
        )

    def with_artifacts(self, artifacts: PatchArtifacts, run_result: AgentRunResult) -> "RepairPipelineState":
        return RepairPipelineState(
            self.context, self.worktree, self.output_dir, artifacts, run_result,
            self.evaluation, self.failure_feedback,
        )

    def with_evaluation(self, evaluation: BenchmarkEvaluation) -> "RepairPipelineState":
        return RepairPipelineState(
            self.context, self.worktree, self.output_dir, self.artifacts, self.run_result,
            evaluation, self.failure_feedback,
        )

    def with_failure_feedback(self, feedback: dict | None) -> "RepairPipelineState":
        return RepairPipelineState(
            self.context, self.worktree, self.output_dir, self.artifacts, self.run_result,
            self.evaluation, feedback,
        )


class RepairPipelineStateStore:
    """将阶段间数据放在任务专属目录，消息中永远不传源码、patch 或对话历史。"""

    FILE_NAME = "pipeline_state.json"

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root

    def output_dir(self, task_id: UUID) -> Path:
        return self._output_root / str(task_id)

    def save(self, state: RepairPipelineState) -> None:
        state.output_dir.mkdir(parents=True, exist_ok=True)
        target = state.output_dir / self.FILE_NAME
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._payload(state), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def load(self, task_id: UUID) -> RepairPipelineState:
        target = self.output_dir(task_id) / self.FILE_NAME
        if not target.is_file():
            raise PipelineStateError(f"Pipeline state is missing for task {task_id}")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            context = RepairTaskContext.from_payload(payload["context"])
            if context.task_id != task_id:
                raise PipelineStateError("Pipeline state belongs to another task")
            return RepairPipelineState(
                context=context,
                worktree=Path(payload["worktree"]),
                output_dir=Path(payload["outputDir"]),
                artifacts=self._artifacts(payload.get("artifacts")),
                run_result=self._run_result(payload.get("runResult")),
                evaluation=self._evaluation(payload.get("evaluation")),
                failure_feedback=payload.get("failureFeedback") if isinstance(payload.get("failureFeedback"), dict) else None,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            if isinstance(error, PipelineStateError):
                raise
            raise PipelineStateError(f"Pipeline state is invalid for task {task_id}: {error}") from error

    @staticmethod
    def _payload(state: RepairPipelineState) -> dict:
        return {
            "schemaVersion": 1,
            "context": state.context.to_payload(),
            "worktree": str(state.worktree),
            "outputDir": str(state.output_dir),
            "artifacts": None if state.artifacts is None else {
                "patch": state.artifacts.patch,
                "changedFiles": list(state.artifacts.changed_files),
            },
            "runResult": None if state.run_result is None else {
                "exitCode": state.run_result.exit_code,
                "outputTail": state.run_result.output_tail,
                "durationMs": state.run_result.duration_ms,
                "promptTokens": state.run_result.prompt_tokens,
                "completionTokens": state.run_result.completion_tokens,
                "totalTokens": state.run_result.total_tokens,
            },
            "evaluation": None if state.evaluation is None else {
                "status": state.evaluation.status,
                "failToPassPassed": state.evaluation.fail_to_pass_passed,
                "failToPassFailed": state.evaluation.fail_to_pass_failed,
                "passToPassPassed": state.evaluation.pass_to_pass_passed,
                "passToPassFailed": state.evaluation.pass_to_pass_failed,
                "summary": state.evaluation.summary,
                "harnessLogPath": state.evaluation.harness_log_path,
            },
            "failureFeedback": state.failure_feedback,
        }

    @staticmethod
    def _artifacts(payload: object) -> PatchArtifacts | None:
        if not isinstance(payload, dict):
            return None
        return PatchArtifacts(
            str(payload.get("patch") or ""),
            tuple(str(item) for item in (payload.get("changedFiles") or [])),
        )

    @staticmethod
    def _run_result(payload: object) -> AgentRunResult | None:
        if not isinstance(payload, dict):
            return None
        return AgentRunResult(
            int(payload.get("exitCode") or 0),
            str(payload.get("outputTail") or ""),
            int(payload.get("durationMs") or 0),
            int(payload.get("promptTokens") or 0),
            int(payload.get("completionTokens") or 0),
            int(payload.get("totalTokens") or 0),
        )

    @staticmethod
    def _evaluation(payload: object) -> BenchmarkEvaluation | None:
        if not isinstance(payload, dict):
            return None
        return BenchmarkEvaluation(
            status=str(payload.get("status") or "NOT_RUN"),
            fail_to_pass_passed=int(payload.get("failToPassPassed") or 0),
            fail_to_pass_failed=int(payload.get("failToPassFailed") or 0),
            pass_to_pass_passed=int(payload.get("passToPassPassed") or 0),
            pass_to_pass_failed=int(payload.get("passToPassFailed") or 0),
            summary=str(payload.get("summary") or ""),
            harness_log_path=str(payload.get("harnessLogPath") or ""),
        )
