from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repo_agent.core.llm import DEFAULT_MODEL
from repo_agent.core.public_validation import evaluate_public_plan
from repo_agent.core.static_analysis import newly_undefined_names
from repo_agent.core.differential_validation import assess_differential_validation
from repo_agent.core.experience import (
    assess_lightweight_validation, required_edit_scope_issues, build_previous_failure_constraints,
)
from repo_agent.core.verification import (
    assess_verification_result,
    prepare_verification_command,
    verification_command_safety_reason,
    test_overlay_digest,
)


def main(argv: list[str] | None = None) -> int:
    """批量实验入口，并在启动前执行模式完整性检查。"""
    args = build_parser().parse_args(argv)
    # 学习模式必须用独立评测证明成功后才能写记忆；正式评测则冻结记忆。
    if args.repair_mode == "experience-build":
        if not args.evaluate:
            raise SystemExit("experience-build requires --evaluate so memory is written only after independent success")
        if not args.memory_provenance.strip():
            raise SystemExit("experience-build requires --memory-provenance")
    if args.repair_mode == "benchmark" and args.evolve_memory:
        raise SystemExit("benchmark mode is frozen/read-only; remove --evolve-memory")
    started = time.time()
    work_dir = args.work_dir.resolve()
    output_dir = args.output_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if args.env_file:
        env.update(read_env_file(args.env_file))
    if args.proxy:
        env.update(
            {
                "HTTP_PROXY": args.proxy,
                "HTTPS_PROXY": args.proxy,
                "http_proxy": args.proxy,
                "https_proxy": args.proxy,
            }
        )

    instances = load_instances(args.dataset, args.instance_id, args.limit, args.start_index)
    summaries: list[dict[str, Any]] = []
    predictions: list[dict[str, str]] = []
    for index, row in enumerate(instances, 1):
        instance_id = row["instance_id"]
        print(f"\n[{index}/{len(instances)}] {instance_id}", flush=True)
        instance_output = output_dir / instance_id
        instance_output.mkdir(parents=True, exist_ok=True)
        summary_path = instance_output / "summary.json"
        if args.skip_existing and summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            patch = (instance_output / "model.patch").read_text(encoding="utf-8", errors="replace")
            summaries.append(summary)
            predictions.append(prediction_for(instance_id, patch, args.model))
            print(f"  skipped existing: {summary.get('status')}", flush=True)
            continue

        repo_dir = work_dir / "repos" / instance_id
        summary = run_instance_once(row, repo_dir, instance_output, args, env)
        patch = summary.get("model_patch", "")
        (instance_output / "model.patch").write_text(patch, encoding="utf-8")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries.append(strip_large_fields(summary))
        predictions.append(prediction_for(instance_id, patch, args.model))
        print(
            f"  status={summary['status']} exit={summary.get('agent_exit_code')} "
            f"patch_chars={len(patch)} seconds={summary['seconds']:.1f}",
            flush=True,
        )

    predictions_path = output_dir / "predictions.json"
    predictions_path.write_text(json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8")
    run_summary = {
        "dataset": str(args.dataset),
        "model": args.model,
        "implement_model": args.implement_model or args.model,
        "reasoning_effort": args.reasoning_effort,
        "coordinator_reasoning_effort": args.coordinator_reasoning_effort,
        "implement_reasoning_effort": args.implement_reasoning_effort,
        "diagnostic_only": args.diagnostic_mode != "none",
        "diagnostic_mode": args.diagnostic_mode,
        "leaderboard_eligible": args.diagnostic_mode == "none" and args.repair_mode == "benchmark",
        "repair_mode": args.repair_mode,
        "evaluation_policy": {
            "agent_feedback": (
                f"{args.feedback_source}_failure_up_to_twice"
                if args.repair_mode == "experience-build"
                else "disabled"
            ),
            "max_corrections": 2 if args.repair_mode == "experience-build" else 0,
            "max_full_harness_runs_per_instance": (
                3 if args.repair_mode == "experience-build" and args.feedback_source == "harness" else 1
            ),
            "max_full_harness_runs_per_candidate": 1,
            "harness_feedback_to_agent": args.repair_mode == "experience-build" and args.feedback_source == "harness",
            "feedback_source": args.feedback_source,
            "prebuild_instance_image": should_prebuild_instance_image(args),
        },
        "count": len(instances),
        "seconds": round(time.time() - started, 3),
        "predictions_path": str(predictions_path),
        "stage_attribution": summarize_stage_attributions(summaries),
        "instances": summaries,
    }
    (output_dir / "batch_summary.json").write_text(json.dumps(run_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nPredictions: {predictions_path}", flush=True)
    print(f"Summary: {output_dir / 'batch_summary.json'}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RepoAgent over a SWE-bench pass@1 batch.")
    parser.add_argument("--dataset", required=True, type=Path, help="SWE-bench parquet file.")
    parser.add_argument("--repo-agent-bin", required=True, type=Path, help="Path to repo-agent executable.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--implement-model",
        help="Optional model override for Implement and corrective Implement Workers.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "max"),
        default="high",
        help="Reasoning effort for reasoning-enabled review Workers.",
    )
    parser.add_argument(
        "--coordinator-reasoning-effort",
        choices=("low", "medium", "high", "max"),
        default="max",
        help="Reasoning effort for the Coordinator itself.",
    )
    parser.add_argument(
        "--implement-reasoning-effort",
        choices=("low", "medium", "high", "max"),
        default="max",
        help="Reasoning effort for Implement and corrective Implement.",
    )
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, help="Optional .env file for model API settings.")
    parser.add_argument("--proxy", help="Optional HTTP(S) proxy for git clone/fetch.")
    parser.add_argument(
        "--offline-cache",
        action="store_true",
        help="Require an existing repository mirror and skip network fetches. Useful for reproducible cached runs.",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--instance-id", action="append", default=[], help="Explicit instance id. Repeatable.")
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument(
        "--repair-mode",
        choices=("benchmark", "experience-build"),
        default="benchmark",
        help="benchmark uses frozen dual memory and one patch; experience-build permits up to two feedback corrections and memory write.",
    )
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds per RepoAgent run.")
    parser.add_argument("--feedback-source", choices=("public-tests", "harness"), default="public-tests",
                        help="Learning-mode feedback source. public-tests uses baseline-generated tests; harness is a historical diagnostic path.")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Run the SWE-bench harness once for each final candidate patch.",
    )
    parser.add_argument("--swebench-dir", type=Path, help="SWE-bench checkout. Defaults to dataset parent.")
    parser.add_argument(
        "--harness-python",
        type=Path,
        help="Python interpreter containing SWE-bench and its dependencies. Defaults to the batch interpreter.",
    )
    parser.add_argument("--run-id", default="repoagent-batch", help="Base run_id for SWE-bench harness.")
    parser.add_argument("--harness-timeout", type=int, default=900, help="SWE-bench per-instance timeout.")
    parser.add_argument("--cache-level", default="env")
    parser.add_argument("--namespace", default="none")
    parser.add_argument("--docker-network", default="", help="Optional SWEBENCH_DOCKER_NETWORK value.")
    parser.add_argument(
        "--prebuild-instance-image",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Build and verify the SWE-bench instance image before Agent execution. "
            "Defaults to enabled for experience-build and disabled for benchmark."
        ),
    )
    parser.add_argument(
        "--image-build-timeout",
        type=int,
        default=3600,
        help="Maximum seconds allowed for prebuilding one SWE-bench instance image.",
    )
    parser.add_argument("--memory-root", type=Path, help="Optional RepoAgent memory root.")
    parser.add_argument(
        "--memory-scope",
        help="Optional shared memory scope. Supports {repo} and {instance_id}, for example swebench-lite:{repo}.",
    )
    parser.add_argument("--no-repair-memory", action="store_true", help="Disable repair_recipe recall in RepoAgent runs.")
    parser.add_argument(
        "--candidate-mode",
        choices=("auto", "required", "off"),
        default="off",
        help="Candidate-pool ablation policy. off is the simplified single-patch default.",
    )
    parser.add_argument(
        "--enable-test-expander",
        action="store_true",
        help="Diagnostic opt-in for LLM-generated public differential cases. Disabled in the normal simplified flow.",
    )
    parser.add_argument(
        "--full-agent-profile",
        action="store_true",
        help="Use the full RepoAgent maintainer profile instead of the slim SWE-bench profile.",
    )
    parser.add_argument(
        "--evolve-memory",
        action="store_true",
        help="After a resolved instance, run CaseCurator to save an abstract repair_recipe. Do not use for official scoring.",
    )
    parser.add_argument(
        "--memory-provenance",
        default="",
        help="Required for experience-build, for example swebench-dev. It is stored unchanged in memory metadata.",
    )
    parser.add_argument(
        "--diagnostic-mode",
        choices=("none", "baseline", "gold_files", "gold_loci", "corrective"),
        default="none",
        help="Diagnostic-only ablation. Any value other than none is not leaderboard-eligible.",
    )
    parser.add_argument(
        "--corrective-source-dir",
        type=Path,
        help="Baseline diagnostic output containing candidate.patch and harness failure logs.",
    )
    return parser


def load_instances(dataset: Path, instance_ids: list[str], limit: int, start_index: int) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise SystemExit("pandas is required to read SWE-bench parquet files. Run with the SWE-bench venv.") from exc

    df = pd.read_parquet(dataset)
    if instance_ids:
        wanted = set(instance_ids)
        rows = [row.to_dict() for _, row in df.iterrows() if row["instance_id"] in wanted]
        missing = sorted(wanted - {row["instance_id"] for row in rows})
        if missing:
            raise SystemExit(f"Instance ids not found in dataset: {missing}")
        return rows
    return [row.to_dict() for _, row in df.iloc[start_index : start_index + limit].iterrows()]


def run_instance_once(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    """执行一道题的完整状态机：checkout、推理、patch 校验、harness 与归因。

    benchmark 只产生一个最终 patch，再由 harness 评测，结果不会回流给 Agent；
    experience-build 才允许把首次失败送入 FailureReview，并最多执行一次纠正。
    """
    started = time.time()
    public_feedback = args.repair_mode == "experience-build" and getattr(args, "feedback_source", "harness") == "public-tests"
    instance_id = row["instance_id"]
    passes: list[dict[str, Any]] = []
    verification_image = ""
    image_lifecycle: dict[str, Any] = {
        "prebuild_requested": should_prebuild_instance_image(args),
        "prebuild": {},
        "cleanup": {},
    }
    try:
        checkout_repo(
            row["repo"],
            row["base_commit"],
            repo_dir,
            args.work_dir / "repo_cache",
            env,
            offline_cache=args.offline_cache,
        )
        pass_output = output_dir / "pass-1"
        pass_output.mkdir(parents=True, exist_ok=True)
        if image_lifecycle["prebuild_requested"]:
            image_lifecycle["prebuild"] = prebuild_swebench_instance_image(
                row,
                pass_output,
                args,
                env,
            )
            verification_image = str(image_lifecycle["prebuild"].get("image") or "")
        if args.diagnostic_mode == "corrective":
            if args.corrective_source_dir is None:
                raise ValueError("--corrective-source-dir is required for diagnostic corrective mode")
            pass_summary = run_corrective_diagnostic_pass(row, repo_dir, pass_output, args, env, 0)
        else:
            task = build_task(
                row,
                repo_dir,
                diagnostic_mode=args.diagnostic_mode,
                repair_mode=args.repair_mode,
            )
            pass_summary = run_agent_pass(
                row,
                repo_dir,
                pass_output,
                task,
                args,
                env,
                0,
                verification_image=verification_image,
            )
        patch = pass_summary.get("model_patch", "")
        candidate_patch = patch
        (output_dir / "candidate.patch").write_text(candidate_patch, encoding="utf-8")
        patch_validation = validate_model_patch(
            patch,
            repo_dir=repo_dir,
            agent_output_dir=pass_output,
            require_patch_review=args.full_agent_profile,
            reject_failed_verification=args.repair_mode != "experience-build",
            stage_attribution=(
                pass_summary.get("stage_attribution")
                if args.diagnostic_mode != "corrective"
                else None
            ),
        )
        pass_summary["patch_validation"] = patch_validation
        if patch_validation.get("discard_patch") and patch.strip():
            rejected_patch_path = pass_output / "rejected_model.patch"
            rejected_patch_path.write_text(patch, encoding="utf-8")
            pass_summary["rejected_model_patch_path"] = str(rejected_patch_path)
            pass_summary["discarded_patch_chars"] = len(patch)
            patch = ""
            pass_summary["model_patch"] = patch
            pass_summary["patch_chars"] = 0
        failed_patch_for_memory = ""
        feedback_file_for_memory: Path | None = None
        initial_harness: dict[str, Any] = {}
        correction_applied = False
        if public_feedback and args.diagnostic_mode != "corrective":
            patch = run_public_test_corrections(
                row, repo_dir, pass_output, task, patch, pass_summary, args, env,
                verification_image=verification_image,
            )
        if (
            args.repair_mode == "experience-build"
            and not public_feedback
            and args.evaluate
            and patch.strip()
            and not patch_validation.get("discard_patch")
        ):
            initial_harness = run_harness_for_instance(
                row,
                patch,
                pass_output / "initial-evaluation",
                args,
                env,
                0,
            )
            pass_summary["initial_harness"] = strip_large_fields(initial_harness)
            harness_feedback = build_experience_harness_feedback(initial_harness, patch)
            pass_summary["harness_feedback"] = strip_large_fields(harness_feedback)
            if harness_feedback.get("feedback_eligible"):
                failed_patch = patch
                correction = run_experience_correction(
                    row,
                    repo_dir,
                    pass_output / "experience-correction",
                    task,
                    failed_patch,
                    harness_feedback,
                    (
                        ((pass_summary.get("stage_attribution") or {}).get("stages") or {})
                        .get("implementation", {})
                        .get("report", {})
                    ),
                    args,
                    env,
                )
                corrected_patch = correction.get("model_patch", "")
                if correction.get("attempted"):
                    correction_validation = validate_model_patch(
                        corrected_patch,
                        repo_dir=repo_dir,
                        agent_output_dir=Path(correction["output_dir"]),
                        require_patch_review=False,
                        reject_failed_verification=False,
                        stage_attribution=correction.get("stage_attribution"),
                    )
                    correction["patch_validation"] = correction_validation
                    if (
                        corrected_patch.strip()
                        and corrected_patch != failed_patch
                        and not correction_validation.get("discard_patch")
                    ):
                        patch = corrected_patch
                        pass_summary["model_patch"] = corrected_patch
                        pass_summary["patch_chars"] = len(corrected_patch)
                        pass_summary["patch_validation"] = correction_validation
                        failed_patch_for_memory = failed_patch
                        feedback_file_for_memory = Path(correction["feedback_file"])
                        correction_applied = True
                    elif corrected_patch == failed_patch:
                        correction["no_effect"] = True
                    else:
                        correction["rejected"] = True
                        restore_worktree_patch(repo_dir, failed_patch)
                else:
                    correction["patch_validation"] = pass_summary.get("patch_validation") or {}
                pass_summary["experience_correction"] = strip_large_fields(correction)

        # 每个候选只评测一次；学习模式最多有两个纠正候选。
        if args.evaluate:
            final_patch_validation = pass_summary.get("patch_validation") or patch_validation
            if args.repair_mode == "experience-build" and initial_harness:
                if correction_applied:
                    pass_summary["harness"] = run_harness_for_instance(
                        row,
                        patch,
                        pass_output / "corrected-evaluation",
                        args,
                        env,
                        1,
                    )
                else:
                    pass_summary["harness"] = initial_harness
            elif final_patch_validation.get("discard_patch"):
                pass_summary["harness"] = skipped_harness_for_invalid_patch(final_patch_validation, 0)
                if args.diagnostic_mode != "none" and candidate_patch.strip():
                    pass_summary["diagnostic_candidate_harness"] = run_harness_for_instance(
                        row,
                        candidate_patch,
                        pass_output / "candidate-evaluation",
                        args,
                        env,
                        90,
                    )
            else:
                pass_summary["harness"] = run_harness_for_instance(row, patch, pass_output, args, env, 0)
                if args.diagnostic_mode != "none":
                    pass_summary["diagnostic_candidate_harness"] = pass_summary["harness"]

        if args.repair_mode == "experience-build" and correction_applied and not public_feedback:
            second_feedback = build_experience_harness_feedback(pass_summary["harness"], patch)
            second_feedback["previous_failure_constraints"] = build_previous_failure_constraints(
                harness_feedback, (pass_summary.get("experience_correction") or {}).get("feedback", {}),
                round_index=0,
            )
            if second_feedback.get("feedback_eligible"):
                previous_patch = patch
                previous_correction = pass_summary.get("experience_correction") or {}
                report_text = (previous_correction.get("worker_result") or {}).get("text", "{}")
                previous_report = extract_json_object(report_text) or {}
                correction2 = run_experience_correction(
                    row, repo_dir, pass_output / "experience-correction-2", task,
                    previous_patch, second_feedback, previous_report, args, env,
                )
                candidate2 = correction2.get("model_patch", "")
                if correction2.get("attempted"):
                    validation2 = validate_model_patch(
                        candidate2, repo_dir=repo_dir,
                        agent_output_dir=Path(correction2["output_dir"]),
                        require_patch_review=False, reject_failed_verification=False,
                        stage_attribution=correction2.get("stage_attribution"),
                    )
                    correction2["patch_validation"] = validation2
                    if (correction2.get("agent_exit_code", 0) == 0 and candidate2.strip()
                            and candidate2 != previous_patch and not validation2.get("discard_patch")):
                        patch = candidate2
                        pass_summary["model_patch"] = patch
                        pass_summary["patch_chars"] = len(patch)
                        pass_summary["patch_validation"] = validation2
                        failed_patch_for_memory = previous_patch
                        feedback_file_for_memory = Path(correction2["feedback_file"])
                        pass_summary["second_harness"] = pass_summary["harness"]
                        pass_summary["harness"] = run_harness_for_instance(
                            row, patch, pass_output / "corrected-evaluation-2", args, env, 2,
                        )
                    else:
                        correction2["no_effect"] = candidate2 == previous_patch
                        correction2["rejected"] = candidate2 != previous_patch
                        restore_worktree_patch(repo_dir, previous_patch)
                pass_summary["experience_correction_2"] = strip_large_fields(correction2)

        if (
            args.repair_mode == "experience-build"
            and not public_feedback
            and (pass_summary.get("harness") or {}).get("resolved")
        ):
            pass_summary["experience_curator"] = run_experience_curator_for_success(
                row,
                repo_dir,
                pass_output,
                task=task,
                failed_patch=failed_patch_for_memory,
                successful_patch=patch,
                feedback_file=feedback_file_for_memory,
                validation=pass_summary.get("harness") or {},
                args=args,
                env=env,
            )
        if args.evolve_memory and not public_feedback and (pass_summary.get("harness") or {}).get("resolved"):
            pass_summary["case_curator"] = run_case_curator_for_success(row, repo_dir, pass_output, pass_summary, args, env)
        pass_summary["stage_attribution"] = finalize_stage_attribution(pass_summary)
        (pass_output / "stage_attribution.json").write_text(
            json.dumps(pass_summary["stage_attribution"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        passes.append(strip_large_fields(pass_summary))
        status = passes[-1].get("status", "agent_ok") if passes else "agent_ok"
        final_validation = passes[-1].get("patch_validation", {}) if passes else {}
        if status == "agent_ok" and final_validation.get("invalid") and not (passes[-1].get("harness") or {}).get("resolved"):
            status = "invalid_patch"
        agent_exit_code = passes[-1].get("agent_exit_code") if passes else None
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        agent_exit_code = None
        (output_dir / "agent_stdout.txt").write_text(
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 - batch runner should capture per-instance failures.
        status = "runner_error"
        agent_exit_code = None
        (output_dir / "agent_stdout.txt").write_text(
            f"{type(exc).__name__}: {exc}",
            encoding="utf-8",
            errors="replace",
        )

    if verification_image and args.cache_level in {"none", "base", "env"}:
        image_lifecycle["cleanup"] = cleanup_swebench_instance_image(
            verification_image,
            output_dir,
            env,
        )
    raw_patch = git_diff(repo_dir) if repo_dir.exists() else ""
    final_validation = passes[-1].get("patch_validation", {}) if passes else {}
    patch = "" if final_validation.get("discard_patch") else raw_patch
    api_errors = collect_api_errors(output_dir)
    if api_errors and not any((p.get("harness") or {}).get("resolved") for p in passes):
        status = "api_error"
    return {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "status": status,
        "api_errors": api_errors,
        "agent_exit_code": agent_exit_code,
        "seconds": round(time.time() - started, 3),
        "patch_chars": len(patch),
        "candidate_patch_chars": len(raw_patch),
        "model_patch": patch,
        "output_dir": str(output_dir),
        "passes": passes,
        "official_pass1": args.diagnostic_mode == "none" and args.repair_mode == "benchmark",
        "official_no_hints": args.diagnostic_mode in {"none", "baseline"},
        "diagnostic_only": args.diagnostic_mode != "none" or args.repair_mode != "benchmark",
        "diagnostic_mode": args.diagnostic_mode,
        "repair_mode": args.repair_mode,
        "verification_image_lifecycle": image_lifecycle,
        "resolved": any((pass_data.get("harness") or {}).get("resolved") for pass_data in passes),
        "diagnostic_candidate_resolved": any(
            (pass_data.get("diagnostic_candidate_harness") or {}).get("resolved") for pass_data in passes
        ),
    }


def api_error_type(stdout: str) -> str | None:
    # 只识别异常行，避免把任务或模型输出中提及的 API 错误当作真实故障。
    if re.search(r"(?m)^repo_agent\.core\.llm\.LLMStreamInterruptedError:", stdout):
        return "LLMStreamInterruptedError"
    if re.search(r"(?m)^RuntimeError: Incomplete LLM stream: finish_reason=network_error\s*$", stdout):
        return "LLMStreamInterruptedError"
    match = re.search(
        r"(?m)^openai\.(APITimeoutError|APIConnectionError|RateLimitError|"
        r"AuthenticationError|PermissionDeniedError|InternalServerError|"
        r"BadRequestError|NotFoundError|UnprocessableEntityError|APIStatusError):",
        stdout,
    )
    return match.group(1) if match else None


def agent_process_status(result: subprocess.CompletedProcess) -> str:
    if result.returncode == 0:
        return "agent_ok"
    if re.search(r"(?m)^repo_agent\.core\.llm\.LLMOutputLimitError:", result.stdout or ""):
        return "output_limit"
    return "api_error" if api_error_type(result.stdout or "") else "agent_error"


def collect_api_errors(output_dir: Path) -> list[dict[str, str]]:
    errors = []
    for path in sorted(output_dir.rglob("*.txt")):
        if "stdout" not in path.name:
            continue
        error = api_error_type(path.read_text(encoding="utf-8", errors="replace"))
        if error:
            errors.append({"type": error, "log_path": str(path),
                           "stage": str(path.parent.relative_to(output_dir))})
    return errors


def run_agent_pass(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    task: str,
    args: argparse.Namespace,
    env: dict[str, str],
    round_index: int,
    *,
    verification_image: str = "",
) -> dict[str, Any]:
    started = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    agent_task = task
    (output_dir / "task.md").write_text(agent_task, encoding="utf-8")
    command = [
        str(args.repo_agent_bin),
        "run",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--coordinator",
        "--swebench-profile",
        "--max-steps",
        str(args.max_steps),
        "--output-dir",
        str(output_dir),
        "--candidate-mode",
        args.candidate_mode,
        "--repair-mode",
        args.repair_mode,
        "--reasoning-effort",
        args.reasoning_effort,
        "--coordinator-reasoning-effort",
        args.coordinator_reasoning_effort,
        "--implement-reasoning-effort",
        args.implement_reasoning_effort,
        "--task",
        agent_task,
    ]
    if args.repair_mode == "experience-build" and getattr(args, "feedback_source", "harness") == "public-tests":
        command.append("--public-validation")
    if args.implement_model:
        command.extend(["--implement-model", args.implement_model])
    forced_scope = diagnostic_scope_for_row(row, repo_dir, args.diagnostic_mode)
    if forced_scope:
        forced_scope_path = output_dir / "forced_scope.json"
        forced_scope_path.write_text(json.dumps(forced_scope, ensure_ascii=False, indent=2), encoding="utf-8")
        command.extend(["--forced-scope-file", str(forced_scope_path)])
    if args.full_agent_profile:
        command.remove("--swebench-profile")
    add_memory_cli_args(command, row, args)
    agent_env = env.copy()
    if verification_image:
        agent_env["REPO_AGENT_VERIFY_DOCKER_IMAGE"] = verification_image
    else:
        agent_env.pop("REPO_AGENT_VERIFY_DOCKER_IMAGE", None)
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
        env=agent_env,
    )
    (output_dir / "agent_stdout.txt").write_text(result.stdout, encoding="utf-8", errors="replace")
    patch = git_diff(repo_dir)
    (output_dir / "model.patch").write_text(patch, encoding="utf-8")
    stage_attribution = read_json_file(output_dir / "stage_attribution.json")
    return {
        "round": round_index,
        "instance_id": row["instance_id"],
        "status": agent_process_status(result),
        "agent_exit_code": result.returncode,
        "seconds": round(time.time() - started, 3),
        "patch_chars": len(patch),
        "model_patch": patch,
        "output_dir": str(output_dir),
        "stage_attribution": stage_attribution,
    }


def swebench_eval_image(instance_id: str) -> str:
    """返回 SWE-bench harness 使用的单实例 eval 镜像名。"""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", instance_id):
        raise ValueError(f"Invalid SWE-bench instance_id: {instance_id}")
    return f"sweb.eval.x86_64.{instance_id}:latest"


def should_prebuild_instance_image(args: argparse.Namespace) -> bool:
    """模式一默认预构建；模式二保持原有只在最终 harness 构建的行为。"""

    configured = getattr(args, "prebuild_instance_image", None)
    if configured is not None:
        return bool(configured)
    return getattr(args, "repair_mode", "benchmark") == "experience-build"


def prebuild_swebench_instance_image(
    row: dict[str, Any],
    output_dir: Path,
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    """用 harness Python 预构建并确认单实例镜像，不把测试元数据暴露给 Agent。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "verification_image.json"
    stdout_path = output_dir / "verification_image_build.log"
    harness_python = str(getattr(args, "harness_python", None) or sys.executable)
    command = [
        harness_python,
        str(PROJECT_ROOT / "scripts" / "prebuild_swebench_image.py"),
        "--dataset",
        str(args.dataset),
        "--instance-id",
        str(row["instance_id"]),
        "--namespace",
        str(args.namespace),
        "--result-file",
        str(result_path),
    ]
    build_env = dict(env)
    if args.docker_network:
        build_env["SWEBENCH_DOCKER_NETWORK"] = args.docker_network
        build_env["SWEBENCH_DOCKER_BUILD_NETWORK"] = args.docker_network
    if args.proxy:
        build_env["SWEBENCH_GIT_PROXY"] = args.proxy
    started = time.time()
    result = subprocess.run(
        command,
        cwd=(args.swebench_dir or args.dataset.parent).resolve(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=int(getattr(args, "image_build_timeout", 3600)),
        check=False,
        env=build_env,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    payload = read_json_file(result_path)
    payload.update(
        {
            "exit_code": result.returncode,
            "seconds": payload.get("seconds", round(time.time() - started, 3)),
            "stdout_path": str(stdout_path),
        }
    )
    expected_image = (
        swebench_eval_image(str(row["instance_id"]))
        if str(args.namespace).strip().lower() in {"", "none"}
        else str(payload.get("image") or "")
    )
    if (
        result.returncode != 0
        or payload.get("status") != "ready"
        or payload.get("image") != expected_image
    ):
        detail = payload.get("error") or result.stdout[-2_000:] or "unknown image build failure"
        raise RuntimeError(f"SWE-bench verification image is not ready: {detail}")
    return payload


def cleanup_swebench_instance_image(
    image: str,
    output_dir: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """任务结束后删除 instance 层；共享 base/env 层由 Docker 缓存保留。"""

    result = subprocess.run(
        ["docker", "image", "rm", image],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
        env=env,
    )
    payload = {
        "attempted": True,
        "image": image,
        "removed": result.returncode == 0 or "No such image" in result.stdout,
        "exit_code": result.returncode,
        "output": result.stdout[-2_000:],
    }
    (output_dir / "verification_image_cleanup.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def run_corrective_diagnostic_pass(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    args: argparse.Namespace,
    env: dict[str, str],
    round_index: int,
) -> dict[str, Any]:
    """直接运行一次 corrective Implement Worker，不重新经过 Coordinator。"""

    started = time.time()
    source_dir = args.corrective_source_dir.resolve() / row["instance_id"]
    source_summary_path = source_dir / "summary.json"
    source_patch_path = source_dir / "candidate.patch"
    if not source_summary_path.exists() or not source_patch_path.exists():
        raise FileNotFoundError(f"Missing corrective source artifacts under {source_dir}")
    source_summary = json.loads(source_summary_path.read_text(encoding="utf-8"))
    source_patch = source_patch_path.read_text(encoding="utf-8", errors="replace")
    if not source_patch.strip():
        return {
            "round": round_index,
            "instance_id": row["instance_id"],
            "status": "diagnostic_source_patch_missing",
            "agent_exit_code": None,
            "seconds": round(time.time() - started, 3),
            "patch_chars": 0,
            "model_patch": "",
            "output_dir": str(output_dir),
            "stage_attribution": {
                "schema_version": 1,
                "pipeline": "diagnostic_corrective",
                "pre_harness_attribution": "source_patch_missing",
                "submission_ready": False,
                "token_usage": {"coordinator": 0, "workers": 0, "total": 0},
            },
        }

    applied = subprocess.run(
        ["git", "-C", str(repo_dir), "apply", "--whitespace=nowarn", "-"],
        input=source_patch,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(f"Could not apply diagnostic source patch: {applied.stdout[-2000:]}")

    source_passes = source_summary.get("passes") or []
    source_pass = source_passes[-1] if source_passes else {}
    source_harness = source_pass.get("diagnostic_candidate_harness") or source_pass.get("harness") or {}
    failure_excerpt = str(source_harness.get("failure_excerpt") or "")
    failure_attribution = build_corrective_failure_attribution(
        source_patch,
        failure_excerpt,
        failure_facts=source_harness.get("failure_facts") or {},
        instance_report=source_harness.get("instance_report") or {},
    )
    constraints = corrective_constraints_from_patch(
        source_patch,
        repo_dir,
        problem_statement=str(row["problem_statement"]),
    )
    task = build_corrective_task(row, source_patch, failure_excerpt, failure_attribution)
    task_path = output_dir / "task.md"
    constraints_path = output_dir / "constraints.json"
    task_path.write_text(task, encoding="utf-8")
    constraints_path.write_text(json.dumps(constraints, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        str(args.repo_agent_bin),
        "corrective-implement",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--output-dir",
        str(output_dir),
        "--max-steps",
        str(args.max_steps),
        "--swebench-profile",
        "--task-file",
        str(task_path),
        "--constraints-file",
        str(constraints_path),
    ]
    if args.implement_model:
        command.extend(["--implement-model", args.implement_model])
    add_reasoning_cli_args(command, args)
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
        env=env,
    )
    (output_dir / "agent_stdout.txt").write_text(result.stdout, encoding="utf-8", errors="replace")
    patch = git_diff(repo_dir)
    (output_dir / "model.patch").write_text(patch, encoding="utf-8")
    worker_result = read_json_file(output_dir / "worker_result.json")
    worker_tokens = int(worker_result.get("total_tokens") or 0)
    return {
        "round": round_index,
        "instance_id": row["instance_id"],
        "status": agent_process_status(result),
        "agent_exit_code": result.returncode,
        "seconds": round(time.time() - started, 3),
        "patch_chars": len(patch),
        "model_patch": patch,
        "output_dir": str(output_dir),
        "diagnostic_source_patch_chars": len(source_patch),
        "diagnostic_failure_log_chars": len(failure_excerpt),
        "worker_result": worker_result,
        "stage_attribution": {
            "schema_version": 1,
            "pipeline": "diagnostic_corrective",
            "pre_harness_attribution": "awaiting_harness" if patch.strip() else "implementation_delivery",
            "submission_ready": False,
            "stages": {
                "corrective_implementation": {
                    "attempts": 1,
                    "completed": bool(worker_result.get("completed")),
                    "stop_reason": worker_result.get("stop_reason") or "",
                    "allowed_files": constraints["allowed_files"],
                    "allowed_ranges": constraints["allowed_ranges"],
                    "failure_log_present": bool(failure_excerpt.strip()),
                    "failure_attribution": failure_attribution,
                }
            },
            "token_usage": {"coordinator": 0, "workers": worker_tokens, "total": worker_tokens},
        },
    }


def lightweight_validation_from_attribution(stage_attribution: dict[str, Any]) -> dict[str, Any]:
    """从 pass@1 轨迹提取 patch 前后同命令的公开行为差分。"""

    stages = stage_attribution.get("stages") or {}
    reproduction = stages.get("reproduction") or {}
    focused = stages.get("focused_verification") or {}
    baseline = dict(reproduction.get("baseline_execution") or {})
    patched = dict(focused.get("result") or {})
    if baseline and "command" not in baseline:
        baseline["command"] = baseline.get("requested_command") or ""
    validation = assess_lightweight_validation(baseline, patched).to_dict()
    # FailureReview 只能消费已经由控制器注册并在基线执行过的语义证据，
    # 不能把模型临时猜测或最终 harness 信息当作直接证据。
    semantic_evidence = reproduction.get("contract")
    if isinstance(semantic_evidence, dict) and semantic_evidence:
        validation["semantic_evidence"] = semantic_evidence
    return validation


def should_run_experience_differential(
    validation: dict[str, Any],
    *,
    enabled: bool = False,
) -> bool:
    """Test Expander 仅供显式诊断实验使用，正常修复流程默认不支付该成本。"""

    if not enabled:
        return False

    if validation.get("status") == "fixed":
        return True
    if validation.get("status") != "invalid_reproducer":
        return False
    baseline = validation.get("baseline") or {}
    command = str(
        baseline.get("command")
        or baseline.get("requested_command")
        or validation.get("command")
        or ""
    ).strip()
    if not command:
        # 没有公开命令是证据缺口，不是环境失败。Experience-build 可以让
        # Test Expander 根据公开题意、当前 diff 和局部源码生成一次差分计划。
        return baseline.get("attempted") is False
    if verification_command_safety_reason(command):
        return False
    if re.search(r"\bimport\s+(?:subprocess|socket|requests|urllib)\b", command):
        return False
    if replaceable_invalid_reproducer(baseline):
        return True
    assessment = assess_verification_result(baseline)
    return assessment.get("classification") == "behavior_passed"


def replaceable_invalid_reproducer(baseline: dict[str, Any]) -> bool:
    """区分验证构造错误与真正无法执行的运行环境。"""

    if baseline.get("attempted") is False:
        return False
    if str(baseline.get("status") or "") == "blocked_by_environment":
        return False
    evidence = str(
        baseline.get("evidence")
        or baseline.get("output")
        or baseline.get("stderr")
        or ""
    ).lower()
    environment_markers = (
        "docker daemon",
        "no such image",
        "cannot connect to the docker",
        "connection timed out",
        "temporary failure in name resolution",
        "lightweight feedback command timed out",
    )
    return not any(marker in evidence for marker in environment_markers)


def correction_rejected_invalid_validation(correction: dict[str, Any]) -> bool:
    """识别只否定验证本身、尚未消耗代码纠正机会的审查结果。"""

    feedback = correction.get("feedback") or {}
    return (
        correction.get("attempted") is False
        and correction.get("route") == "none"
        and correction.get("failure_stage") == "invalid_test"
        and feedback.get("correction_readiness") == "not_needed"
    )


def run_experience_differential_validation(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    task: str,
    candidate_patch: str,
    initial_validation: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    verification_image: str = "",
) -> dict[str, Any]:
    """初始验证已修复或无区分力时，用公开差分输入检查语义边界。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    task_path = output_dir / "task.md"
    patch_path = output_dir / "candidate.patch"
    validation_path = output_dir / "initial_validation.json"
    task_path.write_text(task, encoding="utf-8")
    patch_path.write_text(candidate_patch, encoding="utf-8")
    validation_path.write_text(
        json.dumps(initial_validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    design_dir = output_dir / "test-expander"
    command = [
        str(args.repo_agent_bin),
        "design-differential-tests",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--output-dir",
        str(design_dir),
        "--task-file",
        str(task_path),
        "--patch-file",
        str(patch_path),
        "--validation-file",
        str(validation_path),
    ]
    add_memory_cli_args(command, row, args)
    add_reasoning_cli_args(command, args)
    started = time.time()
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=min(int(args.timeout), 900),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "validation_kind": "differential_trace",
            "status": "test_expander_timeout",
            "feedback_eligible": False,
            "reason": str(exc),
            "seconds": round(time.time() - started, 3),
        }
    (output_dir / "test_expander_stdout.txt").write_text(
        result.stdout,
        encoding="utf-8",
        errors="replace",
    )
    plan = read_json_file(design_dir / "differential_test_plan.json")
    if result.returncode != 0 or plan.get("parse_error") or not plan.get("command"):
        return {
            "validation_kind": "differential_trace",
            "status": "invalid_differential_plan",
            "feedback_eligible": False,
            "reason": "Test Expander did not produce a valid public differential command",
            "test_expander_exit_code": result.returncode,
            "test_expander": read_json_file(design_dir / "worker_result.json"),
            "stdout_excerpt": result.stdout[-2_000:],
            "seconds": round(time.time() - started, 3),
        }

    probe_command = str(plan["command"])
    baseline = run_lightweight_feedback_command(
        row,
        repo_dir,
        probe_command,
        args,
        env,
        verification_image=verification_image,
        apply_host_patch=False,
    )
    patched = run_lightweight_feedback_command(
        row,
        repo_dir,
        probe_command,
        args,
        env,
        verification_image=verification_image,
        apply_host_patch=True,
    )
    assessment = assess_differential_validation(
        plan=plan,
        baseline=baseline,
        patched=patched,
    )
    assessment.update(
        {
            "test_expander_exit_code": result.returncode,
            "test_expander": read_json_file(design_dir / "worker_result.json"),
            "seconds": round(time.time() - started, 3),
        }
    )
    return assessment


def run_lightweight_feedback_command(
    row: dict[str, Any],
    repo_dir: Path,
    command: str,
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    verification_image: str = "",
    apply_host_patch: bool = True,
    public_test_overlay: Path | None = None,
) -> dict[str, Any]:
    """在现有 SWE-bench eval 镜像中重跑同一条轻量公开命令，不启动完整 harness。"""

    command = command.strip()
    unsafe = verification_command_safety_reason(command)
    if not command or unsafe:
        return {
            "attempted": False,
            "status": "blocked_by_environment",
            "command": command,
            "exit_code": None,
            "evidence": unsafe or "missing feedback command",
        }
    prepared, runtime = prepare_verification_command(
        command,
        repo_dir,
        docker_image=verification_image or swebench_eval_image(row["instance_id"]),
        apply_host_patch=apply_host_patch,
        test_overlay=public_test_overlay,
    )
    try:
        result = subprocess.run(
            prepared,
            cwd=repo_dir,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=min(int(args.timeout), 300),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True,
            "status": "blocked_by_environment",
            "command": command,
            "execution_command": prepared,
            "runtime": runtime,
            "exit_code": None,
            "evidence": f"lightweight feedback command timed out: {exc}",
        }
    return {
        "attempted": True,
        "status": "passed" if result.returncode == 0 else "failed",
        "command": command,
        "execution_command": prepared,
        "runtime": runtime,
        "exit_code": result.returncode,
        "evidence": result.stdout[-4_000:],
        "test_files_sha256": test_overlay_digest(public_test_overlay),
    }


def run_public_test_corrections(
    row, repo_dir, pass_output, task, patch, pass_summary, args, env, *, verification_image="",
):
    """固定公开用例驱动纠正；官方评测在调用方最后执行，不参与选择或学习。"""
    artifact = read_json_file(pass_output / "public_validation.json")
    plan = artifact.get("plan") or {"status": "unavailable", "cases": [], "unknowns": ["No public validation plan"]}
    baseline = artifact.get("baseline") or {}
    test_overlay = Path(artifact["test_files_dir"]) if artifact.get("test_files_dir") else None

    def evaluate():
        return evaluate_public_plan(plan, baseline, lambda command: run_lightweight_feedback_command(
            row, repo_dir, command, args, env, verification_image=verification_image,
            public_test_overlay=test_overlay,
        ))

    validation = artifact.get("validation") or evaluate()
    pass_summary["initial_public_validation"] = validation
    retained_patch, retained_validation = patch, validation
    retained_check = pass_summary.get("patch_validation", {})

    def passing_cases(result):
        return {case["id"] for case in result.get("cases", [])
                if case.get("usable") and case.get("patched_classification") == "behavior_passed"}

    failed_patch = ""
    feedback_file = None
    report = (((pass_summary.get("stage_attribution") or {}).get("stages") or {})
              .get("implementation", {}).get("report", {}))
    for index in range(2):
        if not patch.strip() or not validation.get("feedback_eligible"):
            break
        correction = run_experience_correction(
            row, repo_dir, pass_output / f"public-correction-{index + 1}", task,
            patch, validation, report, args, env,
        )
        pass_summary.setdefault("public_corrections", []).append(strip_large_fields(correction))
        candidate = correction.get("model_patch", "")
        if not correction.get("attempted") or not candidate.strip() or candidate == patch:
            break
        check = validate_model_patch(candidate, repo_dir=repo_dir,
                                     agent_output_dir=Path(correction["output_dir"]),
                                     require_patch_review=False, reject_failed_verification=False,
                                     stage_attribution=correction.get("stage_attribution"))
        if check.get("discard_patch"):
            restore_worktree_patch(repo_dir, patch)
            break
        failed_patch, patch = patch, candidate
        report = (((correction.get("stage_attribution") or {}).get("stages") or {})
                  .get("implementation", {}).get("report", {}))
        feedback_file = Path(correction["feedback_file"])
        pass_summary.update(model_patch=patch, patch_chars=len(patch), patch_validation=check)
        validation = evaluate()
        # 仍允许下一轮修正新回归；预算结束时保留没有丢失已通过公开用例的版本。
        previous_passing = passing_cases(retained_validation)
        candidate_passing = passing_cases(validation)
        previous_usable = {case["id"] for case in retained_validation.get("cases", []) if case.get("usable")}
        candidate_usable = {case["id"] for case in validation.get("cases", []) if case.get("usable")}
        # 固定用例中已有的基线缺口不应抹掉全部可信用例均通过的改进。
        improved_inconclusive = (
            validation.get("status") == "inconclusive"
            and previous_usable | candidate_usable <= candidate_passing
            and bool(previous_usable - previous_passing)
        )
        if (validation.get("status") in {"passed", "failed"} or improved_inconclusive) and previous_passing <= candidate_passing:
            retained_patch, retained_validation, retained_check = patch, validation, check
    if patch != retained_patch:
        pass_summary["public_regression_rollback"] = {
            "lost_passing_cases": sorted(passing_cases(retained_validation) - passing_cases(validation)),
            "discarded_validation": validation,
        }
        restore_worktree_patch(repo_dir, retained_patch)
        patch, validation = retained_patch, retained_validation
        pass_summary.update(model_patch=patch, patch_chars=len(patch), patch_validation=retained_check)
    pass_summary["public_validation"] = validation
    (pass_output / "public_validation_final.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if patch.strip() and validation.get("status") == "passed":
        pass_summary["experience_curator"] = run_experience_curator_for_success(
            row, repo_dir, pass_output, task=task, failed_patch=failed_patch,
            successful_patch=patch, feedback_file=feedback_file, validation=validation,
            args=args, env=env,
        )
    return patch


def run_experience_correction(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    task: str,
    failed_patch: str,
    lightweight_validation: dict[str, Any],
    implementation_report: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    """执行一个独立的“失败证据 -> 归因 -> 修正”回合，次数由调用方限制。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    task_path = output_dir / "task.md"
    failed_patch_path = output_dir / "failed.patch"
    failure_log_path = output_dir / "failure.log"
    validation_path = output_dir / "failed_validation.json"
    implementation_report_path = output_dir / "implementation_report.json"
    task_path.write_text(task, encoding="utf-8")
    failed_patch_path.write_text(failed_patch, encoding="utf-8")
    failure_log_path.write_text(
        str(
            lightweight_validation.get("failure_evidence")
            or (lightweight_validation.get("patched") or {}).get("evidence")
            or ""
        ),
        encoding="utf-8",
    )
    validation_path.write_text(
        json.dumps(lightweight_validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    implementation_report_path.write_text(
        json.dumps(implementation_report or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    review_dir = output_dir / "failure-review"
    review_command = [
        str(args.repo_agent_bin),
        "review-failure",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--output-dir",
        str(review_dir),
        "--task-file",
        str(task_path),
        "--patch-file",
        str(failed_patch_path),
        "--failure-log-file",
        str(failure_log_path),
        "--validation-file",
        str(validation_path),
        "--implementation-report-file",
        str(implementation_report_path),
    ]
    add_memory_cli_args(review_command, row, args)
    add_reasoning_cli_args(review_command, args)
    review = subprocess.run(
        review_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=min(args.timeout, 900),
        check=False,
        env=env,
    )
    (review_dir / "review_stdout.txt").write_text(
        review.stdout,
        encoding="utf-8",
        errors="replace",
    )
    feedback_file = review_dir / "failure_feedback.json"
    raw_feedback = read_json_file(feedback_file)
    gated_feedback = enforce_differential_relation_gate(
        raw_feedback,
        lightweight_validation,
        failed_patch,
    )
    if gated_feedback != raw_feedback:
        feedback_file = review_dir / "controller_failure_feedback.json"
        feedback_file.write_text(
            json.dumps(gated_feedback, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raw_feedback = gated_feedback
    correction_route = validated_failure_feedback_route(raw_feedback)
    feedback = raw_feedback if correction_route is not None else {
        "failure_stage": "insufficient_evidence",
        "correction_readiness": "insufficient_evidence",
        "correction_route": "none",
        "parse_error": True,
        "raw": raw_feedback,
    }
    failure_stage = str(feedback.get("failure_stage") or "insufficient_evidence")
    correction_route = correction_route or "none"

    review_error = api_error_type(review.stdout or "") if review.returncode else None
    if review_error:
        return {"status": "api_error", "api_error": review_error, "model_patch": failed_patch,
                "patch_chars": len(failed_patch), "output_dir": str(output_dir),
                "route": "none", "attempted": False, "failure_stage": "api_error",
                "failure_review_exit_code": review.returncode, "feedback_file": str(feedback_file)}

    started = time.time()
    route = "none"
    attempted = False
    correction_summary: dict[str, Any]
    if correction_route in {"corrective_implement", "coordinator_replan"}:
        route = correction_route
        attempted = True
        correction_summary = run_inline_experience_corrective(
            row,
            repo_dir,
            output_dir / "corrective-implement",
            task,
            failed_patch,
            feedback,
            args,
            env,
        )
    else:
        correction_summary = {
            "status": "feedback_not_actionable",
            "model_patch": failed_patch,
            "patch_chars": len(failed_patch),
            "output_dir": str(output_dir),
            "stage_attribution": {"pre_harness_attribution": failure_stage},
        }

    correction_summary.update(
        {
            "seconds": round(time.time() - started, 3),
            "route": route,
            "attempted": attempted,
            "failure_stage": failure_stage,
            "feedback": feedback,
            "feedback_file": str(feedback_file),
            "failure_review_exit_code": review.returncode,
        }
    )
    return correction_summary


def build_experience_harness_feedback(
    harness: dict[str, Any],
    failed_patch: str,
) -> dict[str, Any]:
    """把首次 harness 结果压缩成模式一唯一允许消费的失败反馈。"""

    failure_excerpt = str(harness.get("failure_excerpt") or "").strip()
    failure_facts = dict(harness.get("failure_facts") or {})
    instance_report = harness.get("instance_report") or {}
    test_counts = _harness_test_counts(instance_report)
    environment_contamination = dict(harness.get("environment_contamination") or {})
    failed_tests = test_counts["fail_to_pass_failed"] + test_counts["pass_to_pass_failed"]
    unresolved = bool(harness.get("unresolved")) or failed_tests > 0
    feedback_eligible = bool(
        unresolved
        and not harness.get("resolved")
        and not harness.get("error")
        and not environment_contamination.get("contaminated")
        and failure_excerpt
    )
    if harness.get("resolved"):
        reason = "the initial harness resolved the instance"
    elif harness.get("error"):
        reason = "harness infrastructure errors are not code feedback"
    elif environment_contamination.get("contaminated"):
        reason = "target behavior passed; remaining failures are harness dependency/toolchain contamination"
    elif not unresolved:
        reason = "the harness did not report an unresolved behavioral failure"
    elif not failure_excerpt:
        reason = "the harness reported unresolved without an actionable failure log"
    else:
        reason = "the initial harness produced direct unresolved failure evidence"
    return {
        "validation_kind": "swebench_harness_feedback",
        "status": "failed" if feedback_eligible else "not_actionable",
        "feedback_eligible": feedback_eligible,
        "reason": reason,
        "failure_evidence": failure_excerpt,
        "failure_facts": failure_facts,
        "environment_contamination": environment_contamination,
        "test_counts": test_counts,
        "failure_attribution": build_corrective_failure_attribution(
            failed_patch,
            failure_excerpt,
            failure_facts=failure_facts,
            instance_report=instance_report,
        ),
    }


def restore_worktree_patch(repo_dir: Path, patch: str) -> None:
    """纠正候选无效时恢复初始候选，避免工作树和最终 prediction 不一致。"""

    reset = subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if reset.returncode != 0:
        raise RuntimeError(f"Could not restore repository baseline: {reset.stdout[-2000:]}")
    if not patch.strip():
        return
    applied = subprocess.run(
        ["git", "-C", str(repo_dir), "apply", "--whitespace=nowarn", "-"],
        input=patch,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(f"Could not restore initial patch: {applied.stdout[-2000:]}")


def differential_complete_gate_violations(
    validation: dict[str, Any],
) -> list[str]:
    """只返回能由 exact/invariant oracle 直接证明的候选错误。"""

    if validation.get("validation_kind") != "differential_trace":
        return []
    plan = validation.get("test_plan") or {}
    violations: list[str] = []
    matrix = {
        str(item.get("case") or ""): item
        for item in validation.get("case_matrix") or []
        if isinstance(item, dict)
    }
    for case in plan.get("cases") or []:
        case_id = str(case.get("id") or "")
        relation = str(case.get("expected_relation") or "observe")
        result = matrix.get(case_id)
        if result is None:
            violations.append(f"{case_id}: declared case has no comparable output")
            continue
        oracle_strength = str(
            result.get("oracle_strength")
            or case.get("oracle_strength")
            or ("invariant" if relation == "must_preserve" else "differential_only")
        )
        if oracle_strength == "exact" and result.get("oracle_passed") is False:
            violations.append(
                f"{case_id}: patched observation does not match the exact public oracle"
            )
        elif oracle_strength == "invariant" and result.get("changed"):
            violations.append(f"{case_id}: expected invariant behavior but candidate changed")
    for item in plan.get("guard_coverage") or []:
        obligation_id = str(item.get("obligation_id") or "")
        relation = str(item.get("required_relation") or "observe")
        cases = [matrix.get(str(case_id)) for case_id in item.get("case_ids") or []]
        cases = [case for case in cases if case is not None]
        if not cases:
            violations.append(f"{obligation_id}: no executable mapped case")
            continue
        if relation == "must_preserve" and not all(
            not case.get("changed") for case in cases
        ):
            violations.append(f"{obligation_id}: candidate changed a preserve boundary")
    return violations


def differential_oracle_gaps(validation: dict[str, Any]) -> list[str]:
    """返回无法据此宣布 complete、也不能据此纠正代码的弱证据。"""

    if validation.get("validation_kind") != "differential_trace":
        return []
    gaps = [str(value) for value in validation.get("oracle_gaps") or [] if str(value)]
    plan = validation.get("test_plan") or {}
    matrix = {
        str(row.get("case") or ""): row
        for row in validation.get("case_matrix") or []
        if isinstance(row, dict)
    }
    declared = list(plan.get("cases") or [])
    declared_ids = {str(case.get("id") or "") for case in declared if isinstance(case, dict)}
    for case in declared:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "")
        row = matrix.get(case_id) or {}
        relation = str(row.get("expected_relation") or case.get("expected_relation") or "observe")
        role = str(
            row.get("role")
            or case.get("role")
            or ("invariant" if relation == "must_preserve" else "target" if relation == "must_change" else "exploratory")
        )
        strength = str(
            row.get("oracle_strength")
            or case.get("oracle_strength")
            or ("invariant" if relation == "must_preserve" else "differential_only")
        )
        if role in {"target", "invariant"} and strength == "differential_only":
            gaps.append(case_id or "unnamed_case")
    for obligation in plan.get("guard_coverage") or []:
        if not isinstance(obligation, dict):
            continue
        relation = str(obligation.get("required_relation") or "observe")
        if relation != "must_change":
            continue
        for case_id in obligation.get("case_ids") or []:
            case_id = str(case_id)
            row = matrix.get(case_id) or {}
            if case_id not in declared_ids and str(row.get("oracle_strength") or "differential_only") == "differential_only":
                gaps.append(case_id)
    if not plan.get("guard_coverage_complete"):
        gaps.append("required guard coverage is incomplete")
    return list(dict.fromkeys(gaps))


def differential_invalid_target_cases(validation: dict[str, Any]) -> list[str]:
    """target baseline 已满足 exact oracle 时，probe 没有复现公开 bug。"""

    cases = [
        str(value)
        for value in validation.get("invalid_target_cases") or []
        if str(value)
    ]
    cases.extend(
        str(row.get("case") or "")
        for row in validation.get("case_matrix") or []
        if isinstance(row, dict)
        and str(row.get("role") or "") == "target"
        and row.get("baseline_matches_expected") is True
    )
    return list(dict.fromkeys(value for value in cases if value))


def enforce_differential_relation_gate(
    feedback: dict[str, Any],
    validation: dict[str, Any],
    failed_patch: str,
) -> dict[str, Any]:
    """关系矩阵已有直接反证时，覆盖无效或错误的 FailureReview 路由。"""

    invalid_targets = differential_invalid_target_cases(validation)
    if invalid_targets:
        return {
            "failure_stage": "invalid_test",
            "issue_contract": [
                str(value) for value in feedback.get("issue_contract") or [] if str(value).strip()
            ],
            "observed_failure": [
                f"{case_id}: clean baseline already matches expected_after; target probe did not reproduce the bug"
                for case_id in invalid_targets
            ],
            "failure_observation_quality": "behavior_delta",
            "root_cause": "The public differential target is non-discriminating because its clean baseline already satisfies the exact target oracle.",
            "invalidated_assumption": "A baseline-passing target was incorrectly treated as evidence that the candidate still needed to change.",
            "required_correction": "Replace the target probe with a faithful public reproducer; do not rewrite the patch from this evidence.",
            "evidence_ledger": [
                {
                    "id": "O1",
                    "source": "validation_summary",
                    "fact": "; ".join(invalid_targets),
                    "support": "direct",
                }
            ],
            "correction_evidence_ids": [],
            "scope_expansion": [],
            "regression_guards": [],
            "correction_readiness": "not_needed",
            "next_evidence_source": "public_test",
            "correction_route": "none",
            "confidence": 1.0,
            "controller_oracle_gate": True,
        }

    violations = differential_complete_gate_violations(validation)
    gaps = differential_oracle_gaps(validation)
    if not violations and gaps:
        return {
            "failure_stage": "insufficient_evidence",
            "issue_contract": [
                str(value) for value in feedback.get("issue_contract") or [] if str(value).strip()
            ],
            "observed_failure": [f"oracle gap: {value}" for value in gaps],
            "failure_observation_quality": "behavior_delta",
            "root_cause": "The differential trace shows change/no-change but lacks an exact or invariant oracle for every required target behavior.",
            "invalidated_assumption": "A must_change relation was treated as proof that the changed result was correct.",
            "required_correction": "No evidence-backed code correction is available from this trace.",
            "evidence_ledger": [
                {
                    "id": "O1",
                    "source": "validation_summary",
                    "fact": "; ".join(gaps),
                    "support": "direct",
                }
            ],
            "correction_evidence_ids": [],
            "scope_expansion": [],
            "regression_guards": [
                str(value) for value in feedback.get("regression_guards") or [] if str(value).strip()
            ],
            "correction_readiness": "insufficient_evidence",
            "next_evidence_source": "public_test",
            "correction_route": "none",
            "confidence": 1.0,
            "controller_oracle_gate": True,
        }
    if not violations:
        return feedback
    if (
        feedback.get("failure_stage") in {"implementation_semantics", "regression"}
        and feedback.get("correction_readiness") == "actionable"
        and feedback.get("correction_route") == "corrective_implement"
    ):
        return feedback
    issue_contract = [
        str(value)
        for value in feedback.get("issue_contract") or []
        if str(value).strip()
    ] or ["The public task requires the changed compound guard to preserve its full behavior contract."]
    return {
        "failure_stage": "implementation_semantics",
        "issue_contract": issue_contract,
        "observed_failure": violations,
        "failure_observation_quality": "behavior_delta",
        "root_cause": (
            "The candidate contradicts an exact target oracle or changes a baseline-backed invariant."
        ),
        "invalidated_assumption": (
            "Passing a weak change/no-change check was treated as proof that the exact result and old behavior were correct."
        ),
        "required_correction": (
            "Repair only the first predicate, conversion, output rule, or state mutation contradicted by the exact/invariant oracle. "
            "Do not broaden edits outside the current patch scope. Violations: "
            + "; ".join(violations)
        ),
        "evidence_ledger": [
            {
                "id": "G1",
                "source": "validation_summary",
                "fact": "; ".join(violations),
                "support": "direct",
            },
            {
                "id": "G2",
                "source": "failed_patch",
                "fact": failed_patch[:2_000],
                "support": "direct",
            },
        ],
        "correction_evidence_ids": ["G1", "G2"],
        "scope_expansion": [],
        "regression_guards": [
            str(value)
            for value in feedback.get("regression_guards") or []
            if str(value).strip()
        ],
        "correction_readiness": "actionable",
        "next_evidence_source": "none",
        "correction_route": "corrective_implement",
        "confidence": 1.0,
        "controller_complete_gate": True,
    }


def enforce_differential_complete_gate(
    feedback: dict[str, Any],
    validation: dict[str, Any],
    failed_patch: str,
) -> dict[str, Any]:
    """向后兼容旧调用名；实际门禁已独立于 complete 状态。"""

    return enforce_differential_relation_gate(feedback, validation, failed_patch)


def validated_failure_feedback_route(feedback: dict[str, Any]) -> str | None:
    """Validate the CLI-normalized route without importing the Agent runtime."""

    if not isinstance(feedback, dict):
        return None
    route = str(feedback.get("correction_route") or "")
    readiness = str(feedback.get("correction_readiness") or "")
    if route == "none":
        return route if readiness in {"not_needed", "insufficient_evidence"} else None
    if route not in {"corrective_implement", "coordinator_replan"}:
        return None
    allowed_observations = {"expected_actual", "exception_detail", "behavior_delta", "failure_only"}
    if (
        readiness != ("needs_replan" if route == "coordinator_replan" else "actionable")
        or feedback.get("scope_expansion")
        or feedback.get("failure_observation_quality")
        not in allowed_observations
    ):
        return None
    if route == "coordinator_replan" and (not feedback.get("replan_reason") or feedback.get("next_evidence_source") != "repository_code"):
        return None
    ledger = {
        str(item.get("id") or ""): item
        for item in feedback.get("evidence_ledger") or []
        if isinstance(item, dict)
    }
    evidence_ids = [
        str(identifier)
        for identifier in feedback.get("correction_evidence_ids") or []
    ]
    selected = [ledger.get(identifier) for identifier in evidence_ids]
    if not selected or any(
        item is None or item.get("support") == "unknown"
        for item in selected
    ):
        return None
    direct_sources = {
        str(item.get("source") or "")
        for item in selected
        if item and item.get("support") == "direct"
    }
    if route == "corrective_implement" and feedback.get("failure_observation_quality") == "failure_only" and "repository_code" not in direct_sources:
        return None
    return (
        route
        if direct_sources & {"failure_log", "validation_summary"}
        and (route == "coordinator_replan" or direct_sources & {"public_task", "failed_patch", "semantic_evidence", "repository_code"})
        else None
    )


def run_inline_experience_corrective(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    task: str,
    failed_patch: str,
    feedback: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    constraints = corrective_constraints_from_patch(
        failed_patch,
        repo_dir,
        padding=120,
        problem_statement=task,
    )
    replan = None
    # 反馈可能指出原 patch 之外的根因；只有协调阶段能确认并授权新范围。
    requested_paths = set(re.findall(
        r"(?<![\w/])([\w.-]+(?:/[\w.-]+)+\.(?:py|js|ts|java|cpp|c|h|rs))\b",
        str(feedback.get("required_correction") or ""),
    ))
    outside_paths = requested_paths - set(constraints["allowed_files"])
    missing_scope = required_edit_scope_issues(feedback.get("required_edits") or [], constraints)
    if outside_paths or missing_scope or feedback.get("correction_readiness") == "needs_replan":
        replan_dir = output_dir.parent / "coordinator-replan"
        replan_dir.mkdir(parents=True, exist_ok=True)
        replan_task = replan_dir / "task.md"
        replan_task.write_text(
            f"## Public Task\n{task}\n\n## Current Patch\n```diff\n{failed_patch}\n```\n"
            f"## Validated FailureFeedback\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n"
            f"## Current Scope\n{json.dumps(constraints, ensure_ascii=False)}\n"
            f"## Uncovered Required Edits\n{json.dumps(missing_scope, ensure_ascii=False)}\n"
            "\n## 本轮范围重规划说明\n"
            "现在进入新的范围重规划回合。上面的 patch 和第一次修改范围属于历史上下文，"
            "不代表本轮需要编辑的文件。已有改动已保留在工作区，默认无需再次修改。"
            "请根据最新失败反馈确定本轮真正需要修改的文件，不要直接沿用第一次的文件名单。"
            "allowed_files 只列本轮需要编辑的文件；保留历史 patch 不需要再次授权，"
            "不要为了让最终补丁包含旧改动而把旧文件加入名单。"
            "如果确实需要修改或撤回历史改动，必须在本轮先读取对应源码，"
            "明确说明修改理由后才加入 allowed_files。\n\n"
            "The feedback points outside the current scope. Confirm the production source and decide "
            "a new complete EditBrief and narrow scope. Do not edit code or delegate repair selection.",
            encoding="utf-8",
        )
        command = [str(args.repo_agent_bin), "coordinator-replan", "--repo", str(repo_dir),
                   "--model", args.model, "--repair-mode", "experience-build",
                   "--output-dir", str(replan_dir), "--task-file", str(replan_task)]
        add_reasoning_cli_args(command, args)
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=args.timeout, check=False, env=env)
        (replan_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        replan = read_json_file(replan_dir / "replan.json")
        if result.returncode != 0 or replan.get("status") != "approved":
            return {"status": "replan_blocked", "agent_exit_code": result.returncode or 1,
                    "model_patch": failed_patch, "patch_chars": len(failed_patch),
                    "output_dir": str(output_dir), "coordinator_replan": replan,
                    "implement_attempted": False}
        constraints = {key: replan.get(key, {}) for key in
                       ("allowed_files", "allowed_ranges", "context_ranges")}
        missing_scope = required_edit_scope_issues(
            replan.get("required_edits", feedback.get("required_edits") or []), constraints
        )
        if missing_scope:
            return {"status": "replan_blocked", "model_patch": failed_patch,
                    "patch_chars": len(failed_patch), "output_dir": str(output_dir),
                    "coordinator_replan": replan, "scope_issues": missing_scope,
                    "implement_attempted": False}
    task_path = output_dir / "task.md"
    constraints_path = output_dir / "constraints.json"
    corrective_task = (
        f"## Public Task\n{task}\n\n"
        f"## FailureFeedback\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n\n"
        f"## Current Failed Patch\n```diff\n{failed_patch}\n```\n\n"
        "Correct this patch in place exactly once. Every edit must map to the authorizing direct subset of "
        "correction_evidence_ids; inference may explain causality but cannot add an edit obligation.\n"
        "The feedback comes from a recorded validation run. Treat only logged exceptions, expected/actual "
        "mismatches, failing target behavior, and observed regressions as acceptance criteria; do not infer hidden test "
        "source or a gold implementation. For each actionable violation, state which existing predicate, output rule, "
        "or state mutation caused the observed mismatch.\n"
        "Before editing, reconstruct the changed guard's compact truth table: structural type versus semantic property, "
        "True/False/None, and sign boundaries that decide the irreversible return. Minimal means the smallest "
        "semantically complete guard, not the fewest changed characters. Do not retain a predicate already disproved "
        "by direct relation evidence merely because Public Task omitted that predicate's terminology.\n"
        "Only return blocked_untrusted_failure_feedback when direct evidence actually contradicts the Public Task, "
        "production code, public tests, or baseline facts; omission from the issue text alone is not a conflict. "
        "For mutable geometry/state, do not infer that an index observed after the failed patch was also in-bounds for "
        "the original trigger. If direct evidence shows input-dependent cardinality, rebuild the canonical state or use "
        "its owner setter instead of restoring an out-of-bounds indexed mutation. "
        "Preserve regression_guards, do not search, do not edit tests, and stay inside the enforced hunk scope."
    )
    if replan is not None:
        corrective_task += (
            "\n\n## Source-confirmed Coordinator EditBrief\n"
            "本轮以此源码确认方案及必要位置为准；若它修正了前述候选方案，落实此方案而非旧候选。\n"
            + replan["edit_brief"]
        )
    corrective_task += (
        "\n\nBefore editing, check that every necessary edit is authorized, including callers and overrides. "
        "If a necessary location is outside scope, return status=blocked with remaining_changes before editing. "
        "Return status=completed only after all required edits are applied; otherwise use partial/blocked."
    )
    task_path.write_text(corrective_task, encoding="utf-8")
    constraints_path.write_text(json.dumps(constraints, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        str(args.repo_agent_bin),
        "corrective-implement",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--repair-mode",
        "experience-build",
        "--output-dir",
        str(output_dir),
        "--task-file",
        str(task_path),
        "--constraints-file",
        str(constraints_path),
    ]
    if args.implement_model:
        command.extend(["--implement-model", args.implement_model])
    add_reasoning_cli_args(command, args)
    add_memory_cli_args(command, row, args)
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
        env=env,
    )
    (output_dir / "agent_stdout.txt").write_text(
        result.stdout,
        encoding="utf-8",
        errors="replace",
    )
    patch = git_diff(repo_dir)
    (output_dir / "model.patch").write_text(patch, encoding="utf-8")
    worker_result = read_json_file(output_dir / "worker_result.json")
    report = extract_json_object(worker_result.get("text") or "") or {}
    delivered = (
        result.returncode == 0 and worker_result.get("completed") is True
        and report.get("status") == "completed" and not report.get("remaining_changes")
        and not report.get("block_reason")
    )
    if not delivered:
        # 保留部分 patch 作为诊断产物，恢复调用前的候选，禁止半成品进入 harness。
        restore_worktree_patch(repo_dir, failed_patch)
    process_status = agent_process_status(result)
    return {
        "status": process_status if delivered or process_status != "agent_ok" else "corrective_incomplete",
        "delivery_report": report,
        "coordinator_replan": replan,
        "implement_attempted": True,
        "agent_exit_code": result.returncode,
        "patch_chars": len(patch if delivered else failed_patch),
        "model_patch": patch if delivered else failed_patch,
        "output_dir": str(output_dir),
        "worker_result": worker_result,
        "stage_attribution": {
            "schema_version": 1,
            "pipeline": "experience_build_corrective",
            "pre_harness_attribution": "awaiting_harness" if delivered and patch.strip() else "implementation_delivery",
            "token_usage": {
                "coordinator": 0,
                "workers": int(worker_result.get("total_tokens") or 0),
                "total": int(worker_result.get("total_tokens") or 0),
            },
        },
    }


def build_corrective_failure_attribution(
    source_patch: str,
    failure_excerpt: str,
    *,
    failure_facts: dict[str, Any] | None = None,
    instance_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 harness 日志压缩为 corrective Worker 可执行、但不包含答案的因果分类。"""

    facts = dict(failure_facts or {})
    if not facts and failure_excerpt:
        facts = extract_failure_facts(failure_excerpt)
    tests = _harness_test_counts(instance_report or {})
    if tests["fail_to_pass_failed"] and tests["pass_to_pass_failed"]:
        failure_class = "target_incomplete_and_public_regression"
    elif tests["fail_to_pass_failed"]:
        failure_class = "target_semantics_incomplete"
    elif tests["pass_to_pass_failed"]:
        failure_class = "public_regression"
    elif failure_excerpt.strip():
        failure_class = "explicit_failure_without_test_counts"
    else:
        failure_class = "missing_failure_evidence"

    corpus = f"{source_patch}\n{failure_excerpt}".lower()
    hypothesis_flags: list[str] = []
    if re.search(r"dataframe|series|multioutput|nullable|dtype|\bshape\b|\b1d\b|\b2d\b", corpus):
        hypothesis_flags.append("input_representation_partition_gap")
    if (
        "typehints" in corpus
        or re.search(r"arguments\.get|has_description|lookup[_ ](?:key|identity)|rendered[_ ](?:key|name)", corpus)
        or re.search(r"['\"]\*{1,2}['\"]\s*\+\s*\w+", source_patch)
    ):
        hypothesis_flags.append("lookup_identity_not_propagated")
    if re.search(r"except\s+[^\n:]+:\s*\n\+?\s*\w+\s*=\s*\d+", source_patch, flags=re.I):
        hypothesis_flags.append("constant_fallback_masks_unsupported_semantics")
    if re.search(r"^-[^\n]*(?:append|extend|\[[^]]+\]\s*=|update\(|set_[xy]data)", source_patch, flags=re.M):
        hypothesis_flags.append("state_or_cardinality_update_removed")
    if tests["pass_to_pass_failed"]:
        hypothesis_flags.append("patch_overgeneralized_shared_behavior")

    obligations = [
        "trace the first divergent predicate, lookup identity, conversion, or state transition before changing the final output",
        "cover every public input representation implicated by the issue and failure evidence",
        "preserve one adjacent legal input that reaches the same shared path",
    ]
    if tests["pass_to_pass_failed"]:
        obligations.append("narrow or remove the behavior that caused public regressions; do not stack another broad fallback")
    if "constant_fallback_masks_unsupported_semantics" in hypothesis_flags:
        obligations.append("derive output precision/value from domain boundaries or the existing algorithm instead of a constant")
    if "lookup_identity_not_propagated" in hypothesis_flags:
        obligations.append("propagate the actually matched identity through subsequent writes and rendering")
    if "state_or_cardinality_update_removed" in hypothesis_flags:
        obligations.append("restore or replace the removed update while proving shape/cardinality/closure and lifecycle invariants")

    return {
        "failure_class": failure_class,
        "test_counts": tests,
        "first_divergence_candidates": [
            *list(facts.get("assertion_mismatches") or []),
            *list(facts.get("diff_mismatches") or []),
        ][:4],
        "exception_types": list(facts.get("exception_types") or [])[:8],
        "failed_public_tests": list(facts.get("failed_tests") or [])[:12],
        "hypothesis_flags": _unique(hypothesis_flags),
        "correction_obligations": obligations,
    }


def build_corrective_task(
    row: dict[str, Any],
    source_patch: str,
    failure_excerpt: str,
    failure_attribution: dict[str, Any] | None = None,
) -> str:
    attribution_text = json.dumps(failure_attribution or {}, ensure_ascii=False, indent=2)
    return (
        "This is a diagnostic-only corrective repair experiment and is not leaderboard-eligible.\n"
        "The repository already contains the first generated patch. Use the public issue and explicit failure log "
        "to correct that patch in place. Do not search the repository, do not use gold patch content, and do not edit tests.\n\n"
        f"## Instance\n{row['instance_id']}\n\n"
        f"## Public Problem Statement\n{row['problem_statement']}\n\n"
        f"## Current Generated Patch\n```diff\n{source_patch}\n```\n\n"
        f"## Explicit Diagnostic Failure Log\n```text\n{failure_excerpt or '(no failure excerpt available)'}\n```\n\n"
        f"## Deterministic Failure Attribution\n```json\n{attribution_text}\n```\n\n"
        "Before editing, output a compact `corrective_diagnosis` JSON containing: failure_class, first_divergence, "
        "invalidated_patch_assumption, missing_input_partition_or_state, root_cause_layer, correction_obligations, "
        "and regression_guards. Treat the deterministic attribution as evidence, not as a patch instruction. "
        "Every directly observed relation violation is an acceptance criterion. Public Task omission alone does not "
        "invalidate a case that isolates a predicate in the changed guard. Reconstruct structural-versus-semantic, "
        "True/False/None, and sign behavior; do not retain a predicate disproved by executed relation evidence. "
        "Trace the failure backward to the first wrong predicate/identity/state transition; do not patch only the final assertion. "
        "If target failures and public regressions coexist, the correction must explain both. If the current patch uses a "
        "constant fallback, broad exception, single-container protocol, alias lookup without identity propagation, or removes "
        "a state/cardinality update, explicitly test that assumption against the failure evidence. Then make the narrowest "
        "correction inside allowed_ranges. Preserve adjacent public behavior and leave a non-empty diff."
    )


def corrective_constraints_from_patch(
    patch: str,
    repo_dir: Path,
    padding: int = 160,
    *,
    problem_statement: str = "",
) -> dict[str, Any]:
    ranges: dict[str, list[list[int]]] = {}
    current_file = ""
    for line in patch.splitlines():
        match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if match:
            current_file = match.group(2)
            ranges.setdefault(current_file, [])
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if current_file and hunk:
            start = int(hunk.group(1))
            length = max(1, int(hunk.group(2) or 1))
            full_path = repo_dir / current_file
            total_lines = len(full_path.read_text(encoding="utf-8", errors="replace").splitlines()) if full_path.exists() else start + length + padding
            ranges[current_file].append([max(1, start - padding), min(total_lines, start + length - 1 + padding)])
    ranges = {path: merge_line_ranges(values) for path, values in ranges.items() if values}
    constraints: dict[str, Any] = {
        "allowed_files": sorted(ranges),
        "allowed_ranges": ranges,
    }
    return constraints


def merge_line_ranges(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def run_case_curator_for_success(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    pass_summary: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    case_dir = output_dir / "case-curator"
    case_dir.mkdir(parents=True, exist_ok=True)
    validation_path = case_dir / "validation_summary.json"
    validation = {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "patch_validation": pass_summary.get("patch_validation"),
        "harness": _curator_harness_summary(pass_summary.get("harness") or {}),
    }
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        str(args.repo_agent_bin),
        "curate-repair",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--output-dir",
        str(case_dir),
        "--task-file",
        str(output_dir / "task.md"),
        "--patch-file",
        str(output_dir / "model.patch"),
        "--result",
        f"SWE-bench harness resolved {row['instance_id']} in pass@1.",
        "--validation-file",
        str(validation_path),
    ]
    add_memory_cli_args(command, row, args)
    add_reasoning_cli_args(command, args)
    started = time.time()
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=min(args.timeout, 900),
        check=False,
        env=env,
    )
    (case_dir / "case_curator_stdout.txt").write_text(result.stdout, encoding="utf-8", errors="replace")
    return {
        "exit_code": result.returncode,
        "seconds": round(time.time() - started, 3),
        "output_dir": str(case_dir),
        "stdout_excerpt": result.stdout[-2_000:],
    }


def run_experience_curator_for_success(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    *,
    task: str,
    failed_patch: str,
    successful_patch: str,
    feedback_file: Path | None,
    validation: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
) -> dict[str, Any]:
    """只在独立 harness 明确 resolved 后更新 episodic/semantic 双记忆。"""

    curator_dir = output_dir / "experience-curator"
    curator_dir.mkdir(parents=True, exist_ok=True)
    task_path = curator_dir / "task.md"
    successful_patch_path = curator_dir / "successful.patch"
    failed_patch_path = curator_dir / "failed.patch"
    validation_path = curator_dir / "validation.json"
    task_path.write_text(task, encoding="utf-8")
    successful_patch_path.write_text(successful_patch, encoding="utf-8")
    failed_patch_path.write_text(failed_patch, encoding="utf-8")
    validation_path.write_text(
        json.dumps(
            validation if validation.get("validation_kind") == "public_test_suite" else {
                "repo": row["repo"],
                "resolved": validation.get("resolved"),
                "error": validation.get("error"),
                "failure_facts": validation.get("failure_facts") or {},
                "instance_report": validation.get("instance_report") or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    command = [
        str(args.repo_agent_bin),
        "curate-experience",
        "--repo",
        str(repo_dir),
        "--model",
        args.model,
        "--output-dir",
        str(curator_dir),
        "--task-file",
        str(task_path),
        "--successful-patch-file",
        str(successful_patch_path),
        "--validation-file",
        str(validation_path),
        "--provenance",
        args.memory_provenance,
    ]
    if failed_patch.strip():
        command.extend(["--failed-patch-file", str(failed_patch_path)])
    if feedback_file is not None and feedback_file.is_file():
        command.extend(["--feedback-file", str(feedback_file)])
    add_memory_cli_args(command, row, args)
    add_reasoning_cli_args(command, args)

    started = time.time()
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=min(args.timeout, 900),
        check=False,
        env=env,
    )
    (curator_dir / "curator_stdout.txt").write_text(
        result.stdout,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "exit_code": result.returncode,
        "seconds": round(time.time() - started, 3),
        "output_dir": str(curator_dir),
        "provenance": args.memory_provenance,
        "trajectory_kind": (
            "failure_feedback_success"
            if failed_patch.strip() and feedback_file is not None
            else "input_success"
        ),
        "stdout_excerpt": result.stdout[-2_000:],
    }


def add_memory_cli_args(command: list[str], row: dict[str, Any], args: argparse.Namespace) -> None:
    if args.memory_root:
        command.extend(["--memory-root", str(args.memory_root)])
    scope = format_memory_scope(row, args)
    if scope:
        command.extend(["--memory-scope", scope])
    if args.no_repair_memory:
        command.append("--no-repair-memory")


def add_reasoning_cli_args(command: list[str], args: argparse.Namespace) -> None:
    command.extend(
        [
            "--reasoning-effort",
            str(args.reasoning_effort),
            "--coordinator-reasoning-effort",
            str(args.coordinator_reasoning_effort),
            "--implement-reasoning-effort",
            str(args.implement_reasoning_effort),
        ]
    )


def format_memory_scope(row: dict[str, Any], args: argparse.Namespace) -> str:
    scope = args.memory_scope or ""
    if not scope:
        return ""
    return scope.format(repo=row["repo"], instance_id=row["instance_id"])


def _curator_harness_summary(harness: dict[str, Any]) -> dict[str, Any]:
    return {
        "resolved": harness.get("resolved"),
        "unresolved": harness.get("unresolved"),
        "error": harness.get("error"),
        "empty_patch": harness.get("empty_patch"),
        "failure_facts": harness.get("failure_facts"),
    }


def run_harness_for_instance(
    row: dict[str, Any],
    patch: str,
    output_dir: Path,
    args: argparse.Namespace,
    env: dict[str, str],
    round_index: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    swebench_dir = (args.swebench_dir or args.dataset.parent).resolve()
    run_id = sanitize_run_id(f"{args.run_id}-{row['instance_id']}-r{round_index}")
    prediction_path = output_dir / "predictions.json"
    prediction_path.write_text(
        json.dumps([prediction_for(row["instance_id"], patch, args.model)], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    harness_env = dict(env)
    if args.docker_network:
        harness_env["SWEBENCH_DOCKER_NETWORK"] = args.docker_network
    if args.proxy:
        harness_env["SWEBENCH_GIT_PROXY"] = args.proxy
    harness_python = str(getattr(args, "harness_python", None) or sys.executable)
    command = [
        harness_python,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(args.dataset),
        "--predictions_path",
        str(prediction_path),
        "--max_workers",
        "1",
        "--instance_ids",
        row["instance_id"],
        "--run_id",
        run_id,
        "--namespace",
        args.namespace,
        "--cache_level",
        args.cache_level,
        "--timeout",
        str(args.harness_timeout),
    ]
    started = time.time()
    result = subprocess.run(
        command,
        cwd=swebench_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=max(args.harness_timeout + 900, args.harness_timeout * 2),
        check=False,
        env=harness_env,
    )
    stdout = result.stdout
    (output_dir / "harness_stdout.txt").write_text(stdout, encoding="utf-8", errors="replace")
    report_path = find_report_path(stdout, swebench_dir)
    report_data: dict[str, Any] = {}
    instance_report: dict[str, Any] = {}
    if report_path and report_path.exists():
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
        instance_report = load_instance_report(swebench_dir, run_id, args.model, row["instance_id"])
    test_output_path = (
        swebench_dir
        / "logs"
        / "run_evaluation"
        / run_id
        / f"repoagent-{args.model}"
        / row["instance_id"]
        / "test_output.txt"
    )
    failure_excerpt = read_failure_excerpt(
        test_output_path,
        instance_report=instance_report,
        changed_files=changed_files_from_patch(patch),
    )
    if not failure_excerpt and result.returncode != 0:
        failure_excerpt = stdout[-6_000:]
    harness_result = {
        "round": round_index,
        "exit_code": result.returncode,
        "seconds": round(time.time() - started, 3),
        "run_id": run_id,
        "report_path": str(report_path) if report_path else "",
        "test_output_path": str(test_output_path) if test_output_path.exists() else "",
        "resolved": row["instance_id"] in report_data.get("resolved_ids", []),
        "unresolved": row["instance_id"] in report_data.get("unresolved_ids", []),
        "error": row["instance_id"] in report_data.get("error_ids", []) or result.returncode != 0,
        "empty_patch": not patch.strip(),
        "instance_report": instance_report,
        "failure_excerpt": failure_excerpt,
        "failure_facts": extract_failure_facts(failure_excerpt),
    }
    full_test_output = ""
    if test_output_path.exists():
        full_test_output = test_output_path.read_text(encoding="utf-8", errors="replace")
    harness_result["environment_contamination"] = classify_harness_environment_contamination(
        instance_report,
        full_test_output or failure_excerpt,
    )
    return harness_result


def classify_harness_environment_contamination(
    instance_report: dict[str, Any],
    test_output: str,
) -> dict[str, Any]:
    """识别目标已通过、仅因已知工具链漂移导致 P2P 失败的结果。"""

    target_failures = _failed_harness_test_names(instance_report, "FAIL_TO_PASS")
    regression_failures = _failed_harness_test_names(instance_report, "PASS_TO_PASS")
    if target_failures or not regression_failures or not test_output.strip():
        return {"contaminated": False, "reasons": []}

    markers = {
        "version_scheme_deprecation": (
            "DeprecationWarning: Version scheme 'release-branch-semver' has been renamed"
        ),
        "unclosed_render_artifact": "ResourceWarning: unclosed file",
        "pytest_unraisable_cleanup": "PytestUnraisableExceptionWarning",
    }
    blocks = _harness_failure_blocks(test_output)
    matched = _match_failure_blocks(regression_failures, blocks, test_output)
    if len(matched) != len(regression_failures):
        return {"contaminated": False, "reasons": []}

    reasons: list[str] = []
    for _, block in matched:
        block_reasons = [name for name, marker in markers.items() if marker in block]
        if not block_reasons:
            return {"contaminated": False, "reasons": []}
        reasons.extend(block_reasons)
    return {
        "contaminated": True,
        "reasons": list(dict.fromkeys(reasons)),
        "failed_regression_tests": regression_failures,
    }


def skipped_harness_for_invalid_patch(patch_validation: dict[str, Any], round_index: int) -> dict[str, Any]:
    return {
        "round": round_index,
        "exit_code": None,
        "seconds": 0.0,
        "run_id": "",
        "report_path": "",
        "test_output_path": "",
        "resolved": False,
        "unresolved": True,
        "error": False,
        "empty_patch": True,
        "skipped": True,
        "skip_reason": "invalid_patch: " + ", ".join(patch_validation.get("reasons") or []),
        "instance_report": {},
        "failure_excerpt": "",
        "failure_facts": {},
    }


def validate_model_patch(
    patch: str,
    *,
    repo_dir: Path | None = None,
    agent_output_dir: Path | None = None,
    require_patch_review: bool = True,
    reject_failed_verification: bool = True,
    stage_attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    verification_assessment: dict[str, Any] = {}
    stats = patch_change_stats(patch)
    if not patch.strip():
        reasons.append("empty_patch")
    elif stats["whitespace_only"]:
        reasons.append("whitespace_only_patch")
    if patch.strip():
        reasons.extend(validate_deterministic_patch_policy(patch))
        reasons.extend(validate_no_test_edits(patch))
    if patch.strip() and repo_dir is not None:
        reasons.extend(validate_repo_static_sanity(repo_dir, patch))
    if require_patch_review and patch.strip() and agent_output_dir is not None:
        reasons.extend(validate_patch_review_trace(agent_output_dir))
    if patch.strip() and stage_attribution is not None:
        stages = stage_attribution.get("stages") or {}
        reproduction = stages.get("reproduction") or {}
        semantic_evidence_issues = list(reproduction.get("semantic_evidence_issues") or [])
        if semantic_evidence_issues:
            warnings.append("semantic_evidence_degraded")

        verification = ((stage_attribution.get("stages") or {}).get("focused_verification") or {})
        verification_assessment = dict(verification.get("assessment") or {})
        if not verification_assessment:
            verification_assessment = assess_verification_result(verification.get("result") or {})
        if verification.get("skipped_reason") == "no_trusted_verification_plan":
            warnings.append("verification_not_available")
        elif not verification.get("attempts"):
            warnings.append("verification_not_attempted")
        elif not verification.get("result_parseable"):
            warnings.append("verification_result_unparseable")
        elif verification_assessment.get("reject_patch") and reject_failed_verification:
            reasons.append(
                "verification_evidence_rejected_patch:"
                + str(verification_assessment.get("classification") or "failed")
            )
        elif verification_assessment.get("reject_patch"):
            warnings.append(
                "verification_failed_feedback_eligible:"
                + str(verification_assessment.get("classification") or "failed")
            )
        elif not verification_assessment.get("submission_ready"):
            warnings.append(
                "verification_inconclusive:"
                + str(verification_assessment.get("classification") or "unknown")
            )
        if verification_assessment.get("submission_ready") and not stage_attribution.get("submission_ready"):
            warnings.append("submission_gate_not_ready")
    return {
        "invalid": bool(reasons),
        "reasons": reasons,
        "warnings": warnings,
        "verification_assessment": verification_assessment,
        "semantic_changes": stats["semantic_changes"],
        "changed_lines": stats["changed_lines"],
        "discard_patch": bool(reasons),
    }


def validate_repo_static_sanity(repo_dir: Path, patch: str) -> list[str]:
    reasons: list[str] = []
    if (repo_dir / ".git").exists():
        diff_check = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--check"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if diff_check.returncode != 0:
            excerpt = " ".join(diff_check.stdout.split())[:300]
            reasons.append(f"git_diff_check_failed:{excerpt}")
    for path in changed_files_from_patch(patch):
        if not path.endswith(".py"):
            continue
        full_path = repo_dir / path
        if not full_path.exists():
            continue
        try:
            current_source = full_path.read_text(encoding="utf-8")
            ast.parse(current_source)
        except SyntaxError as exc:
            reasons.append(f"python_syntax_error:{path}:{exc.lineno}:{exc.msg}")
            continue
        except UnicodeDecodeError as exc:
            reasons.append(f"python_decode_error:{path}:{exc}")
            continue
        base_source = git_head_file_text(repo_dir, path)
        if base_source is not None:
            for undefined in newly_undefined_names(base_source, current_source, path):
                reasons.append(
                    f"python_new_undefined_name:{path}:{undefined.scope}:{undefined.lineno}:{undefined.name}"
                )
    return reasons


def git_head_file_text(repo_dir: Path, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"HEAD:{path}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def validate_deterministic_patch_policy(patch: str) -> list[str]:
    added_by_file = added_lines_by_file_from_patch(patch)
    reasons: list[str] = []

    init_added = "\n".join(added_by_file.get("lib/matplotlib/__init__.py", []))
    if init_added and matplotlib_backend_restore_policy_violation(init_added):
        reasons.append(
            "matplotlib_rc_context_backend_restore_policy:"
            "do_not_restore_backend_in_finally_or_call_rcparams_get_pop"
        )

    art3d_added = "\n".join(added_by_file.get("lib/mpl_toolkits/mplot3d/art3d.py", []))
    if art3d_added and matplotlib_art3d_private_cache_policy_violation(art3d_added):
        reasons.append(
            "matplotlib_art3d_private_color_cache_policy:"
            "do_not_synthesize_2d_color_caches_from_3d_private_state"
        )
    return reasons


def validate_no_test_edits(patch: str) -> list[str]:
    edited_tests = [path for path in changed_files_from_patch(patch) if looks_like_test_path(path)]
    if not edited_tests:
        return []
    return ["test_edit_denied:" + ",".join(edited_tests[:10])]


def looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or "/test/" in normalized
        or normalized.startswith("fixtures/")
        or "/fixtures/" in normalized
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def added_lines_by_file_from_patch(patch: str) -> dict[str, list[str]]:
    added_by_file: dict[str, list[str]] = {}
    current_path = ""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current_path = _path_from_diff_header(line)
            continue
        if line.startswith("+++ "):
            current_path = _clean_patch_path(line[4:].split("\t", 1)[0].strip()) or current_path
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_by_file.setdefault(current_path, []).append(line[1:])
    return added_by_file


def matplotlib_backend_restore_policy_violation(added_text: str) -> bool:
    risky_patterns = (
        "orig.pop('backend'",
        'orig.pop("backend"',
        "rcParams.get('backend'",
        'rcParams.get("backend"',
        "dict.__setitem__(rcParams, 'backend'",
        'dict.__setitem__(rcParams, "backend"',
        "_backend =",
    )
    return any(pattern in added_text for pattern in risky_patterns)


def matplotlib_art3d_private_cache_policy_violation(added_text: str) -> bool:
    risky_patterns = (
        "_facecolors2d = self._facecolor3d",
        "_edgecolors2d = self._edgecolor3d",
        "getattr(self, '_facecolors2d'",
        'getattr(self, "_facecolors2d"',
        "getattr(self, '_edgecolors2d'",
        'getattr(self, "_edgecolors2d"',
    )
    return any(pattern in added_text for pattern in risky_patterns)


def validate_patch_review_trace(agent_output_dir: Path) -> list[str]:
    trace_path = agent_output_dir / "trace.jsonl"
    if not trace_path.exists():
        return ["missing_patch_review"]
    reviews: list[dict[str, Any]] = []
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_result" or event.get("tool") != "agent":
            continue
        response = event.get("response") or {}
        data = response.get("data") or {}
        if data.get("type") != "patch_review" or not data.get("completed"):
            continue
        review = extract_json_object(response.get("text") or "")
        if review:
            reviews.append(review)
    if not reviews:
        return ["missing_patch_review"]
    return patch_review_policy_reasons(reviews[-1])


def patch_review_policy_reasons(review: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    patch_risk = str(review.get("patch_risk") or review.get("risk") or "").strip().lower()
    verdict = str(review.get("verdict") or "").strip().lower()
    if patch_risk == "high" or verdict in {"revise", "reject"}:
        reasons.append(f"patch_review_not_accepted:{patch_risk or 'unknown'}:{verdict or 'unknown'}")
        return reasons
    if not (patch_risk in {"low", "medium"} and verdict == "accept"):
        reasons.append("patch_review_ambiguous")
        return reasons
    if review.get("coverage_gaps") or review.get("required_changes"):
        reasons.append("patch_review_accept_with_required_changes")
    for finding in review.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        risk_type = str(finding.get("risk_type") or "").strip().lower()
        suggested_fix = str(finding.get("suggested_fix") or "").strip()
        if risk_type == "coverage_gap":
            reasons.append("patch_review_accept_with_coverage_gap")
        if suggested_fix and not is_noop_suggested_fix(suggested_fix):
            reasons.append("patch_review_accept_with_suggested_fix")
    return _unique(reasons)


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    candidates.extend(fenced)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def is_noop_suggested_fix(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    return normalized in {
        "",
        "no change needed",
        "no fix needed",
        "none",
        "n/a",
        "not needed",
        "无需修改",
        "不需要修改",
    }


def is_high_fanout_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    markers = (
        "lib/matplotlib/__init__.py",
        "lib/matplotlib/artist.py",
        "lib/matplotlib/axis.py",
        "lib/matplotlib/axes/",
        "lib/matplotlib/figure.py",
        "lib/matplotlib/widgets.py",
        "lib/matplotlib/colorbar.py",
        "lib/mpl_toolkits/mplot3d/art3d.py",
        "django/db/migrations/",
        "django/db/models/sql/",
        "django/contrib/admin/checks.py",
        "src/_pytest/",
        "_pytest/",
        "requests/sessions.py",
        "requests/models.py",
        "sklearn/utils/validation.py",
        "sklearn/base.py",
    )
    return any(marker in normalized for marker in markers)


def patch_change_stats(patch: str) -> dict[str, Any]:
    added: list[str] = []
    removed: list[str] = []
    added_by_file: dict[str, list[str]] = {}
    removed_by_file: dict[str, list[str]] = {}
    current_path = ""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current_path = _path_from_diff_header(line)
            continue
        if line.startswith("+++ "):
            current_path = _clean_patch_path(line[4:].split("\t", 1)[0].strip()) or current_path
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            value = line[1:]
            added.append(value)
            added_by_file.setdefault(current_path, []).append(value)
        elif line.startswith("-"):
            value = line[1:]
            removed.append(value)
            removed_by_file.setdefault(current_path, []).append(value)

    changed_lines = len(added) + len(removed)
    added_semantic = [_semantic_line(line) for line in added if _semantic_line(line)]
    removed_semantic = [_semantic_line(line) for line in removed if _semantic_line(line)]
    semantic_changes = len(added_semantic) + len(removed_semantic)
    indentation_sensitive = _has_indentation_sensitive_change(added_by_file, removed_by_file)
    whitespace_only = changed_lines > 0 and (
        semantic_changes == 0 or sorted(added_semantic) == sorted(removed_semantic)
    ) and not indentation_sensitive
    return {
        "changed_lines": changed_lines,
        "semantic_changes": changed_lines if indentation_sensitive else (0 if whitespace_only else semantic_changes),
        "whitespace_only": whitespace_only,
        "indentation_sensitive": indentation_sensitive,
    }


def changed_files_from_patch(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = _clean_patch_path(line[4:].split("\t", 1)[0].strip())
            if path and path not in files:
                files.append(path)
    return files


def _semantic_line(line: str) -> str:
    return re.sub(r"\s+", "", line)


def _has_indentation_sensitive_change(
    added_by_file: dict[str, list[str]], removed_by_file: dict[str, list[str]]
) -> bool:
    paths = set(added_by_file) | set(removed_by_file)
    for path in paths:
        if not path.endswith(".py"):
            continue
        added = [line for line in added_by_file.get(path, []) if _semantic_line(line)]
        removed = [line for line in removed_by_file.get(path, []) if _semantic_line(line)]
        if not added or not removed:
            continue
        if sorted(_semantic_line(line) for line in added) != sorted(_semantic_line(line) for line in removed):
            continue
        if sorted(added) != sorted(removed):
            return True
    return False


def _path_from_diff_header(line: str) -> str:
    parts = line.split()
    if len(parts) >= 4:
        return _clean_patch_path(parts[3])
    return ""


def _clean_patch_path(value: str) -> str:
    if value == "/dev/null":
        return ""
    if value.startswith(("a/", "b/")):
        return value[2:]
    return value


def checkout_repo(
    repo: str,
    base_commit: str,
    repo_dir: Path,
    cache_root: Path,
    env: dict[str, str],
    *,
    offline_cache: bool = False,
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / f"{repo.replace('/', '__')}.git"
    url = f"https://github.com/{repo}.git"
    if not cache.exists() and offline_cache:
        raise RuntimeError(f"Offline cache is missing repository mirror: {cache}")
    if not cache.exists():
        run(["git", "clone", "--mirror", url, str(cache)], env=env, timeout=900)
    elif not offline_cache:
        run(["git", "-C", str(cache), "fetch", "--all", "--prune"], env=env, timeout=900, allow_failure=True)

    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", str(cache), str(repo_dir)], env=env, timeout=600)
    checkout = run(["git", "-C", str(repo_dir), "checkout", base_commit], env=env, timeout=300, allow_failure=True)
    if checkout.returncode != 0:
        run(["git", "-C", str(repo_dir), "fetch", "origin", base_commit], env=env, timeout=600)
        run(["git", "-C", str(repo_dir), "checkout", base_commit], env=env, timeout=300)
    run(["git", "-C", str(repo_dir), "reset", "--hard", base_commit], env=env, timeout=300)


def build_task(
    row: dict[str, Any],
    repo_dir: Path,
    diagnostic_mode: str = "none",
    repair_mode: str = "benchmark",
) -> str:
    task = (
        f"## Instance\n{row['instance_id']}\n\n"
        f"## Repository\n{repo_dir}\n\n"
        f"## Problem Statement\n{row['problem_statement']}\n"
    )
    if diagnostic_mode == "gold_files":
        files = gold_patch_files(str(row.get("patch") or ""))
        task += (
            "\n## Diagnostic Localization Oracle\n"
            "This run is diagnostic-only and not leaderboard-eligible. The reference solution modifies only these "
            f"production files: {json.dumps(files)}. No patch content or target tests are provided. "
            "Treat these as the allowed localization scope and reason out the implementation yourself.\n"
        )
    elif diagnostic_mode == "gold_loci":
        loci = gold_patch_loci(str(row.get("patch") or ""))
        task += (
            "\n## Diagnostic Edit-Locus Oracle\n"
            "This run is diagnostic-only and not leaderboard-eligible. The reference solution touches the following "
            f"pre-change line regions: {json.dumps(loci, ensure_ascii=False)}. No removed or added patch text is provided. "
            "Use these exact regions as allowed_ranges and derive the required semantics from the issue and surrounding code.\n"
        )
    return task


def gold_patch_files(patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/\S+", patch, flags=re.M)))


def gold_patch_loci(patch: str) -> dict[str, list[list[int]]]:
    loci: dict[str, list[list[int]]] = {}
    current_file = ""
    for line in patch.splitlines():
        match = re.match(r"diff --git a/(\S+) b/\S+", line)
        if match:
            current_file = match.group(1)
            loci.setdefault(current_file, [])
            continue
        hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if current_file and hunk:
            start = int(hunk.group(1))
            length = max(1, int(hunk.group(2) or 1))
            loci[current_file].append([start, start + length - 1])
    return {path: ranges for path, ranges in loci.items() if ranges}


def diagnostic_scope_for_row(row: dict[str, Any], repo_dir: Path, diagnostic_mode: str) -> dict[str, Any]:
    if diagnostic_mode == "gold_files":
        ranges: dict[str, list[list[int]]] = {}
        for path in gold_patch_files(str(row.get("patch") or "")):
            full_path = repo_dir / path
            total_lines = len(full_path.read_text(encoding="utf-8", errors="replace").splitlines()) if full_path.exists() else 1
            ranges[path] = [[1, max(1, total_lines)]]
        return {"allowed_files": sorted(ranges), "allowed_ranges": ranges}
    if diagnostic_mode == "gold_loci":
        ranges = gold_patch_loci(str(row.get("patch") or ""))
        return {"allowed_files": sorted(ranges), "allowed_ranges": ranges}
    return {}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def find_report_path(stdout: str, swebench_dir: Path) -> Path | None:
    match = re.search(r"Report written to (.+)", stdout)
    if not match:
        return None
    value = match.group(1).strip()
    path = Path(value)
    return path if path.is_absolute() else swebench_dir / path


def load_instance_report(swebench_dir: Path, run_id: str, model: str, instance_id: str) -> dict[str, Any]:
    path = (
        swebench_dir
        / "logs"
        / "run_evaluation"
        / run_id
        / f"repoagent-{model}"
        / instance_id
        / "report.json"
    )
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(instance_id, {})


def read_failure_excerpt(
    path: Path,
    max_chars: int = 10_000,
    *,
    instance_report: dict[str, Any] | None = None,
    changed_files: list[str] | tuple[str, ...] = (),
) -> str:
    if not path.exists():
        return ""
    text = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        path.read_text(encoding="utf-8", errors="replace"),
    )
    prioritized = _prioritized_harness_failure_excerpt(
        text,
        instance_report or {},
        changed_files=changed_files,
        max_chars=max_chars,
    )
    if prioritized:
        return prioritized
    markers = [
        "FAILURES",
        "FAILED ",
        "short test summary info",
        "Traceback",
        "AssertionError",
    ]
    starts = [index for marker in markers if (index := text.find(marker)) >= 0]
    if starts:
        return text[min(starts) : min(starts) + max_chars]
    return text[-max_chars:]


def _prioritized_harness_failure_excerpt(
    text: str,
    instance_report: dict[str, Any],
    *,
    changed_files: list[str] | tuple[str, ...],
    max_chars: int,
) -> str:
    """按目标失败、相关回归、环境摘要的顺序选择真实测试段落。"""

    target_names = _failed_harness_test_names(instance_report, "FAIL_TO_PASS")
    regression_names = _failed_harness_test_names(instance_report, "PASS_TO_PASS")
    if not target_names and not regression_names:
        return ""
    blocks = _harness_failure_blocks(text)
    target_blocks = _match_failure_blocks(target_names, blocks, text)
    regression_blocks = _match_failure_blocks(regression_names, blocks, text)
    changed_paths = [path.replace("\\", "/").lstrip("./") for path in changed_files]
    regression_blocks.sort(
        key=lambda item: _regression_failure_score(item[1], changed_paths),
        reverse=True,
    )

    sections: list[str] = []
    used_blocks: set[str] = set()
    for name, block in target_blocks[:3]:
        if block in used_blocks:
            continue
        used_blocks.add(block)
        sections.append(
            f"## TARGET FAILURE: {name}\n{_clip_failure_block(block, 4_000)}"
        )
    regression_count = 0
    for name, block in regression_blocks:
        if block in used_blocks or _regression_failure_score(block, changed_paths) <= 0:
            continue
        used_blocks.add(block)
        sections.append(
            f"## REGRESSION FAILURE: {name}\n{_clip_failure_block(block, 2_500)}"
        )
        regression_count += 1
        if regression_count >= 2 or sum(len(section) for section in sections) >= max_chars - 1_500:
            break

    selected = "\n\n".join(sections)
    if len(selected) < max_chars - 500:
        noise = _environment_failure_summary(
            [block for _, block in regression_blocks if block not in used_blocks]
        )
        if noise:
            selected = f"{selected}\n\n## OTHER FAILURE SUMMARY\n{noise}" if selected else noise
    return _clip_failure_block(selected, max_chars) if selected else ""


def _failed_harness_test_names(
    instance_report: dict[str, Any],
    group: str,
) -> list[str]:
    tests_status = instance_report.get("tests_status") if isinstance(instance_report, dict) else {}
    tests_status = tests_status if isinstance(tests_status, dict) else {}
    values = (tests_status.get(group) or tests_status.get(group.lower()) or {}).get("failure") or []
    return [str(value) for value in values if str(value).strip()]


def _harness_failure_blocks(text: str) -> list[tuple[str, str]]:
    header = re.compile(
        r"(?m)^(?P<header>(?:_{2,}[^\S\n]*[^_\s].*?[^\S\n]*_{2,}|(?:ERROR|FAIL):[^\S\n]+.+))[^\S\n]*$"
    )
    matches = list(header.finditer(text))
    terminal_positions = [
        match.start()
        for match in re.finditer(
            r"(?m)^(?:=+[^\S\n]*(?:PASSES|warnings summary|short test summary info)[^\S\n]*=+|Ran \d+ tests?.*)$",
            text,
        )
    ]
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        ends = [
            position
            for position in terminal_positions
            if position > match.start()
        ]
        if index + 1 < len(matches):
            ends.append(matches[index + 1].start())
        end = min(ends) if ends else len(text)
        block = text[match.start() : end].strip()
        if block:
            blocks.append((match.group("header").strip("_ "), block))
    return blocks


def _match_failure_blocks(
    names: list[str],
    blocks: list[tuple[str, str]],
    text: str,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for name in names:
        keys = _test_name_match_keys(name)
        match = next(
            (
                block
                for title, block in blocks
                if any(key.lower() in f"{title}\n{block}".lower() for key in keys)
            ),
            "",
        )
        if not match:
            match = _failure_context_for_test(text, keys)
        if match:
            selected.append((name, match))
    return selected


def _test_name_match_keys(name: str) -> list[str]:
    leaf = name.rsplit("::", 1)[-1].strip()
    method = leaf.split(" (", 1)[0].strip()
    base_method = method.split("[", 1)[0].strip()
    return list(dict.fromkeys(value for value in [name, leaf, method, base_method] if len(value) >= 4))


def _failure_context_for_test(text: str, keys: list[str]) -> str:
    positions = [
        position
        for key in keys
        if (position := text.lower().find(key.lower())) >= 0
    ]
    if not positions:
        return ""
    position = min(positions)
    start = max(0, text.rfind("\n", 0, max(0, position - 500)))
    return text[start : min(len(text), position + 3_500)].strip()


def _regression_failure_score(block: str, changed_paths: list[str]) -> int:
    normalized = block.replace("\\", "/")
    score = 0
    if any(path and path in normalized for path in changed_paths):
        score += 5
    has_behavior_failure = bool(
        re.search(r"AssertionError|TypeError|ValueError|expected|actual|\bassert\b", block, re.I)
    )
    if has_behavior_failure:
        score += 3
    # 同段的依赖警告不能抵消真实断言，尤其是在标题解析失败的兜底路径上。
    if not has_behavior_failure and re.search(
        r"site-packages/.+(?:DeprecationWarning|ModuleNotFoundError)", block, re.I
    ):
        score -= 4
    return score


def _environment_failure_summary(blocks: list[str], max_lines: int = 8) -> str:
    lines: list[str] = []
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or not re.search(
                r"(?:DeprecationWarning|ModuleNotFoundError|ImportError|Timeout|ConnectionError)",
                stripped,
            ):
                continue
            compact = " ".join(stripped.split())[:500]
            if compact not in lines:
                lines.append(compact)
            if len(lines) >= max_lines:
                return "\n".join(f"- {value}" for value in lines)
    return "\n".join(f"- {value}" for value in lines)


def _clip_failure_block(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(1, int(limit * 0.7))
    tail = max(1, limit - head - 40)
    return f"{text[:head]}\n... [middle clipped] ...\n{text[-tail:]}"


def extract_failure_facts(text: str) -> dict[str, Any]:
    clean_text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    expected_output = ""
    actual_output = ""
    for line in text.splitlines():
        quoted = _literal_strings_in_line(line)
        if not quoted:
            continue
        if "where False" in line and "startswith" in line:
            expected_output = quoted[-1]
        elif "where <built-in method startswith" in line and " = " in line and ".startswith" in line:
            actual_output = quoted[0]

    captured_stdout = _captured_stdout_section(text)
    expected_messages = _extract_l031_messages(expected_output)
    actual_messages = _unique(
        [
            *_extract_l031_messages(actual_output),
            *_extract_l031_messages(captured_stdout),
        ]
    )
    all_messages = _unique(
        [
            *expected_messages,
            *actual_messages,
            *_extract_l031_messages(text),
        ]
    )

    facts: dict[str, Any] = {}
    if expected_output or actual_output:
        facts["assertion_outputs"] = {
            "expected_output": expected_output,
            "actual_output": actual_output,
        }
    if expected_messages or actual_messages:
        facts["lint_messages"] = {
            "expected_l031": expected_messages,
            "actual_l031": actual_messages,
            "all_l031": all_messages,
        }
    if "pytest.raises" in text:
        facts["contains_pytest_raises"] = True
    assertion_mismatches: list[dict[str, str]] = []
    for line in clean_text.splitlines():
        match = re.search(r"\bE\s+AssertionError:\s*assert\s+(.{1,500}?)\s+==\s+(.{1,500})$", line)
        if not match:
            match = re.search(r"\bE\s+assert\s+(.{1,500}?)\s+==\s+(.{1,500})$", line)
        if not match:
            continue
        actual_expression = match.group(1).strip()
        expected_expression = match.group(2).strip()
        if actual_expression != expected_expression:
            assertion_mismatches.append(
                {
                    "actual_expression": actual_expression,
                    "expected_expression": expected_expression,
                }
            )
    if assertion_mismatches:
        facts["assertion_mismatches"] = assertion_mismatches[:8]

    expected_fragments: list[str] = []
    actual_fragments: list[str] = []
    for line in clean_text.splitlines():
        expected_match = re.search(r"\bE\s+-\s+(.{1,500})$", line)
        actual_match = re.search(r"\bE\s+\+\s+(.{1,500})$", line)
        if expected_match:
            expected_fragments.append(expected_match.group(1).strip())
        elif actual_match:
            actual_fragments.append(actual_match.group(1).strip())
    diff_mismatches = [
        {"actual_fragment": actual, "expected_fragment": expected}
        for expected, actual in zip(expected_fragments, actual_fragments)
        if actual != expected
    ]
    if diff_mismatches:
        facts["diff_mismatches"] = diff_mismatches[:8]

    failed_tests = _unique(re.findall(r"^FAILED\s+(\S+)", clean_text, flags=re.M))
    if failed_tests:
        facts["failed_tests"] = failed_tests[:30]
    exception_types = _unique(
        re.findall(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning))\b", clean_text)
    )
    if exception_types:
        facts["exception_types"] = exception_types[:20]
    return facts


def _literal_strings_in_line(line: str) -> list[str]:
    values: list[str] = []
    for raw in _iter_quoted_literals(line):
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            values.append(value)
    return values


def _iter_quoted_literals(line: str, *, max_literal_chars: int = 2_000) -> list[str]:
    literals: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        quote = line[index]
        if quote not in {"'", '"'}:
            index += 1
            continue
        start = index
        index += 1
        escaped = False
        while index < length:
            char = line[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                index += 1
                if index - start <= max_literal_chars:
                    literals.append(line[start:index])
                break
            index += 1
        else:
            break
    return literals


def _captured_stdout_section(text: str) -> str:
    marker = "Captured stdout call"
    if marker not in text:
        return ""
    tail = text.split(marker, 1)[1]
    stop_markers = [
        "\n===============================",
        "\n====================================",
        "\n-- Docs:",
    ]
    stops = [index for marker in stop_markers if (index := tail.find(marker)) >= 0]
    return tail[: min(stops)] if stops else tail


def _extract_l031_messages(text: str) -> list[str]:
    if not text:
        return []
    messages = []
    for match in re.finditer(r"L031 \| ([^\\'\n\r]+)", text):
        messages.append(match.group(1).strip())
    return _unique(messages)


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def sanitize_run_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value)[:180]


def run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env=env,
    )
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stdout[-4000:]}")
    return result


def git_diff(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def prediction_for(instance_id: str, patch: str, model: str) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"repoagent-{model}",
    }


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def finalize_stage_attribution(pass_summary: dict[str, Any]) -> dict[str, Any]:
    """把 Agent 阶段记录与 harness 结果合并成互斥的首要失败归因。

    归因不是修改结果，而是回答“最早在哪个阶段失去成功可能”，便于区分
    空 patch、环境错误、目标语义未修复和回归，从而避免只盯 resolved 调 prompt。
    """
    attribution = json.loads(json.dumps(pass_summary.get("stage_attribution") or {}))
    attribution.setdefault("schema_version", 1)
    attribution.setdefault("pipeline", "simplified_pass1")
    patch_validation = pass_summary.get("patch_validation") or {}
    harness = pass_summary.get("harness") or {}
    tests = _harness_test_counts(harness.get("instance_report") or {})

    if pass_summary.get("status") == "output_limit":
        primary = "model_output_limit"
    elif pass_summary.get("status") == "agent_error":
        primary = "agent_runtime"
    elif harness.get("error"):
        primary = "harness_environment"
    elif (harness.get("environment_contamination") or {}).get("contaminated"):
        primary = "harness_dependency_contamination"
    elif not int(pass_summary.get("patch_chars") or 0):
        primary = "implementation_delivery"
    elif patch_validation.get("invalid"):
        validation_reasons = list(patch_validation.get("reasons") or [])
        primary = (
            "focused_verification_rejected_patch"
            if any(str(reason).startswith("verification_evidence_rejected_patch:") for reason in validation_reasons)
            else "deterministic_validation"
        )
    elif harness.get("resolved"):
        primary = "success"
    elif tests["fail_to_pass_failed"] and tests["pass_to_pass_failed"]:
        primary = "semantic_fix_and_regression"
    elif tests["fail_to_pass_failed"]:
        primary = "semantic_fix_incomplete"
    elif tests["pass_to_pass_failed"]:
        primary = "public_regression"
    else:
        primary = "unresolved_unknown"

    attribution["harness_attribution"] = {
        "primary": primary,
        "resolved": bool(harness.get("resolved")),
        "error": bool(harness.get("error")),
        "tests": tests,
        "patch_validation_reasons": list(patch_validation.get("reasons") or []),
        "patch_validation_warnings": list(patch_validation.get("warnings") or []),
        "verification_assessment": patch_validation.get("verification_assessment") or {},
    }
    return attribution


def _harness_test_counts(instance_report: dict[str, Any]) -> dict[str, int]:
    tests_status = instance_report.get("tests_status") if isinstance(instance_report, dict) else {}
    tests_status = tests_status if isinstance(tests_status, dict) else {}

    def count(group: str, result: str) -> int:
        value = tests_status.get(group) or tests_status.get(group.lower()) or {}
        if not isinstance(value, dict):
            return 0
        items = value.get(result) or []
        if isinstance(items, (list, tuple, set, dict)):
            return len(items)
        return int(items) if isinstance(items, (int, float)) else 0

    return {
        "fail_to_pass_passed": count("FAIL_TO_PASS", "success"),
        "fail_to_pass_failed": count("FAIL_TO_PASS", "failure"),
        "pass_to_pass_passed": count("PASS_TO_PASS", "success"),
        "pass_to_pass_failed": count("PASS_TO_PASS", "failure"),
    }


def summarize_stage_attributions(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    primary_counts: dict[str, int] = {}
    pre_harness_counts: dict[str, int] = {}
    review_required = 0
    review_completed = 0
    total_tokens = 0
    token_samples = 0
    for summary in summaries:
        passes = summary.get("passes") or []
        attribution = (passes[-1].get("stage_attribution") if passes else None) or summary.get("stage_attribution") or {}
        primary = ((attribution.get("harness_attribution") or {}).get("primary") or "missing")
        pre_harness = attribution.get("pre_harness_attribution") or "missing"
        primary_counts[primary] = primary_counts.get(primary, 0) + 1
        pre_harness_counts[pre_harness] = pre_harness_counts.get(pre_harness, 0) + 1
        review = (attribution.get("stages") or {}).get("conditional_review") or {}
        review_required += int(bool(review.get("required")))
        review_completed += int(bool(review.get("completed")))
        tokens = (attribution.get("token_usage") or {}).get("total")
        if isinstance(tokens, int):
            total_tokens += tokens
            token_samples += 1
    return {
        "primary_counts": primary_counts,
        "pre_harness_counts": pre_harness_counts,
        "conditional_review_required": review_required,
        "conditional_review_completed": review_completed,
        "total_tokens": total_tokens,
        "average_tokens": round(total_tokens / token_samples, 1) if token_samples else None,
    }


def strip_large_fields(summary: dict[str, Any]) -> dict[str, Any]:
    clean = dict(summary)
    clean.pop("model_patch", None)
    return clean


if __name__ == "__main__":
    raise SystemExit(main())
