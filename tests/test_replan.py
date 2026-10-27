import json

import pytest

from repo_agent.core.replan import validate_replan
from repo_agent.core.experience import (
    parse_failure_feedback, required_edit_scope_issues, build_failure_review_prompt,
    build_previous_failure_constraints,
)
from repo_agent.core.subagent import _failure_review_completion_guard


@pytest.mark.parametrize("problem", [None, "unread", "test_file", "missing_ranges", "wide", "escape"])
def test_replan_requires_read_production_source_and_bounded_scope(tmp_path, problem):
    source = tmp_path / "module.py"
    source.write_text("value = 1\n" * 700)
    name = "module.py"
    read_files = {name}
    ranges = {name: [[1, 20]]}
    if problem == "unread":
        read_files = set()
    elif problem == "test_file":
        name = "test_module.py"
        (tmp_path / name).write_text(source.read_text())
        read_files = {name}
        ranges = {name: [[1, 20]]}
    elif problem == "missing_ranges":
        ranges = {}
    elif problem == "wide":
        ranges[name] = [[1, 650]]
    elif problem == "escape":
        name = "../outside.py"
        ranges = {name: [[1, 20]]}
        read_files = {name}
    payload = {"status": "approved", "edit_brief": "Change the source-confirmed return only.",
               "allowed_files": [name], "allowed_ranges": ranges}
    reason = validate_replan(json.dumps(payload), tmp_path, read_files)
    assert (reason is None) == (problem is None)


@pytest.mark.parametrize("defect", [None, "unrelated_scope", "missing_reason", "missing_failure", "failure_only"])
def test_failure_review_routes_new_source_scope_to_coordinator(defect):
    payload = {
        "failure_stage": "implementation_semantics",
        "issue_contract": ["predict returns ndarray"],
        "observed_failure": ["predict returned DataFrame"],
        "failure_observation_quality": "expected_actual",
        "root_cause": "predict calls wrapped transform",
        "invalidated_assumption": "consumer-only fix was sufficient",
        "required_correction": "Confirm and repair producer.py predict",
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "fact": "actual DataFrame", "support": "direct"},
            {"id": "E2", "source": "public_task", "fact": "predict must return ndarray", "support": "direct"},
        ],
        "correction_evidence_ids": ["E1", "E2"], "scope_expansion": [],
        "regression_guards": ["preserve transform pandas output"],
        "correction_readiness": "needs_replan", "next_evidence_source": "repository_code",
        "replan_reason": "Need producer.py outside consumer.py edit scope", "confidence": 0.9,
    }
    payload["evidence_ledger"][0]["round"] = 0
    if defect == "unrelated_scope":
        payload["scope_expansion"] = ["Add unrelated serialization feature"]
    elif defect == "missing_reason":
        payload["replan_reason"] = ""
    elif defect == "missing_failure":
        payload["correction_evidence_ids"] = ["E2"]
    elif defect == "failure_only":
        payload["correction_evidence_ids"] = ["E1"]
        payload["failure_observation_quality"] = "failure_only"
    text = json.dumps(payload)
    feedback = parse_failure_feedback(text)
    assert feedback is not None
    assert feedback.to_dict()["evidence_ledger"][0]["round"] == 0
    allowed = defect in {None, "failure_only"}
    assert feedback.requires_coordinator_replan == allowed
    assert (_failure_review_completion_guard(text) is None) == allowed
    if defect == "failure_only":
        payload.update(correction_readiness="actionable", next_evidence_source="none")
        answer = json.dumps(payload)
        assert _failure_review_completion_guard(answer) is not None
        assert parse_failure_feedback(answer).correction_route == "none"


def test_required_edits_check_same_file_line_ranges():
    edits = [{"path": "module.py", "start_line": 580, "end_line": 585, "change": "caller"}]
    scope = {"allowed_files": ["module.py"], "allowed_ranges": {"module.py": [[198, 444]]}}
    assert required_edit_scope_issues(edits, scope) == ["module.py:580-585: caller"]
    scope["allowed_ranges"]["module.py"].append([575, 590])
    assert required_edit_scope_issues(edits, scope) == []


def test_replan_rejects_half_authorized_plan(tmp_path):
    (tmp_path / "module.py").write_text("value = 1\n" * 100)
    payload = {"status": "approved", "edit_brief": "Change caller and callee",
               "allowed_files": ["module.py"], "allowed_ranges": {"module.py": [[1, 20]]},
               "required_edits": [{"path": "module.py", "start_line": 80, "end_line": 90, "change": "caller"}]}
    assert "every required edit" in validate_replan(json.dumps(payload), tmp_path, {"module.py"})


def test_failure_review_receives_previous_regression_evidence():
    history = build_previous_failure_constraints(
        {"failure_evidence": "strip_signature_backslash=True requires escaped name"},
        {"regression_guards": ["preserve configured escaping"], "evidence_ledger": [
            {"id": "old-E1", "source": "repository_code", "fact": "conditional escaping", "support": "inference"}
        ]}, round_index=0,
    )
    prompt = build_failure_review_prompt(task="fix escaping", failed_patch="diff", failure_log="current failure",
        validation={"previous_failure_constraints": history})
    assert "strip_signature_backslash=True requires escaped name" in prompt
    assert "preserve configured escaping" in prompt
    assert history["evidence"][0]["source"] == "failure_log"
    assert history["evidence"][0]["support"] == "direct"
    assert history["evidence"][0]["round"] == 0
    assert history["evidence"][1]["support"] == "inference"
    assert "不得把 previous_failure_constraints 当成 source" in prompt


