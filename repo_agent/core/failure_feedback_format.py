"""Translate between the model's four-part report and the stored workflow record."""

import json

_SECTIONS = {"failure", "repair", "evidence", "decision"}
_ACTIONS = {"correct": "actionable", "replan": "needs_replan", "stop": "insufficient_evidence",
            "regenerate_tests": "not_needed"}


def flatten_failure_feedback(payload: dict, *, require_sections: bool = False) -> dict | None:
    if not require_sections and ("failure_stage" in payload or not (_SECTIONS & payload.keys())):
        return payload
    if set(payload) != _SECTIONS or any(not isinstance(payload[key], dict) for key in _SECTIONS):
        return None
    failure, repair, evidence, decision = (payload[key] for key in ("failure", "repair", "evidence", "decision"))
    action, request = decision.get("action"), decision.get("request")
    if not isinstance(action, str) or action not in _ACTIONS or not isinstance(request, str):
        return None
    if (action == "correct" and request.strip()) or (action != "correct" and not request.strip()):
        return None
    kind = failure.get("kind")
    if action == "regenerate_tests" and kind != "invalid_test":
        return None
    readiness = "not_needed" if action == "stop" and kind in ("complete", "invalid_test") else _ACTIONS[action]
    return {
        "failure_stage": kind,
        "observed_failure": failure.get("observations"),
        "failure_observation_quality": failure.get("quality"),
        "root_cause": failure.get("cause"),
        "required_edits": repair.get("edits"),
        "regression_guards": repair.get("preserve"),
        "evidence_ledger": evidence.get("items"),
        "correction_evidence_ids": evidence.get("used_ids"),
        "correction_readiness": readiness,
        "replan_reason": request if action == "replan" else "",
        "stop_reason": request if action == "stop" else "",
        "test_revision_request": request if action == "regenerate_tests" else "",
        "scope_expansion": [],
    }


def format_failure_handoff(payload: dict) -> dict:
    return {
        "failure": {
            "kind": payload.get("failure_stage"),
            "observations": payload.get("observed_failure", []),
            "quality": payload.get("failure_observation_quality"),
            "cause": payload.get("root_cause", ""),
        },
        "repair": {"edits": payload.get("required_edits", []), "preserve": payload.get("regression_guards", [])},
        "evidence": {"items": payload.get("evidence_ledger", []), "used_ids": payload.get("correction_evidence_ids", [])},
        "decision": {
            "action": {"corrective_implement": "correct", "coordinator_replan": "replan",
                       "test_regeneration": "regenerate_tests"}.get(
                payload.get("correction_route"), "stop"
            ),
            "request": payload.get("replan_reason") or payload.get("test_revision_request") or payload.get("stop_reason", ""),
        },
    }


FAILURE_HANDOFF_GUIDE = (
    "failure.observations records what failed; failure.cause explains which code operation caused it. "
    "Apply repair.edits: each item names a repository-relative path, inclusive current source lines, and what to change. "
    "Keep the behaviors in repair.preserve. Use evidence.used_ids to find the supporting entries in evidence.items; "
    "direct entries are checkable facts, inference entries are explanations, not independent edit authorization. "
    "decision.action is the routing decision; decision.request describes what to inspect or authorize for a replan. "
    "Neither this report nor its line numbers grant write access outside the supplied edit scope."
)


def render_failure_handoff(payload: dict) -> str:
    return json.dumps(format_failure_handoff(payload), ensure_ascii=False, indent=2) + "\n\n" + FAILURE_HANDOFF_GUIDE
