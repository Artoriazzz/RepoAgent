from uuid import uuid4

import json

from repo_agent.worker.memory_query import (
    build_memory_query,
    extract_memory_query_features,
    merge_memory_query_features,
)
from repo_agent.worker.models import RepairTaskContext


def test_memory_query_falls_back_to_issue_text_when_no_analysis_features_exist():
    context = _context()

    query = build_memory_query(context)

    assert query.features == {}
    assert query.fallback_text == "Fix parser\nReader rejects valid input"


def test_memory_query_uses_only_supplied_stable_features():
    context = _context(memory_query_features={
        "object": "nullable numeric array",
        "operation": "classify target type",
        "symptom": "known numeric input becomes unknown",
        "expectedBehavior": "match ordinary numeric arrays",
        "triggerConditions": "extension dtype with missing values",
        "unverifiedRootCause": "must not be sent",
    })

    query = build_memory_query(context)

    assert query.features == {
        "object": "nullable numeric array",
        "operation": "classify target type",
        "symptom": "known numeric input becomes unknown",
        "expectedBehavior": "match ordinary numeric arrays",
        "triggerConditions": "extension dtype with missing values",
    }
    assert "unverifiedRootCause" not in query.features


def test_coordinator_attribution_refines_only_direct_public_contract_facts(tmp_path):
    tmp_path.joinpath("stage_attribution.json").write_text(
        json.dumps({
            "stages": {
                "reproduction": {
                    "semantic_evidence": {
                        "reproduction_contract": {
                            "trigger": ["nullable dtype with a missing value"],
                            "current_behavior": ["the public classifier returns unknown"],
                            "expected_behavior": ["classify it like an ordinary numeric array"],
                            "unverified_root_cause": "do not include this guess",
                        }
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    features = extract_memory_query_features(tmp_path)

    assert features == {
        "symptom": "the public classifier returns unknown",
        "expectedBehavior": "classify it like an ordinary numeric array",
        "triggerConditions": "nullable dtype with a missing value",
    }


def test_merge_keeps_existing_stable_context_and_adds_missing_contract_fields():
    merged = merge_memory_query_features(
        {"object": "array classifier", "operation": "classify"},
        {"object": "incorrect replacement", "symptom": "returns unknown"},
    )

    assert merged == {
        "object": "array classifier",
        "operation": "classify",
        "symptom": "returns unknown",
    }


def _context(**overrides) -> RepairTaskContext:
    task_id = uuid4()
    values = {
        "task_id": task_id,
        "issue_id": uuid4(),
        "repository_id": uuid4(),
        "repository_provider": "SWE_BENCH",
        "repository_external_id": "owner/repo",
        "mirror_key": "owner__repo",
        "github_repository_id": None,
        "repository_full_name": "owner/repo",
        "clone_url": None,
        "default_branch": "main",
        "github_issue_number": None,
        "issue_external_id": "owner__repo-1",
        "benchmark_dataset_split": "lite",
        "issue_title": "Fix parser",
        "issue_body": "Reader rejects valid input",
        "base_sha": "abc",
        "branch_name": f"run-{task_id}",
    }
    values.update(overrides)
    return RepairTaskContext(**values)