def test_section_name_is_not_an_allowed_evidence_source():
    from repo_agent.core.experience import FailureEvidence

    item = FailureEvidence("E1", "failure_log", "old failure", "direct", round=0)
    payload = {"failure_stage": "regression", "evidence_ledger": [item.to_dict()]}
    assert parse_failure_feedback(json.dumps(payload)) is not None
    payload["evidence_ledger"][0]["source"] = "previous_failure_constraints"
    assert parse_failure_feedback(json.dumps(payload)) is None


def test_failure_only_can_be_actionable_with_source_confirmed_mechanism():
    payload = {
        "failure_stage": "implementation_semantics", "issue_contract": ["print relation as lhs == rhs"],
        "observed_failure": ["assert print(Eq(x, y)) == 'x == y' failed"],
        "failure_observation_quality": "failure_only", "root_cause": "required handler is missing",
        "invalidated_assumption": "existing fallback handles relations",
        "required_correction": "add relation handler", "required_edits": [],
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "fact": "given equality assertion failed", "support": "direct"},
            {"id": "E2", "source": "public_task", "fact": "output is valid target language", "support": "direct"},
            {"id": "E3", "source": "repository_code", "fact": "printer.py:20 dispatch falls back for this unsupported type", "support": "direct"},
        ],
        "correction_evidence_ids": ["E1", "E2", "E3"], "scope_expansion": [], "regression_guards": [],
        "correction_readiness": "actionable", "next_evidence_source": "none", "confidence": 0.8,
    }
    answer = json.dumps(payload)
    assert _failure_review_completion_guard(answer) is None
    assert parse_failure_feedback(answer).correction_route == "corrective_implement"
    payload["evidence_ledger"][2]["support"] = "inference"
    answer = json.dumps(payload)
    assert _failure_review_completion_guard(answer) is not None
    assert parse_failure_feedback(answer).correction_route == "none"


def test_failure_review_reports_stage_and_evidence_errors_in_one_correction():
    payload = {
        "failure_stage": "localization",
        "issue_contract": ["Keep the existing API importable"],
        "observed_failure": ["ImportError: cannot import name 'existing_api'"],
        "failure_observation_quality": "exception_detail",
        "root_cause": "The insertion deleted the adjacent API definition",
        "invalidated_assumption": "The anchor function was preserved",
        "required_correction": "Restore the deleted definition",
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "fact": "existing_api import failed", "support": "direct"},
            {"id": "E2", "source": "repository_code", "fact": "caller.py imports existing_api", "support": "direct"},
            {"id": "E3", "source": "failed_patch", "fact": "patch removes def existing_api", "support": "direct"},
        ],
        "correction_evidence_ids": ["E1", "E2"],
        "scope_expansion": [], "regression_guards": [],
        "correction_readiness": "actionable", "next_evidence_source": "none", "confidence": 0.9,
    }
    reason = _failure_review_completion_guard(json.dumps(payload))
    assert "localization/problem_contract" in reason
    assert "direct Public Task, failed patch, or SemanticEvidence" in reason

    # 仅修正分类仍不能授权编辑；一次补全两处后才可进入纠正。
    payload["failure_stage"] = "implementation_semantics"
    assert _failure_review_completion_guard(json.dumps(payload)) is not None
    payload["correction_evidence_ids"].append("E3")
    answer = json.dumps(payload)
    assert _failure_review_completion_guard(answer) is None
    assert parse_failure_feedback(answer).correction_route == "corrective_implement"


def test_failure_review_identifies_invalid_source_and_missing_authorization_together():
    payload = {
        "failure_stage": "regression", "issue_contract": ["Preserve existing numeric formatting"],
        "observed_failure": ["Nested numeric formatting assertion failed"],
        "failure_observation_quality": "failure_only",
        "root_cause": "The new formatter bypasses parent formatting",
        "invalidated_assumption": "str(value) preserves printer context",
        "required_correction": "Delegate to parent formatting before conversion",
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "fact": "format assertion failed", "support": "direct"},
            {"id": "E2", "source": "repository_code", "fact": "parent formatter handles nesting", "support": "direct"},
            {"id": "E3", "source": "inference", "fact": "bypassing parent explains regression", "support": "inference"},
        ],
        "correction_evidence_ids": ["E1", "E2"], "scope_expansion": [], "regression_guards": [],
        "correction_readiness": "actionable", "next_evidence_source": "none", "confidence": 0.8,
    }
    reason = _failure_review_completion_guard(json.dumps(payload))
    assert "id=E3" in reason and "source='inference'" in reason
    assert "support=inference" in reason and "do not promote it to direct" in reason
    assert "direct Public Task, failed patch, or SemanticEvidence" in reason

    payload["evidence_ledger"][2]["source"] = "repository_code"
    assert _failure_review_completion_guard(json.dumps(payload)) is not None
    payload["evidence_ledger"].append(
        {"id": "E4", "source": "failed_patch", "fact": "formatter calls str(value)", "support": "direct"}
    )
    payload["correction_evidence_ids"].append("E4")
    answer = json.dumps(payload)
    assert _failure_review_completion_guard(answer) is None
    assert parse_failure_feedback(answer).correction_route == "corrective_implement"

    # 无论该条目是否被引用，未知来源都不能静默通过。
    payload["evidence_ledger"][2]["source"] = "inference"
    assert _failure_review_completion_guard(json.dumps(payload)) is not None
