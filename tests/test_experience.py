import json

import pytest

from repo_agent.cli import main
from repo_agent.core.experience import (
    RepairRunMode,
    assess_lightweight_validation,
    augment_implement_with_episodes,
    augment_task_with_semantic_insights,
    build_failure_review_prompt,
    build_experience_curator_prompt,
    compact_failure_validation,
    constraints_from_patch,
    independent_validation_succeeded,
    parse_failure_feedback,
    repair_mode_policy,
)
from repo_agent.core.memory import MemoryStore
from repo_agent.core.subagent import FAILURE_REVIEW_PROMPT, EXPERIENCE_CURATOR_PROMPT


def test_repair_mode_policies_separate_learning_and_benchmark():
    learning = repair_mode_policy(RepairRunMode.EXPERIENCE_BUILD)
    benchmark = repair_mode_policy(RepairRunMode.BENCHMARK)

    assert learning.semantic_recall is False
    assert learning.episodic_recall is False
    assert learning.memory_write is True
    assert learning.feedback_correction is True
    assert learning.coordinator_replan is True
    assert learning.max_corrections == 1
    assert benchmark.semantic_recall is True
    assert benchmark.episodic_recall is True
    assert benchmark.memory_write is False
    assert benchmark.feedback_correction is False
    assert benchmark.max_corrections == 0


def test_failure_review_requires_public_result_and_invalidated_patch_assumption():
    prompt = build_failure_review_prompt(
        task="Return the exact generated field name.",
        failed_patch="diff --git a/a.py b/a.py\n+return 'fallback'",
        failure_log="candidate output did not match",
        validation={"status": "failed", "exit_code": 1},
    )

    assert "Return the exact generated field name." in prompt
    assert "原授权范围内可修：actionable；next_evidence_source=none" in FAILURE_REVIEW_PROMPT
    assert "有具体源码补查方向" in FAILURE_REVIEW_PROMPT
    assert "查询预算耗尽或观测仅为 failure_only 时仍可提出此请求" in FAILURE_REVIEW_PROMPT
    assert "Review Protocol" not in prompt
    assert prompt.count('"correction_readiness":') == 1


def test_failure_review_receives_implementation_risks_as_inference_only():
    prompt = build_failure_review_prompt(
        task="Preserve the slider geometry.",
        failed_patch="-xy[4] = xy[0]",
        failure_log="expected 0.25, got 0.0",
        validation={"status": "failed", "exit_code": 1},
        implementation_report={
            "changed_decision": "remove the closing vertex update",
            "preserve_cases": ["closed polygon geometry"],
            "known_risks": [
                {
                    "risk": "a consumer may require five vertices",
                    "overlaps_changed_contract": True,
                }
            ],
        },
    )

    assert "Initial Implementation Report" in prompt
    assert "a consumer may require five vertices" in prompt
    assert "implementation_report" in prompt
    assert "implementation_report/inference" in FAILURE_REVIEW_PROMPT


def test_curator_rules_live_in_system_and_payload_keeps_provenance_and_schema():
    prompt = build_experience_curator_prompt(task="public task", successful_patch="successful diff",
        validation={"status": "success", "exit_code": 0}, provenance="swebench-test-diagnostic")
    assert "swebench-test-diagnostic" in prompt
    assert "successful diff" in prompt
    assert prompt.count('"operations":') == 1
    assert "Merge Policy" not in prompt
    assert "Global Experience Policy" not in prompt
    assert "不能保存未经证实的失败归因" in EXPERIENCE_CURATOR_PROMPT
    assert "NOOP" in EXPERIENCE_CURATOR_PROMPT


def test_implementation_report_evidence_is_never_direct_authorization():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "implementation_semantics",
                "issue_contract": ["preserve geometry"],
                "observed_failure": ["lower bound is wrong"],
                "failure_observation_quality": "expected_actual",
                "root_cause": "the reported risk occurred",
                "invalidated_assumption": "closure was unnecessary",
                "required_correction": "restore canonical geometry",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "implementation_report",
                        "fact": "five vertices may be required",
                        "support": "direct",
                    }
                ],
                "correction_evidence_ids": ["E1"],
                "scope_expansion": [],
                "regression_guards": [],
                "correction_readiness": "actionable",
                "next_evidence_source": "none",
                "confidence": 0.9,
            }
        )
    )

    assert feedback is not None
    assert feedback.evidence_ledger[0].support == "inference"
    assert feedback.correction_readiness == "insufficient_evidence"
    assert feedback.correction_route == "none"


