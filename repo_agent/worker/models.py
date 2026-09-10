from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RepairTaskMessage:
    task_id: UUID
    issue_id: UUID
    base_sha: str
    branch_name: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RepairTaskMessage":
        try:
            return cls(
                task_id=UUID(str(payload["taskId"])),
                issue_id=UUID(str(payload["issueId"])),
                base_sha=str(payload["baseSha"]),
                branch_name=str(payload["branchName"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid repair task message") from error


@dataclass(frozen=True)
class RepairTaskContext:
    task_id: UUID
    issue_id: UUID
    repository_id: UUID
    github_repository_id: int
    repository_full_name: str
    clone_url: str
    default_branch: str
    github_issue_number: int
    issue_title: str
    issue_body: str
    base_sha: str
    branch_name: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RepairTaskContext":
        try:
            return cls(
                task_id=UUID(str(payload["taskId"])),
                issue_id=UUID(str(payload["issueId"])),
                repository_id=UUID(str(payload["repositoryId"])),
                github_repository_id=int(payload["githubRepositoryId"]),
                repository_full_name=str(payload["repositoryFullName"]),
                clone_url=str(payload["cloneUrl"]),
                default_branch=str(payload["defaultBranch"]),
                github_issue_number=int(payload["githubIssueNumber"]),
                issue_title=str(payload["issueTitle"]),
                issue_body=str(payload.get("issueBody") or ""),
                base_sha=str(payload["baseSha"]),
                branch_name=str(payload["branchName"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid repair task context") from error


@dataclass(frozen=True)
class PatchArtifacts:
    patch: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class AgentRunResult:
    exit_code: int
    output_tail: str
