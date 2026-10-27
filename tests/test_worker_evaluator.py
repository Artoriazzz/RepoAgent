import json
import subprocess

from repo_agent.worker.evaluator import HistoricalReplayEvaluator, SweBenchHarnessEvaluator
from tests.test_worker_service import _context
from dataclasses import replace


def test_harness_evaluator_reads_resolved_report_and_test_counts(tmp_path, monkeypatch):
    context = replace(
        _context(),
        repository_provider="SWE_BENCH",
        repository_external_id="pallets/flask",
        mirror_key="pallets__flask",
        github_repository_id=None,
        clone_url=None,
        github_issue_number=None,
        issue_external_id="pallets__flask-4045",
    )
    swebench_dir = tmp_path / "swebench"
    swebench_dir.mkdir()
    report = swebench_dir / "result.json"
    report.write_text(json.dumps({"resolved_ids": [context.issue_external_id]}), encoding="utf-8")
    instance_report = (
        swebench_dir
        / "logs"
        / "run_evaluation"
        / f"service-{context.task_id}"
        / "repoagent-test-model"
        / context.issue_external_id
        / "report.json"
    )
    instance_report.parent.mkdir(parents=True)
    instance_report.write_text(
        json.dumps({
            context.issue_external_id: {
                "tests_status": {
                    "FAIL_TO_PASS": {"success": ["f1", "f2"], "failure": []},
                    "PASS_TO_PASS": {"success": ["p1"], "failure": []},
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"Report written to {report}\n", stderr=None
        ),
    )
    evaluator = SweBenchHarnessEvaluator(
        tmp_path / "dataset.parquet",
        swebench_dir,
        "test-model",
    )

    result = evaluator.evaluate(context, "diff --git a/a b/a\n", tmp_path / "output")

    assert result.status == "RESOLVED"
    assert result.fail_to_pass_passed == 2
    assert result.pass_to_pass_passed == 1


def test_historical_evaluator_replays_result_without_harness(tmp_path):
    context = replace(
        _context(),
        repository_provider="SWE_BENCH",
        issue_external_id="pallets__flask-4045",
    )
    reports_root = tmp_path / "logs"
    report_path = reports_root / context.issue_external_id / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps({
            context.issue_external_id: {
                "resolved": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": ["f1"], "failure": ["f2"]},
                    "PASS_TO_PASS": {"success": ["p1", "p2"], "failure": []},
                },
            }
        }),
        encoding="utf-8",
    )
    evaluator = HistoricalReplayEvaluator(reports_root)

    result = evaluator.evaluate(context, "historical patch", tmp_path / "output")

    assert result.status == "UNRESOLVED"
    assert result.fail_to_pass_passed == 1
    assert result.fail_to_pass_failed == 1
    assert result.pass_to_pass_passed == 2
    assert "Historical SWE-bench result replayed" in result.summary
    assert (tmp_path / "output/evaluation/historical_report.json").is_file()


def test_historical_evaluator_marks_missing_report_as_harness_error(tmp_path):
    context = replace(
        _context(),
        repository_provider="SWE_BENCH",
        issue_external_id="missing-case",
    )

    result = HistoricalReplayEvaluator(tmp_path / "logs").evaluate(
        context, "historical patch", tmp_path / "output"
    )

    assert result.status == "HARNESS_ERROR"
    assert "not found" in result.summary
