from __future__ import annotations

from pathlib import Path
from typing import Protocol
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import re
import socket
import threading
import time
from uuid import UUID, uuid4

from repo_agent.worker.backend_client import (
    BackendRequestError,
    BackendTaskUnavailableError,
    BackendTransportError,
)
from repo_agent.worker.models import AgentRunResult, PatchArtifacts, RepairTaskContext, RepairTaskMessage
from repo_agent.worker.evaluator import BenchmarkEvaluation
from repo_agent.worker.models import RepairPipelineStage
from repo_agent.worker.memory_query import (
    build_memory_query,
)
from repo_agent.worker.pipeline_state import (
    PipelineStateError,
    RepairPipelineState,
    RepairPipelineStateStore,
)
from repo_agent.worker.executor import AgentTimeoutError, TaskCancelledError
from repo_agent.worker.workspace import WorkspaceError
from repo_agent.core.experience import parse_failure_feedback


class WorkerBackend(Protocol):
    def get_context(self, task_id): ...

    def update(self, task_id, status: str, **kwargs): ...

    def update_evaluation(self, task_id, status: str, **kwargs): ...

    def claim(self, task_id, worker_id: str, lease_seconds: int = 90, stage=None): ...

    def renew_lease(self, task_id, worker_id: str, lease_token, lease_seconds: int = 90): ...

    def get_control(self, task_id, worker_id: str, lease_token): ...

    def report_event(self, task_id, stage: str, message: str, worker_id: str, lease_token, metadata_json=None): ...

    def heartbeat(self, worker_id: str, status: str, active_task_id=None, metadata_json=None, **kwargs): ...

    def register_worker(self, worker_id: str, instance_id: str, role: str): ...

    def unregister_worker(self, worker_id: str, instance_id: str): ...

    def search_memories(self, task_id, query: str, **kwargs): ...

    def record_memory_observation(self, task_id, **kwargs): ...

    def record_memory_feedback(self, memory_id, task_id, revision: int, **kwargs): ...

    def handoff_stage(self, task_id, current_stage, next_stage, worker_id, lease_token, message: str): ...

    def complete_pipeline(self, task_id, **kwargs): ...


class WorkerWorkspace(Protocol):
    def prepare(self, context: RepairTaskContext) -> Path: ...

    def collect_patch(self, worktree: Path) -> PatchArtifacts: ...


class WorkerExecutor(Protocol):
    def run(
        self,
        context: RepairTaskContext,
        worktree: Path,
        output_dir: Path,
        should_cancel=None,
    ) -> AgentRunResult: ...

    def run_corrective(
        self,
        context: RepairTaskContext,
        worktree: Path,
        output_dir: Path,
        artifacts: PatchArtifacts,
        feedback: dict,
        should_cancel=None,
    ) -> AgentRunResult: ...


class WorkerEvaluator(Protocol):
    def evaluate(
        self, context: RepairTaskContext, patch: str, output_dir: Path, should_cancel=None
    ) -> BenchmarkEvaluation: ...


class WorkerMemoryCurator(Protocol):
    def curate(self, context, artifacts, evaluation, observation_id): ...