def test_independent_validation_success_is_explicit():
    assert independent_validation_succeeded({"resolved": True, "error": False})
    assert independent_validation_succeeded({"status": "success", "exit_code": 0})
    assert not independent_validation_succeeded({"resolved": False, "error": False})
    assert not independent_validation_succeeded({"status": "success", "exit_code": 1})
    assert not independent_validation_succeeded("pytest passed")


def test_lightweight_validation_requires_a_failing_clean_baseline():
    result = assess_lightweight_validation(
        {
            "command": "python -m pytest tests/test_public.py -q",
            "exit_code": 0,
            "status": "passed",
            "output": "1 passed",
        },
        {
            "command": "python -m pytest tests/test_public.py -q",
            "exit_code": 1,
            "status": "failed",
            "output": "1 failed",
        },
    )

    assert result.status == "invalid_reproducer"
    assert result.feedback_eligible is False


def test_lightweight_validation_routes_only_reproduced_post_patch_failure():
    baseline = {
        "requested_command": "python -m pytest tests/test_public.py -q",
        "exit_code": 1,
        "output": "expected 2, got 1",
    }
    failed = assess_lightweight_validation(
        baseline,
        {
            "command": "python -m pytest tests/test_public.py -q",
            "exit_code": 1,
            "status": "failed",
            "evidence": "expected 2, got 0",
        },
    )
    fixed = assess_lightweight_validation(
        baseline,
        {
            "command": "python -m pytest tests/test_public.py -q",
            "exit_code": 0,
            "status": "passed",
            "evidence": "1 passed",
        },
    )

    assert failed.status == "still_failing"
    assert failed.feedback_eligible is True
    assert fixed.status == "fixed"
    assert fixed.feedback_eligible is False


def test_compact_failure_validation_prioritizes_relation_violations():
    compact = compact_failure_validation(
        {
            "validation_kind": "differential_trace",
            "baseline": {"evidence": "x" * 20_000},
            "patched": {"evidence": "y" * 20_000},
            "test_plan": {
                "guard_coverage_complete": True,
                "guard_obligations": [{"id": "negative"}],
                "guard_coverage": [{"obligation_id": "negative"}],
                "limitations": [],
            },
            "case_matrix": [
                {
                    "case": "negative",
                    "expected_relation": "must_change",
                    "baseline": {"value_repr": "0"},
                    "patched": {"value_repr": "0"},
                    "changed": False,
                }
            ],
        }
    )

    assert "baseline" not in compact
    assert compact["relation_violations"] == [
        "negative: must_change but baseline and candidate are unchanged"
    ]


def test_failure_review_prompt_prioritizes_direct_failure_evidence():
    prompt = build_failure_review_prompt(
        task="A compound guard returns zero too broadly.",
        failed_patch="+ value.is_integer",
        failure_log="symbolic case remained unchanged",
        validation={
            "validation_kind": "differential_trace",
            "case_matrix": [
                {
                    "case": "symbolic",
                    "expected_relation": "must_change",
                    "baseline": {"value_repr": "0"},
                    "patched": {"value_repr": "0"},
                    "changed": False,
                }
            ],
        },
    )

    assert "expected_actual" in prompt
    assert "behavior_delta" in prompt
    assert "direct" in prompt
    assert "inference" in prompt
    assert "invalid_test" in prompt


def test_specialized_cli_commands_reject_conflicting_mode(tmp_path):
    with pytest.raises(SystemExit):
        main(
            [
                "benchmark",
                "--repo",
                str(tmp_path),
                "--task",
                "fix it",
                "--repair-mode",
                "experience-build",
            ]
        )


def test_legacy_failure_feedback_is_parsed_but_not_used_for_correction():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "problem_contract",
                "root_cause": "The producer and consumer disagree on identity.",
                "invalidated_assumption": "Changing the producer alone was sufficient.",
                "required_correction": "Re-plan the complete public contract.",
                "evidence": ["public assertion mismatch"],
                "regression_guards": ["preserve the old sentinel"],
                "confidence": 1.4,
            }
        )
    )

    assert feedback is not None
    assert feedback.requires_coordinator_replan is False
    assert feedback.correction_route == "none"
    assert feedback.confidence == 1.0
    assert parse_failure_feedback('{"failure_stage": "unknown"}') is None


