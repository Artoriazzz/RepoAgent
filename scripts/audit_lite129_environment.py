"""Read-only audit of the historical 129 unresolved Lite cases on WSL."""

import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path("/home/artoria/workspace/SWE-bench/agent_work/runs")
SELECTION = Path("/tmp/repoagent-lite-remaining119-selection.json")
RUNS = [
    "lite-invalid27-localization-recovery",
    "lite-invalid12-observation-followup",
    "lite-remaining119-public-behavior-20261119",
    "lite-remaining119-public-behavior-resume-20261120",
    "lite-remaining119-from-issue38-20261120",
    "lite-remaining119-from-issue38-resume-20261120",
]
RECOVERY = [
    "lite119-infra-recovery-20261121",
    "lite119-infra-recovery-v2-20261121",
    "lite119-infra-recovery-v3-20261121",
]
SIGNATURES = {
    "pytest_version_setup": r"minversion requires pytest-2\.0|actual pytest-0\.1\.dev",
    "matplotlib_scm_warning": r"Version scheme 'release-branch-semver' has been renamed",
    "matplotlib_numpy_pandas_mismatch": r"Please upgrade numpy to >= 1\.26\.0 to use this pandas version",
    "flask_werkzeug_mismatch": r"as_tuple|werkzeug\.urls\.url_parse",
    "network_or_proxy": r"ProxyError|Connection refused|ConnectionError|NameResolutionError|Temporary failure in name resolution|Max retries exceeded|ReadTimeout|ConnectTimeout",
    "missing_dependency": r"ModuleNotFoundError|ImportError: No module named|PackagesNotFoundError",
    "test_collection_failure": r"ERROR collecting|Interrupted: .*error during collection",
    "resource_warning": r"ResourceWarning: unclosed|PytestUnraisableExceptionWarning",
}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def harness_from_summary(summary):
    for row in reversed(summary.get("passes", [])):
        harness = row.get("harness")
        if isinstance(harness, dict):
            return harness
    return None


def main():
    selection = read_json(SELECTION)
    diag_success = {x["instance_id"] for x in selection["diagnostic_resolved"]}
    diag_fail = set(selection["excluded_diagnostic_ids"]) - diag_success
    success119 = {
        "django__django-13660", "django__django-14411",
        "django__django-16229", "pylint-dev__pylint-7114",
        "scikit-learn__scikit-learn-10297", "scikit-learn__scikit-learn-13497",
        "scikit-learn__scikit-learn-14983", "scikit-learn__scikit-learn-25500",
        "scikit-learn__scikit-learn-25570", "sympy__sympy-18057",
    }
    fail119 = set(selection["run_ids"]) - success119
    assert len(diag_fail) == 20 and len(fail119) == 109
    failed = diag_fail | fail119
    assert len(failed) == 129

    rows = {}
    for instance_id in sorted(failed):
        row = {"instance_id": instance_id, "source": "diagnostic27" if instance_id in diag_fail else "remaining119"}
        history = []
        for run in RUNS:
            p = ROOT / run / "cases" / instance_id / "results" / instance_id / "summary.json"
            data = read_json(p)
            if data:
                history.append((run, data))
        if history:
            run, data = history[-1]
            row.update(run=run, status=data.get("status"), patch_chars=data.get("patch_chars"))
            harness = harness_from_summary(data)
            if harness:
                row["harness"] = harness
        for run in RECOVERY:
            p = ROOT / run / "evaluation" / instance_id / "result.json"
            data = read_json(p)
            if data:
                row.update(run=run, harness=data)
            p = ROOT / run / "cases" / instance_id / "results" / instance_id / "summary.json"
            data = read_json(p)
            if data:
                row.update(run=run, status=data.get("status"), patch_chars=data.get("patch_chars"))
                harness = harness_from_summary(data)
                if harness:
                    row["harness"] = harness
        harness = row.pop("harness", None)
        if harness:
            row["harness_error"] = harness.get("error")
            row["resolved"] = harness.get("resolved")
            report = harness.get("instance_report") or {}
            ts = report.get("tests_status") or {}
            row["f2p"] = [len(ts.get("FAIL_TO_PASS", {}).get(k, [])) for k in ("success", "failure")]
            row["p2p"] = [len(ts.get("PASS_TO_PASS", {}).get(k, [])) for k in ("success", "failure")]
            path = Path(harness.get("test_output_path") or "/nonexistent")
            try:
                log = path.read_text(errors="replace")
            except OSError:
                log = ""
            row["log_bytes"] = len(log)
            row["signatures"] = [name for name, pattern in SIGNATURES.items() if re.search(pattern, log, re.IGNORECASE)]
            row["raw_passed"] = bool(re.search(r"\b\d+ passed\b", log))
            row["raw_failed"] = bool(re.search(r"\b\d+ failed\b", log))
            row["test_output_path"] = str(path)
        rows[instance_id] = row
    print(json.dumps({
        "counts": {
            "total": len(rows),
            "has_harness": sum("harness_error" in r for r in rows.values()),
            "harness_error": sum(r.get("harness_error") is True for r in rows.values()),
            "no_harness": sum("harness_error" not in r for r in rows.values()),
            "by_signature": dict(Counter(s for r in rows.values() for s in r.get("signatures", []))),
        },
        "rows": rows,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
