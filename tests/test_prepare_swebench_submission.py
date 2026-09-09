from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.prepare_swebench_submission import (
    collect_log_artifacts,
    collect_outcomes,
    collect_predictions_and_traces,
    scan_potential_secrets,
    summarize_resolved,
    validate_prediction_coverage,
)


def test_collect_predictions_rejects_duplicate_instance(tmp_path: Path) -> None:
    runs = [tmp_path / "run-a", tmp_path / "run-b"]
    for run in runs:
        run.mkdir()
        (run / "predictions.json").write_text(
            json.dumps(
                [
                    {
                        "instance_id": "owner__repo-1",
                        "model_name_or_path": "model",
                        "model_patch": "diff",
                    }
                ]
            ),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="Duplicate prediction"):
        collect_predictions_and_traces(runs)


def test_later_eval_directory_overlays_artifacts(tmp_path: Path) -> None:
    first = tmp_path / "first" / "model" / "owner__repo-1"
    second = tmp_path / "second" / "model" / "owner__repo-1"
    for directory, marker in ((first, "old"), (second, "new")):
        directory.mkdir(parents=True)
        for name in ("patch.diff", "report.json", "test_output.txt"):
            (directory / name).write_text(marker, encoding="utf-8")

    artifacts = collect_log_artifacts([tmp_path / "first", tmp_path / "second"])

    assert artifacts["owner__repo-1"]["patch.diff"].read_text(encoding="utf-8") == "new"


def test_later_report_overlays_error_outcome(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"error_ids": ["owner__repo-1"]}), encoding="utf-8")
    second.write_text(json.dumps({"resolved_ids": ["owner__repo-1"]}), encoding="utf-8")

    assert collect_outcomes([first, second]) == {"owner__repo-1": "resolved"}


def test_prediction_coverage_reports_missing_and_extra() -> None:
    with pytest.raises(ValueError, match="missing=.*owner__repo-2.*extra=.*owner__repo-3"):
        validate_prediction_coverage(
            ["owner__repo-1", "owner__repo-2"],
            {
                "owner__repo-1": {},
                "owner__repo-3": {},
            },
        )


def test_summarize_resolved_groups_by_repo_and_year() -> None:
    rows = [
        {
            "instance_id": "owner__repo-1",
            "repo": "owner/repo",
            "created_at": "2021-01-02T00:00:00Z",
        },
        {
            "instance_id": "owner__repo-2",
            "repo": "owner/repo",
            "created_at": "2022-03-04T00:00:00Z",
        },
    ]

    by_repo, by_time = summarize_resolved(rows, {"owner__repo-2"})

    assert by_repo == {"owner/repo": {"resolved": 1, "total": 2}}
    assert by_time == {
        "2021": {"resolved": 0, "total": 1},
        "2022": {"resolved": 1, "total": 1},
    }


def test_scan_potential_secrets_reports_only_credential_shaped_values(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("OPENAI_API_KEY is configured outside the trace", encoding="utf-8")
    (tmp_path / "unsafe.txt").write_text("token=sk-abcdefghijklmnop1234", encoding="utf-8")

    assert scan_potential_secrets(tmp_path) == ["unsafe.txt"]