def test_failure_feedback_routes_only_direct_exact_correction_to_implement():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "implementation_semantics",
                "issue_contract": ["The public call must preserve the matched canonical identity."],
                "observed_failure": ["The post-patch output still contains the requested alias."],
                "failure_observation_quality": "expected_actual",
                "root_cause": "The patch propagates the requested alias instead of the matched identity.",
                "invalidated_assumption": "Lookup identity and output identity are interchangeable.",
                "required_correction": "Propagate the already matched identity to the output field.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "public_task",
                        "fact": "Output must use the matched canonical identity.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "failed_patch",
                        "fact": "The patch writes the requested alias to the output field.",
                        "support": "direct",
                    },
                    {
                        "id": "E3",
                        "source": "failure_log",
                        "fact": "Expected canonical identity but received the alias.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": ["E1", "E2", "E3"],
                "scope_expansion": [],
                "regression_guards": ["Keep lookup aliases accepted."],
                "correction_readiness": "actionable",
                "next_evidence_source": "none",
                "confidence": 0.9,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_route == "corrective_implement"
    assert feedback.requires_coordinator_replan is False


def test_failure_feedback_allows_inference_only_as_non_authorizing_support():
    payload = {
        "failure_stage": "implementation_semantics",
        "issue_contract": ["Return the canonical identity."],
        "observed_failure": ["Expected canonical, got alias."],
        "failure_observation_quality": "expected_actual",
        "root_cause": "The patch propagated the lookup alias.",
        "invalidated_assumption": "Lookup and output identity are interchangeable.",
        "required_correction": "Propagate the matched identity.",
        "evidence_ledger": [
            {"id": "E1", "source": "public_task", "fact": "Canonical identity is required.", "support": "direct"},
            {"id": "E2", "source": "failure_log", "fact": "Expected canonical, got alias.", "support": "direct"},
            {"id": "E3", "source": "failed_patch", "fact": "The alias assignment caused the mismatch.", "support": "inference"},
        ],
        "correction_evidence_ids": ["E1", "E2", "E3"],
        "scope_expansion": [],
        "regression_guards": [],
        "correction_readiness": "actionable",
        "next_evidence_source": "none",
        "confidence": 0.8,
    }

    feedback = parse_failure_feedback(json.dumps(payload))
    assert feedback is not None
    assert feedback.correction_route == "corrective_implement"

    payload["evidence_ledger"][1]["support"] = "inference"
    feedback = parse_failure_feedback(json.dumps(payload))
    assert feedback is not None
    assert feedback.correction_route != "corrective_implement"


def test_failure_feedback_can_authorize_from_direct_failure_and_repository_cause():
    payload = {
        "failure_stage": "implementation_semantics",
        "issue_contract": ["Autoscale must emit one changed notification."],
        "observed_failure": ["Expected one notification, received four."],
        "failure_observation_quality": "expected_actual",
        "root_cause": "Each bound setter notifies before autoscale has finished updating both bounds.",
        "required_correction": "Block intermediate notifications and notify once after updating.",
        "required_edits": [{"path": "colors.py", "start_line": 10, "end_line": 15,
                            "change": "Block callbacks in autoscale and emit one final notification."}],
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "fact": "Expected 1, actual 4.", "support": "direct"},
            {"id": "E2", "source": "repository_code", "fact": "Setters notify on each update.", "support": "direct"},
        ],
        "correction_evidence_ids": ["E1", "E2"],
        "scope_expansion": [], "regression_guards": [],
        "correction_readiness": "actionable", "next_evidence_source": "none", "confidence": 0.8,
    }
    feedback = parse_failure_feedback(json.dumps(payload))
    assert feedback.correction_route == "corrective_implement"

    payload["correction_readiness"] = "needs_replan"
    payload["replan_reason"] = "The confirmed edit is outside the previous file scope."
    payload["next_evidence_source"] = "repository_code"
    assert parse_failure_feedback(json.dumps(payload)).correction_route == "coordinator_replan"

    payload["evidence_ledger"][0]["support"] = "inference"
    assert parse_failure_feedback(json.dumps(payload)).correction_route == "none"

    payload["evidence_ledger"][0]["support"] = "direct"
    payload["evidence_ledger"][1]["support"] = "unknown"
    assert parse_failure_feedback(json.dumps(payload)).correction_route == "none"


