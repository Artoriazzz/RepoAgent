from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from repo_agent.worker.models import RepairTaskContext


class BackendRequestError(RuntimeError):
    """后端明确拒绝请求，重试通常无意义。"""


class BackendTransportError(RuntimeError):
    """后端或网络暂不可用，消息消费者应重新入队。"""


class BackendClient:
    def __init__(self, base_url: str, worker_token: str, timeout_seconds: int = 15) -> None:
        self._base_url = base_url.rstrip("/")
        self._worker_token = worker_token
        self._timeout_seconds = timeout_seconds

    def get_context(self, task_id: UUID) -> RepairTaskContext:
        payload = self._request("GET", f"/internal/repair-tasks/{task_id}/context")
        return RepairTaskContext.from_payload(payload)

    def update(
        self,
        task_id: UUID,
        status: str,
        *,
        patch_summary: str | None = None,
        validation_summary: str | None = None,
        pr_url: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/repair-tasks/{task_id}/updates",
            {
                "status": status,
                "patchSummary": patch_summary,
                "validationSummary": validation_summary,
                "prUrl": pr_url,
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
