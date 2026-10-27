from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from repo_agent.core.experience import constraints_from_patch
from repo_agent.worker.models import AgentRunResult, PatchArtifacts, RecalledMemory, RepairTaskContext

if TYPE_CHECKING:
    from repo_agent.worker.evaluator import BenchmarkEvaluation


class RepoAgentExecutor:
    def __init__(self, executable: str, model: str, max_steps: int, timeout_seconds: int) -> None:
        self._executable = executable
        self._model = model
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds
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
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self._executable,
            "run",
            "--repo",
            str(worktree),
            "--task",
            self._build_task(context),
            "--model",
            self._model,
            "--coordinator",
            "--max-steps",
            str(self._max_steps),
            "--output-dir",
            str(output_dir),
        ]
        if context.repository_provider == "SWE_BENCH":
            command.extend([
                "--swebench-profile",
                "--repair-mode", "experience-build",
                "--no-repair-memory",
            ])
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        last_stage = ""
        while process.poll() is None:
            stage = self._detect_stage(output_dir)
            if stage and stage != last_stage:
                last_stage = stage
                if self._stage_callback is not None:
                    self._stage_callback(stage, self._stage_message(stage))
            elapsed = time.monotonic() - started
            if should_cancel is not None and should_cancel():
                self._terminate(process)
                output = process.communicate()[0] or ""
                output_dir.joinpath("repo_agent.log").write_text(output, encoding="utf-8")
                raise TaskCancelledError("Repair task was cancelled while RepoAgent was running")
            if elapsed >= self._timeout_seconds:
                self._terminate(process)
                output = process.communicate()[0] or ""
                output_dir.joinpath("repo_agent.log").write_text(output, encoding="utf-8")
                raise AgentTimeoutError(
                    f"RepoAgent timed out after {self._timeout_seconds}s\n{output[-2_000:]}"
                )
            time.sleep(1)

        output = process.communicate()[0] or ""
        output_dir.joinpath("repo_agent.log").write_text(output, encoding="utf-8")
        usage = self._read_usage(output_dir)
        return AgentRunResult(
            exit_code=process.returncode or 0,
            output_tail=output[-2_000:],
            duration_ms=int((time.monotonic() - started) * 1_000),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )

    def run_corrective(
        self,
        context: RepairTaskContext,
        worktree: Path,
        output_dir: Path,
        artifacts: PatchArtifacts,
        feedback: dict[str, Any],
        should_cancel: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        """只运行一次受限 Implement，不重新启动 Coordinator。"""

        corrective_dir = output_dir / "corrective_implement"
        corrective_dir.mkdir(parents=True, exist_ok=True)
        constraints = constraints_from_patch(artifacts.patch, worktree)
        if not constraints.has_scope:
            raise RuntimeError("Failed patch does not provide a safe corrective scope")
        constraints_payload = {
            "allowed_files": sorted(constraints.allowed_files),
            "allowed_ranges": {
                path: [[item.start, item.end] for item in ranges]
                for path, ranges in (constraints.allowed_ranges or {}).items()
            },
        }
        task_file = corrective_dir / "task.md"
        constraints_file = corrective_dir / "constraints.json"
        task_file.write_text(
            self._build_corrective_task(context, artifacts.patch, feedback),
            encoding="utf-8",
        )
        constraints_file.write_text(
            json.dumps(constraints_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        command = [
            self._executable,
            "corrective-implement",
            "--repo", str(worktree),
            "--task-file", str(task_file),
            "--constraints-file", str(constraints_file),
            "--model", self._model,
            "--repair-mode", "experience-build",
            "--no-repair-memory",
            "--max-steps", str(self._max_steps),
            "--output-dir", str(corrective_dir),
        ]
        return self._run_command(
            command,
            corrective_dir,
            should_cancel,
            log_name="repo_agent.log",
            label="Corrective Implement",
        )

    def review_failure(
        self,
        context: RepairTaskContext,
        worktree: Path,
        output_dir: Path,
        artifacts: PatchArtifacts,
        evaluation: "BenchmarkEvaluation",
        should_cancel: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        """运行独立的 FailureReview 子 Agent，结果写入任务专属状态目录。"""
        review_dir = output_dir / "failure_review"
        review_dir.mkdir(parents=True, exist_ok=True)
        task_file = review_dir / "task.md"
        patch_file = review_dir / "failed.patch"
        failure_log_file = review_dir / "failure.log"
        validation_file = review_dir / "validation.json"
        task_file.write_text(self._build_task(context), encoding="utf-8")
        patch_file.write_text(artifacts.patch, encoding="utf-8")
        failure_detail = evaluation.summary
        harness_log = Path(evaluation.harness_log_path) if evaluation.harness_log_path else None
        if harness_log is not None and harness_log.is_file():
            failure_detail = harness_log.read_text(encoding="utf-8", errors="replace")[-12_000:]
        failure_log_file.write_text(failure_detail, encoding="utf-8")
        validation_file.write_text(
            json.dumps(
                {
                    "validation_kind": "swebench_harness_feedback",
                    "status": evaluation.status,
                    "test_counts": {
                        "fail_to_pass_passed": evaluation.fail_to_pass_passed,
                        "fail_to_pass_failed": evaluation.fail_to_pass_failed,
                        "pass_to_pass_passed": evaluation.pass_to_pass_passed,
                        "pass_to_pass_failed": evaluation.pass_to_pass_failed,
                    },
                    "failure_excerpt": failure_detail,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        command = [
            self._executable,
            "review-failure",
            "--repo", str(worktree),
            "--task-file", str(task_file),
            "--patch-file", str(patch_file),
            "--failure-log-file", str(failure_log_file),
            "--validation-file", str(validation_file),
            "--model", self._model,
            "--max-steps", str(min(self._max_steps, 8)),
            "--output-dir", str(review_dir),
        ]
        return self._run_command(command, review_dir, should_cancel)

    def _run_command(
        self,
        command: list[str],
        output_dir: Path,
        should_cancel: Callable[[], bool] | None,
        *,
        log_name: str = "failure_review.log",
        label: str = "FailureReview",
    ) -> AgentRunResult:
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while process.poll() is None:
            if should_cancel is not None and should_cancel():
                self._terminate(process)
                output = process.communicate()[0] or ""
                output_dir.joinpath(log_name).write_text(output, encoding="utf-8")
                raise TaskCancelledError(f"Repair task was cancelled while {label} was running")
            if time.monotonic() - started >= self._timeout_seconds:
                self._terminate(process)
                output = process.communicate()[0] or ""
                output_dir.joinpath(log_name).write_text(output, encoding="utf-8")
                raise AgentTimeoutError(
                    f"{label} timed out after {self._timeout_seconds}s\n{output[-2_000:]}"
                )
            time.sleep(1)
        output = process.communicate()[0] or ""
        output_dir.joinpath(log_name).write_text(output, encoding="utf-8")
        usage = self._read_usage(output_dir)
        return AgentRunResult(
            exit_code=process.returncode or 0,
            output_tail=output[-2_000:],
            duration_ms=int((time.monotonic() - started) * 1_000),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )

    @staticmethod
    def _detect_stage(output_dir: Path) -> str:
        subagents = output_dir / "subagents"
        if not subagents.is_dir():
            return "COORDINATOR"
        entries = [entry for entry in subagents.iterdir() if entry.is_dir()]
        if not entries:
            return "COORDINATOR"
        entries.sort(key=lambda entry: entry.stat().st_mtime_ns)
        latest_role = entries[-1].name.rsplit("-", 1)[-1]
        failure_reviews = [entry for entry in entries if entry.name.endswith("-failure_review")]
        if latest_role == "failure_review":
            return "FAILURE_REVIEW"
        if latest_role == "implement":
            if failure_reviews and entries[-1].stat().st_mtime_ns > max(
                entry.stat().st_mtime_ns for entry in failure_reviews
            ):
                return "CORRECTIVE_IMPLEMENT"
            return "IMPLEMENT"
        if latest_role in {"verify", "patch_review", "test_expander"}:
            return "PUBLIC_VALIDATION"
        return "COORDINATOR"

    @staticmethod
    def _stage_message(stage: str) -> str:
        return {
            "COORDINATOR": "Coordinator is locating the repair contract",
            "IMPLEMENT": "Implement Worker is creating the initial patch",
            "PUBLIC_VALIDATION": "Review and public validation are checking the patch",
            "FAILURE_REVIEW": "FailureReview is analyzing the first failed attempt",
            "CORRECTIVE_IMPLEMENT": "Corrective Implement is applying the single allowed correction",
        }[stage]

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

    @staticmethod
    def _read_usage(output_dir: Path) -> tuple[int, int, int]:
        attribution = output_dir / "stage_attribution.json"
        if not attribution.is_file():
            return 0, 0, 0
        try:
            payload = json.loads(attribution.read_text(encoding="utf-8"))
            token_usage = payload.get("token_usage") or {}
            total = int(token_usage.get("total") or 0)
            return 0, 0, total
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0, 0, 0

    @staticmethod
    def _build_task(context: RepairTaskContext) -> str:
        memory_section = RepoAgentExecutor._memory_section(context)
        retry_section = ""
        if context.attempt_number > 1:
            retry_section = (
                "\n\n这是模式三允许的第二次、也是最后一次修复。先把上一次失败日志作为"
                "一次只读失败归因提供的直接证据，解释初始 patch 错在何处，再执行一次限定范围的代码纠正"
                " 生成更窄的修复；不得重复同一修改。\n上一次失败反馈：\n"
                f"{(context.previous_failure_summary or '上次修复失败，但没有可用日志。')[-8_000:]}"
            )
        if context.repository_provider == "SWE_BENCH":
            return (
                f"修复 SWE-bench 实例 {context.issue_external_id}。\n\n"
                f"仓库：{context.repository_external_id}\n"
                f"基线提交：{context.base_sha}\n\n"
                f"问题描述：\n{context.issue_body}\n\n"
                "请仅根据问题描述、当前仓库代码和公开测试完成最小修复。"
                "不得查找或使用 gold patch、隐藏测试及最终 harness 反馈。"
                f"{memory_section}{retry_section}"
            )
        return (
            f"修复 GitHub Issue #{context.github_issue_number}：{context.issue_title}\n\n"
            f"仓库：{context.repository_full_name}\n"
            f"基线提交：{context.base_sha}\n\n"
            f"Issue 描述：\n{context.issue_body}\n\n"
            "请在当前 worktree 中完成最小修复，保留现有风格，并运行可用的相关验证。"
            f"{memory_section}{retry_section}"
        )

    @staticmethod
    def _build_corrective_task(
        context: RepairTaskContext,
        failed_patch: str,
        feedback: dict[str, Any],
    ) -> str:
        public_task = context.issue_body or context.issue_title
        return (
            f"## Public Task\n{public_task}\n\n"
            f"## FailureFeedback\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n\n"
            f"## Current Failed Patch\n```diff\n{failed_patch}\n```\n\n"
            "The worktree already contains Current Failed Patch. Correct it in place exactly once; do not run a new "
            "Coordinator and do not solve the task from scratch. Every edit must be authorized by the direct evidence "
            "referenced in correction_evidence_ids. Preserve regression_guards and every hunk not invalidated by that "
            "evidence. Do not search, run commands, edit tests, recall memory, or expand the enforced scope."
        )

    @staticmethod
    def _memory_section(context: RepairTaskContext) -> str:
        if not context.recalled_memories:
            return ""
        global_memories = tuple(
            memory for memory in context.recalled_memories if memory.scope_type == "GLOBAL"
        )
        repository_memories = tuple(
            memory for memory in context.recalled_memories if memory.scope_type != "GLOBAL"
        )

        def render(title: str, memories: tuple[RecalledMemory, ...], purpose: str) -> str:
            body = "\n\n".join(memory.prompt_text() for memory in memories) or "（未召回）"
            return f"### {title}\n仅用于：{purpose}\n\n{body}"

        memories = (
            render(
                "全局经验",
                global_memories,
                "问题机制、适用条件、通用修复步骤、验证思路和错误做法",
            )
            + "\n\n"
            + render(
                "仓库经验",
                repository_memories,
                "相关模块/API、调用关系、可复用工具函数、仓库特有契约和公开验证方法",
            )
        )[:10_000]
        return (
            "\n\n## Coordinator 可复用修复经验\n"
            "以下内容只供 Coordinator 规划使用，是历史成功案例的抽象假设；必须用当前源码重新确认适用性，"
            "不得原样转发给代码修改、失败归因或二次纠正阶段。确实采用某条经验时，在最终回答末尾"
            "输出 `MEMORY_USED: <memory-id>@<revision>`；未采用则不要输出标记。\n\n"
            f"{memories}"
        )


class TaskCancelledError(RuntimeError):
    pass


class AgentTimeoutError(RuntimeError):
    pass
