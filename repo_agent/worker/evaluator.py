from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from repo_agent.worker.models import RepairTaskContext
from repo_agent.worker.executor import TaskCancelledError


@dataclass(frozen=True)
class BenchmarkEvaluation:
    status: str
    fail_to_pass_passed: int = 0
    fail_to_pass_failed: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_failed: int = 0
    summary: str = ""
    harness_log_path: str = ""


class HistoricalReplayEvaluator:
    """读取已完成 SWE-bench 实验的报告，不重新运行 Docker harness。"""

    def __init__(self, reports_root: Path) -> None:
        self._reports_root = reports_root

    def evaluate(
        self,
        context: RepairTaskContext,
        patch: str,
        output_dir: Path,
        should_cancel=None,
    ) -> BenchmarkEvaluation:
        if context.repository_provider != "SWE_BENCH" or not context.issue_external_id:
            raise ValueError("Historical evaluator received a non-benchmark task")
        if should_cancel is not None and should_cancel():
            raise TaskCancelledError("Repair task was cancelled before historical evaluation")

        source = self._reports_root / context.issue_external_id / "report.json"
        if not source.is_file():
            return BenchmarkEvaluation(
                status="HARNESS_ERROR",
                summary=f"Historical SWE-bench report not found: {source}",
            )
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            report = payload.get(context.issue_external_id, payload)
        except (OSError, json.JSONDecodeError) as error:
            return BenchmarkEvaluation(
                status="HARNESS_ERROR",
                summary=f"Historical SWE-bench report could not be read: {error}",
            )
        if not isinstance(report, dict) or not isinstance(report.get("resolved"), bool):
            return BenchmarkEvaluation(
                status="HARNESS_ERROR",
                summary=f"Historical SWE-bench report has no resolved result: {source}",
            )

        evaluation_dir = output_dir / "evaluation"
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        artifact = evaluation_dir / "historical_report.json"
        artifact.write_text(
            json.dumps(
                {
                    "simulation": True,
                    "source": str(source),
                    "instance_id": context.issue_external_id,
                    "report": report,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        counts = _test_counts(report)
        status = "RESOLVED" if report["resolved"] else "UNRESOLVED"
        summary = (
            f"Historical SWE-bench result replayed: {status}; "
            f"F2P {counts['fail_to_pass_passed']} passed, "
            f"{counts['fail_to_pass_failed']} failed; "
            f"P2P {counts['pass_to_pass_passed']} passed, "
            f"{counts['pass_to_pass_failed']} failed."
        )
        return BenchmarkEvaluation(
            status=status,
            summary=summary,
            harness_log_path=str(artifact),
            **counts,
        )


class SweBenchHarnessEvaluator:
    def __init__(
        self,
        dataset: Path,
        swebench_dir: Path,
        model: str,
        *,
        python: str = sys.executable,
        namespace: str = "none",
        cache_level: str = "env",
        timeout_seconds: int = 900,
        docker_network: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self._dataset = dataset
        self._swebench_dir = swebench_dir
        self._model = model
        self._python = python
        self._namespace = namespace
        self._cache_level = cache_level
        self._timeout_seconds = timeout_seconds
        self._docker_network = docker_network
        self._proxy = proxy

    def evaluate(
        self,
        context: RepairTaskContext,
        patch: str,
        output_dir: Path,
        should_cancel=None,
    ) -> BenchmarkEvaluation:
        if context.repository_provider != "SWE_BENCH" or not context.issue_external_id:
            raise ValueError("SWE-bench evaluator received a non-benchmark task")
        evaluation_dir = output_dir / "evaluation"
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        predictions = evaluation_dir / "predictions.json"
        model_name = f"repoagent-{self._model}"
        predictions.write_text(
            json.dumps(
                [{
                    "instance_id": context.issue_external_id,
                    "model_patch": patch,
                    "model_name_or_path": model_name,
                }],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        run_id = f"service-{context.task_id}"
        command = [
            self._python,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            str(self._dataset),
            "--predictions_path",
            str(predictions),
            "--max_workers",
            "1",
            "--instance_ids",
            context.issue_external_id,
            "--run_id",
            run_id,
            "--namespace",
            self._namespace,
            "--cache_level",
            self._cache_level,
            "--timeout",
            str(self._timeout_seconds),
        ]
        env = os.environ.copy()
        if self._docker_network:
            env["SWEBENCH_DOCKER_NETWORK"] = self._docker_network
        if self._proxy:
            env["SWEBENCH_GIT_PROXY"] = self._proxy
            env["HTTP_PROXY"] = self._proxy
            env["HTTPS_PROXY"] = self._proxy
            env["http_proxy"] = self._proxy
            env["https_proxy"] = self._proxy
        process_timeout = max(self._timeout_seconds * 2, self._timeout_seconds + 900)
        if should_cancel is None:
            try:
                result = subprocess.run(
                    command,
                    cwd=self._swebench_dir,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=process_timeout,
                )
                output = result.stdout or ""
                result_returncode = result.returncode
            except subprocess.TimeoutExpired as error:
                output = str(error.stdout or "")
                log_path = evaluation_dir / "harness_stdout.txt"
                log_path.write_text(output, encoding="utf-8", errors="replace")
                return BenchmarkEvaluation(
                    status="HARNESS_ERROR",
                    summary=f"Harness timed out after {self._timeout_seconds} seconds.",
                    harness_log_path=str(log_path),
                )
        else:
            process = subprocess.Popen(
                command,
                cwd=self._swebench_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            started = time.monotonic()
            while process.poll() is None:
                if should_cancel():
                    self._terminate(process)
                    process.communicate()
                    raise TaskCancelledError("Repair task was cancelled during SWE-bench evaluation")
                if time.monotonic() - started >= process_timeout:
                    self._terminate(process)
                    output = process.communicate()[0] or ""
                    log_path = evaluation_dir / "harness_stdout.txt"
                    log_path.write_text(output, encoding="utf-8", errors="replace")
                    return BenchmarkEvaluation(
                        status="HARNESS_ERROR",
                        summary=f"Harness timed out after {self._timeout_seconds} seconds.",
                        harness_log_path=str(log_path),
                    )
                time.sleep(1)
            output = process.communicate()[0] or ""
            result_returncode = process.returncode or 0
        if not output and result_returncode != 0:
            log_path = evaluation_dir / "harness_stdout.txt"
            log_path.write_text(output, encoding="utf-8", errors="replace")

        log_path = evaluation_dir / "harness_stdout.txt"
        log_path.write_text(output, encoding="utf-8", errors="replace")
        report = self._load_report(output)
        instance_report = self._load_instance_report(run_id, model_name, context.issue_external_id)
        test_output_path, test_output = self._load_test_output(
            run_id,
            model_name,
            context.issue_external_id,
        )
        counts = _test_counts(instance_report)
        if report is None or result_returncode != 0 or context.issue_external_id in report.get("error_ids", []):
            status = "HARNESS_ERROR"
        elif context.issue_external_id in report.get("resolved_ids", []):
            status = "RESOLVED"
        else:
            status = "UNRESOLVED"
        return BenchmarkEvaluation(
            status=status,
            summary=(test_output[-12_000:] if test_output else output[-4_000:]),
            harness_log_path=str(test_output_path or log_path),
            **counts,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _load_report(self, stdout: str) -> dict | None:
        match = re.search(r"Report written to (.+)", stdout)
        if not match:
            return None
        path = Path(match.group(1).strip())
        if not path.is_absolute():
            path = self._swebench_dir / path
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_instance_report(self, run_id: str, model_name: str, instance_id: str) -> dict:
        path = self._instance_log_dir(run_id, model_name, instance_id) / "report.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get(instance_id, {})

    def _load_test_output(
        self,
        run_id: str,
        model_name: str,
        instance_id: str,
    ) -> tuple[Path | None, str]:
        root = self._instance_log_dir(run_id, model_name, instance_id)
        for name in ("test_output.txt", "run_instance.log"):
            path = root / name
            if path.is_file():
                return path, path.read_text(encoding="utf-8", errors="replace")
        return None, ""

    def _instance_log_dir(self, run_id: str, model_name: str, instance_id: str) -> Path:
        return self._swebench_dir / "logs" / "run_evaluation" / run_id / model_name / instance_id


def _test_counts(instance_report: dict) -> dict[str, int]:
    statuses = instance_report.get("tests_status", {}) if isinstance(instance_report, dict) else {}

    def count(group: str, result: str) -> int:
        values = statuses.get(group, {}).get(result, []) if isinstance(statuses, dict) else []
        return len(values) if isinstance(values, (list, tuple, set, dict)) else 0

    return {
        "fail_to_pass_passed": count("FAIL_TO_PASS", "success"),
        "fail_to_pass_failed": count("FAIL_TO_PASS", "failure"),
        "pass_to_pass_passed": count("PASS_TO_PASS", "success"),
        "pass_to_pass_failed": count("PASS_TO_PASS", "failure"),
    }