def test_failure_feedback_accepts_validated_semantic_invariant_as_direct_evidence():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "regression",
                "issue_contract": ["Reject the non-integer base counterexample."],
                "observed_failure": [
                    "case=symbolic_integer_exponent expected 0, actual Mod(l**p, l)."
                ],
                "failure_observation_quality": "expected_actual",
                "root_cause": "The patch replaced semantic exponent checks with a structural literal check.",
                "invalidated_assumption": "A literal exponent covers symbolic integer assumptions.",
                "required_correction": "Preserve symbolic positive integer exponents while rejecting non-integer bases.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "semantic_evidence",
                        "fact": "The registered source-backed invariant requires symbolic integer exponents to pass.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "failure_log",
                        "fact": "The symbolic integer exponent case expected 0 but remained unevaluated.",
                        "support": "direct",
                    },
                    {
                        "id": "E3",
                        "source": "failed_patch",
                        "fact": "The candidate still uses a structural Integer predicate.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": ["E1", "E2", "E3"],
                "scope_expansion": [],
                "regression_guards": ["Preserve positive symbolic integer exponents."],
                "correction_readiness": "actionable",
                "next_evidence_source": "none",
                "confidence": 0.95,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_route == "corrective_implement"


def test_failure_feedback_accepts_direct_behavior_delta():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "regression",
                "issue_contract": ["The public issue input must change."],
                "observed_failure": ["A source-backed old-behavior case changed unexpectedly."],
                "failure_observation_quality": "behavior_delta",
                "root_cause": "The candidate narrowed the original guard.",
                "invalidated_assumption": "The public example covered every original guard input.",
                "required_correction": "Preserve the source-backed old guard case.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "failure_log",
                        "fact": "The baseline and candidate values differ for the old guard case.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "failed_patch",
                        "fact": "The candidate adds a narrower predicate.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": ["E1", "E2"],
                "scope_expansion": [],
                "regression_guards": ["Keep the old guard case."],
                "correction_readiness": "actionable",
                "next_evidence_source": "none",
                "confidence": 0.9,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_route == "corrective_implement"


def test_failure_feedback_can_finish_differential_review_without_correction():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "complete",
                "issue_contract": ["The public issue input must change."],
                "observed_failure": ["No remaining failure was observed in valid public cases."],
                "failure_observation_quality": "behavior_delta",
                "root_cause": "No remaining root cause is supported by the public differential trace.",
                "invalidated_assumption": "none",
                "required_correction": "none",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "validation_summary",
                        "fact": "Every valid public differential case supports the candidate behavior.",
                        "support": "direct",
                    }
                ],
                "correction_evidence_ids": [],
                "scope_expansion": [],
                "regression_guards": [],
                "correction_readiness": "not_needed",
                "next_evidence_source": "none",
                "confidence": 0.8,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_readiness == "not_needed"
    assert feedback.correction_route == "none"


def test_failure_feedback_scope_expansion_prevents_correction():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "implementation_semantics",
                "issue_contract": ["Only the power identity is described by the issue."],
                "observed_failure": ["A public assertion still fails without expected/actual values."],
                "failure_observation_quality": "failure_only",
                "root_cause": "The remaining semantic cause is not directly observable.",
                "invalidated_assumption": "The first guarded branch was the only failing behavior.",
                "required_correction": "Inspect the public contract before changing adjacent equality branches.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "public_task",
                        "fact": "The issue describes the power identity branch.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "failure_log",
                        "fact": "The log contains only AssertionError.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": ["E1", "E2"],
                "scope_expansion": [
                    "Changing direct equality branches would add behavior not named by the issue."
                ],
                "regression_guards": ["Preserve direct self-modulo behavior."],
                "correction_readiness": "actionable",
                "next_evidence_source": "public_test",
                "confidence": 0.5,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_readiness == "insufficient_evidence"
    assert feedback.correction_route == "none"


def test_failure_feedback_directional_algorithm_advice_is_not_actionable():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "problem_contract",
                "issue_contract": ["Use the correct mathematical bound."],
                "observed_failure": ["The target value and two regressions remain wrong."],
                "failure_observation_quality": "expected_actual",
                "root_cause": "The patch guessed a formula not established by the inputs.",
                "invalidated_assumption": "A plausible-looking formula is sufficient.",
                "required_correction": "Recover the exact bound relation from public code or documentation.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "failed_patch",
                        "fact": "The patch introduces a new unreferenced formula.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "validation_summary",
                        "fact": "The target and regression checks fail after the patch.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": [],
                "scope_expansion": [],
                "regression_guards": ["Keep existing valid bounds valid."],
                "correction_readiness": "needs_replan",
                "next_evidence_source": "public_documentation",
                "confidence": 0.8,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_readiness == "insufficient_evidence"
    assert feedback.correction_route == "none"


