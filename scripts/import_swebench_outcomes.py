#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from repo_agent.benchmark.simulator import build_payload, load_rows, submit_instance


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a balanced set of historically resolved and unresolved SWE-bench Issues."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--reports-root", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--success-count", type=int, default=10)
    parser.add_argument("--failure-count", type=int, default=10)
    parser.add_argument("--split", default="lite")
    args = parser.parse_args()

    rows = {str(row["instance_id"]): row for row in load_rows(args.dataset)}
    prediction_ids = _prediction_ids(args.predictions) if args.predictions else set(rows)
    outcomes = _load_outcomes(args.reports_root)
    successful_candidates = sorted(
        instance_id for instance_id, resolved in outcomes.items()
        if resolved and instance_id in rows and instance_id in prediction_ids
    )
    failed_candidates = sorted(
        instance_id for instance_id, resolved in outcomes.items()
        if not resolved and instance_id in rows and instance_id in prediction_ids
    )
    successful = _select_across_repositories(successful_candidates, rows, args.success_count)
    failed = _select_across_repositories(failed_candidates, rows, args.failure_count)
    if len(successful) != args.success_count or len(failed) != args.failure_count:
        raise RuntimeError(
            f"Not enough fixtures: successful={len(successful)}, failed={len(failed)}"
        )

    for historical_outcome, instance_ids in (("resolved", successful), ("unresolved", failed)):
        for instance_id in instance_ids:
            response = submit_instance(
                args.backend_url,
                args.user_id,
                build_payload(rows[instance_id], args.split),
                timeout=30,
                issues_only=True,
            )
            print(json.dumps({
                "instance_id": instance_id,
                "historical_outcome": historical_outcome,
                "issue_id": response["issueId"],
            }))


def _load_outcomes(root: Path) -> dict[str, bool]:
    outcomes: dict[str, bool] = {}
    for report_path in root.glob("*/report.json"):
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for instance_id, report in payload.items():
            if isinstance(report, dict) and isinstance(report.get("resolved"), bool):
                outcomes[str(instance_id)] = bool(report["resolved"])
    return outcomes


def _prediction_ids(path: Path) -> set[str]:
    return {
        str(row["instance_id"])
        for row in load_rows(path)
        if row.get("instance_id") and str(row.get("model_patch") or "").strip()
    }


def _select_across_repositories(
    candidates: list[str], rows: dict[str, dict], count: int
) -> list[str]:
    by_repository: dict[str, list[str]] = {}
    for instance_id in candidates:
        by_repository.setdefault(str(rows[instance_id]["repo"]), []).append(instance_id)
    selected: list[str] = []
    repositories = sorted(by_repository)
    while repositories and len(selected) < count:
        remaining: list[str] = []
        for repository in repositories:
            selected.append(by_repository[repository].pop(0))
            if by_repository[repository]:
                remaining.append(repository)
            if len(selected) == count:
                break
        repositories = remaining
    return selected


if __name__ == "__main__":
    main()
