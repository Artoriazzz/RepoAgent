from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RecalledMemory:
    memory_id: UUID
    revision: int
    score: float
    title: str
    summary: str
    applicability: str
    repair_strategy: str
    verification_strategy: str = ""
    anti_patterns: str = ""
    retrieval_text: str = ""
    scope_type: str = "REPOSITORY"

    def prompt_text(self) -> str:
        sections = [
            f"### [{self.scope_type}] {_clip(self.title, 180)} [{self.memory_id}@{self.revision}]",
            f"- 摘要：{_clip(self.summary, 500)}",
            f"- 适用条件：{_clip(self.applicability, 700)}",
            f"- 修复策略：{_clip(self.repair_strategy, 900)}",
        ]
        if self.verification_strategy:
            sections.append(f"- 验证策略：{_clip(self.verification_strategy, 500)}")
        if self.anti_patterns:
            sections.append(f"- 避免：{_clip(self.anti_patterns, 500)}")
        return "\n".join(sections)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.memory_id),
            "revision": self.revision,
            "score": self.score,
            "title": self.title,
            "summary": self.summary,
            "retrievalText": self.retrieval_text,
            "applicability": self.applicability,
            "repairStrategy": self.repair_strategy,
            "verificationStrategy": self.verification_strategy,
            "antiPatterns": self.anti_patterns,
            "scopeType": self.scope_type,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RecalledMemory":
        return cls(
            memory_id=UUID(str(payload["id"])),
            revision=int(payload["revision"]),
            score=float(payload.get("score") or 0),
            title=str(payload.get("title") or ""),
            summary=str(payload.get("summary") or ""),
            retrieval_text=str(payload.get("retrievalText") or ""),
            applicability=str(payload.get("applicability") or ""),
            repair_strategy=str(payload.get("repairStrategy") or ""),
            verification_strategy=str(payload.get("verificationStrategy") or ""),
            anti_patterns=str(payload.get("antiPatterns") or ""),
            scope_type=(
                "GLOBAL"
                if str(payload.get("scopeType") or "").upper() == "GLOBAL"
                else "REPOSITORY"
            ),
        )


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "...[截断]"


