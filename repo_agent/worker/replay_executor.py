from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from repo_agent.worker.executor import TaskCancelledError
from repo_agent.worker.models import AgentRunResult, RepairTaskContext


class ReplayPredictionStore:
    """按 SWE-bench instance_id 读取历史模型返回，不调用外部 LLM。"""

    def __init__(self, predictions_path: Path) -> None:
        self._path = predictions_path
        self._predictions = self._load(predictions_path)

    def get(self, instance_id: str) -> dict[str, Any]:
        prediction = self._predictions.get(instance_id)
        if prediction is None:
            raise KeyError(f"No replay prediction for SWE-bench instance: {instance_id}")
        return prediction

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            raise ValueError(f"Replay predictions file does not exist: {path}")
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            payload = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("predictions"), list):
            rows = payload["predictions"]
        elif isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = [
                {"instance_id": instance_id, **value}
                for instance_id, value in payload.items()
                if isinstance(value, dict)
            ]
        else:
            raise ValueError("Replay predictions must be a JSON list or object")
        predictions: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("instance_id"):
                raise ValueError("Every replay prediction requires instance_id")
            instance_id = str(row["instance_id"])
            if instance_id in predictions:
                raise ValueError(f"Duplicate replay prediction: {instance_id}")
            predictions[instance_id] = row
        return predictions


class ReplayRepoAgentExecutor:
    """把历史 model_patch 视为模拟模型输出，继续走真实 Worker 和评测流程。"""

    def __init__(self, predictions_path: Path) -> None:
        self._store = ReplayPredictionStore(predictions_path)
        self._source = predictions_path
        self._stage_callback: Callable[[str, str], None] | None = None

    def set_stage_callback(self, callback: Callable[[str, str], None] | None) -> None:
        self._stage_callback = callback

    def run(
        self,
        context: RepairTaskContext,
        worktree: Path,
        output_dir: Path,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        started = time.monotonic()
        if context.repository_provider != "SWE_BENCH" or not context.issue_external_id:
            return AgentRunResult(2, "Replay mode supports SWE-bench tasks only")
        if should_cancel is not None and should_cancel():
            raise TaskCancelledError("Repair task was cancelled before replay")

        try:
            prediction = self._store.get(context.issue_external_id)
        except KeyError as error:
            return AgentRunResult(2, str(error))
        patch = str(prediction.get("model_patch") or "")
        expected_base = str(prediction.get("base_sha") or prediction.get("base_commit") or "")
        if expected_base and expected_base != context.base_sha:
            return AgentRunResult(
                2,
                f"Replay base SHA mismatch: fixture={expected_base}, task={context.base_sha}",
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        if self._stage_callback is not None:
            self._stage_callback("IMPLEMENT", "Replaying the historical model patch")
        if patch.strip():
            checked = self._git_apply(worktree, patch, check=True)
            if checked.returncode != 0:
                return AgentRunResult(2, f"Replay patch check failed:\n{checked.stdout[-2_000:]}")
            if should_cancel is not None and should_cancel():
                raise TaskCancelledError("Repair task was cancelled before replay patch application")
            applied = self._git_apply(worktree, patch, check=False)
            if applied.returncode != 0:
                return AgentRunResult(2, f"Replay patch application failed:\n{applied.stdout[-2_000:]}")
        if self._stage_callback is not None:
            self._stage_callback("PUBLIC_VALIDATION", "Checking replayed patch artifacts")

        final_answer = str(
            prediction.get("model_response")
            or prediction.get("final_answer")
            or f"Simulated historical response for {context.issue_external_id}"
        )
        total_tokens = int(prediction.get("total_tokens") or 0)
        trace_event = {
            "type": "llm_response",
            "step": 1,
            "response": {
                "content": final_answer,
                "simulation": True,
                "replay_instance_id": context.issue_external_id,
                "replay_source": str(self._source),
            },
            "usage": {"total_tokens": total_tokens},
        }
        output_dir.joinpath("trace.jsonl").write_text(
            json.dumps(trace_event, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        output_dir.joinpath("messages.json").write_text(
            json.dumps(
                [
                    {"role": "user", "content": context.issue_body},
                    {"role": "assistant", "content": final_answer, "simulation": True},
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        output_dir.joinpath("final_answer.md").write_text(final_answer, encoding="utf-8")
        output_dir.joinpath("session.json").write_text(
            json.dumps(
                {
                    "simulation": True,
                    "execution_mode": "replay",
                    "instance_id": context.issue_external_id,
                    "total_tokens": total_tokens,
                    "completed": True,
                    "stop_reason": "historical_prediction_replayed",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        output_dir.joinpath("stage_attribution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pipeline": "historical_replay",
                    "simulation": True,
                    "instance_id": context.issue_external_id,
                    "token_usage": {"total": total_tokens},
                    "stages": {"implementation": {"patch_chars": len(patch)}},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log = (
            f"SIMULATED LLM REPLAY\ninstance_id={context.issue_external_id}\n"
            f"source={self._source}\npatch_chars={len(patch)}\n"
        )
        output_dir.joinpath("repo_agent.log").write_text(log, encoding="utf-8")
        duration_ms = int((time.monotonic() - started) * 1_000)
        return AgentRunResult(0, final_answer[-2_000:], duration_ms, 0, 0, total_tokens)

    @staticmethod
    def _git_apply(worktree: Path, patch: str, *, check: bool) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(worktree), "apply", "--whitespace=nowarn"]
        if check:
            command.append("--check")
        command.append("-")
        return subprocess.run(
            command,
            input=patch,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
