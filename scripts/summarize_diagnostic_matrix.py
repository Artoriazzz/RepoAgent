from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


GROUPS = ("baseline", "gold_files", "gold_loci", "corrective")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize the four-way SWE-bench diagnostic matrix.")
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()

    groups = {name: load_group(args.root / name) for name in GROUPS}
    instance_ids = sorted(set().union(*(set(group) for group in groups.values())))
    rows: list[dict[str, Any]] = []
    bottlenecks: Counter[str] = Counter()
    corrective_outcomes: Counter[str] = Counter()

    for instance_id in instance_ids:
        results = {name: group_result(groups[name].get(instance_id)) for name in GROUPS}
        baseline = results["baseline"]
        gold_files = results["gold_files"]
        gold_loci = results["gold_loci"]
        corrective = results["corrective"]

        if baseline["submission_resolved"]:
            bottleneck = "end_to_end_success"
        elif baseline["candidate_resolved"]:
            bottleneck = "submission_gate"
        elif gold_files["candidate_resolved"]:
            bottleneck = "file_localization"
        elif gold_loci["candidate_resolved"]:
            bottleneck = "edit_locus_localization"
        else:
            bottleneck = "semantic_understanding_or_implementation"

        if not baseline["candidate_patch"]:
            corrective_outcome = "source_patch_generation_failed"
        elif corrective["candidate_resolved"]:
            corrective_outcome = "corrective_can_recover"
        else:
            corrective_outcome = "corrective_insufficient_with_explicit_failure"

        bottlenecks[bottleneck] += 1
        corrective_outcomes[corrective_outcome] += 1
        rows.append(
            {
                "instance_id": instance_id,
                "groups": results,
                "primary_bottleneck": bottleneck,
                "corrective_outcome": corrective_outcome,
            }
        )

    summary = {
        "diagnostic_only": True,
        "leaderboard_eligible": False,
        "count": len(rows),
        "group_totals": {
            name: {
                "submission_resolved": sum(int(row["groups"][name]["submission_resolved"]) for row in rows),
                "candidate_resolved": sum(int(row["groups"][name]["candidate_resolved"]) for row in rows),
                "nonempty_candidates": sum(int(row["groups"][name]["candidate_patch"]) for row in rows),
                "tokens": sum(int(row["groups"][name]["tokens"]) for row in rows),
            }
            for name in GROUPS
        },
        "bottleneck_counts": dict(bottlenecks),
        "corrective_outcome_counts": dict(corrective_outcomes),
        "rows": rows,
    }
    output = args.root / "diagnostic_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSummary: {output}")
    return 0


def load_group(path: Path) -> dict[str, dict[str, Any]]:
    summary_path = path / "batch_summary.json"
    if not summary_path.exists():
        return {}
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    return {item["instance_id"]: item for item in data.get("instances") or []}


def group_result(instance: dict[str, Any] | None) -> dict[str, Any]:
    if not instance:
        return {
            "present": False,
            "submission_resolved": False,
            "candidate_resolved": False,
            "candidate_patch": False,
            "candidate_patch_chars": 0,
            "status": "missing",
            "tokens": 0,
        }
    passes = instance.get("passes") or []
    last_pass = passes[-1] if passes else {}
    candidate_harness = last_pass.get("diagnostic_candidate_harness") or last_pass.get("harness") or {}
    attribution = last_pass.get("stage_attribution") or {}
    candidate_patch_chars = int(instance.get("candidate_patch_chars") or last_pass.get("patch_chars") or 0)
    return {
        "present": True,
        "submission_resolved": bool(instance.get("resolved")),
        "candidate_resolved": bool(candidate_harness.get("resolved")),
        "candidate_patch": candidate_patch_chars > 0,
        "candidate_patch_chars": candidate_patch_chars,
        "status": instance.get("status") or "",
        "tokens": int((attribution.get("token_usage") or {}).get("total") or 0),
        "pre_harness_attribution": attribution.get("pre_harness_attribution") or "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