def _memory_query_features(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    allowed = ("object", "operation", "symptom", "expectedBehavior", "triggerConditions")
    limits = {
        "object": 512,
        "operation": 512,
        "symptom": 2_000,
        "expectedBehavior": 2_000,
        "triggerConditions": 2_000,
    }
    features = {
        name: str(value[name]).strip()[:limits[name]]
        for name in allowed
        if value.get(name) is not None and str(value[name]).strip()
    }
    return features or None


class RepairPipelineStage(str, Enum):
    """跨进程工作流阶段；每个阶段有一条独立 RabbitMQ 队列。"""

    COORDINATOR = "COORDINATOR"
    IMPLEMENT = "IMPLEMENT"
    VALIDATION = "VALIDATION"
    FAILURE_REVIEW = "FAILURE_REVIEW"
    CORRECTIVE_IMPLEMENT = "CORRECTIVE_IMPLEMENT"
    EXPERIENCE_MEMORY = "EXPERIENCE_MEMORY"

    @property
    def queue_name(self) -> str:
        return {
            RepairPipelineStage.COORDINATOR: "repoagent.repair.stage.coordinator",
            RepairPipelineStage.IMPLEMENT: "repoagent.repair.stage.implement",
            RepairPipelineStage.VALIDATION: "repoagent.repair.stage.validation",
            RepairPipelineStage.FAILURE_REVIEW: "repoagent.repair.stage.failure-review",
            RepairPipelineStage.CORRECTIVE_IMPLEMENT: "repoagent.repair.stage.corrective-implement",
            RepairPipelineStage.EXPERIENCE_MEMORY: "repoagent.repair.stage.experience-memory",
        }[self]

    @property
    def worker_role(self) -> str:
        return {
            RepairPipelineStage.COORDINATOR: "coordinator",
            RepairPipelineStage.IMPLEMENT: "implement",
            RepairPipelineStage.VALIDATION: "validation",
            RepairPipelineStage.FAILURE_REVIEW: "failure-review",
            RepairPipelineStage.CORRECTIVE_IMPLEMENT: "corrective-implement",
            RepairPipelineStage.EXPERIENCE_MEMORY: "experience-memory",
        }[self]


@dataclass(frozen=True)
class RepairTaskMessage:
    task_id: UUID
    issue_id: UUID
    base_sha: str
    branch_name: str
    stage: RepairPipelineStage = RepairPipelineStage.COORDINATOR
    stage_sequence: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RepairTaskMessage":
        try:
            return cls(
                task_id=UUID(str(payload["taskId"])),
                issue_id=UUID(str(payload["issueId"])),
                base_sha=str(payload["baseSha"]),
                branch_name=str(payload["branchName"]),
                stage=RepairPipelineStage(str(payload.get("stage") or "COORDINATOR")),
                stage_sequence=int(payload.get("stageSequence") or 0),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid repair task message") from error


@dataclass(frozen=True)
class RepairTaskContext:
    task_id: UUID
    issue_id: UUID
    repository_id: UUID
    repository_provider: str
    repository_external_id: str
    mirror_key: str | None
    github_repository_id: int | None
    repository_full_name: str
    clone_url: str | None
    default_branch: str
    github_issue_number: int | None
    issue_external_id: str | None
    benchmark_dataset_split: str | None
    issue_title: str
    issue_body: str
    base_sha: str
    branch_name: str
    attempt_number: int = 1
    previous_failure_summary: str | None = None
    memory_query_features: dict[str, str] | None = None
    recalled_memories: tuple[RecalledMemory, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RepairTaskContext":
        try:
            return cls(
                task_id=UUID(str(payload["taskId"])),
                issue_id=UUID(str(payload["issueId"])),
                repository_id=UUID(str(payload["repositoryId"])),
                repository_provider=str(payload["repositoryProvider"]),
                repository_external_id=str(payload["repositoryExternalId"]),
                mirror_key=None if payload.get("mirrorKey") is None else str(payload["mirrorKey"]),
                github_repository_id=(
                    None if payload.get("githubRepositoryId") is None else int(payload["githubRepositoryId"])
                ),
                repository_full_name=str(payload["repositoryFullName"]),
                clone_url=None if payload.get("cloneUrl") is None else str(payload["cloneUrl"]),
                default_branch=str(payload["defaultBranch"]),
                github_issue_number=(
                    None if payload.get("githubIssueNumber") is None else int(payload["githubIssueNumber"])
                ),
                issue_external_id=(
                    None if payload.get("issueExternalId") is None else str(payload["issueExternalId"])
                ),
                benchmark_dataset_split=(
                    None
                    if payload.get("benchmarkDatasetSplit") is None
                    else str(payload["benchmarkDatasetSplit"])
                ),
                issue_title=str(payload["issueTitle"]),
                issue_body=str(payload.get("issueBody") or ""),
                base_sha=str(payload["baseSha"]),
                branch_name=str(payload["branchName"]),
                attempt_number=int(payload.get("attemptNumber") or 1),
                previous_failure_summary=(
                    None
                    if payload.get("previousFailureSummary") is None
                    else str(payload["previousFailureSummary"])
                ),
                memory_query_features=_memory_query_features(payload.get("memoryQueryFeatures")),
                recalled_memories=tuple(
                    RecalledMemory.from_payload(item)
                    for item in (payload.get("recalledMemories") or [])
                    if isinstance(item, dict)
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid repair task context") from error

    def to_payload(self) -> dict[str, Any]:
        return {
            "taskId": str(self.task_id),
            "issueId": str(self.issue_id),
            "repositoryId": str(self.repository_id),
            "repositoryProvider": self.repository_provider,
            "repositoryExternalId": self.repository_external_id,
            "mirrorKey": self.mirror_key,
            "githubRepositoryId": self.github_repository_id,
            "repositoryFullName": self.repository_full_name,
            "cloneUrl": self.clone_url,
            "defaultBranch": self.default_branch,
            "githubIssueNumber": self.github_issue_number,
            "issueExternalId": self.issue_external_id,
            "benchmarkDatasetSplit": self.benchmark_dataset_split,
            "issueTitle": self.issue_title,
            "issueBody": self.issue_body,
            "baseSha": self.base_sha,
            "branchName": self.branch_name,
            "attemptNumber": self.attempt_number,
            "previousFailureSummary": self.previous_failure_summary,
            "memoryQueryFeatures": self.memory_query_features,
            "recalledMemories": [memory.to_payload() for memory in self.recalled_memories],
        }


@dataclass(frozen=True)
class PatchArtifacts:
    patch: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class AgentRunResult:
    exit_code: int
    output_tail: str
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
