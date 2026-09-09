from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


OUTCOME_KEYS = ("resolved_ids", "unresolved_ids", "empty_patch_ids", "error_ids")
LOG_ARTIFACTS = ("patch.diff", "report.json", "test_output.txt")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prepare_submission(
        dataset=args.dataset,
        submission_dir=args.submission_dir,
        run_dirs=args.run_dir,
        eval_dirs=args.eval_dir,
        report_paths=args.report,
        entry_name=args.entry_name,
        site=args.site,
        technical_report=args.technical_report,
        authors=args.author,
        models=args.model,
        organization=args.organization,
        source_revisions=args.source_revision,
        draft_reason=args.draft_reason,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a SWE-bench experiments/evaluation/lite submission directory."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--submission-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument(
        "--eval-dir",
        type=Path,
        action="append",
        default=[],
        help="Harness run directory. Later directories replace earlier artifacts for the same instance.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        default=[],
        help="Harness report. Later reports replace earlier outcomes for the same instance.",
    )
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--technical-report", required=True)
    parser.add_argument("--author", action="append", required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--organization", default="RepoAgent")
    parser.add_argument("--source-revision", action="append", default=[])
    parser.add_argument(
        "--draft-reason",
        default="",
        help="Non-empty text marks the bundle as not ready for a leaderboard pull request.",
    )
    return parser


def prepare_submission(
    *,
    dataset: Path,
    submission_dir: Path,
    run_dirs: list[Path],
    eval_dirs: list[Path],
    report_paths: list[Path],
    entry_name: str,
    site: str,
    technical_report: str,
    authors: list[str],
    models: list[str],
    organization: str,
    source_revisions: list[str],
    draft_reason: str,
) -> dict[str, Any]:
    if submission_dir.exists() and any(submission_dir.iterdir()):
        raise ValueError(f"Submission directory is not empty: {submission_dir}")
    submission_dir.mkdir(parents=True, exist_ok=True)

    dataset_rows = load_dataset_rows(dataset)
    dataset_ids = [str(row["instance_id"]) for row in dataset_rows]
    if len(dataset_ids) != len(set(dataset_ids)):
        raise ValueError("Dataset contains duplicate instance ids")

    predictions, trace_sources = collect_predictions_and_traces(run_dirs)
    validate_prediction_coverage(dataset_ids, predictions)
    ordered_predictions = [predictions[instance_id] for instance_id in dataset_ids]
    write_jsonl(submission_dir / "all_preds.jsonl", ordered_predictions)

    traj_dir = submission_dir / "trajs"
    traj_dir.mkdir()
    missing_traces: list[str] = []
    for instance_id in dataset_ids:
        trace_source = trace_sources.get(instance_id)
        if trace_source is None or not trace_source.is_file():
            missing_traces.append(instance_id)
            continue
        shutil.copy2(trace_source, traj_dir / f"{instance_id}.jsonl")
    if missing_traces:
        raise ValueError(f"Missing inference-time traces for {len(missing_traces)} instances: {missing_traces}")

    artifact_sources = collect_log_artifacts(eval_dirs)
    logs_dir = submission_dir / "logs"
    logs_dir.mkdir()
    copied_log_ids: list[str] = []
    for instance_id in dataset_ids:
        artifacts = artifact_sources.get(instance_id, {})
        if not all(name in artifacts for name in LOG_ARTIFACTS):
            continue
        expected_patch = str(predictions[instance_id].get("model_patch", ""))
        actual_patch = artifacts["patch.diff"].read_text(encoding="utf-8", errors="replace")
        if actual_patch != expected_patch:
            raise ValueError(f"Harness patch differs from prediction for {instance_id}")
        target = logs_dir / instance_id
        target.mkdir()
        for name in LOG_ARTIFACTS:
            shutil.copy2(artifacts[name], target / name)
        copied_log_ids.append(instance_id)

    outcomes = collect_outcomes(report_paths)
    status_counts = Counter(outcomes.get(instance_id, "missing") for instance_id in dataset_ids)
    patch_empty_ids = [
        row["instance_id"] for row in ordered_predictions if not str(row.get("model_patch", "")).strip()
    ]
    inconsistent_empty = sorted(
        set(patch_empty_ids)
        ^ {instance_id for instance_id, status in outcomes.items() if status == "empty_patch"}
    )
    if report_paths and inconsistent_empty:
        raise ValueError(f"Prediction/report empty-patch mismatch: {inconsistent_empty}")

    resolved_ids = {instance_id for instance_id, status in outcomes.items() if status == "resolved"}
    resolved_by_repo, resolved_by_time = summarize_resolved(dataset_rows, resolved_ids)
    results_dir = submission_dir / "results"
    results_dir.mkdir()
    write_json(
        results_dir / "results.json",
        {
            "no_generation": sorted(patch_empty_ids),
            "no_logs": [],
            "resolved": sorted(resolved_ids),
        },
    )
    write_json(results_dir / "resolved_by_repo.json", resolved_by_repo)
    write_json(results_dir / "resolved_by_time.json", resolved_by_time)

    manifest = {
        "entry_name": entry_name,
        "draft": bool(draft_reason),
        "draft_reason": draft_reason,
        "dataset": str(dataset),
        "instances": len(dataset_ids),
        "predictions": len(ordered_predictions),
        "trajectories": len(dataset_ids) - len(missing_traces),
        "evaluation_log_folders": len(copied_log_ids),
        "empty_patches": len(patch_empty_ids),
        "outcomes": dict(sorted(status_counts.items())),
        "resolved_by_repo": resolved_by_repo,
        "resolved_by_time": resolved_by_time,
        "models": models,
        "source_revisions": source_revisions,
        "run_dirs": [str(path) for path in run_dirs],
        "eval_dirs": [str(path) for path in eval_dirs],
        "reports": [str(path) for path in report_paths],
    }
    write_json(submission_dir / "submission_manifest.json", manifest)
    write_metadata(
        submission_dir / "metadata.yaml",
        entry_name=entry_name,
        site=site,
        technical_report=technical_report,
        authors=authors,
        models=models,
        organization=organization,
    )
    write_readme(
        submission_dir / "README.md",
        entry_name=entry_name,
        manifest=manifest,
        site=site,
        technical_report=technical_report,
    )
    write_pr_description(submission_dir / "PR_DESCRIPTION.md", entry_name, manifest, authors)
    secret_files = scan_potential_secrets(submission_dir)
    if secret_files:
        raise ValueError(f"Potential API credentials found in submission files: {secret_files}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def load_dataset_rows(dataset: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required to read the SWE-bench parquet dataset") from exc
    return [row.to_dict() for _, row in pd.read_parquet(dataset).iterrows()]


def collect_predictions_and_traces(
    run_dirs: list[Path],
) -> tuple[dict[str, dict[str, str]], dict[str, Path]]:
    predictions: dict[str, dict[str, str]] = {}
    traces: dict[str, Path] = {}
    for run_dir in run_dirs:
        rows = json.loads((run_dir / "predictions.json").read_text(encoding="utf-8"))
        for row in rows:
            instance_id = str(row["instance_id"])
            if instance_id in predictions:
                raise ValueError(f"Duplicate prediction across run directories: {instance_id}")
            predictions[instance_id] = {
                "instance_id": instance_id,
                "model_name_or_path": str(row["model_name_or_path"]),
                "model_patch": str(row.get("model_patch", "")),
            }
            trace_path = run_dir / instance_id / "pass-1" / "trace.jsonl"
            traces[instance_id] = trace_path
    return predictions, traces


def validate_prediction_coverage(
    dataset_ids: list[str], predictions: dict[str, dict[str, str]]
) -> None:
    expected = set(dataset_ids)
    actual = set(predictions)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"Prediction coverage mismatch: missing={missing}, extra={extra}")


