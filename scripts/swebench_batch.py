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

from repo_agent.core.verification import assess_verification_result
from repo_agent.core.static_analysis import newly_undefined_names


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        "diagnostic_only": args.diagnostic_mode != "none",
        "diagnostic_mode": args.diagnostic_mode,
        "leaderboard_eligible": args.diagnostic_mode == "none",
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
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--implement-model",
        help="Optional model override for Implement and corrective Implement Workers.",
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
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds per RepoAgent run.")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--evaluate", action="store_true", help="Run SWE-bench harness after each Agent pass.")
    parser.add_argument("--swebench-dir", type=Path, help="SWE-bench checkout. Defaults to dataset parent.")
    parser.add_argument("--run-id", default="repoagent-batch", help="Base run_id for SWE-bench harness.")
    parser.add_argument("--harness-timeout", type=int, default=900, help="SWE-bench per-instance timeout.")
    parser.add_argument("--cache-level", default="env")
    parser.add_argument("--namespace", default="none")
    parser.add_argument("--docker-network", default="", help="Optional SWEBENCH_DOCKER_NETWORK value.")
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
    started = time.time()
    instance_id = row["instance_id"]
    passes: list[dict[str, Any]] = []
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
        if args.diagnostic_mode == "corrective":
            if args.corrective_source_dir is None:
                raise ValueError("--corrective-source-dir is required for diagnostic corrective mode")
            pass_summary = run_corrective_diagnostic_pass(row, repo_dir, pass_output, args, env, 0)
        else:
            task = build_task(row, repo_dir, diagnostic_mode=args.diagnostic_mode)
            pass_summary = run_agent_pass(row, repo_dir, pass_output, task, args, env, 0)
        patch = pass_summary.get("model_patch", "")
        candidate_patch = patch
        (output_dir / "candidate.patch").write_text(candidate_patch, encoding="utf-8")
        patch_validation = validate_model_patch(
            patch,
            repo_dir=repo_dir,
            agent_output_dir=pass_output,
            require_patch_review=args.full_agent_profile,
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
        if args.evaluate:
            if patch_validation.get("discard_patch"):
                pass_summary["harness"] = skipped_harness_for_invalid_patch(patch_validation, 0)
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
        if args.evolve_memory and (pass_summary.get("harness") or {}).get("resolved"):
            pass_summary["case_curator"] = run_case_curator_for_success(row, repo_dir, pass_output, pass_summary, args, env)
        pass_summary["stage_attribution"] = finalize_stage_attribution(pass_summary)
        (pass_output / "stage_attribution.json").write_text(
            json.dumps(pass_summary["stage_attribution"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        passes.append(strip_large_fields(pass_summary))
        status = passes[-1].get("status", "agent_ok") if passes else "agent_ok"
        final_validation = passes[-1].get("patch_validation", {}) if passes else {}
        if final_validation.get("invalid") and not (passes[-1].get("harness") or {}).get("resolved"):
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

    raw_patch = git_diff(repo_dir) if repo_dir.exists() else ""
    final_validation = passes[-1].get("patch_validation", {}) if passes else {}
    patch = "" if final_validation.get("discard_patch") else raw_patch
    return {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "status": status,
        "agent_exit_code": agent_exit_code,
        "seconds": round(time.time() - started, 3),
        "patch_chars": len(patch),
        "candidate_patch_chars": len(raw_patch),
        "model_patch": patch,
        "output_dir": str(output_dir),
        "passes": passes,
        "official_pass1": args.diagnostic_mode == "none",
        "official_no_hints": args.diagnostic_mode in {"none", "baseline"},
        "diagnostic_only": args.diagnostic_mode != "none",
        "diagnostic_mode": args.diagnostic_mode,
        "resolved": any((pass_data.get("harness") or {}).get("resolved") for pass_data in passes),
        "diagnostic_candidate_resolved": any(
            (pass_data.get("diagnostic_candidate_harness") or {}).get("resolved") for pass_data in passes
        ),
    }


def run_agent_pass(
    row: dict[str, Any],
    repo_dir: Path,
    output_dir: Path,
    task: str,
    args: argparse.Namespace,
    env: dict[str, str],
    round_index: int,
) -> dict[str, Any]:
    started = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "task.md").write_text(task, encoding="utf-8")
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
        "--task",
        task,
    ]
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
    agent_env["REPO_AGENT_VERIFY_DOCKER_IMAGE"] = swebench_eval_image(row["instance_id"])
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
        "status": "agent_ok" if result.returncode == 0 else "agent_error",
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
    constraints = corrective_constraints_from_patch(source_patch, repo_dir)
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
        "status": "agent_ok" if result.returncode == 0 else "agent_error",
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
        "Trace the failure backward to the first wrong predicate/identity/state transition; do not patch only the final assertion. "
        "If target failures and public regressions coexist, the correction must explain both. If the current patch uses a "
        "constant fallback, broad exception, single-container protocol, alias lookup without identity propagation, or removes "
        "a state/cardinality update, explicitly test that assumption against the failure evidence. Then make the narrowest "
        "correction inside allowed_ranges. Preserve adjacent public behavior and leave a non-empty diff."
    )


def corrective_constraints_from_patch(patch: str, repo_dir: Path, padding: int = 160) -> dict[str, Any]:
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
    return {"allowed_files": sorted(ranges), "allowed_ranges": ranges}


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


def add_memory_cli_args(command: list[str], row: dict[str, Any], args: argparse.Namespace) -> None:
    if args.memory_root:
        command.extend(["--memory-root", str(args.memory_root)])
    scope = format_memory_scope(row, args)
    if scope:
        command.extend(["--memory-scope", scope])
    if args.no_repair_memory:
        command.append("--no-repair-memory")


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
    command = [
        sys.executable,
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
    failure_excerpt = read_failure_excerpt(test_output_path)
    return {
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

        deterministic_validation = stages.get("deterministic_validation") or {}
        semantic_patch_issues = [
            str(issue)
            for issue in deterministic_validation.get("issues") or []
            if str(issue).startswith("semantic_")
        ]
        if semantic_patch_issues:
            reasons.extend(f"semantic_patch_gate_failed:{issue}" for issue in semantic_patch_issues)

        terminal_reason = str(stage_attribution.get("workflow_terminal_reason") or "")
        if terminal_reason == "semantic_patch_gate_failed":
            reasons.append("workflow_terminal_gate:" + terminal_reason)

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
        elif verification_assessment.get("reject_patch"):
            reasons.append(
                "verification_evidence_rejected_patch:"
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


def build_task(row: dict[str, Any], repo_dir: Path, diagnostic_mode: str = "none") -> str:
    task = (
        "You are solving a SWE-bench instance using only the public problem statement and repository contents.\n"
        "Do not use hints_text, hidden gold patch, hidden test patch, PASS_TO_PASS, FAIL_TO_PASS, or harness feedback. "
        "Make the smallest source-code change that fixes the issue.\n\n"
        f"## Instance\n{row['instance_id']}\n\n"
        f"## Repository\n{repo_dir}\n\n"
        f"## Problem Statement\n{row['problem_statement']}\n"
        "\n"
        "## Instructions\n"
        "- Use the simplified Coordinator path: lightweight localization -> optional soft-evidence reproducer -> one scoped implement -> deterministic validation -> at most one focused verify.\n"
        "- Before editing, write a compact Problem Contract with trigger input, exact expected behavior, forbidden behavior, and adjacent old behavior that must remain unchanged.\n"
        "- Use direct search/read tools for at most 5 narrow calls. Once the candidate file, function, and edit locus are known, stop investigating.\n"
        "- Reproducer is optional and non-blocking. Use it only when public behavior remains unclear after localization; malformed evidence or an unavailable verify command must not block implementation.\n"
        "- Dispatch one implement Worker with target_code_context, old_behavior_invariants, minimal_fix_strategy, allowed_files, and narrow allowed_ranges. Exact field names are optional; semantic precision matters.\n"
        "- When Reproducer is used, treat source-backed SemanticEvidence as supplemental context rather than an unlock condition for Implement.\n"
        "- Before its first write, implement must produce an at-most-8-line semantic_challenge that marks each critical EditBrief assumption proven, contradicted, or unproven using code/reproducer evidence. Coordinator line edits are hypotheses, not authority.\n"
        "- implement may only read and edit inside allowed_ranges. It must not search the repository, run commands, inspect git history, or read whole files.\n"
        "- Prefer an exact original snippet or line_replacements. Change the contract owner when its public output is wrong; do not merely wrap a downstream symptom.\n"
        "- Require implement to report changed_files, semantic_coverage, preserved old behavior, and known risks, then keep its first non-empty patch as the solution. Do not run corrective implement for heuristic coverage/order warnings.\n"
        "- Only call patch_review when the implement tool explicitly returns `Conditional patch review required`; review is read-only attribution and must not trigger a second solution attempt.\n"
        "- Static safety is checked deterministically: empty/whitespace patch, git diff --check, syntax, test-file edits, and narrow repository policies.\n"
        "- Run at most one focused verification after a non-empty diff, and only when a trustworthy public command is available. It must return JSON with status, command, exit_code, and evidence.\n"
        "- Verification JSON must include covered_cases and cover every required SemanticEvidence case. The single command must execute behavior directly; do not spend it creating a script or asserting a proposed internal representation instead of public output.\n"
        "- A verify command must assert the requested post-fix behavior. It must not pass by confirming the old exception, `unknown` classification, or other pre-fix bug.\n"
        "- Missing dependencies, command-not-found, or weak verification are inconclusive rather than proof that the patch is wrong; preserve a statically safe patch for the final harness and report the evidence class honestly.\n"
        "- Never install dependencies, run pip install, create virtual environments, or repair the SWE-bench Docker environment from inside the agent.\n"
        "- If an API call is involved, read the target signature or nearby existing usage before choosing parameter names.\n"
        "- Separate contract_owner from symptom_site. When producer output breaks a consumer, verify the producer's public return contract and sibling methods before adding downstream reshape/cast/ravel wrappers.\n"
        "- For classification/dispatch/validation pipelines, place normalization before the first predicate that depends on it; changing only a later return branch is too late.\n"
        "- Trace the first dependent predicate from the public entry point, including helper calls that can return early. A late dtype fallback is not the first predicate merely because it exposes the observed error.\n"
        "- Split every representation named or directly implied by the public issue into a concrete expected behavior, and prefer an existing repository normalization helper over ad-hoc conversion.\n"
        "- Keep canonical lookup identity separate from rendered identity; when fallback resolves an alias, propagate the resolved identity through its direct consumer.\n"
        "- Do not replace unsupported semantics with a fixed precision/default, and do not remove sequence/state updates without proving shape/cardinality/closure/lifecycle invariants.\n"
        "- If CLI output, exception messages, lint descriptions, or rule text are involved, preserve exact expected strings.\n"
        "- Do not modify tests unless the problem explicitly asks for test-only changes.\n"
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


def read_failure_excerpt(path: Path, max_chars: int = 6_000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
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
    """把 Agent 内部阶段记录与 harness 结果合并，给出单一失败归因。"""
    attribution = json.loads(json.dumps(pass_summary.get("stage_attribution") or {}))
    attribution.setdefault("schema_version", 1)
    attribution.setdefault("pipeline", "simplified_pass1")
    patch_validation = pass_summary.get("patch_validation") or {}
    harness = pass_summary.get("harness") or {}
    tests = _harness_test_counts(harness.get("instance_report") or {})

    if pass_summary.get("status") == "agent_error":
        primary = "agent_runtime"
    elif harness.get("error"):
        primary = "harness_environment"
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