def test_failure_only_observation_cannot_route_directly_to_corrective():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "implementation_semantics",
                "issue_contract": ["The public trigger reaches one named branch."],
                "observed_failure": ["The log contains only AssertionError."],
                "failure_observation_quality": "failure_only",
                "root_cause": "An adjacent branch might be wrong.",
                "invalidated_assumption": "The first branch was sufficient.",
                "required_correction": "Change an adjacent branch.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "public_task",
                        "fact": "The public trigger reaches one branch.",
                        "support": "direct",
                    },
                    {
                        "id": "E2",
                        "source": "validation_summary",
                        "fact": "A test still fails.",
                        "support": "direct",
                    },
                ],
                "correction_evidence_ids": ["E1", "E2"],
                "scope_expansion": [],
                "regression_guards": [],
                "correction_readiness": "actionable",
                "next_evidence_source": "public_test",
                "confidence": 0.6,
            }
        )
    )

    assert feedback is not None
    assert feedback.correction_readiness == "insufficient_evidence"
    assert feedback.correction_route == "none"


def test_recoverable_insufficient_evidence_remains_non_actionable():
    feedback = parse_failure_feedback(
        json.dumps(
            {
                "failure_stage": "insufficient_evidence",
                "issue_contract": ["The public trigger reaches the patched branch."],
                "observed_failure": ["Only AssertionError is visible."],
                "failure_observation_quality": "failure_only",
                "root_cause": "Unknown from the current failure excerpt.",
                "invalidated_assumption": "The first patch was sufficient.",
                "required_correction": "Inspect the named public test to recover expected/actual behavior.",
                "evidence_ledger": [
                    {
                        "id": "E1",
                        "source": "validation_summary",
                        "fact": "A named test still fails.",
                        "support": "direct",
                    }
                ],
                "correction_evidence_ids": [],
                "scope_expansion": [],
                "regression_guards": [],
                "correction_readiness": "insufficient_evidence",
                "next_evidence_source": "public_test",
                "confidence": 0.3,
            }
        )
    )

    assert feedback is not None
    assert feedback.failure_stage == "insufficient_evidence"
    assert feedback.correction_readiness == "insufficient_evidence"
    assert feedback.correction_route == "none"


def test_role_memory_is_injected_into_the_correct_prompt(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="localize the first divergence",
        description="trace the first wrong predicate before choosing edit scope",
        type="semantic_insight",
        content="applicability: output mismatch. Rule: trace backward from the assertion.",
        role="coordinator",
        provenance="public-ci",
    )
    store.save_memory(
        name="preserve adjacent input partition",
        description="pair the target input with one adjacent legal representation",
        type="repair_episode",
        content="problem_signature: container representation gap. successful_output: narrow the guard.",
        role="implement",
        provenance="public-ci",
    )
    store.save_memory(
        name="narrow representation guards",
        description="reuse an established representation predicate instead of concrete dtype names",
        type="semantic_insight",
        content="applicability: container representation gaps. Rule: reuse the existing predicate.",
        role="implement",
        provenance="public-ci",
    )

    task, insights = augment_task_with_semantic_insights(
        task="Fix an output mismatch in a shared parser.",
        memory_store=store,
    )
    implement_prompt, implement_insights = augment_task_with_semantic_insights(
        task="Implement a narrow fix for the container representation gap.",
        memory_store=store,
        role="implement",
    )
    prompt, episodes = augment_implement_with_episodes(
        prompt=implement_prompt,
        memory_store=store,
    )

    assert "Recalled Semantic Repair Insights" in task
    assert insights[0].header.role == "coordinator"
    assert implement_insights[0].header.role == "implement"
    assert "Retrieved Repair Episodes" in prompt
    assert "Recalled Semantic Repair Insights" in prompt
    assert "Implement a narrow fix" in prompt
    assert "Coordinator 的 required change 是否与 Failed Attempt 同构" in prompt
    assert "该 Failed Attempt 是已验证反模式" in prompt
    assert episodes[0].header.role == "implement"


def test_constraints_from_patch_stay_inside_changed_file(tmp_path):
    target = tmp_path / "pkg" / "reader.py"
    target.parent.mkdir()
    target.write_text("\n".join(f"line {index}" for index in range(1, 101)), encoding="utf-8")
    patch = (
        "diff --git a/pkg/reader.py b/pkg/reader.py\n"
        "--- a/pkg/reader.py\n"
        "+++ b/pkg/reader.py\n"
        "@@ -40,2 +40,3 @@\n"
        "-old\n"
        "+new\n"
    )

    constraints = constraints_from_patch(patch, tmp_path, padding=5)

    assert constraints.allowed_files == frozenset({"pkg/reader.py"})
    line_range = constraints.allowed_ranges["pkg/reader.py"][0]
    assert line_range.start == 35
    assert line_range.end == 47