class RepairWorkerService:
    _DELAYED_STAGES = frozenset({
        "ANALYZING",
        "FAILURE_REVIEW",
        "COORDINATOR",
        "IMPLEMENT",
        "CORRECTIVE_IMPLEMENT",
        "PUBLIC_VALIDATION",
        "VALIDATING",
        "RETRYING",
        "EXPERIENCE_MEMORY",
        "PATCH_READY",
        "FINAL_EVALUATION",
    })

    def __init__(
        self,
        backend: WorkerBackend,
        workspace: WorkerWorkspace,
        executor: WorkerExecutor,
        output_root: Path,
        evaluator: WorkerEvaluator | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 90,
        model_name: str | None = None,
        memory_mode: str = "learn",
        memory_recall_limit: int = 4,
        memory_curator: WorkerMemoryCurator | None = None,
        stage_delay_seconds: float = 0,
        worker_role: str = "repair",
    ) -> None:
        if memory_mode not in {"off", "recall", "learn", "evolve"}:
            raise ValueError("memory_mode must be off, recall, learn, or evolve")
        self._backend = backend
        self._workspace = workspace
        self._executor = executor
        self._output_root = output_root
        self._evaluator = evaluator
        self._worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._lease_seconds = lease_seconds
        self._model_name = model_name
        self._memory_mode = memory_mode
        self._memory_recall_limit = max(1, min(memory_recall_limit, 10))
        self._memory_curator = memory_curator
        self._stage_delay_seconds = max(0, stage_delay_seconds)
        self._worker_role = worker_role
        self._pipeline_state = RepairPipelineStateStore(output_root)
        self._activity_lock = threading.Lock()
        self._activity_status = "IDLE"
        self._active_task_id = None
        self._presence_instance_id = uuid4().hex
        self._presence_started_at = datetime.now(timezone.utc).isoformat()
        self._heartbeat_sequence = 0
        self._presence_registered = False
        self._presence_superseded = False

    def process_stage(self, message: RepairTaskMessage) -> bool:
        """Consume exactly one durable pipeline stage, then hand the task to the next queue."""
        lease_token: UUID | None = None
        run_result: AgentRunResult | None = None
        try:
            try:
                lease_token = self._backend.claim(
                    message.task_id,
                    self._worker_id,
                    self._lease_seconds,
                    stage=message.stage,
                    stage_sequence=message.stage_sequence,
                )
            except TypeError as error:
                # 允许滚动升级期间仍在使用旧 claim 协议的测试替身或 Worker SDK。
                if "stage_sequence" not in str(error):
                    raise
                lease_token = self._backend.claim(
                    message.task_id, self._worker_id, self._lease_seconds, stage=message.stage
                )
            self._set_activity("BUSY", message.task_id)
            self.send_heartbeat()
            if message.stage == RepairPipelineStage.COORDINATOR:
                self._run_pipeline_coordinator(message, lease_token)
            elif message.stage == RepairPipelineStage.IMPLEMENT:
                run_result = self._run_pipeline_implement(message, lease_token, corrective=False)
            elif message.stage == RepairPipelineStage.VALIDATION:
                self._run_pipeline_validation(message, lease_token)
            elif message.stage == RepairPipelineStage.FAILURE_REVIEW:
                self._run_pipeline_failure_review(message, lease_token)
            elif message.stage == RepairPipelineStage.CORRECTIVE_IMPLEMENT:
                run_result = self._run_pipeline_implement(message, lease_token, corrective=True)
            elif message.stage == RepairPipelineStage.EXPERIENCE_MEMORY:
                self._run_pipeline_memory(message, lease_token)
            else:
                raise RuntimeError(f"Unsupported repair pipeline stage: {message.stage}")
            return True
        except BackendTransportError:
            raise
        except BackendTaskUnavailableError:
            return False
        except TaskCancelledError as error:
            if lease_token is not None:
                self._update(
                    message.task_id,
                    lease_token,
                    "CANCELLED",
                    validation_summary=str(error)[-4_000:],
                    run_result=run_result,
                )
            return False
        except Exception as error:
            self._report_failure(message, error, lease_token, run_result)
            return False
        finally:
            self._set_activity("IDLE", None)
            self.send_heartbeat()

    def _run_pipeline_coordinator(self, message: RepairTaskMessage, lease_token: UUID) -> None:
        self._event(
            message.task_id,
            lease_token,
            "COORDINATOR",
            "Coordinator is loading the task, optionally recalling repair experience, and preparing an isolated worktree",
        )
        context = self._backend.get_context(message.task_id)
        if context.task_id != message.task_id or context.issue_id != message.issue_id:
            raise RuntimeError("Backend task context does not match RabbitMQ message")
        context = self._recall_memories(context, lease_token, reason="Coordinator planning")
        prepare_or_reuse = getattr(self._workspace, "prepare_or_reuse", self._workspace.prepare)
        worktree = prepare_or_reuse(context)
        output_dir = self._pipeline_state.output_dir(message.task_id)
        self._pipeline_state.save(RepairPipelineState(context, worktree, output_dir))
        self._handoff(
            message,
            lease_token,
            RepairPipelineStage.IMPLEMENT,
            "Coordinator finished the repair brief; Implement Worker may now create the patch",
        )

    def _run_pipeline_implement(
        self,
        message: RepairTaskMessage,
        lease_token: UUID,
        *,
        corrective: bool,
    ) -> AgentRunResult:
        state = self._pipeline_state.load(message.task_id)
        context = state.context
        if corrective:
            context = replace(
                context,
                attempt_number=max(2, context.attempt_number),
                previous_failure_summary=self._failure_summary(state),
            )
        stage = "CORRECTIVE_IMPLEMENT" if corrective else "IMPLEMENT"
        detail = (
            "Corrective Implement is applying the single evidence-backed retry"
            if corrective
            else "Implement Worker is creating the initial patch"
        )
        self._event(message.task_id, lease_token, stage, detail)
        cancellation_check = self._cancellation_check(message.task_id, lease_token)
        if cancellation_check():
            raise TaskCancelledError("Repair task was cancelled before Implement started")
        set_stage_callback = getattr(self._executor, "set_stage_callback", None)
        if callable(set_stage_callback):
            # The outer queue owns the visible phase. Inner Agent events remain in its own trajectory.
            set_stage_callback(None)
        corrective_run = getattr(self._executor, "run_corrective", None)
        if corrective and callable(corrective_run):
            if state.artifacts is None or state.failure_feedback is None:
                raise PipelineStateError("Corrective Implement stage has incomplete failure state")
            run_result = corrective_run(
                context,
                state.worktree,
                state.output_dir,
                state.artifacts,
                state.failure_feedback,
                should_cancel=cancellation_check,
            )
        else:
            run_result = self._executor.run(
                context, state.worktree, state.output_dir, should_cancel=cancellation_check
            )
        if run_result.exit_code != 0:
            raise AgentProcessError(
                f"RepoAgent exited with code {run_result.exit_code}: {run_result.output_tail}"
            )
        artifacts = self._workspace.collect_patch(state.worktree)
        if not artifacts.patch.strip():
            raise EmptyPatchError("RepoAgent completed without a patch")
        state.output_dir.mkdir(parents=True, exist_ok=True)
        patch_path = state.output_dir / "model.patch"
        patch_path.write_text(artifacts.patch, encoding="utf-8")
        next_state = state.with_context(context).with_artifacts(artifacts, run_result)
        self._pipeline_state.save(next_state)
        self._update(message.task_id, lease_token, "FIXING", run_result=run_result)
        self._handoff(
            message,
            lease_token,
            RepairPipelineStage.VALIDATION,
            "Patch is ready for independent validation",
        )
        return run_result

    def _run_pipeline_validation(self, message: RepairTaskMessage, lease_token: UUID) -> None:
        state = self._pipeline_state.load(message.task_id)
        if state.artifacts is None:
            raise PipelineStateError("Validation stage has no patch artifacts")
        self._event(
            message.task_id,
            lease_token,
            "VALIDATING",
            "Validation Worker is determining the repair result",
        )
        cancellation_check = self._cancellation_check(message.task_id, lease_token)
        evaluation = BenchmarkEvaluation(
            status="NOT_RUN",
            summary="No independent benchmark evaluator is configured for this repository task.",
        )
        if state.context.repository_provider == "SWE_BENCH" and self._evaluator is not None:
            self._backend.update_evaluation(
                message.task_id,
                "RUNNING",
                worker_id=self._worker_id,
                lease_token=lease_token,
            )
            evaluation = self._evaluator.evaluate(
                state.context,
                state.artifacts.patch,
                state.output_dir,
                should_cancel=cancellation_check,
            )
        state = state.with_evaluation(evaluation)
        self._pipeline_state.save(state)
        if state.context.attempt_number > 1:
            next_stage = RepairPipelineStage.EXPERIENCE_MEMORY
            next_message = (
                "Final Harness completed after Corrective Implement; routing the outcome "
                "to ExperienceCurator without another repair loop"
            )
        elif evaluation.status == "UNRESOLVED":
            next_stage = RepairPipelineStage.FAILURE_REVIEW
            next_message = "Validation failed; routing evidence to FailureReview before the one allowed retry"
        else:
            next_stage = RepairPipelineStage.EXPERIENCE_MEMORY
            next_message = "Validation completed; routing the final outcome to experience memory"
        self._handoff(message, lease_token, next_stage, next_message)

    def _run_pipeline_failure_review(self, message: RepairTaskMessage, lease_token: UUID) -> None:
        state = self._pipeline_state.load(message.task_id)
        if state.artifacts is None or state.evaluation is None:
            raise PipelineStateError("FailureReview stage has incomplete validation state")
        self._event(
            message.task_id,
            lease_token,
            "RETRYING",
            "FailureReview is attributing the validation failure before deciding whether a correction is safe",
        )
        feedback_payload: dict | None = None
        review = getattr(self._executor, "review_failure", None)
        if callable(review):
            review_result = review(
                state.context,
                state.worktree,
                state.output_dir,
                state.artifacts,
                state.evaluation,
                should_cancel=self._cancellation_check(message.task_id, lease_token),
            )
            if review_result.exit_code == 0:
                feedback_file = state.output_dir / "failure_review" / "failure_feedback.json"
                if feedback_file.is_file():
                    raw_feedback = feedback_file.read_text(encoding="utf-8", errors="replace")
                    parsed_feedback = parse_failure_feedback(raw_feedback)
                    if parsed_feedback is not None:
                        feedback_payload = parsed_feedback.to_dict()
        if feedback_payload is None:
            # Historical replay has no fresh model response. Preserve the failed result without inventing a retry.
            feedback_payload = {
                "correction_route": "none",
                "correction_readiness": "insufficient_evidence",
                "required_correction": "No independently actionable correction evidence was produced.",
            }
        state = state.with_failure_feedback(feedback_payload)
        self._pipeline_state.save(state)
        if feedback_payload.get("correction_route") == "corrective_implement":
            next_stage = RepairPipelineStage.CORRECTIVE_IMPLEMENT
            next_message = "FailureReview authorized one scoped Corrective Implement pass"
        else:
            next_stage = RepairPipelineStage.EXPERIENCE_MEMORY
            next_message = "FailureReview did not authorize a safe correction; preserving the final failed outcome"
        self._handoff(message, lease_token, next_stage, next_message)

    def _run_pipeline_memory(self, message: RepairTaskMessage, lease_token: UUID) -> None:
        state = self._pipeline_state.load(message.task_id)
        if state.artifacts is None:
            raise PipelineStateError("Experience memory stage has no patch artifacts")
        evaluation = state.evaluation or BenchmarkEvaluation(
            status="NOT_RUN", summary="No independent evaluation result was produced."
        )
        run_result = state.run_result or AgentRunResult(0, "")
        self._event(
            message.task_id,
            lease_token,
            "EXPERIENCE_MEMORY",
            "ExperienceCurator is processing the final independently validated outcome",
        )
        self._finalize_memory(state.context, state.artifacts, run_result, evaluation, lease_token)
        patch_path = state.output_dir / "model.patch"
        self._backend.complete_pipeline(
            message.task_id,
            worker_id=self._worker_id,
            lease_token=lease_token,
            patch_summary=self._patch_summary(state.artifacts),
            patch_artifact_path=str(patch_path) if patch_path.is_file() else None,
            validation_summary=self._validation_summary(run_result),
            evaluation_status=evaluation.status,
            fail_to_pass_passed=evaluation.fail_to_pass_passed,
            fail_to_pass_failed=evaluation.fail_to_pass_failed,
            pass_to_pass_passed=evaluation.pass_to_pass_passed,
            pass_to_pass_failed=evaluation.pass_to_pass_failed,
            evaluation_summary=evaluation.summary,
            harness_log_path=evaluation.harness_log_path,
            model_name=self._model_name,
            duration_ms=run_result.duration_ms,
            prompt_tokens=run_result.prompt_tokens,
            completion_tokens=run_result.completion_tokens,
            total_tokens=run_result.total_tokens,
        )

    def _handoff(
        self,
        message: RepairTaskMessage,
        lease_token: UUID,
        next_stage: RepairPipelineStage,
        detail: str,
    ) -> None:
        self._backend.handoff_stage(
            message.task_id,
            message.stage,
            next_stage,
            self._worker_id,
            lease_token,
            detail,
        )

    @staticmethod
    def _failure_summary(state: RepairPipelineState) -> str:
        payload = state.failure_feedback or {}
        if payload:
            return json.dumps(payload, ensure_ascii=False)[-8_000:]
        if state.evaluation is not None:
            return state.evaluation.summary[-8_000:]
        return "The initial repair failed without structured feedback."

    def process(self, message: RepairTaskMessage) -> bool:
        lease_token: UUID | None = None
        run_result: AgentRunResult | None = None
        try:
            lease_token = self._backend.claim(message.task_id, self._worker_id, self._lease_seconds)
            self._set_activity("BUSY", message.task_id)
            self.send_heartbeat()
            self._update(message.task_id, lease_token, "ANALYZING")
            self._event(message.task_id, lease_token, "ANALYZING", "Loading task context")
            context = self._backend.get_context(message.task_id)
            if context.task_id != message.task_id or context.issue_id != message.issue_id:
                raise RuntimeError("Backend task context does not match RabbitMQ message")
            context = self._recall_memories(context, lease_token)

            if context.attempt_number > 1:
                self._event(
                    message.task_id, lease_token, "FAILURE_REVIEW",
                    "Analyzing the previous failure before the single Mode 3 retry",
                )

            worktree = self._workspace.prepare(context)
            cancellation_check = self._cancellation_check(message.task_id, lease_token)
            if cancellation_check():
                raise TaskCancelledError("Repair task was cancelled before RepoAgent started")
            output_dir = self._output_root / str(message.task_id)
            self._update(message.task_id, lease_token, "FIXING")
            self._event(
                message.task_id, lease_token, "COORDINATOR",
                "Coordinator started the repair workflow",
            )
            set_stage_callback = getattr(self._executor, "set_stage_callback", None)
            if callable(set_stage_callback):
                def report_stage(stage, detail):
                    if context.attempt_number > 1 and stage == "IMPLEMENT":
                        stage = "CORRECTIVE_IMPLEMENT"
                        detail = "Corrective Implement is applying the single allowed retry"
                    self._event(message.task_id, lease_token, stage, detail)

                set_stage_callback(report_stage)
            run_result = self._executor.run(
                context, worktree, output_dir, should_cancel=cancellation_check
            )
            if run_result.exit_code != 0:
                raise AgentProcessError(
                    f"RepoAgent exited with code {run_result.exit_code}: {run_result.output_tail}"
                )

            artifacts = self._workspace.collect_patch(worktree)
            self._update(message.task_id, lease_token, "VALIDATING", run_result=run_result)
            self._event(message.task_id, lease_token, "VALIDATING", "Collecting patch artifacts")
            if not artifacts.patch.strip():
                raise EmptyPatchError("RepoAgent completed without a patch")

            output_dir.mkdir(parents=True, exist_ok=True)
            patch_path = output_dir / "model.patch"
            patch_path.write_text(artifacts.patch, encoding="utf-8")
            self._update(
                message.task_id, lease_token,
                "PATCH_READY",
                patch_summary=self._patch_summary(artifacts),
                patch_artifact_path=str(patch_path),
                validation_summary=self._validation_summary(run_result),
                run_result=run_result,
            )
            evaluation = None
            if context.repository_provider == "SWE_BENCH" and self._evaluator is not None:
                evaluation = self._evaluate(
                    context, artifacts.patch, output_dir, lease_token,
                    self._cancellation_check(message.task_id, lease_token),
                )
                if evaluation is not None and evaluation.status == "UNRESOLVED":
                    self._event(
                        message.task_id,
                        lease_token,
                        "RETRYING",
                        "Failure Review is analyzing the failed validation before the single retry",
                    )
                if evaluation is not None:
                    self._event(
                        message.task_id,
                        lease_token,
                        "EXPERIENCE_MEMORY",
                        "Recording the repair outcome as reusable experience",
                    )
            self._finalize_memory(context, artifacts, run_result, evaluation, lease_token)
            self._event(
                message.task_id,
                lease_token,
                "PATCH_READY",
                "Patch and repair experience are ready",
            )
            if evaluation is not None:
                self._record_evaluation(context.task_id, lease_token, evaluation)
            return True
        except BackendTransportError:
            raise
        except BackendTaskUnavailableError:
            return False
        except TaskCancelledError as error:
            if lease_token is not None:
                self._update(
                    message.task_id, lease_token, "CANCELLED",
                    validation_summary=str(error)[-4_000:], run_result=run_result,
                )
            return False
        except Exception as error:
            self._report_failure(message, error, lease_token, run_result)
            return False
        finally:
            self._set_activity("IDLE", None)
            self.send_heartbeat()

    def _evaluate(
        self,
        context: RepairTaskContext,
        patch: str,
        output_dir: Path,
        lease_token: UUID,
        cancellation_check,
    ) -> BenchmarkEvaluation | None:
        lease = {"worker_id": self._worker_id, "lease_token": lease_token}
        self._event(
            context.task_id, lease_token, "FINAL_EVALUATION",
            "Determining the final repair result",
        )
        self._backend.update_evaluation(context.task_id, "RUNNING", **lease)
        try:
            result = self._evaluator.evaluate(
                context, patch, output_dir, should_cancel=cancellation_check
            )
        except TaskCancelledError:
            raise
        except Exception as error:
            self._backend.update_evaluation(
                context.task_id,
                "HARNESS_ERROR",
                summary=str(error)[-4_000:],
                **lease,
            )
            return None
        return result

    def _record_evaluation(
        self,
        task_id: UUID,
        lease_token: UUID,
        result: BenchmarkEvaluation,
    ) -> None:
        self._backend.update_evaluation(
            task_id,
            result.status,
            fail_to_pass_passed=result.fail_to_pass_passed,
            fail_to_pass_failed=result.fail_to_pass_failed,
            pass_to_pass_passed=result.pass_to_pass_passed,
            pass_to_pass_failed=result.pass_to_pass_failed,
            summary=result.summary,
            harness_log_path=result.harness_log_path,
            worker_id=self._worker_id,
            lease_token=lease_token,
        )

    def _recall_memories(
        self, context: RepairTaskContext, lease_token: UUID, *, reason: str = ""
    ) -> RepairTaskContext:
        # 长期经验只能在首次 Coordinator 阶段召回。纠正回合和所有子 Agent
        # 都只能消费 Coordinator 已验证后形成的契约，不能再次查询历史记忆。
        if self._memory_mode not in {"recall", "evolve"} or context.attempt_number > 1:
            return context
        try:
            memory_query = build_memory_query(context)
            memories = self._backend.search_memories(
                context.task_id,
                memory_query.fallback_text,
                query_features=memory_query.features,
                limit=self._memory_recall_limit,
            )
        except Exception as error:
            self._event(
                context.task_id,
                lease_token,
                "MEMORY_RECALL_SKIPPED",
                "Memory recall was unavailable; continuing without memory",
                {"error": str(error)[-1_000:]},
            )
            return context
        self._event(
            context.task_id,
            lease_token,
            "MEMORY_RECALLED",
            f"Recalled {len(memories)} global/repository experiences"
            + (f" ({reason})" if reason else ""),
            {
                "memories": [
                    {"id": str(memory.memory_id), "revision": memory.revision, "score": memory.score}
                    for memory in memories
                ]
            },
        )
        return replace(context, recalled_memories=memories)

    def _finalize_memory(
        self,
        context: RepairTaskContext,
        artifacts: PatchArtifacts,
        run_result: AgentRunResult,
        evaluation: BenchmarkEvaluation | None,
        lease_token: UUID,
    ) -> None:
        if evaluation is None:
            return
        used_ids = self._memory_usage_markers(run_result.output_tail)
        successful = evaluation.status == "RESOLVED"
        for memory in context.recalled_memories:
            used = (memory.memory_id, memory.revision) in used_ids
            try:
                self._backend.record_memory_feedback(
                    memory.memory_id,
                    context.task_id,
                    memory.revision,
                    used=used,
                    successful=successful and used,
                )
            except Exception:
                # 反馈是质量信号，不应反向改变已经完成的修复任务状态。
                continue
        if self._memory_mode not in {"learn", "evolve"} or not successful:
            return
        try:
            observation = self._backend.record_memory_observation(
                context.task_id,
                idempotency_key=f"repair-result:{context.task_id}:resolved",
                outcome="RESOLVED",
                problem_signature=f"{context.issue_title}\n{context.issue_body}"[:8_000],
                patch_summary=self._patch_summary(artifacts),
                validation_summary=evaluation.summary[-8_000:],
                evidence={
                    "changedFiles": list(artifacts.changed_files),
                    "f2pPassed": evaluation.fail_to_pass_passed,
                    "f2pFailed": evaluation.fail_to_pass_failed,
                    "p2pPassed": evaluation.pass_to_pass_passed,
                    "p2pFailed": evaluation.pass_to_pass_failed,
                    "patchArtifact": "model.patch",
                },
            )
            self._event(
                context.task_id,
                lease_token,
                "MEMORY_OBSERVED",
                "Stored independently validated repair evidence for memory curation",
                {"observationId": observation.get("id")},
            )
        except Exception as error:
            self._event(
                context.task_id,
                lease_token,
                "MEMORY_WRITE_SKIPPED",
                "Memory observation could not be stored; repair result remains valid",
                {"error": str(error)[-1_000:]},
            )
            return
        if self._memory_curator is not None:
            try:
                result = self._memory_curator.curate(
                    context, artifacts, evaluation, UUID(str(observation["id"]))
                )
                self._event(
                    context.task_id,
                    lease_token,
                    "MEMORY_CURATED",
                    f"Memory proposal finished with {result.proposal_status}",
                    {
                        "operation": result.operation,
                        "memoryId": result.memory_id,
                        "revision": result.revision,
                    },
                )
            except Exception as error:
                self._event(
                    context.task_id,
                    lease_token,
                    "MEMORY_CURATION_SKIPPED",
                    "Memory curation failed; immutable observation was retained for retry",
                    {"observationId": observation.get("id"), "error": str(error)[-1_000:]},
                )

    @staticmethod
    def _memory_usage_markers(output: str) -> set[tuple[UUID, int]]:
        pattern = re.compile(
            r"MEMORY_USED:\s*([0-9a-fA-F-]{36})@(\d+)", re.IGNORECASE
        )
        used: set[tuple[UUID, int]] = set()
        for memory_id, revision in pattern.findall(output):
            try:
                used.add((UUID(memory_id), int(revision)))
            except ValueError:
                continue
        return used

    def _report_failure(
        self,
        message: RepairTaskMessage,
        error: Exception,
        lease_token: UUID | None,
        run_result: AgentRunResult | None,
    ) -> None:
        error_code, retryable = self._classify_error(error)
        try:
            self._update(
                message.task_id, lease_token,
                "FAILED",
                validation_summary=str(error)[-4_000:],
                error_code=error_code,
                error_summary=str(error)[-20_000:],
                retryable=retryable,
                run_result=run_result,
            )
        except BackendTransportError:
            raise
        except Exception:
            return

    def _update(
        self,
        task_id,
        lease_token: UUID | None,
        status: str,
        *,
        run_result: AgentRunResult | None = None,
        **kwargs,
    ):
        usage = {}
        if run_result is not None:
            usage = {
                "duration_ms": run_result.duration_ms,
                "prompt_tokens": run_result.prompt_tokens,
                "completion_tokens": run_result.completion_tokens,
                "total_tokens": run_result.total_tokens,
            }
        return self._backend.update(
            task_id,
            status,
            worker_id=self._worker_id if lease_token is not None else None,
            lease_token=lease_token,
            model_name=self._model_name,
            **usage,
            **kwargs,
        )

    def _event(self, task_id, lease_token: UUID, stage: str, message: str, metadata=None) -> None:
        self._backend.report_event(
            task_id,
            stage,
            message,
            self._worker_id,
            lease_token,
            None if metadata is None else json.dumps(metadata, ensure_ascii=False),
        )
        if self._stage_delay_seconds > 0 and stage in self._DELAYED_STAGES:
            time.sleep(self._stage_delay_seconds)

    def _cancellation_check(self, task_id, lease_token: UUID):
        last_check = 0.0
        last_renewal = 0.0
        cancelled = False

        def check() -> bool:
            nonlocal last_check, last_renewal, cancelled
            now = time.monotonic()
            if now - last_renewal >= self._lease_seconds / 3:
                self._backend.renew_lease(task_id, self._worker_id, lease_token, self._lease_seconds)
                last_renewal = now
            if now - last_check >= 3:
                control = self._backend.get_control(task_id, self._worker_id, lease_token)
                cancelled = bool(control.get("cancelRequested"))
                last_check = now
            return cancelled

        return check

    def send_heartbeat(self) -> None:
        with self._activity_lock:
            status = self._activity_status
            active_task_id = self._active_task_id
            self._heartbeat_sequence += 1
            sequence = self._heartbeat_sequence
        try:
            if not self._presence_registered and not self._presence_superseded:
                self.register_presence()
            try:
                response = self._backend.heartbeat(
                    self._worker_id,
                    status,
                    active_task_id,
                    instance_id=self._presence_instance_id,
                    sequence=sequence,
                    role=self._worker_role,
                    started_at=self._presence_started_at,
                )
                if response.get("accepted") is False:
                    self._presence_registered = False
                    if response.get("registrationRequired"):
                        self._presence_superseded = False
                    elif response.get("presenceAvailable", True):
                        self._presence_superseded = True
            except TypeError:
                # 允许测试替身和滚动升级中的旧后端继续使用原有心跳协议。
                self._backend.heartbeat(self._worker_id, status, active_task_id)
        except Exception:
            return

    def register_presence(self) -> None:
        register = getattr(self._backend, "register_worker", None)
        if not callable(register):
            self._presence_registered = True
            return
        try:
            response = register(self._worker_id, self._presence_instance_id, self._worker_role)
            self._presence_registered = response.get("accepted", True)
            if self._presence_registered:
                self._presence_superseded = False
        except Exception:
            self._presence_registered = False

    def unregister_presence(self) -> None:
        unregister = getattr(self._backend, "unregister_worker", None)
        if not callable(unregister):
            return
        try:
            unregister(self._worker_id, self._presence_instance_id)
        except Exception:
            return

    def _set_activity(self, status: str, active_task_id) -> None:
        with self._activity_lock:
            self._activity_status = status
            self._active_task_id = active_task_id

    @staticmethod
    def _classify_error(error: Exception) -> tuple[str, bool]:
        if isinstance(error, EmptyPatchError):
            return "EMPTY_PATCH", False
        if isinstance(error, AgentTimeoutError):
            return "AGENT_TIMEOUT", True
        if isinstance(error, AgentProcessError):
            return "AGENT_PROCESS_ERROR", True
        if isinstance(error, WorkspaceError):
            return "WORKSPACE_ERROR", True
        if isinstance(error, BackendRequestError):
            return "BACKEND_REJECTED", False
        if isinstance(error, OSError):
            return "IO_ERROR", True
        return "WORKER_ERROR", False

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


class EmptyPatchError(RuntimeError):
    pass


class AgentProcessError(RuntimeError):
    pass
