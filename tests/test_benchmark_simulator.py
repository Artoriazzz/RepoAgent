import json

import pytest

from repo_agent.benchmark.simulator import build_payload, load_rows, select_rows


def test_build_payload_exposes_only_public_instance_fields():
    payload = build_payload(
        {
            "instance_id": "pallets__flask-4045",
            "repo": "pallets/flask",
            "base_commit": "abcdef1",
            "problem_statement": "Fix the parser.",
            "patch": "gold patch must not be sent",
            "FAIL_TO_PASS": "hidden test must not be sent",
        },
        "lite",
    )

    assert payload == {
        "instanceId": "pallets__flask-4045",
        "repository": "pallets/flask",
        "baseCommit": "abcdef1",
        "problemStatement": "Fix the parser.",
        "datasetSplit": "lite",
    }


def test_load_and_select_jsonl_instances(tmp_path):
    dataset = tmp_path / "instances.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps({"instance_id": value}) for value in ("one", "two", "three")
        ),
        encoding="utf-8",
    )

    assert [row["instance_id"] for row in select_rows(load_rows(dataset), [], 1, 1)] == ["two"]
    assert [row["instance_id"] for row in select_rows(load_rows(dataset), ["three"], 0, None)] == ["three"]


def test_select_rows_rejects_unknown_instance():
    with pytest.raises(ValueError, match="Instances not found"):
        select_rows([{"instance_id": "one"}], ["missing"], 0, None)
