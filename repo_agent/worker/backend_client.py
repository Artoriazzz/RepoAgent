from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID
from urllib.parse import urlencode

from repo_agent.worker.models import RecalledMemory, RepairPipelineStage, RepairTaskContext
from dataclasses import dataclass


class BackendRequestError(RuntimeError):
    """后端明确拒绝请求，重试通常无意义。"""


class BackendTransportError(RuntimeError):
    """后端或网络暂不可用，消息消费者应重新入队。"""


class BackendLeaseBusyError(BackendTransportError):
    """任务仍由其他 Worker 持有，应稍后重新入队。"""


class BackendTaskUnavailableError(BackendRequestError):
    """任务已完成或取消，重复消息可以安全确认。"""


@dataclass(frozen=True)
class WorkerLease:
    token: UUID
    stage_sequence: int
    lease_epoch: int


class BackendClient:
    def __init__(self, base_url: str, worker_token: str, timeout_seconds: int = 15) -> None:
        self._base_url = base_url.rstrip("/")
        self._worker_token = worker_token
        self._timeout_seconds = timeout_seconds

    def get_context(self, task_id: UUID) -> RepairTaskContext:
        payload = self._request("GET", f"/internal/repair-tasks/{task_id}/context")
        return RepairTaskContext.from_payload(payload)

    def claim(
        self,
        task_id: UUID,
        worker_id: str,
        lease_seconds: int = 90,
        stage: RepairPipelineStage | None = None,
        stage_sequence: int | None = None,
    ) -> WorkerLease:
        try:
            payload = self._request(
                "POST", f"/internal/repair-tasks/{task_id}/claim",
                {
                    "workerId": worker_id,
                    "leaseSeconds": lease_seconds,
                    "stage": None if stage is None else stage.value,
                    "stageSequence": stage_sequence,
                },
            )
        except BackendRequestError as error:
            if "active worker" in str(error):
                raise BackendLeaseBusyError(str(error)) from error
            raise BackendTaskUnavailableError(str(error)) from error
        return WorkerLease(
            UUID(str(payload["leaseToken"])),
            int(payload.get("stageSequence") or 0),
            int(payload.get("leaseEpoch") or 0),
        )

    @staticmethod
    def _lease_fields(lease: UUID | WorkerLease | None) -> dict[str, Any]:
        if isinstance(lease, WorkerLease):
            return {
                "leaseToken": str(lease.token),
                "stageSequence": lease.stage_sequence,
                "leaseEpoch": lease.lease_epoch,
            }
        return {
            "leaseToken": None if lease is None else str(lease),
            "stageSequence": None,
            "leaseEpoch": None,
        }

    def handoff_stage(
        self,
        task_id: UUID,
        current_stage: RepairPipelineStage,
        next_stage: RepairPipelineStage,
        worker_id: str,
        lease_token: UUID,
        message: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/repair-tasks/{task_id}/stage-handoffs",
            {
                "currentStage": current_stage.value,
                "nextStage": next_stage.value,
                "workerId": worker_id,
                **self._lease_fields(lease_token),
                "message": message,
            },
        )

    def complete_pipeline(
        self,
        task_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        patch_summary: str | None,
        patch_artifact_path: str | None,
        validation_summary: str | None,
        evaluation_status: str,
        fail_to_pass_passed: int = 0,
        fail_to_pass_failed: int = 0,
        pass_to_pass_passed: int = 0,
        pass_to_pass_failed: int = 0,
        evaluation_summary: str = "",
        harness_log_path: str = "",
        model_name: str | None = None,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/repair-tasks/{task_id}/pipeline-completions",
            {
                "workerId": worker_id,
                **self._lease_fields(lease_token),
                "patchSummary": patch_summary,
                "patchArtifactPath": patch_artifact_path,
                "validationSummary": validation_summary,
                "evaluationStatus": evaluation_status,
                "failToPassPassed": fail_to_pass_passed,
                "failToPassFailed": fail_to_pass_failed,
                "passToPassPassed": pass_to_pass_passed,
                "passToPassFailed": pass_to_pass_failed,
                "evaluationSummary": evaluation_summary,
                "harnessLogPath": harness_log_path,
                "modelName": model_name,
                "durationMs": duration_ms,
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "totalTokens": total_tokens,
            },
        )

    def renew_lease(self, task_id: UUID, worker_id: str, lease_token: UUID | WorkerLease, lease_seconds: int = 90) -> dict[str, Any]:
        return self._request(
            "POST", f"/internal/repair-tasks/{task_id}/lease/renew",
            {"workerId": worker_id, **self._lease_fields(lease_token), "leaseSeconds": lease_seconds},
        )

    def get_control(self, task_id: UUID, worker_id: str, lease_token: UUID | WorkerLease) -> dict[str, Any]:
        query = urlencode({"workerId": worker_id, **self._lease_fields(lease_token)})
        return self._request("GET", f"/internal/repair-tasks/{task_id}/control?{query}")

    def report_event(
        self,
        task_id: UUID,
        stage: str,
        message: str,
        worker_id: str,
        lease_token: UUID,
        metadata_json: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/internal/repair-tasks/{task_id}/events",
            {
                "stage": stage,
                "message": message,
                "metadataJson": metadata_json,
                "workerId": worker_id,
                **self._lease_fields(lease_token),
            },
        )

    def heartbeat(
        self,
        worker_id: str,
        status: str,
        active_task_id: UUID | None = None,
        metadata_json: str | None = None,
        *,
        instance_id: str | None = None,
        sequence: int | None = None,
        role: str | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/internal/workers/{worker_id}/heartbeat",
            {
                "status": status,
                "activeTaskId": None if active_task_id is None else str(active_task_id),
                "metadataJson": metadata_json,
                "instanceId": instance_id,
                "sequence": sequence,
                "role": role,
                "startedAt": started_at,
            },
        )

    def register_worker(self, worker_id: str, instance_id: str, role: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/workers/{worker_id}/registrations",
            {"instanceId": instance_id, "role": role},
        )

    def unregister_worker(self, worker_id: str, instance_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/internal/workers/{worker_id}/registrations/{instance_id}"
        )

    def update(
        self,
        task_id: UUID,
        status: str,
        *,
        patch_summary: str | None = None,
        patch_artifact_path: str | None = None,
        validation_summary: str | None = None,
        pr_url: str | None = None,
        worker_id: str | None = None,
        lease_token: UUID | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        retryable: bool | None = None,
        model_name: str | None = None,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/repair-tasks/{task_id}/updates",
            {
                "status": status,
                "patchSummary": patch_summary,
                "patchArtifactPath": patch_artifact_path,
                "validationSummary": validation_summary,
                "prUrl": pr_url,
                "workerId": worker_id,
                **self._lease_fields(lease_token),
                "errorCode": error_code,
                "errorSummary": error_summary,
                "retryable": retryable,
                "modelName": model_name,
                "durationMs": duration_ms,
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "totalTokens": total_tokens,
            },
        )

    def update_evaluation(
        self,
        task_id: UUID,
        status: str,
        *,
        fail_to_pass_passed: int = 0,
        fail_to_pass_failed: int = 0,
        pass_to_pass_passed: int = 0,
        pass_to_pass_failed: int = 0,
        summary: str = "",
        harness_log_path: str = "",
        worker_id: str | None = None,
        lease_token: UUID | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/repair-tasks/{task_id}/evaluation",
            {
                "status": status,
                "failToPassPassed": fail_to_pass_passed,
                "failToPassFailed": fail_to_pass_failed,
                "passToPassPassed": pass_to_pass_passed,
                "passToPassFailed": pass_to_pass_failed,
                "summary": summary,
                "harnessLogPath": harness_log_path,
                "workerId": worker_id,
                **self._lease_fields(lease_token),
            },
        )

    def search_memories(
        self,
        task_id: UUID,
        query: str,
        *,
        language_name: str | None = None,
        framework_name: str | None = None,
        problem_type: str | None = None,
        query_features: Mapping[str, str] | None = None,
        limit: int = 4,
        track_recall: bool = True,
    ) -> tuple[RecalledMemory, ...]:
        payload = self._request(
            "POST",
            "/internal/memories/search",
            {
                "taskId": str(task_id),
                "query": query,
                "languageName": language_name,
                "frameworkName": framework_name,
                "problemType": problem_type,
                "object": None if query_features is None else query_features.get("object"),
                "operation": None if query_features is None else query_features.get("operation"),
                "symptom": None if query_features is None else query_features.get("symptom"),
                "expectedBehavior": (
                    None if query_features is None else query_features.get("expectedBehavior")
                ),
                "triggerConditions": (
                    None if query_features is None else query_features.get("triggerConditions")
                ),
                "limit": limit,
                "trackRecall": track_recall,
            },
        )
        recalled = []
        for hit in payload.get("hits") or []:
            memory = hit.get("memory") or {}
            content = memory.get("content") or {}
            recalled.append(
                RecalledMemory(
                    memory_id=UUID(str(memory["id"])),
                    revision=int(memory["revision"]),
                    score=float(hit.get("score") or 0),
                    title=str(content.get("title") or "未命名经验"),
                    summary=str(content.get("summary") or ""),
                    retrieval_text=str(content.get("retrievalText") or ""),
                    applicability=str(content.get("applicability") or ""),
                    repair_strategy=str(content.get("repairStrategy") or ""),
                    verification_strategy=str(content.get("verificationStrategy") or ""),
                    anti_patterns=str(content.get("antiPatterns") or ""),
                    scope_type=(
                        "GLOBAL"
                        if str(memory.get("scopeType") or hit.get("scopeType") or "").upper()
                        == "GLOBAL"
                        else "REPOSITORY"
                    ),
                )
            )
        return tuple(recalled)

    def record_memory_observation(
        self,
        task_id: UUID,
        *,
        idempotency_key: str,
        outcome: str,
        problem_signature: str,
        patch_summary: str,
        validation_summary: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/memories/observations",
            {
                "taskId": str(task_id),
                "idempotencyKey": idempotency_key,
                "outcome": outcome,
                "problemSignature": problem_signature,
                "patchSummary": patch_summary,
                "validationSummary": validation_summary,
                "evidence": evidence,
            },
        )

    def record_memory_feedback(
        self,
        memory_id: UUID,
        task_id: UUID,
        revision: int,
        *,
        used: bool,
        successful: bool,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/memories/{memory_id}/feedback",
            {
                "taskId": str(task_id),
                "revision": revision,
                "used": used,
                "successful": successful,
            },
        )

    def submit_memory_proposal(
        self,
        *,
        observation_id: UUID,
        idempotency_key: str,
        operation: str,
        change_reason: str,
        memory_id: UUID | None = None,
        base_version: int | None = None,
        scope_type: str | None = None,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/memories/proposals",
            {
                "observationId": str(observation_id),
                "idempotencyKey": idempotency_key,
                "operation": operation,
                "memoryId": None if memory_id is None else str(memory_id),
                "baseVersion": base_version,
                "scopeType": scope_type,
                "content": content,
                "changeReason": change_reason,
            },
        )

    def claim_memory_merge_batch(
        self, worker_id: str, lease_seconds: int = 180
    ) -> dict[str, Any] | None:
        """领取一个版本固定的记忆合并批次；204 表示当前没有待处理提案。"""
        payload = self._request(
            "POST",
            "/internal/memory-merge-batches/claim",
            {"workerId": worker_id, "leaseSeconds": lease_seconds},
        )
        return payload or None

    def complete_memory_merge_batch(
        self,
        batch_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        action: str,
        content: dict[str, Any] | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/memory-merge-batches/{batch_id}/complete",
            {
                "workerId": worker_id,
                "leaseToken": str(lease_token),
                "action": action,
                "content": content,
                "changeReason": change_reason,
            },
        )

    def abandon_memory_merge_batch(
        self,
        batch_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        reason: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/memory-merge-batches/{batch_id}/abandon",
            {
                "workerId": worker_id,
                "leaseToken": str(lease_token),
                "reason": reason,
            },
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-RepoAgent-Worker-Token": self._worker_token,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[-2_000:]
            if error.code >= 500:
                raise BackendTransportError(f"Backend returned {error.code}: {detail}") from error
            raise BackendRequestError(f"Backend returned {error.code}: {detail}") from error
        except URLError as error:
            raise BackendTransportError(f"Backend request failed: {error.reason}") from error

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise BackendTransportError("Backend returned invalid JSON") from error