def collect_log_artifacts(eval_dirs: list[Path]) -> dict[str, dict[str, Path]]:
    artifacts: dict[str, dict[str, Path]] = {}
    for eval_dir in eval_dirs:
        if not eval_dir.is_dir():
            continue
        for patch_path in eval_dir.rglob("patch.diff"):
            instance_dir = patch_path.parent
            instance_id = instance_dir.name
            current = {
                name: instance_dir / name
                for name in LOG_ARTIFACTS
                if (instance_dir / name).is_file()
            }
            if current:
                artifacts[instance_id] = current
    return artifacts


def collect_outcomes(report_paths: list[Path]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for report_path in report_paths:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for key in OUTCOME_KEYS:
            status = key.removesuffix("_ids")
            for instance_id in report.get(key, []):
                outcomes[str(instance_id)] = status
    return outcomes


def summarize_resolved(
    dataset_rows: list[dict[str, Any]], resolved_ids: set[str]
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    by_repo: dict[str, dict[str, int]] = {}
    by_time: dict[str, dict[str, int]] = {}
    for row in dataset_rows:
        instance_id = str(row["instance_id"])
        repo = str(row["repo"])
        year = str(datetime.fromisoformat(str(row["created_at"]).rstrip("Z")).year)
        for mapping, key in ((by_repo, repo), (by_time, year)):
            value = mapping.setdefault(key, {"resolved": 0, "total": 0})
            value["total"] += 1
            if instance_id in resolved_ids:
                value["resolved"] += 1
    return dict(sorted(by_repo.items())), dict(sorted(by_time.items(), key=lambda item: int(item[0])))


def write_metadata(
    path: Path,
    *,
    entry_name: str,
    site: str,
    technical_report: str,
    authors: list[str],
    models: list[str],
    organization: str,
) -> None:
    lines = [
        "info:",
        '  logo: ""',
        f"  name: {yaml_string(entry_name)}",
        f"  site: {yaml_string(site)}",
        f"  report: {yaml_string(technical_report)}",
        "  authors:",
        *[f"    - {yaml_string(author)}" for author in authors],
        "tags:",
        "  checked: false",
        "model:",
        *[f"  - {yaml_string(model)}" for model in models],
        "org:",
        f"  - {yaml_string(organization)}",
        "os_model: false",
        "os_system: true",
        "system:",
        '  attempts: "1"',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_readme(
    path: Path,
    *,
    entry_name: str,
    manifest: dict[str, Any],
    site: str,
    technical_report: str,
) -> None:
    outcomes = manifest["outcomes"]
    resolved = int(outcomes.get("resolved", 0))
    instances = int(manifest["instances"])
    rate = resolved * 100 / instances if instances else 0.0
    draft = ""
    if manifest["draft"]:
        draft = f"\n> DRAFT: {manifest['draft_reason']}\n"
    text = f"""# {entry_name}
{draft}
## Results

Resolved {resolved}/{instances} SWE-bench Lite instances ({rate:.2f}%).

- Unresolved: {outcomes.get('unresolved', 0)}
- Empty predictions: {outcomes.get('empty_patch', 0)}
- Harness errors after re-evaluation: {outcomes.get('error', 0)}

## System

RepoAgent is an open-source coordinator-style repository repair agent. A Coordinator performs scoped localization and creates an EditBrief, an Implement Worker makes one constrained source edit, and deterministic checks reject only malformed or unsafe patches. The submitted run uses one prediction per task and does not use SWE-bench hints, PASS_TO_PASS, FAIL_TO_PASS, gold patches, hidden tests, or harness feedback during inference.

Models used:

{chr(10).join(f'- `{model}`' for model in manifest['models'])}

Source code: {site}

Technical report: {technical_report}

## Inference Policy

- Pass@1 with one submitted patch per instance.
- Repository source and the public issue statement are the only task-specific inputs.
- Web browsing is unavailable to the Agent; repository access is restricted to the checked-out base commit.
- Repair memory evolution is disabled for the official candidate runs.
- Evaluation is performed only after the prediction has been finalized.

## Artifacts

- `all_preds.jsonl`: {manifest['predictions']} predictions in dataset order.
- `trajs/`: {manifest['trajectories']} inference-time JSONL traces.
- `logs/`: {manifest['evaluation_log_folders']} harness artifact folders. Empty predictions have no harness execution folder.
- `metadata.yaml`: leaderboard metadata.
"""
    path.write_text(text, encoding="utf-8")


def write_pr_description(
    path: Path, entry_name: str, manifest: dict[str, Any], authors: list[str]
) -> None:
    outcomes = manifest["outcomes"]
    resolved = int(outcomes.get("resolved", 0))
    instances = int(manifest["instances"])
    rate = resolved * 100 / instances if instances else 0.0
    repo_lines = format_result_breakdown(manifest["resolved_by_repo"])
    time_lines = format_result_breakdown(manifest["resolved_by_time"])
    text = f"""# {entry_name}

## `analysis.get_results` output

```text
Submission summary for {entry_name} on SWE-bench lite split
==================================================
Resolved {resolved} instances ({rate:.2f}%)
==================================================
Resolved by Repository
{repo_lines}
==================================================
Resolved by Time
{time_lines}
```

## Authors

{chr(10).join(f'- {author}' for author in authors)}

## Submission Checklist

- [x] Is a pass@1 submission (does not attempt the same task instance more than once)
- [x] Does not use SWE-bench test knowledge (`PASS_TO_PASS`, `FAIL_TO_PASS`)
- [x] Does not use the `hints` field in SWE-bench
- [x] Does not have web browsing during inference
- [ ] Technical report or blog URL is public and final
- [ ] All 300 predictions were generated with one frozen source revision and configuration
- [ ] `python -m analysis.get_results evaluation/lite/<submission>` completed successfully
- [ ] First author profile is included and @john-b-yang has push access to the branch
"""
    path.write_text(text, encoding="utf-8")


def format_result_breakdown(values: dict[str, dict[str, int]]) -> str:
    lines = []
    for key, value in values.items():
        resolved = value["resolved"]
        total = value["total"]
        rate = resolved * 100 / total if total else 0.0
        lines.append(f"- {key}: {resolved}/{total} ({rate:.2f}%)")
    return "\n".join(lines)


def scan_potential_secrets(root: Path) -> list[str]:
    api_key_pattern = re.compile(rb"sk-[A-Za-z0-9_-]{16,}")
    matches = []
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 20_000_000:
            continue
        if api_key_pattern.search(path.read_bytes()):
            matches.append(str(path.relative_to(root)))
    return sorted(matches)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
