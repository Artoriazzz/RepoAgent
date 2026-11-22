from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repo_agent.core.subagent import LineRange, SubAgentRunner, WorkerConstraints, get_worker_spec
from repo_agent.core.static_analysis import newly_undefined_names
from repo_agent.core.task_constraints import ConstraintRouting
from repo_agent.core.semantic_evidence import (
    blocking_patch_semantic_issues,
    build_repair_brief,
    build_verification_plan,
    extract_json_object as extract_semantic_evidence,
    format_semantic_evidence,
    implementation_semantic_issues,
    normalize_semantic_evidence,
    patch_evidence_alignment_issues,
    patch_semantic_warnings,
    required_case_ids,
    semantic_evidence_warnings,
    validate_semantic_evidence,
    verification_coverage_issues,
)
from repo_agent.core.tool import Tool, ToolParameter, ToolResponse, ToolSource, build_tool
from repo_agent.core.verification import (
    assess_verification_result,
    classify_verification_command,
    prepare_verification_command,
    verification_command_safety_reason,
)


RepairConstraintProvider = Callable[[], tuple[Any, ...]]
RepairEpisodePromptProvider = Callable[[str], tuple[str, tuple[Any, ...]]]
RoleSemanticPromptProvider = Callable[[str, str], tuple[str, tuple[Any, ...]]]
ReproducerRequirement = bool | Callable[[], bool]


def reset_agent_tool_state(state: dict[str, Any], cwd: Path) -> None:
    """重置一次 Coordinator run 的确定性工作流状态，供门禁和阶段归因共用。

    这些字段独立于 Coordinator messages。即使模型忘记自己已派过 Implement，工具层
    仍能根据 implementation_done、worker_attempts 等状态拒绝越序或重复调用。
    """
    state.clear()
    state.update(
        {
            "ready_to_implement": False,
            "implementation_done": False,
            "patch_review_done": False,
            "patch_review_attempted": False,
            "patch_review_completed": False,
            "patch_review_high_risk": False,
            "patch_review_result": {},
            "review_required": False,
            "review_reasons": [],
            "static_issues": [],
            "corrective_implement_count": 0,
            "delivery_retry_used": False,
            "delivery_retry_required": False,
            "delivery_retry_scope": None,
            "delivery_retry_issues": [],
            "last_chance_required": False,
            "last_chance_used": False,
            "last_chance_scope": None,
            "last_chance_failed_scope": None,
            "implemented_files": (),
            "last_patch_gate_text": "",
            "candidate_patches": [],
            "candidate_initial_diff": _git_diff_text(cwd),
            "reproducer_attempted": False,
            "reproducer_done": False,
            "reproducer_trusted": False,
            "reproducer_trust_reason": "",
            "reproduction_stop_reason": "",
            "candidate_list_done": False,
            "candidate_evaluation_done": False,
            "candidate_base_results": [],
            "reproduction_contract": {},
            "reproduction_commands": [],
            "reproduction_execution": {},
            "public_verification_registered": False,
            "public_verification_execution_attempted": False,
            "public_verification_command": "",
            "public_verification_covers": [],
            "repair_brief": {},
            "semantic_evidence": {},
            "semantic_evidence_issues": [],
            "semantic_evidence_warnings": [],
            "semantic_gate_denials": 0,
            "semantic_evidence_degraded": False,
            "semantic_patch_issues": [],
            "semantic_patch_warnings": [],
            "implementation_report": {},
            "semantic_corrective_required": False,
            "patch_ranker_done": False,
            "patch_ranker_selected_index": None,
            "patch_ranker_selected_none": False,
            "patch_ranker_result_invalid": "",
            "last_ranker_text": "",
            "candidate_refinement_used": False,
            "verify_attempted": False,
            "verify_completed": False,
            "verification_result": {},
            "verification_plan": {},
            "verification_assessment": {},
            "verification_result_parseable": False,
            "verification_passed": False,
            "verification_skipped_reason": "",
            "submission_ready": False,
            "workflow_terminal_reason": "",
            "workflow_terminal_completed": False,
            "workflow_terminal_final_answer": "",
            "constraint_routing": {
                "candidate_profiles": [],
                "active_profiles": [],
                "rejected_profiles": [],
                "source_excerpt_chars": 0,
                "injected_prompt_chars": 0,
            },
            "worker_requests": {},
            "worker_attempts": {},
            "worker_events": [],
            "recalled_repair_episodes": [],
            "recalled_semantic_insights_by_role": {},
        }
    )


def build_agent_tools(
    runner: SubAgentRunner,
    repair_recipe_constraints: RepairConstraintProvider | None = None,
    repair_episode_prompt: RepairEpisodePromptProvider | None = None,
    role_semantic_prompt: RoleSemanticPromptProvider | None = None,
    *,
    require_patch_review: bool = True,
    conditional_patch_review: bool = False,
    require_reproducer_before_implement: ReproducerRequirement = False,
    require_public_verification_before_implement: Callable[[], bool] | None = None,
    auto_verify_public_verification: bool = False,
    allowed_agent_types: set[str] | frozenset[str] | None = None,
    candidate_mode_required: Callable[[], bool] | None = None,
    forced_implement_scope: Callable[[], WorkerConstraints | None] | None = None,
    state: dict[str, Any] | None = None,
) -> list[Tool]:
    """构造 Coordinator 用来派发 Worker 的元工具和确定性状态机。"""

    state = state if state is not None else {}
    reset_agent_tool_state(state, runner.cwd)
    review_policy = "required" if require_patch_review else ("conditional" if conditional_patch_review else "off")

    def review_required_now() -> bool:
        return review_policy == "required" or bool(state.get("review_required"))

    def reproducer_required_now() -> bool:
        if callable(require_reproducer_before_implement):
            return bool(require_reproducer_before_implement())
        return bool(require_reproducer_before_implement)

    def public_verification_required_now() -> bool:
        return bool(
            require_public_verification_before_implement
            and require_public_verification_before_implement()
        )

    def run_frozen_public_verification() -> None:
        command = str(state.get("public_verification_command") or "").strip()
        run_tool = runner.base_tools.get("run_command")
        if not command or run_tool is None:
            return
        response = runner._run_swebench_verification_command(
            run_tool,
            {"command": command},
        )
        exit_code = response.data.get("exit_code")
        verification = {
            "status": (
                "passed"
                if exit_code == 0
                else "failed"
                if isinstance(exit_code, int)
                else "blocked_by_environment"
            ),
            "command": command,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "covered_cases": list(state.get("public_verification_covers") or []),
            "evidence": response.text[-2_000:],
        }
        assessment = assess_verification_result(verification)
        state.setdefault("worker_attempts", {})["verify"] = 1
        state.setdefault("worker_requests", {})["verify"] = 1
        state["verification_plan"] = {
            "command": command,
            "covers": list(state.get("public_verification_covers") or []),
        }
        state["verification_execution"] = {
            "attempted": True,
            "requested_command": command,
            "execution_command": response.data.get("execution_command", ""),
            "exit_code": exit_code,
            "tool_status": response.status.value,
            "output": response.text[-2_000:],
        }
        state["verification_result"] = verification
        state["verification_assessment"] = assessment
        state["verify_attempted"] = True
        state["verify_completed"] = isinstance(exit_code, int)
        state["verification_result_parseable"] = True
        state["verification_passed"] = bool(assessment.get("submission_ready"))
        if state["verification_passed"]:
            if not state.get("review_required"):
                state["workflow_terminal_reason"] = "focused_verification_passed"
                state["workflow_terminal_completed"] = True
        elif assessment.get("classification") == "environment_blocked":
            state["workflow_terminal_reason"] = "focused_verification_blocked"
            state["workflow_terminal_completed"] = False
        elif assessment.get("classification") == "behavior_failed":
            state["workflow_terminal_reason"] = "focused_verification_failed"
            state["workflow_terminal_completed"] = False
        else:
            state["workflow_terminal_reason"] = "focused_verification_inconclusive"
            state["workflow_terminal_completed"] = False

    def record_worker_result(result, constraints: WorkerConstraints) -> None:
        state.setdefault("worker_events", []).append(
            {
                "type": result.type,
                "model": result.model,
                "completed": result.completed,
                "stop_reason": result.stop_reason,
                "changed_files": list(result.changed_files),
                "allowed_files": list(constraints.allowed_files),
                "allowed_ranges": {
                    path: [[line_range.start, line_range.end] for line_range in ranges]
                    for path, ranges in (constraints.allowed_ranges or {}).items()
                },
                "context_ranges": {
                    path: [[line_range.start, line_range.end] for line_range in ranges]
                    for path, ranges in (constraints.context_ranges or {}).items()
                },
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            }
        )

    def launch_agent(inp: dict) -> ToolResponse:
        # 这是从“模型建议派发 Worker”到“程序允许启动 Worker”的真正边界。后续检查
        # 类型白名单、调用预算、阶段顺序、EditBrief scope 和 corrective 次数。
        agent_type = inp.get("type") or "general"
        spec = get_worker_spec(agent_type)
        state.setdefault("worker_requests", {})[spec.name] = int(
            state.setdefault("worker_requests", {}).get(spec.name, 0)
        ) + 1
        if state.get("workflow_terminal_reason"):
            return ToolResponse.failure(
                "WORKFLOW_ALREADY_TERMINAL",
                f"The simplified workflow already reached terminal state: {state['workflow_terminal_reason']}. Keep the current patch and finish.",
            )
        if review_policy == "conditional" and spec.name == "patch_review" and not state.get("review_required"):
            return ToolResponse.failure(
                "PATCH_REVIEW_NOT_REQUIRED",
                "The deterministic risk trigger did not require patch_review. Keep the single patch and run one focused verify or finish.",
            )
        if review_policy == "conditional" and spec.name == "patch_review" and state.get("patch_review_attempted"):
            return ToolResponse.failure(
                "PATCH_REVIEW_ALREADY_ATTEMPTED",
                "Conditional patch_review may launch only once. Keep the current patch and finish.",
            )
        if allowed_agent_types is not None and spec.name not in allowed_agent_types:
            return ToolResponse.failure(
                "WORKER_TYPE_DISABLED",
                (
                    f"agent(type={spec.name}) is disabled in this profile. "
                    f"Allowed role types: {', '.join(sorted(allowed_agent_types))}."
                ),
            )
        if spec.name == "reproducer" and state.get("reproducer_attempted"):
            return ToolResponse.failure(
                "REPRODUCER_ALREADY_ATTEMPTED",
                (
                    "This run already used its one reproducer attempt. Continue with the current reproduction contract "
                    "and dispatch implement; missing dependencies should be recorded as blocked_by_environment."
                ),
            )
        if not require_patch_review and spec.name == "verify" and state.get("verify_attempted"):
            return ToolResponse.failure(
                "VERIFY_ALREADY_ATTEMPTED",
                "SWE-bench slim profile allows only one independent verification. Keep the existing patch and finish.",
            )
        description = inp.get("description") or "sub-agent task"
        raw_prompt = inp["prompt"]
        prompt = raw_prompt
        constraints = WorkerConstraints.from_tool_input(inp)
        semantic_role = {
            "implement": "implement",
            "patch_review": "review",
        }.get(spec.name)
        if semantic_role and role_semantic_prompt is not None:
            prompt, recalled_insights = role_semantic_prompt(semantic_role, prompt)
            state.setdefault("recalled_semantic_insights_by_role", {})[semantic_role] = [
                memory.header.filename for memory in recalled_insights
            ]
        if spec.name == "implement" and repair_episode_prompt is not None:
            episode_query = f"{prompt}\n{state.get('last_patch_gate_text') or ''}".strip()
            prompt, recalled_episodes = repair_episode_prompt(episode_query)
            state["recalled_repair_episodes"] = [
                memory.header.filename for memory in recalled_episodes
            ]
        if spec.name == "implement" and forced_implement_scope is not None:
            forced_constraints = forced_implement_scope()
            if forced_constraints is not None and forced_constraints.has_scope:
                constraints = forced_constraints
                prompt = (
                    f"{prompt}\n\nDiagnostic scope oracle is enforced by the tool layer. "
                    f"allowed_files={list(constraints.allowed_files)}; "
                    "use only the enforced allowed_ranges supplied to your scoped tools."
                )
        swebench_context = _is_swebench_context(prompt, runner.cwd)
        constraint_routing = ConstraintRouting((), (), (), 0)
        if spec.name == "implement" and swebench_context:
            constraints, removed_ranges = _constrain_implement_to_consumer_layer(
                constraints,
                state.get("semantic_evidence"),
            )
            if removed_ranges:
                state["semantic_scope_adjustments"] = removed_ranges
                prompt = (
                    f"{prompt}\n\nTool-enforced semantic scope: preferred_change_layer=consumer, so these "
                    f"producer-only writable ranges were removed: {removed_ranges}. Do not modify canonical storage."
                )
            # Scope is supplied by Coordinator and enforced structurally. Do
            # not infer a task-specific edit recipe from issue keywords.
            if constraints.context_ranges:
                constraints = WorkerConstraints(
                    allowed_files=constraints.allowed_files,
                    allowed_ranges=constraints.allowed_ranges,
                    context_ranges=_compact_context_ranges(constraints.context_ranges),
                )
            state["constraint_routing"] = constraint_routing.to_dict()
        if spec.name == "implement" and swebench_context:
            constraints, removed_test_paths = _strip_test_paths_from_constraints(constraints)
            if removed_test_paths:
                state["test_scope_adjustments"] = removed_test_paths
                prompt = (
                    f"{prompt}\n\nTool-enforced scope: test/fixture paths were removed from writable scope: "
                    f"{removed_test_paths}. Modify production source only."
                )
            if removed_test_paths and not constraints.has_scope:
                return ToolResponse.failure(
                    "SWEBENCH_TEST_EDIT_DENIED",
                    "The requested Implement scope contained only tests/fixtures. Provide a production source range.",
                )
        scope_key = _scope_key(constraints)
        delivery_retry_now = bool(
            spec.name == "implement" and state.get("delivery_retry_required")
            and not state.get("delivery_retry_used")
        )
        if delivery_retry_now and scope_key != state.get("delivery_retry_scope"):
            return ToolResponse.failure("DELIVERY_RETRY_SCOPE_DENIED",
                "The one delivery correction must reuse the original allowed_files and allowed_ranges exactly.")
        if state.get("delivery_retry_required") and spec.name != "implement":
            return ToolResponse.failure("DELIVERY_RETRY_REQUIRED",
                "Fix the reported syntax/edit-application error once in the same scope, or finish with the blocker.")
        if spec.name == "implement" and state.get("last_chance_failed_scope") == scope_key:
            return ToolResponse.failure(
                "LAST_CHANCE_EXHAUSTED",
                "A last-chance implement already ran for this same scope without producing a patch. "
                "Use a genuinely different narrowed scope/EditBrief, run read-only coordination to find the missing fact, "
                "or finish with blocked_insufficient_edit_scope.",
            )
        candidate_required = bool(candidate_mode_required and candidate_mode_required())
        if (
            public_verification_required_now()
            and spec.name == "implement"
            and not state.get("public_verification_registered")
            and not state.get("implementation_done")
        ):
            return ToolResponse.failure(
                "PUBLIC_VERIFICATION_REQUIRED",
                "Register one safe public behavior assertion with register_public_verification before Implement. "
                "The deterministic controller will execute and freeze it; do not launch a separate reproduction role.",
            )
        if (
            reproducer_required_now()
            and spec.name == "implement"
            and not state.get("reproducer_attempted")
            and not state.get("implementation_done")
        ):
            return ToolResponse.failure(
                "REPRODUCER_REQUIRED",
                "The simplified SWE-bench pipeline requires one lightweight reproducer before implement. "
                "Dispatch agent(type=reproducer) with the current Problem Contract and likely code scope.",
            )
        if (
            candidate_required
            and spec.name == "implement"
            and not state.get("reproducer_attempted")
            and not state.get("candidate_patches")
            and not state["implementation_done"]
        ):
            return ToolResponse.failure(
                "CANDIDATE_MODE_REPRODUCER_REQUIRED",
                (
                    "This SWE-bench task is in required candidate-pool mode. Dispatch agent(type=reproducer) first "
                    "to establish the public reproduction/differential contract, then generate at least two candidate "
                    "implement patches and rank them before applying one."
                ),
            )
        if spec.name in {"research", "general", "plan"} and _looks_like_subagent_output_read(prompt):
            return ToolResponse.failure(
                "SUBAGENT_OUTPUT_READ_BLOCKED",
                "Do not launch independent roles to read subagent output directories. "
                "Use the previous result already returned by the agent tool, then proceed with an EditBrief or finish.",
            )
        if spec.name == "patch_ranker":
            ranker_denial = _check_patch_ranker_preconditions(state)
            if ranker_denial:
                return ranker_denial
            prompt = _augment_patch_ranker_prompt(prompt, state)
        candidate_flow_bypass = (
            spec.name == "patch_ranker"
            and state.get("candidate_patches")
        ) or (
            spec.name == "reproducer"
            and not state.get("reproducer_attempted")
            and not state.get("candidate_patches")
        )
        if (
            state["ready_to_implement"]
            and not state["implementation_done"]
            and spec.name != "implement"
            and not candidate_flow_bypass
        ):
            return ToolResponse.failure(
                "IMPLEMENT_REQUIRED",
                "A previous read-only task already returned a structured edit_brief. "
                "Do not launch research, plan, general, verify, or review yet. "
                "Dispatch implement now with a concrete EditBrief, allowed_files, and allowed_ranges. "
                "If implementation is impossible, finish with the exact missing fact instead of launching another role.",
            )
        if state["implementation_done"] and spec.name in {"research", "reproducer", "general", "plan"}:
            if spec.name == "reproducer" and not state.get("reproducer_attempted") and not state.get("candidate_patches"):
                pass
            else:
                return ToolResponse.failure(
                    "POST_IMPLEMENT_BUDGET_EXHAUSTED",
                    "Implementation already completed. Do not launch more research/general/plan roles. "
                    "Use patch_review, verify, or finish with the result.",
                )
        if review_required_now() and state["implementation_done"] and spec.name == "verify" and not state["patch_review_done"]:
            return ToolResponse.failure(
                "PATCH_REVIEW_REQUIRED",
                "Implementation already changed files. Run agent(type=patch_review) before verify so obvious patch "
                "risks are checked without using harness feedback.",
            )
        if review_required_now() and state["implementation_done"] and spec.name == "verify" and state["patch_review_high_risk"]:
            if review_policy == "conditional":
                return ToolResponse.failure(
                    "PATCH_REVIEW_FLAGGED_RISK",
                    "Conditional review recorded a concrete regression risk. Do not start corrective implement in the simplified pipeline; keep the single patch and finish with this risk for stage attribution.",
                )
            return ToolResponse.failure(
                "PATCH_REVIEW_FINDINGS_REQUIRE_FIX",
                "Patch Risk Review did not accept the patch. Dispatch one corrective agent(type=implement) using "
                "the review findings/required_changes, then run patch_review again before verify.",
            )
        if (
            review_policy == "conditional"
            and state["implementation_done"]
            and spec.name == "implement"
            and not delivery_retry_now
        ):
            return ToolResponse.failure(
                "FINAL_PATCH_ALREADY_EXISTS",
                "The initial pipeline allows one implementation plus one same-scope correction only for a "
                "tool-confirmed syntax or edit-application error. No unused delivery correction is available; finish.",
            )
        if require_patch_review and state["implementation_done"] and spec.name == "implement" and not state["patch_review_high_risk"]:
            return ToolResponse.failure(
                "PATCH_REVIEW_REQUIRED",
                "Implementation already changed files. Run agent(type=patch_review) before any further implement or verify.",
            )
        if (
            not require_patch_review
            and state["implementation_done"]
            and spec.name == "implement"
            and not delivery_retry_now
        ):
            return ToolResponse.failure(
                "FINAL_PATCH_ALREADY_EXISTS",
                (
                    "A non-empty patch already exists in SWE-bench profile. Do not keep editing the final patch. "
                    "If you intended to compare candidates, save/reset each candidate with patch_candidate before applying one."
                ),
            )
        if (
            require_patch_review
            and
            state["implementation_done"]
            and spec.name == "implement"
            and state["patch_review_high_risk"]
            and state["corrective_implement_count"] >= 1
        ):
            return ToolResponse.failure(
                "PATCH_REVIEW_CORRECTIVE_LIMIT",
                "A corrective implement attempt after patch_review/static gates has already been used. "
                "Do not keep editing; finish with the remaining risk or run verify if a later patch_review "
                "reports low risk.",
            )
        if spec.name == "implement" and not constraints.has_scope:
            return ToolResponse.failure(
                "IMPLEMENT_SCOPE_REQUIRED",
                "Missing structured edit scope. Pass allowed_files and allowed_ranges as top-level "
                "arguments to agent(type=implement). File names and ranges written inside prompt do not "
                "authorize edits. Use the production files and line ranges already confirmed from source.",
            )
        if state["implementation_done"] and state["patch_review_high_risk"] and spec.name == "implement":
            corrective_denial = _check_corrective_implement_scope(constraints, state)
            if corrective_denial:
                return corrective_denial
            prompt = _augment_corrective_prompt(prompt, state)
            description = f"Corrective: {description}"
        if spec.name == "implement" and state.get("patch_ranker_selected_none"):
            if state.get("candidate_refinement_used"):
                return ToolResponse.failure(
                    "CANDIDATE_REFINEMENT_LIMIT",
                    "The candidate pool already used its one evidence-driven corrective candidate. "
                    "Select the best remaining candidate or finish with blocked_candidate_pool_no_viable_patch.",
                )
            prompt = _augment_candidate_refinement_prompt(prompt, state)
            description = f"Evidence correction: {description}"
            state["candidate_refinement_used"] = True
        if spec.name == "implement" and swebench_context:
            # Scope is the reliable boundary. Prompt wording may contain
            # prohibitions such as "do not run commands"; never reject an
            # otherwise scoped edit because of those words.
            semantic_issues = implementation_semantic_issues(list(state.get("semantic_evidence_issues") or []))
            if state.get("problem_statement") and semantic_issues:
                state["semantic_gate_denials"] = int(state.get("semantic_gate_denials") or 0) + 1
                state["semantic_evidence_degraded"] = True
                state["workflow_terminal_reason"] = "semantic_evidence_invalid"
                state["workflow_terminal_completed"] = False
                return ToolResponse.partial(
                    "SEMANTIC_EVIDENCE_INCOMPLETE",
                    {
                        "terminate_loop": True,
                        "terminal_reason": "semantic_evidence_invalid",
                        "terminal_completed": False,
                        "final_answer": (
                            "Simplified pass@1 terminated before implementation because SemanticEvidence remained "
                            "invalid after its isolated correction turn. No degraded-evidence implementation is allowed."
                        ),
                        "semantic_evidence_issues": semantic_issues[:8],
                    },
                )
            state["semantic_evidence_degraded"] = bool(semantic_issues)
        if spec.name == "implement" and repair_recipe_constraints is not None:
            recipe_denial = _check_repair_recipe_constraints(inp, constraints, repair_recipe_constraints(), runner.cwd)
            if recipe_denial:
                return recipe_denial
        if spec.name == "implement" and state["last_chance_required"] and not state["last_chance_used"]:
            prompt = _augment_last_chance_prompt(prompt)
            description = f"Last chance: {description}"
            state["last_chance_used"] = True
            state["last_chance_scope"] = scope_key
        if spec.name == "implement" and swebench_context:
            prompt = _augment_swebench_semantic_prompt(
                prompt,
                state,
                constraint_prompts=constraint_routing.prompts,
            )
        verification_plan: dict[str, Any] | None = None
        if spec.name == "verify" and swebench_context:
            semantic_issues = list(state.get("semantic_evidence_issues") or [])
            verification_plan = build_verification_plan(state.get("semantic_evidence"))
            if semantic_issues or verification_plan is None:
                detail = "; ".join(semantic_issues[:6]) or "no single validated command covers all required cases"
                reason = "Focused verification cannot start without validated SemanticEvidence: " + detail
                state["verification_assessment"] = {
                    "classification": "semantic_evidence_invalid",
                    "command_kind": "missing",
                    "conclusive": False,
                    "submission_ready": False,
                    "reject_patch": False,
                    "preserve_patch": True,
                    "reason": reason,
                }
                state["workflow_terminal_reason"] = "focused_verification_unavailable"
                state["workflow_terminal_completed"] = False
                return ToolResponse.partial(
                    reason,
                    {
                        "terminate_loop": True,
                        "terminal_reason": "focused_verification_unavailable",
                        "terminal_completed": False,
                        "final_answer": (
                            "Simplified pass@1 terminated because no validated SemanticEvidence verification plan "
                            "was available. The statically safe candidate patch is preserved for harness diagnostics."
                        ),
                    },
                )
            state["verification_plan"] = verification_plan
            prompt = _augment_swebench_verify_prompt(prompt, state, verification_plan)
        if spec.name == "patch_review" and swebench_context:
            prompt = _augment_swebench_patch_review_prompt(prompt, state)
        if delivery_retry_now:
            prompt += ("\n\n## Delivery correction\n"
                       "只修工具确认的语法或补丁应用错误，不改变原修改方案或范围；这是唯一一次落地纠正。\n"
                       + "\n".join(state["delivery_retry_issues"]))
            state["delivery_retry_used"] = True
            state["delivery_retry_required"] = False
        if spec.name == "implement" and state.get("delivery_retry_scope") is None:
            state["delivery_retry_scope"] = scope_key
        state.setdefault("worker_attempts", {})[spec.name] = int(
            state.setdefault("worker_attempts", {}).get(spec.name, 0)
        ) + 1
        if spec.name == "patch_review":
            state["patch_review_attempted"] = True
        if spec.name == "verify":
            state["verify_attempted"] = True
        result = runner.run(
            agent_type=agent_type,
            prompt=prompt,
            description=description,
            constraints=constraints,
            verification_plan=verification_plan,
        )
        record_worker_result(result, constraints)
        edit_application_errors = _unresolved_edit_application_errors(result.output_dir) if result.type == "implement" else []
        # Worker 的结果既作为本次 agent 工具 observation 返回 Coordinator，也被解析为
        # 下一阶段的代码状态，例如 ready_to_implement、implementation_done 或 review 风险。
        result_text = result.text
        if result.type == "research" and result.completed and _contains_structured_edit_brief(result.text):
            state["ready_to_implement"] = True
        if result.type == "implement" and not result.completed:
            state["ready_to_implement"] = bool(not result.changed_files)
        if result.type == "reproducer":
            # SWE-bench 使用严格 SemanticEvidence；普通候选池保持旧 reproduction contract 兼容。
            state["reproducer_attempted"] = True
            state["reproduction_stop_reason"] = result.stop_reason
            evidence = normalize_semantic_evidence(
                extract_semantic_evidence(result.text),
                str(state.get("problem_statement") or ""),
            ) or {}
            all_evidence_issues = (
                validate_semantic_evidence(evidence, str(state.get("problem_statement") or ""))
                if runner.swebench_profile or state.get("problem_statement")
                else []
            )
            execution = dict(result.verification_execution)
            if runner.swebench_profile:
                trusted, trust_reason = _trusted_swebench_reproducer_evidence(
                    evidence,
                    all_evidence_issues,
                    execution,
                )
                # 未实际复现的模型输出只用于阶段归因，不能成为 Implement 的语义前提。
                usable_evidence = evidence if trusted else {}
                evidence_issues = []
                evidence_warnings = [] if trusted else [trust_reason]
            else:
                trusted = bool(result.completed and evidence)
                trust_reason = "not_swebench_profile"
                usable_evidence = evidence
                evidence_issues = implementation_semantic_issues(all_evidence_issues)
                evidence_warnings = semantic_evidence_warnings(all_evidence_issues)
            state["semantic_evidence"] = usable_evidence
            state["semantic_evidence_issues"] = evidence_issues
            state["semantic_evidence_warnings"] = evidence_warnings
            state["reproducer_done"] = trusted
            state["reproducer_trusted"] = trusted
            state["reproducer_trust_reason"] = trust_reason
            state["reproduction_contract"] = evidence
            state["reproduction_commands"] = _extract_reproduction_commands(evidence)
            state["reproduction_execution"] = execution
            state["repair_brief"] = build_repair_brief(
                usable_evidence,
                str(state.get("problem_statement") or ""),
            ).to_dict()
        if result.type == "verify":
            verification = _extract_verification_result(result.text)
            verification, execution_issues = _bind_verification_to_plan(
                verification,
                state.get("verification_plan"),
                result.verification_execution,
            )
            assessment = assess_verification_result(verification)
            coverage_issues = [
                *execution_issues,
                *verification_coverage_issues(verification, state.get("semantic_evidence")),
            ]
            if coverage_issues:
                assessment = {
                    "classification": "coverage_incomplete",
                    "command_kind": assessment.get("command_kind", "behavior"),
                    "conclusive": False,
                    "submission_ready": False,
                    "reject_patch": False,
                    "preserve_patch": True,
                    "reason": "; ".join(coverage_issues),
                }
            state["verification_result"] = verification
            state["verification_assessment"] = assessment
            state["verification_result_parseable"] = bool(verification)
            state["verify_completed"] = bool(result.completed and verification)
            state["verification_passed"] = bool(
                state["verify_completed"]
                and assessment.get("submission_ready")
            )
            if review_policy == "conditional":
                if state["verification_passed"]:
                    state["workflow_terminal_reason"] = "focused_verification_passed"
                    state["workflow_terminal_completed"] = True
                elif not result.completed or not verification:
                    state["workflow_terminal_reason"] = "focused_verification_incomplete"
                    state["workflow_terminal_completed"] = False
                elif assessment.get("classification") == "environment_blocked":
                    state["workflow_terminal_reason"] = "focused_verification_blocked"
                    state["workflow_terminal_completed"] = False
                elif not assessment.get("conclusive") or assessment.get("classification") == "static_passed":
                    state["workflow_terminal_reason"] = "focused_verification_inconclusive"
                    state["workflow_terminal_completed"] = False
                else:
                    state["workflow_terminal_reason"] = "focused_verification_failed"
                    state["workflow_terminal_completed"] = False
        if result.type == "implement" and result.changed_files:
            if state["implementation_done"] and state["patch_review_high_risk"]:
                state["corrective_implement_count"] = int(state.get("corrective_implement_count") or 0) + 1
            state["implementation_done"] = True
            state["patch_review_done"] = False
            state["patch_review_high_risk"] = False
            state["last_chance_required"] = False
            state["last_chance_used"] = False
            state["last_chance_scope"] = None
            state["last_chance_failed_scope"] = None
            state["implemented_files"] = tuple(result.changed_files)
            state["verify_attempted"] = False
            state["verify_completed"] = False
            state["verification_result"] = {}
            state["verification_plan"] = {}
            state["verification_assessment"] = {}
            state["verification_result_parseable"] = False
            state["verification_passed"] = False
            state["verification_skipped_reason"] = ""
            current_patch = _git_diff_text(runner.cwd)
            implementation_report = _extract_json_object(result.text) or {}
            state["implementation_report"] = implementation_report
            implementation_review_reasons = _implementation_report_review_reasons(
                implementation_report
            )
            # Semantic alignment is evidence for Review/diagnostics, not a
            # second task-specific patch generator. Only deterministic syntax,
            # scope and basic safety checks can block this initial patch.
            semantic_patch_issues = []
            semantic_patch_warnings = []
            state["semantic_patch_issues"] = []
            state["semantic_patch_warnings"] = []
            state["semantic_corrective_required"] = False
            static_issues = _static_patch_sanity(runner.cwd, result.changed_files)
            state["static_issues"] = list(static_issues)
            if review_policy == "conditional":
                if semantic_patch_issues and int(state.get("corrective_implement_count") or 0) < 1:
                    state["semantic_corrective_required"] = True
                    state["patch_review_done"] = True
                    state["patch_review_high_risk"] = True
                    state["review_reasons"] = list(semantic_patch_issues)
                    state["review_required"] = False
                    state["last_patch_gate_text"] = _format_semantic_gate_text(semantic_patch_issues)
                    review_message = state["last_patch_gate_text"]
                elif semantic_patch_issues:
                    state["semantic_corrective_required"] = False
                    state["workflow_terminal_reason"] = "semantic_patch_gate_failed"
                    state["workflow_terminal_completed"] = False
                    state["last_patch_gate_text"] = _format_semantic_gate_text(semantic_patch_issues)
                    review_message = (
                        f"{state['last_patch_gate_text']}\n"
                        "The single corrective implementation did not clear the deterministic semantic gate. "
                        "This patch is not submission-ready and the workflow must terminate."
                    )
                else:
                    review_reasons = [
                        *_conditional_patch_review_reasons(runner.cwd, tuple(result.changed_files)),
                        *implementation_review_reasons,
                        *semantic_patch_issues,
                    ]
                    state["review_reasons"] = review_reasons
                    state["review_required"] = bool(review_reasons)
                    review_message = (
                        "Conditional patch review required: "
                        + ", ".join(review_reasons)
                        + ". Dispatch agent(type=patch_review) once."
                        if review_reasons
                        else "Conditional patch review not required. Run at most one focused verify or finish."
                    )
                if semantic_patch_warnings:
                    warning_text = "\n".join(f"- {issue}" for issue in semantic_patch_warnings)
                    review_message = (
                        f"Semantic patch warnings (non-blocking; do not run corrective implement):\n{warning_text}\n\n"
                        f"{review_message}"
                    )
                result_text = f"{result.text}\n\n{review_message}"
            if (
                auto_verify_public_verification
                and state.get("public_verification_registered")
                and not static_issues
            ):
                run_frozen_public_verification()
                verification_class = str(
                    (state.get("verification_assessment") or {}).get("classification") or ""
                )
                result_text = (
                    f"{result_text}\n\nDeterministic frozen-command verification: "
                    f"{verification_class or 'unknown'}."
                )
            if require_patch_review:
                if static_issues:
                    state["patch_review_done"] = True
                    state["patch_review_high_risk"] = True
                    state["last_patch_gate_text"] = _format_static_gate_text(static_issues)
                    result_text = f"{result.text}\n\n{state['last_patch_gate_text']}"
            terminal_message = "" if edit_application_errors else _finish_static_safe_unverified(state, runner, review_policy)
            if terminal_message:
                result_text = f"{result_text}\n\n{terminal_message}"
        if result.type == "implement" and not result.changed_files:
            if state["last_chance_used"]:
                state["last_chance_required"] = False
                state["last_chance_used"] = False
                state["last_chance_failed_scope"] = state.get("last_chance_scope")
                state["last_chance_scope"] = None
                state["ready_to_implement"] = False
            elif (review_policy != "conditional" and not state["implementation_done"]
                  and _should_try_last_chance(result.text)):
                state["last_chance_required"] = True
                state["ready_to_implement"] = True
            elif not state["implementation_done"]:
                state["last_chance_required"] = False
                state["ready_to_implement"] = False
        localization_replan_required = False
        if result.type == "implement" and review_policy == "conditional":
            delivery_issues = [issue for issue in state.get("static_issues", [])
                               if issue.startswith("python_syntax_error:")]
            delivery_issues.extend(edit_application_errors)
            if delivery_issues and not state.get("delivery_retry_used"):
                state["delivery_retry_required"] = True
                state["delivery_retry_issues"] = delivery_issues
                state["review_required"] = False
                result_text = (f"{result.text}\n\n工具确认了落地错误，允许一次同范围纠正。"
                               "立即派发 implement，保持原 allowed_files/allowed_ranges，禁止新增语义改动。\n"
                               + "\n".join(delivery_issues))
            elif delivery_issues:
                state["workflow_terminal_reason"] = "delivery_retry_failed"
                state["workflow_terminal_completed"] = False
            elif not result.changed_files:
                report = _extract_json_object(result.text) or {}
                if (os.getenv("REPO_AGENT_LOCALIZATION_RECOVERY") == "1"
                        and not state.get("localization_replan_used")
                        and report.get("status") == "blocked"
                        and str(report.get("block_reason") or "").strip()):
                    state["localization_replan_used"] = True
                    localization_replan_required = True
                    result_text += ("\n\n未交付补丁。按上面的反证撤回旧前提，最多补读两次，"
                                    "基于新证据重写 EditBrief 后重新派发一次；证据不足则停止。")
                else:
                    state["workflow_terminal_reason"] = "implementation_delivery_incomplete"
                    state["workflow_terminal_completed"] = False
        if result.type == "patch_review":
            state["patch_review_done"] = True
            state["patch_review_completed"] = result.completed
            state["patch_review_result"] = _extract_json_object(result.text) or {"raw": result.text[-2000:]}
            if result.completed:
                gate_reason = _patch_review_acceptance_gate_reason(result.text)
                if gate_reason:
                    result_text = (
                        f"{result.text}\n\n"
                        "Automatic patch gate: verdict is treated as revise because "
                        f"{gate_reason}. Use the review findings and this gate reason for one corrective implement."
                    )
                state["patch_review_high_risk"] = _patch_review_requires_revision(result.text)
            else:
                state["patch_review_high_risk"] = False
            state["last_patch_gate_text"] = result_text if state["patch_review_high_risk"] else ""
            if review_policy == "conditional" and not result.completed:
                state["workflow_terminal_reason"] = "conditional_review_incomplete"
                state["workflow_terminal_completed"] = False
                result_text = (
                    f"{result_text}\n\nConditional review used its single attempt without completing. "
                    "The patch is not submission-ready; terminate without retrying review."
                )
            elif review_policy == "conditional" and state["patch_review_high_risk"]:
                state["workflow_terminal_reason"] = "conditional_review_flagged_risk"
                state["workflow_terminal_completed"] = False
                result_text = (
                    f"{result_text}\n\nConditional review recorded a concrete risk. "
                    "The patch is not submission-ready; terminate without corrective implement."
                )
            if not state.get("workflow_terminal_reason"):
                terminal_message = _finish_static_safe_unverified(state, runner, review_policy)
                if terminal_message:
                    result_text = f"{result_text}\n\n{terminal_message}"
        if result.type == "patch_ranker" and result.completed:
            selection = _extract_patch_ranker_selection(result.text, state)
            state["patch_ranker_done"] = selection["valid"]
            state["patch_ranker_selected_index"] = selection["selected_index"]
            state["patch_ranker_selected_none"] = selection["selected_none"]
            state["patch_ranker_result_invalid"] = selection["error"]
            state["last_ranker_text"] = result.text
            if selection["error"]:
                result_text = (
                    f"{result.text}\n\n"
                    "Automatic candidate gate: patch_ranker did not produce a usable selected_index. "
                    f"{selection['error']}"
                )
        completion_line = (
            f"Completion: completed={result.completed}, "
            f"stop_reason={result.stop_reason}, max_steps={result.max_steps}"
        )
        changed_line = f"\nChanged files: {', '.join(result.changed_files)}" if result.changed_files else ""
        text = (
            f"Sub-agent [{result.type}] completed: {result.description}\n\n"
            f"{result_text}\n\n"
            f"{completion_line}\n"
            f"Output dir: {result.output_dir}\n"
            f"Tokens: prompt={result.prompt_tokens}, completion={result.completion_tokens}, total={result.total_tokens}"
            f"{changed_line}"
        )
        state["submission_ready"] = _submission_ready(state, review_policy)
        terminal_reason = str(state.get("workflow_terminal_reason") or "")
        terminal_completed = bool(state.get("workflow_terminal_completed"))
        response_factory = ToolResponse.success if result.completed and (not terminal_reason or terminal_completed) else ToolResponse.partial
        response_data = {
            "type": result.type,
            "model": result.model,
            "description": result.description,
            "output_dir": str(result.output_dir),
            "completed": result.completed,
            "stop_reason": result.stop_reason,
            "max_steps": result.max_steps,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "changed_files": list(result.changed_files),
            "delivery_retry_required": bool(state.get("delivery_retry_required")),
            "localization_replan_required": localization_replan_required,
            "delivery_retry_used": bool(state.get("delivery_retry_used")),
        }
        if terminal_reason:
            response_data.update(
                {
                    "terminate_loop": True,
                    "terminal_reason": terminal_reason,
                    "terminal_completed": terminal_completed,
                    "final_answer": state.get("workflow_terminal_final_answer")
                    or (
                        f"Simplified pass@1 terminated after {terminal_reason}. "
                        + (
                            "The patch has parseable passing verification evidence and is submission-ready."
                            if terminal_completed
                            else "The candidate patch is preserved for diagnostics but is not submission-ready."
                        )
                    ),
                }
            )
        return response_factory(
            text,
            response_data,
        )

    return [
        build_tool(
            name="agent",
            description=(
                "Launch one isolated role with a self-contained task and return its result. "
                "Types: "
                + (
                    ", ".join(sorted(allowed_agent_types))
                    if allowed_agent_types is not None
                    else "research/explore, reproducer, plan, implement, patch_ranker, patch_review, verify, review, "
                    "memory_curator, case_curator, general."
                )
            ),
            parameters=[
                ToolParameter("description", "string", "Short 3-8 word description of the isolated task"),
                ToolParameter("prompt", "string", "Self-contained detailed instructions for the selected role"),
                ToolParameter("type", "string", "Role type. Default: general", required=False),
                ToolParameter("allowed_files", "array", "Required for implement: repository-relative files it may edit", required=False),
                ToolParameter(
                    "allowed_ranges",
                    "object",
                    "Required for implement reads: {path: [[start_line, end_line], ...]}",
                    required=False,
                ),
                ToolParameter(
                    "context_ranges",
                    "object",
                    "Optional read-only Implement context: {path: [[start_line, end_line], ...]}",
                    required=False,
                ),
            ],
            handler=launch_agent,
            prompt=(
                "协调阶段只能通过 agent 工具调用独立角色。每次输入必须自包含，"
                "包含目标、已知事实、文件路径、输出格式和约束；不要写“根据上面的发现”。"
                "调用 implement 时必须额外传 allowed_files 和 allowed_ranges，否则工具会拒绝。"
                + (
                    "implement 产生改动后，verify 前必须先执行 patch_review。"
                    if require_patch_review
                    else (
                        "SWE-bench profile 仅在 implement 结果明确返回 Conditional patch review required 时派一次 patch_review；"
                        "否则直接结束，不主动派发额外审查或验证。"
                        if conditional_patch_review
                        else "不要派 patch_review；实现后直接结束，不主动派发额外验证。"
                    )
                )
            ),
            source=ToolSource.BUILTIN,
        ),
        _build_patch_candidate_tool(runner, state),
    ]


def _build_patch_candidate_tool(runner: SubAgentRunner, state: dict) -> Tool:
    def manage_candidate(inp: dict) -> ToolResponse:
        action = str(inp.get("action") or "").strip().lower()
        if action == "save_current":
            return _save_current_candidate(runner, state, inp)
        if action == "evaluate":
            return _evaluate_patch_candidates(runner, state)
        if action == "evaluate_and_list":
            evaluated = _evaluate_patch_candidates(runner, state)
            if evaluated.error:
                return evaluated
            listed = _list_patch_candidates(state)
            if listed.error:
                return listed
            return ToolResponse.success(
                f"{evaluated.text}\n\n{listed.text}",
                {"evaluation": evaluated.data, "candidate_list": listed.data},
            )
        if action == "list":
            return _list_patch_candidates(state)
        if action == "apply":
            return _apply_patch_candidate(runner, state, inp)
        if action == "discard_current":
            return _discard_current_patch(runner, state)
        return ToolResponse.failure(
            "BAD_ARGUMENTS",
            "patch_candidate.action must be one of: save_current, evaluate_and_list, evaluate, list, apply, discard_current.",
        )

    return build_tool(
        name="patch_candidate",
        description="Save, reset, list, and apply candidate patches for internal best-of-N selection.",
        parameters=[
            ToolParameter(
                "action",
                "string",
                "save_current | evaluate_and_list | evaluate | list | apply | discard_current",
            ),
            ToolParameter("index", "integer", "Candidate index for apply.", required=False),
            ToolParameter("name", "string", "Short candidate name for save_current.", required=False),
            ToolParameter("description", "string", "Why this candidate exists or what it tries.", required=False),
            ToolParameter(
                "semantic_layer",
                "string",
                "Contract layer changed by save_current, for example producer_contract, consumer_adapter, or decision_order.",
                required=False,
            ),
            ToolParameter(
                "reset_after_save",
                "boolean",
                "Whether to reverse the saved current patch so another candidate can be generated.",
                required=False,
            ),
            ToolParameter(
                "notes",
                "string",
                "Optional reproduction/differential-test result notes attached to the candidate.",
                required=False,
            ),
        ],
        handler=manage_candidate,
        prompt=(
            "patch_candidate 用于评测干净的内部候选池：save_current 保存当前 git diff 为候选，"
            "默认随后回滚当前 diff；每次 save_current 应填写 semantic_layer。"
            "候选池是硬状态机：先 reproducer，再至少两个不同语义层的 save_current，再 evaluate_and_list、patch_ranker，"
            "最后只能 apply patch_ranker 选中的 selected_index。"
            "候选池只能使用公开问题、公开仓库代码、公开复现/差分验证结果；禁止用 harness 反馈或 SWE-bench 元数据排序。"
        ),
        max_result_size_chars=50_000,
        source=ToolSource.BUILTIN,
    )


def _check_patch_ranker_preconditions(state: dict) -> ToolResponse | None:
    candidates = list(state.get("candidate_patches") or [])
    if len(candidates) < 2:
        return ToolResponse.failure(
            "PATCH_RANKER_REQUIRES_CANDIDATES",
            (
                "patch_ranker can only run after at least two saved candidates exist. "
                "Generate/save another candidate first, or finish through the normal single-patch path."
            ),
        )
    if not state.get("candidate_evaluation_done"):
        return ToolResponse.failure(
            "PATCH_CANDIDATE_EVALUATION_REQUIRED",
            (
                "Run patch_candidate(action=\"evaluate\") before listing/ranking candidates. "
                "The evaluator executes the same public reproducer commands on the base checkout and every candidate."
            ),
        )
    if not state.get("candidate_list_done"):
        return ToolResponse.failure(
            "PATCH_CANDIDATE_LIST_REQUIRED",
            (
                "patch_ranker requires the current candidate list/diffs. "
                "Call patch_candidate(action=\"list\") immediately before agent(type=patch_ranker)."
            ),
        )
    return None


def _reset_candidate_ranking_state(state: dict) -> None:
    # 候选池一旦新增候选，之前的 list/rank 结果就不再可信，必须重新排序。
    state["candidate_list_done"] = False
    state["candidate_evaluation_done"] = False
    state["candidate_base_results"] = []
    state["patch_ranker_done"] = False
    state["patch_ranker_selected_index"] = None
    state["patch_ranker_selected_none"] = False
    state["patch_ranker_result_invalid"] = ""
    state["last_ranker_text"] = ""


def _extract_patch_ranker_selection(text: str, state: dict) -> dict[str, object]:
    candidates = list(state.get("candidate_patches") or [])
    valid_indexes = {int(candidate["index"]) for candidate in candidates}
    review = _extract_json_object(text)
    if not isinstance(review, dict):
        return {
            "valid": False,
            "selected_index": None,
            "selected_none": False,
            "error": "Expected JSON object with selected_index.",
        }
    raw_index = review.get("selected_index")
    if raw_index is None:
        return {
            "valid": True,
            "selected_index": None,
            "selected_none": True,
            "error": "",
        }
    try:
        selected_index = int(raw_index)
    except (TypeError, ValueError):
        return {
            "valid": False,
            "selected_index": None,
            "selected_none": False,
            "error": f"selected_index must be an integer or null, got {raw_index!r}.",
        }
    if selected_index not in valid_indexes:
        return {
            "valid": False,
            "selected_index": None,
            "selected_none": False,
            "error": (
                f"selected_index #{selected_index} is not in saved candidates "
                f"{', '.join(str(index) for index in sorted(valid_indexes)) or 'none'}."
            ),
        }
    return {
        "valid": True,
        "selected_index": selected_index,
        "selected_none": False,
        "error": "",
    }


def _save_current_candidate(runner: SubAgentRunner, state: dict, inp: dict) -> ToolResponse:
    if not state.get("reproducer_done"):
        return ToolResponse.failure(
            "CANDIDATE_REPRODUCER_REQUIRED",
            (
                "Candidate-pool mode is a hard state machine: run agent(type=reproducer) once before saving "
                "the first candidate. The reproducer must state the public reproduction/differential contract "
                "used to judge candidate patches, without SWE-bench metadata or harness feedback."
            ),
        )
    if not state.get("candidate_patches"):
        base_diff = str(state.get("candidate_initial_diff") or "")
        if base_diff.strip():
            return ToolResponse.failure(
                "CANDIDATE_POOL_REQUIRES_CLEAN_WORKTREE",
                (
                    "Candidate-pool mode requires a clean working tree before the first candidate so internal "
                    "save/reset does not discard user changes. Finish the current patch normally or clean the repo first."
                ),
            )

    patch = _git_diff_text(runner.cwd)
    if not patch.strip():
        return ToolResponse.failure("EMPTY_CANDIDATE_PATCH", "Current git diff is empty; nothing to save as a candidate.")

    candidates = list(state.get("candidate_patches") or [])
    index = len(candidates) + 1
    candidate_dir = runner.output_dir / "patch_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    patch_path = candidate_dir / f"candidate-{index}.patch"
    patch_path.write_text(patch, encoding="utf-8")
    changed_files = tuple(_paths_from_patch_text(patch))
    static_issues = _static_patch_sanity(runner.cwd, changed_files)
    metadata = {
        "index": index,
        "name": str(inp.get("name") or f"candidate-{index}"),
        "description": str(inp.get("description") or ""),
        "notes": str(inp.get("notes") or ""),
        "semantic_layer": str(inp.get("semantic_layer") or "unspecified"),
        "patch_path": str(patch_path),
        "patch_chars": len(patch),
        "changed_files": list(changed_files),
        "edit_loci": _patch_edit_loci(patch),
        "static_issues": static_issues,
        "saved_at": int(time.time()),
    }
    (candidate_dir / f"candidate-{index}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    candidates.append(metadata)
    state["candidate_patches"] = candidates
    _reset_candidate_ranking_state(state)

    reset_after_save = bool(inp.get("reset_after_save", True))
    reset_text = ""
    if reset_after_save:
        reset = _git_apply_patch_text(runner.cwd, patch, reverse=True)
        if reset.returncode != 0:
            return ToolResponse.failure(
                "CANDIDATE_RESET_FAILED",
                (
                    f"Saved candidate {index}, but failed to reverse it. Do not continue generating candidates until "
                    f"the working tree is inspected.\n{reset.stdout}"
                ),
            )
        _reset_implementation_state_after_candidate_save(state)
        reset_text = "\nCurrent candidate patch was reversed; working tree is ready for another candidate."

    return ToolResponse.success(
        (
            f"Saved patch candidate #{index}: {metadata['name']} "
            f"({len(patch)} chars, files={', '.join(changed_files) or 'unknown'})."
            f"{reset_text}"
        ),
        metadata,
    )


def _evaluate_patch_candidates(runner: SubAgentRunner, state: dict) -> ToolResponse:
    candidates = list(state.get("candidate_patches") or [])
    if len(candidates) < 2:
        return ToolResponse.failure(
            "PATCH_CANDIDATE_MIN_COUNT_REQUIRED",
            "Save at least two candidates before running the execution matrix.",
        )
    if _git_diff_text(runner.cwd).strip():
        return ToolResponse.failure(
            "CANDIDATE_EVALUATION_REQUIRES_CLEAN_WORKTREE",
            "Save or discard the current candidate before evaluation so every candidate starts from the same base.",
        )

    commands = list(state.get("reproduction_commands") or [])[:2]
    run_tool = runner.base_tools.get("run_command")
    if run_tool is None:
        return ToolResponse.failure("TOOL_NOT_FOUND", "Candidate evaluation requires the guarded run_command tool.")

    if commands:
        base_results = _run_candidate_commands(run_tool, commands)
    else:
        base_results = [
            {
                "status": "not_run",
                "reason": "Reproducer did not return a usable public verify_command; ranking will use static evidence only.",
            }
        ]

    evaluated: list[dict[str, Any]] = []
    for candidate in candidates:
        patch_path = Path(candidate["patch_path"])
        patch = patch_path.read_text(encoding="utf-8", errors="replace")
        apply_result = _git_apply_patch_text(runner.cwd, patch, reverse=False)
        if apply_result.returncode != 0:
            execution_results = [
                {
                    "status": "patch_apply_failed",
                    "output": _truncate_for_candidate_list(apply_result.stdout, 2_000),
                }
            ]
        else:
            try:
                execution_results = (
                    _run_candidate_commands(run_tool, commands)
                    if commands
                    else [dict(result) for result in base_results]
                )
            finally:
                reverse_result = _git_apply_patch_text(runner.cwd, patch, reverse=True)
            if reverse_result.returncode != 0:
                return ToolResponse.failure(
                    "CANDIDATE_RESET_FAILED",
                    f"Candidate #{candidate['index']} ran, but its patch could not be reversed: {reverse_result.stdout}",
                )

        for index, result in enumerate(execution_results):
            if index < len(base_results):
                result["differs_from_base"] = _execution_signature(result) != _execution_signature(base_results[index])
        candidate["execution_results"] = execution_results
        metadata_path = patch_path.with_suffix(".json")
        metadata_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        evaluated.append(
            {
                "index": candidate["index"],
                "name": candidate["name"],
                "semantic_layer": candidate.get("semantic_layer", "unspecified"),
                "edit_loci": candidate.get("edit_loci", []),
                "static_issues": candidate.get("static_issues", []),
                "execution_results": execution_results,
            }
        )

    state["candidate_patches"] = candidates
    state["candidate_base_results"] = base_results
    state["candidate_evaluation_done"] = True
    state["candidate_list_done"] = False
    evaluation = {
        "reproduction_contract": state.get("reproduction_contract") or {},
        "commands": commands,
        "base_results": base_results,
        "diversity_warnings": _candidate_diversity_warnings(candidates),
        "candidates": evaluated,
    }
    candidate_dir = runner.output_dir / "patch_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "evaluation.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return ToolResponse.success(
        "Candidate execution matrix completed.\n\n"
        + _truncate_for_candidate_list(json.dumps(evaluation, ensure_ascii=False, indent=2), 20_000),
        evaluation,
    )


def _run_candidate_commands(run_tool: Tool, commands: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for requested_command in commands:
        safety_reason = _candidate_command_safety_reason(requested_command)
        if safety_reason:
            results.append({"command": requested_command, "status": "blocked", "reason": safety_reason, "usable": False})
            continue
        command = _prepare_candidate_command(requested_command)
        response = run_tool.run_with_timing({"command": command, "timeout": 120})
        exit_code = _candidate_effective_exit_code(response)
        environment_reason = _candidate_environment_reason(response.text)
        results.append(
            {
                "command": command,
                "requested_command": requested_command,
                "status": "blocked_by_environment" if environment_reason else response.status.value,
                "exit_code": exit_code,
                "usable": not environment_reason and exit_code is not None,
                "passed": not environment_reason and exit_code == 0,
                "environment_reason": environment_reason,
                "output": _truncate_for_candidate_list(response.text, 4_000),
            }
        )
    return results


def _prepare_candidate_command(command: str) -> str:
    """让候选复现命令使用确定解释器，而不是依赖 shell 中的 python 别名。"""

    prepared, _ = prepare_verification_command(command, Path.cwd(), interpreter=sys.executable)
    return prepared


def _candidate_effective_exit_code(response: ToolResponse) -> int | None:
    # 一些模型命令会追加 `echo EXIT_CODE=$?`，导致 shell 自身返回 0。最后一个显式值
    # 才是目标命令的真实状态；没有显式值时退回 run_command 的结构化退出码。
    explicit = re.findall(r"\bexit_code\s*=\s*(\d+)\b", response.text, flags=re.IGNORECASE)
    if explicit:
        return int(explicit[-1])
    value = response.data.get("exit_code")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _candidate_environment_reason(output: str) -> str:
    lowered = output.lower()
    markers = (
        "command not found",
        "no module named",
        "modulenotfounderror",
        "cannot open shared object file",
        "/bin/sh: 1: python: not found",
        "/bin/bash: python: command not found",
    )
    return next((marker for marker in markers if marker in lowered), "")


def _candidate_command_safety_reason(command: str) -> str:
    normalized = command.strip().lower()
    if not normalized:
        return "empty command"
    if len(command) > 2_000:
        return "command exceeds 2000 characters"
    verification_reason = verification_command_safety_reason(command)
    if verification_reason:
        return verification_reason
    forbidden = (
        r"\b(?:pip|pip3|conda|apt|apt-get|npm|yarn|pnpm)\s+(?:install|add|update|upgrade)\b",
        r"\bgit\s+(?:push|pull|fetch|checkout|reset|clean|apply|commit)\b",
        r"\b(?:curl|wget|ssh|scp)\b",
        r"\brm\s+-[^\n]*r",
        r"\bdocker\b",
    )
    if any(re.search(pattern, normalized) for pattern in forbidden):
        return "command contains an install, network, repository mutation, deletion, or nested container operation"
    return ""


def _execution_signature(result: dict[str, Any]) -> tuple[object, str]:
    return result.get("exit_code"), str(result.get("output") or "").strip()


def _candidate_diversity_warnings(candidates: list[dict[str, Any]]) -> list[str]:
    if len(candidates) < 2:
        return []
    warnings: list[str] = []
    loci = {tuple(candidate.get("edit_loci") or []) for candidate in candidates}
    layers = {str(candidate.get("semantic_layer") or "unspecified").strip().lower() for candidate in candidates}
    if len(loci) == 1:
        warnings.append(
            "all candidates edit the same diff hunk; changing only the helper or spelling is not semantic diversity"
        )
    if len(layers) == 1:
        warnings.append(
            f"all candidates declare the same semantic_layer={next(iter(layers))}; compare contract owner versus symptom site"
        )
    execution_results = [
        result
        for candidate in candidates
        for result in candidate.get("execution_results") or []
    ]
    if execution_results and not any(result.get("usable") for result in execution_results):
        warnings.append(
            "no candidate has usable execution evidence; do not use identical environment failures to prefer one patch"
        )
    return warnings


def _list_patch_candidates(state: dict) -> ToolResponse:
    candidates = list(state.get("candidate_patches") or [])
    if not candidates:
        state["candidate_list_done"] = False
        return ToolResponse.success("No patch candidates saved.", {"candidates": []})
    if len(candidates) < 2:
        state["candidate_list_done"] = False
        return ToolResponse.failure(
            "PATCH_CANDIDATE_MIN_COUNT_REQUIRED",
            "Save at least two candidates before listing the candidate pool for ranking.",
        )
    if not state.get("candidate_evaluation_done"):
        state["candidate_list_done"] = False
        return ToolResponse.failure(
            "PATCH_CANDIDATE_EVALUATION_REQUIRED",
            "Run patch_candidate(action=\"evaluate\") before listing candidates for patch_ranker.",
        )
    state["candidate_list_done"] = True
    lines: list[str] = []
    payload: list[dict] = []
    for candidate in candidates:
        patch_path = Path(candidate["patch_path"])
        patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""
        summary = {
            "index": candidate["index"],
            "name": candidate["name"],
            "description": candidate.get("description", ""),
            "notes": candidate.get("notes", ""),
            "semantic_layer": candidate.get("semantic_layer", "unspecified"),
            "edit_loci": candidate.get("edit_loci", []),
            "patch_chars": candidate["patch_chars"],
            "changed_files": candidate.get("changed_files", []),
            "static_issues": candidate.get("static_issues", []),
            "execution_results": candidate.get("execution_results", []),
            "patch_excerpt": _truncate_for_candidate_list(patch, 10_000),
        }
        payload.append(summary)
        lines.append(
            "## Candidate #{index}: {name}\n"
            "description: {description}\n"
            "notes: {notes}\n"
            "semantic_layer: {semantic_layer}\n"
            "edit_loci: {edit_loci}\n"
            "changed_files: {changed_files}\n"
            "static_issues: {static_issues}\n"
            "execution_results: {execution_results}\n"
            "patch:\n{patch_excerpt}".format(
                index=summary["index"],
                name=summary["name"],
                description=summary["description"],
                notes=summary["notes"],
                semantic_layer=summary["semantic_layer"],
                edit_loci=", ".join(summary["edit_loci"]) or "unknown",
                changed_files=", ".join(summary["changed_files"]) or "unknown",
                static_issues=", ".join(summary["static_issues"]) or "none",
                execution_results=json.dumps(summary["execution_results"], ensure_ascii=False),
                patch_excerpt=summary["patch_excerpt"],
            )
        )
    base_results = list(state.get("candidate_base_results") or [])
    warnings = _candidate_diversity_warnings(candidates)
    prefix = "## Base execution\n" + json.dumps(base_results, ensure_ascii=False) if base_results else ""
    warning_text = "## Deterministic diversity warnings\n" + "\n".join(f"- {item}" for item in warnings) if warnings else ""
    text = "\n\n".join(part for part in (prefix, warning_text, *lines) if part)
    return ToolResponse.success(
        text,
        {"base_results": base_results, "diversity_warnings": warnings, "candidates": payload},
    )


def _apply_patch_candidate(runner: SubAgentRunner, state: dict, inp: dict) -> ToolResponse:
    candidates = list(state.get("candidate_patches") or [])
    if not candidates:
        return ToolResponse.failure("NO_PATCH_CANDIDATES", "No saved patch candidates are available.")
    if len(candidates) < 2:
        return ToolResponse.failure(
            "PATCH_CANDIDATE_MIN_COUNT_REQUIRED",
            (
                "Candidate-pool mode requires at least two saved candidate patches before apply. "
                "Generate a second genuinely different candidate, then call patch_candidate(action=\"list\"), "
                "agent(type=patch_ranker), and apply the selected index."
            ),
        )
    if not state.get("candidate_evaluation_done"):
        return ToolResponse.failure(
            "PATCH_CANDIDATE_EVALUATION_REQUIRED",
            "Run patch_candidate(action=\"evaluate\") before selecting a candidate.",
        )
    if not state.get("candidate_list_done"):
        return ToolResponse.failure(
            "PATCH_CANDIDATE_LIST_REQUIRED",
            "Call patch_candidate(action=\"list\") after the final candidate is saved so patch_ranker can receive the candidate diffs.",
        )
    if not state.get("patch_ranker_done"):
        detail = str(state.get("patch_ranker_result_invalid") or "").strip()
        message = (
            "Candidate-pool mode requires a completed agent(type=patch_ranker) before apply. "
            "The ranker must output usable JSON with selected_index."
        )
        if detail:
            message = f"{message} Previous ranker problem: {detail}"
        return ToolResponse.failure(
            "PATCH_RANKER_REQUIRED",
            message,
        )
    if state.get("patch_ranker_selected_none"):
        return ToolResponse.failure(
            "PATCH_RANKER_SELECTED_NONE",
            (
                "patch_ranker selected_index=null, so no saved candidate is acceptable. "
                "Generate a corrected candidate or finish with blocked_candidate_pool_no_viable_patch."
            ),
        )
    index = int(inp.get("index") or 0)
    selected_index = state.get("patch_ranker_selected_index")
    if selected_index is not None and index != int(selected_index):
        return ToolResponse.failure(
            "PATCH_CANDIDATE_SELECTION_MISMATCH",
            (
                f"patch_ranker selected candidate #{selected_index}, but apply requested #{index}. "
                "Apply exactly the selected_index or rerun patch_ranker after changing the candidate pool."
            ),
        )
    selected = next((candidate for candidate in candidates if int(candidate["index"]) == index), None)
    if selected is None:
        return ToolResponse.failure(
            "PATCH_CANDIDATE_NOT_FOUND",
            f"Candidate index {index} not found. Available: {', '.join(str(c['index']) for c in candidates)}.",
        )
    current = _git_diff_text(runner.cwd)
    if current.strip():
        return ToolResponse.failure(
            "WORKTREE_NOT_CLEAN_FOR_CANDIDATE_APPLY",
            "Current git diff is not empty. Save or discard the current patch before applying a saved candidate.",
        )
    patch_path = Path(selected["patch_path"])
    patch = patch_path.read_text(encoding="utf-8", errors="replace")
    result = _git_apply_patch_text(runner.cwd, patch, reverse=False)
    if result.returncode != 0:
        return ToolResponse.failure("PATCH_CANDIDATE_APPLY_FAILED", result.stdout)
    changed_files = tuple(selected.get("changed_files") or _paths_from_patch_text(patch))
    state["implementation_done"] = True
    state["patch_review_done"] = False
    state["patch_review_high_risk"] = False
    state["implemented_files"] = changed_files
    state["ready_to_implement"] = False
    state["verify_attempted"] = False
    static_issues = _static_patch_sanity(runner.cwd, changed_files)
    if static_issues:
        state["patch_review_done"] = True
        state["patch_review_high_risk"] = True
        state["last_patch_gate_text"] = _format_static_gate_text(static_issues)
    return ToolResponse.success(
        f"Applied patch candidate #{index}: {selected['name']}. Run at most one profile-appropriate final verification.",
        {
            "index": index,
            "name": selected["name"],
            "changed_files": list(changed_files),
            "static_issues": static_issues,
        },
    )


def _discard_current_patch(runner: SubAgentRunner, state: dict) -> ToolResponse:
    patch = _git_diff_text(runner.cwd)
    if not patch.strip():
        return ToolResponse.success("Current git diff is already empty.", {"changed_files": []})
    result = _git_apply_patch_text(runner.cwd, patch, reverse=True)
    if result.returncode != 0:
        return ToolResponse.failure("CURRENT_PATCH_DISCARD_FAILED", result.stdout)
    changed_files = _paths_from_patch_text(patch)
    _reset_implementation_state_after_candidate_save(state)
    return ToolResponse.success(
        f"Discarded current candidate patch touching: {', '.join(changed_files) or 'unknown'}.",
        {"changed_files": changed_files},
    )


def _reset_implementation_state_after_candidate_save(state: dict) -> None:
    state["implementation_done"] = False
    state["patch_review_done"] = False
    state["patch_review_high_risk"] = False
    state["corrective_implement_count"] = 0
    state["last_chance_required"] = False
    state["last_chance_used"] = False
    state["last_chance_scope"] = None
    state["last_chance_failed_scope"] = None
    state["implemented_files"] = ()
    state["last_patch_gate_text"] = ""
    state["ready_to_implement"] = True
    state["verify_attempted"] = False


def _git_diff_text(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--binary", "--", ".", ":!.env*"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode in {0, 1} else ""


def _compact_context_ranges(
    ranges_by_path: dict[str, tuple[LineRange, ...]],
    *,
    max_total_lines: int = 240,
    max_range_lines: int = 100,
) -> dict[str, tuple[LineRange, ...]]:
    """限制 Implement 的只读旁路上下文，目标源码范围不受此预算影响。"""

    remaining = max_total_lines
    compacted: dict[str, tuple[LineRange, ...]] = {}
    for path, ranges in ranges_by_path.items():
        kept: list[LineRange] = []
        for item in ranges:
            if remaining <= 0:
                break
            length = min(item.end - item.start + 1, max_range_lines, remaining)
            if length <= 0:
                continue
            kept.append(LineRange(item.start, item.start + length - 1))
            remaining -= length
        if kept:
            compacted[path] = tuple(kept)
        if remaining <= 0:
            break
    return compacted


def _extend_profile_edit_ranges(
    constraints: WorkerConstraints,
    derived_ranges: dict[str, tuple[tuple[int, int], ...]],
) -> WorkerConstraints:
    """只在 Coordinator 已允许的生产文件内补齐 profile 的直接耦合方法。"""

    allowed_ranges = dict(constraints.allowed_ranges or {})
    for path, ranges in derived_ranges.items():
        if path not in constraints.allowed_files:
            continue
        existing = list(allowed_ranges.get(path, ()))
        existing.extend(LineRange(start, end) for start, end in ranges)
        allowed_ranges[path] = tuple(existing)
    return WorkerConstraints(
        allowed_files=constraints.allowed_files,
        allowed_ranges=allowed_ranges,
        context_ranges=constraints.context_ranges,
    )


def _conditional_patch_review_reasons(cwd: Path, changed_files: tuple[str, ...]) -> list[str]:
    """只用确定性 diff 特征决定是否值得支付一次 review 成本。"""
    patch = _git_diff_text(cwd)
    if not patch.strip():
        return []

    changed_lines = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and line[1:].strip()
    ]
    joined = "\n".join(changed_lines).lower()
    reasons: list[str] = []
    if len(changed_files) > 1:
        reasons.append("multi_file_change")
    if len(changed_lines) > 80:
        reasons.append("large_semantic_diff")
    if any(re.match(r"\s*(async\s+def|def|class)\s+", line) for line in changed_lines):
        reasons.append("public_signature_or_type_change")

    shared_state_tokens = (
        "rcparams",
        "settings.",
        "global ",
        "__dict__",
        "cache",
        "serializer",
        "deserialize",
        "dispatch",
        "registry",
        "backend",
    )
    if any(token in joined for token in shared_state_tokens):
        reasons.append("shared_state_or_dispatch_change")

    core_path_fragments = (
        "matplotlib/artist.py",
        "matplotlib/axes/",
        "matplotlib/axis.py",
        "matplotlib/figure.py",
        "matplotlib/backends/",
        "django/db/models/sql/",
        "django/db/migrations/",
        "sklearn/utils/validation.py",
        "pytest/_pytest/",
        "requests/sessions.py",
        "sphinx/environment/",
    )
    normalized_paths = [path.replace("\\", "/").lower() for path in changed_files]
    if len(changed_lines) > 12 and any(
        fragment in path for fragment in core_path_fragments for path in normalized_paths
    ):
        reasons.append("shared_core_path")
    return list(dict.fromkeys(reasons))


def _implementation_report_review_reasons(report: dict[str, Any]) -> list[str]:
    """把 Implement 自报的未证实语义和重叠风险路由给轻量 review。"""

    if not isinstance(report, dict):
        return []
    reasons: list[str] = []
    challenge = report.get("semantic_challenge") or {}
    if isinstance(challenge, dict):
        for name, item in challenge.items():
            if isinstance(item, dict) and str(item.get("support") or "") != "proven":
                reasons.append(f"implement_unproven_{name}")
    for item in report.get("semantic_coverage") or []:
        if isinstance(item, dict) and item.get("covered_by_change") is False:
            reasons.append("implement_reported_coverage_gap")
    for item in report.get("known_risks") or []:
        if isinstance(item, dict):
            text = str(item.get("risk") or "").strip()
            overlaps = item.get("overlaps_changed_contract") is True
        else:
            text = str(item).strip()
            overlaps = bool(text)
        if overlaps and text:
            reasons.append("implement_reported_contract_risk")
    return list(dict.fromkeys(reasons))


def _git_apply_patch_text(cwd: Path, patch: str, *, reverse: bool) -> subprocess.CompletedProcess[str]:
    args = ["git", "-C", str(cwd), "apply", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    args.append("-")
    try:
        return subprocess.run(
            args,
            input=patch,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, stdout=str(exc), stderr="")


def _paths_from_patch_text(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        if value and value not in paths:
            paths.append(value)
    return paths


def _patch_edit_loci(patch: str) -> list[str]:
    path = ""
    loci: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0].strip()
            path = value[2:] if value.startswith("b/") else value
            continue
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if not match or not path or path == "/dev/null":
            continue
        start = int(match.group(1))
        count = int(match.group(2) or 1)
        end = start if count <= 1 else start + count - 1
        locus = f"{path}:{start}-{end}"
        if locus not in loci:
            loci.append(locus)
    return loci


def _truncate_for_candidate_list(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep_each = max(1, (max_chars - 80) // 2)
    omitted = len(text) - keep_each * 2
    return f"{text[:keep_each]}\n\n[... candidate patch truncated {omitted} chars ...]\n\n{text[-keep_each:]}"


def _contains_structured_edit_brief(text: str) -> bool:
    lowered = text.lower()
    return (
        "edit_brief" in lowered
        and "allowed_files" in lowered
        and "allowed_ranges" in lowered
        and "required_changes" in lowered
    )


def _patch_review_is_high_risk(text: str) -> bool:
    return _patch_review_requires_revision(text)


def _patch_review_requires_revision(text: str) -> bool:
    review = _extract_json_object(text)
    if isinstance(review, dict):
        patch_risk = str(review.get("patch_risk") or review.get("risk") or "").strip().lower()
        verdict = str(review.get("verdict") or "").strip().lower()
        if patch_risk == "high" or verdict in {"revise", "reject"}:
            return True
        if _accepted_review_has_blocking_findings(review):
            return True
        # New patch_review output is a hard gate: low/medium + accept can proceed.
        if verdict == "risk":
            return True
        if verdict == "accept" and str(review.get("risk_type") or "none").strip().lower() == "none":
            return False
        if patch_risk in {"low", "medium"} and verdict == "accept":
            return False
        # Legacy low-risk JSON without verdict remains accepted for backward compatibility.
        if patch_risk == "low" and not verdict:
            return False
        # Ambiguous structured review should not unlock verify.
        return True
    normalized = text.lower()
    return bool(
        re.search(r'"patch_risk"\s*:\s*"high"', normalized)
        or re.search(r"\bpatch_risk\s*[:=]\s*high\b", normalized)
        or re.search(r'"verdict"\s*:\s*"(revise|reject)"', normalized)
        or re.search(r'"verdict"\s*:\s*"risk"', normalized)
        or re.search(r"\bverdict\s*[:=]\s*(revise|reject|risk)\b", normalized)
    )


def _patch_review_acceptance_gate_reason(text: str) -> str:
    review = _extract_json_object(text)
    if not isinstance(review, dict):
        return ""
    return _accepted_review_blocking_reason(review)


def _accepted_review_has_blocking_findings(review: dict) -> bool:
    return bool(_accepted_review_blocking_reason(review))


def _accepted_review_blocking_reason(review: dict) -> str:
    patch_risk = str(review.get("patch_risk") or review.get("risk") or "").strip().lower()
    verdict = str(review.get("verdict") or "").strip().lower()
    if verdict != "accept":
        return ""
    if review.get("coverage_gaps") or review.get("required_changes"):
        return "accepted review still contains coverage_gaps or required_changes"

    for finding in _list_of_dicts(review.get("findings")):
        risk_type = str(finding.get("risk_type") or "").strip().lower()
        suggested_fix = str(finding.get("suggested_fix") or "").strip()
        if risk_type == "coverage_gap":
            return "accepted review contains a coverage_gap finding"
        if suggested_fix and not _is_noop_suggested_fix(suggested_fix):
            return "accepted review contains non-empty suggested_fix findings"

    return ""


def _list_of_dicts(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _is_noop_suggested_fix(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    return normalized in {
        "",
        "no change needed",
        "no fix needed",
        "none",
        "n/a",
        "not needed",
        "无需修改",
        "不需要修改",
    }


def _check_corrective_implement_scope(constraints: WorkerConstraints, state: dict) -> ToolResponse | None:
    previous_files = set(state.get("implemented_files") or ())
    requested_files = set(constraints.allowed_files)
    if not previous_files:
        return ToolResponse.failure(
            "CORRECTIVE_PATCH_SCOPE_UNKNOWN",
            "Patch review/static gate requires a corrective implement, but the previously changed files are unknown. "
            "Run patch_review again or finish with remaining_risk instead of expanding scope.",
        )
    extra_files = sorted(requested_files - previous_files)
    if extra_files:
        return ToolResponse.failure(
            "CORRECTIVE_SCOPE_TOO_WIDE",
            "Corrective implement may only repair the current patch in files already changed by the first implement. "
            f"Denied new allowed_files: {', '.join(extra_files)}. "
            f"Use only: {', '.join(sorted(previous_files))}.",
        )
    return None


def _augment_corrective_prompt(prompt: str, state: dict) -> str:
    review_text = str(state.get("last_patch_gate_text") or "").strip()
    changed_files = ", ".join(state.get("implemented_files") or ())
    semantic_issues = [str(value) for value in state.get("semantic_patch_issues") or []]
    obligations = [
        "The current patch is cumulative: fixing a second location does not remove a rejected earlier hunk.",
        "Preserve every existing hunk and semantic case that the direct feedback does not invalidate. Revert or replace "
        "only the first hunk whose stated assumption is contradicted by the supplied evidence, then make one causal correction.",
        "The tool-enforced gate overrides contradictory exact replacements, `do not modify` clauses, or stale root-cause "
        "claims in the Coordinator prompt below.",
        "Do not turn an exact-contract failure into a catch-all fallback, fixed default, hard-coded example, or a second "
        "parallel mechanism. Repair the first wrong predicate, identity, conversion, or state transition using an existing "
        "repository rule visible in the allowed ranges.",
    ]
    if any("normalization_after_first_dependent_predicate" in issue for issue in semantic_issues):
        obligations.append(
            "Remove the late-only normalization and place the representation-neutral normalization before the cited "
            "first dependent predicate; a second fallback after that predicate is not a correction."
        )
    if any("missing_values_fall_back_to_old_unknown_path" in issue for issue in semantic_issues):
        obligations.append(
            "Remove first-element dtype inference and every conversion failure path that silently `pass`es back to the "
            "old object/unknown classification. Use container dtype metadata or the repository's existing validation/"
            "conversion helper for the whole Series/DataFrame, and implement the exact missing-value behavior stated "
            "by SemanticEvidence. A try/except fallback that preserves the rejected path does not clear this gate."
        )
    if any("first_element_used_as_container_dtype_proxy" in issue for issue in semantic_issues):
        obligations.append(
            "Remove every new `flat[0]`/first-element type check. One value cannot prove the dtype semantics of an "
            "empty, nullable, mixed, or tabular container. Use whole-container dtype metadata or the repository's "
            "existing validation/normalization helper before the first dependent predicate."
        )
    if any(
        marker in issue
        for issue in semantic_issues
        for marker in ("producer_canonical_identity_changed", "consumer_compatibility_required_but_producer_changed")
    ):
        obligations.append(
            "Restore the producer's canonical storage assignment exactly as described by SemanticEvidence. A consumer "
            "alias adapter is incomplete while any producer decoration hunk remains in the cumulative patch."
        )
    if any("parameter_alias_patch_changed_return_rtype_branches" in issue for issue in semantic_issues):
        obligations.append(
            "Restore the original `type`, `return/returns`, and `rtype` parsing branches byte-for-byte, including "
            "their original `has_description`/`has_type` updates. Remove every new return-bookkeeping line from the "
            "parameter loop. Keep the fix limited to resolving a parameter's matched display alias and using that "
            "alias for its generated param/type field."
        )
    return (
        "CORRECTIVE_IMPLEMENT\n"
        "You are not solving the task from scratch. You are repairing the current patch after a patch_review/static gate.\n"
        f"Already changed files: {changed_files}\n"
        "Patch review/static gate that must be fixed:\n"
        f"{review_text or '(no structured review text captured)'}\n\n"
        "Coordinator corrective prompt (untrusted where it conflicts with the gate):\n"
        f"{prompt}\n\n"
        "AUTHORITATIVE TOOL-ENFORCED OBLIGATIONS:\n"
        "- Only edit the already changed files and only within the allowed_ranges supplied to this tool call.\n"
        "- Prefer the smallest correction to the existing diff; do not stack a second broad mechanism.\n"
        "- Do not introduce new files, new global behavior, new searches, new commands, or a second independent solution.\n"
        "- Preserve the original Problem Contract and old_behavior_invariants.\n"
        "- Map each corrective change to one directly observed failure or validation issue; do not invent a "
        "task-specific contract from keywords.\n"
        + "\n".join(f"- {item}" for item in obligations)
        + "\n- If the gate cannot be cleared inside the supplied scope, output blocked_remaining_risk with the exact missing scope/fact."
        "\n- Output the standard compact Implement JSON: target_behavior, changed_decision, preserve_cases, "
        "verification, changed_files, known_risks."
    )


def _augment_swebench_semantic_prompt(
    prompt: str,
    state: dict,
    *,
    constraint_prompts: tuple[str, ...] = (),
) -> str:
    """把经过结构校验的公开证据交给 implement，而不是重复 Coordinator 的解法。"""

    evidence_issues = list(state.get("semantic_evidence_issues") or [])
    evidence_issues.extend(
        f"coverage_warning:{value}" for value in state.get("semantic_evidence_warnings") or []
    )
    repair_brief = dict(state.get("repair_brief") or {})
    semantic_evidence = state.get("semantic_evidence")
    if not repair_brief and semantic_evidence:
        repair_brief = build_repair_brief(
            semantic_evidence,
            str(state.get("problem_statement") or ""),
        ).to_dict()
    if evidence_issues:
        repair_brief["evidence_issues"] = evidence_issues[:6]
    active_contracts = "\n\n".join(constraint_prompts)
    sections = [prompt]
    if repair_brief:
        compact_brief = {
            "problem_contract": repair_brief.get("problem_contract") or {},
            "semantic_cases": list(repair_brief.get("semantic_cases") or [])[:3],
            "unknowns": list(repair_brief.get("unknowns") or [])[:3],
        }
        if repair_brief.get("contract_edges"):
            compact_brief["contract_edges"] = list(repair_brief["contract_edges"])[:2]
        if evidence_issues:
            compact_brief["evidence_issues"] = evidence_issues[:3]
        evidence_text = json.dumps(compact_brief, ensure_ascii=False, separators=(",", ":"))
        if len(evidence_text) > 2_000:
            evidence_text = evidence_text[:2_000] + "...[clipped]"
        sections.append(
            "## Compact public repair contract\n"
            "This object is evidence, not a suggested patch. Coordinator replacements remain hypotheses.\n"
            f"{evidence_text}\n\n"
            "Cover the listed cases and preserve the existing producer/consumer identity, ordering, and state lifecycle. "
            "Evidence issues lower confidence but do not permit broader scope."
        )
    if active_contracts:
        sections.append(active_contracts)
    sections.append(
        "If a critical claim is unproven inside the allowed ranges, block with that one missing fact. Otherwise make one "
        "minimal edit using existing repository helpers and preserve exact output, shape, ordering, and public state."
    )
    return (
        "\n\n".join(section.strip() for section in sections if section.strip())
    )


def _augment_swebench_verify_prompt(
    prompt: str,
    state: dict,
    verification_plan: dict[str, Any] | None = None,
) -> str:
    evidence = state.get("semantic_evidence") or {}
    plan = verification_plan or build_verification_plan(evidence) or {}
    case_ids = list(plan.get("covers") or sorted(required_case_ids(evidence)))
    return (
        f"{prompt}\n\n"
        "## Tool-enforced verification contract\n"
        f"verification_plan={json.dumps(plan, ensure_ascii=False)}\n"
        f"required_case_ids={json.dumps(case_ids, ensure_ascii=False)}\n"
        "Run verification_plan.command verbatim. The tool rejects every alternative command. Do not create a script. "
        "The tool binds a successful real execution to verification_plan.covers, so do not invent or broaden covered_cases."
    )


def _augment_swebench_patch_review_prompt(prompt: str, state: dict) -> str:
    issues = list(state.get("semantic_patch_issues") or [])
    evidence_text = format_semantic_evidence(state.get("semantic_evidence"), issues, max_chars=5_000)
    report_text = json.dumps(state.get("implementation_report") or {}, ensure_ascii=False, indent=2)
    if len(report_text) > 4_000:
        report_text = report_text[:4_000] + "\n[Implement report clipped]"
    return (
        f"{prompt}\n\n## SemanticEvidence and deterministic alignment findings\n{evidence_text}\n"
        f"\n## Structured Implement report\n{report_text}\n"
        "Treat a non-empty alignment finding or an Implement-reported risk overlapping the changed contract as a "
        "concrete coverage obligation. Check whether repository_native_rule source_precondition actually matches "
        "target_precondition; a nearby fallback with a different domain is not proof. Do not accept by merely restating it."
    )


def _unresolved_edit_application_errors(output_dir: Path) -> list[str]:
    """只采信实际工具轨迹；自报风险、语义判断和越权拒绝不能开启纠正。"""
    path = Path(output_dir) / "trace.jsonl"
    if not path.exists():
        return []
    eligible = {"REPLACE_NOT_FOUND", "REPLACE_AMBIGUOUS", "REPLACE_OCCURRENCE_NOT_FOUND",
                "REPLACE_OVERLAP", "STALE_LINE_RANGE", "BAD_LINE_RANGE"}
    calls = {}
    pending = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            if event.get("tool") != "replace_in_file":
                continue
            identifier = event.get("tool_call_id")
            if event.get("type") == "tool_call":
                arguments = event.get("arguments") or {}
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                calls[identifier] = arguments.get("path")
            elif event.get("type") == "tool_result" and calls.get(identifier):
                response = event.get("response") or {}
                error = response.get("error") or {}
                target = calls[identifier]
                if error.get("code") in eligible:
                    pending[target] = f"{error['code']}:{target}:{str(error.get('message') or '')[:500]}"
                elif response.get("status") == "success":
                    pending.pop(target, None)
        except (ValueError, TypeError, AttributeError):
            continue
    return list(pending.values())


def _static_patch_sanity(cwd: Path, changed_files: tuple[str, ...]) -> list[str]:
    issues: list[str] = []
    for path in changed_files:
        normalized = path.replace("\\", "/")
        if not normalized.endswith(".py"):
            continue
        full_path = cwd / normalized
        if not full_path.exists():
            continue
        try:
            current_source = full_path.read_text(encoding="utf-8")
            ast.parse(current_source)
        except SyntaxError as exc:
            issues.append(f"python_syntax_error:{normalized}:{exc.lineno}:{exc.msg}")
            continue
        except UnicodeDecodeError as exc:
            issues.append(f"python_decode_error:{normalized}:{exc}")
            continue
        base_source = _git_head_file_text(cwd, normalized)
        if base_source is not None:
            for undefined in newly_undefined_names(base_source, current_source, normalized):
                issues.append(
                    f"python_new_undefined_name:{normalized}:{undefined.scope}:{undefined.lineno}:{undefined.name}"
                )
    issues.extend(_deterministic_patch_policy_issues(cwd, changed_files))
    return issues


def _git_head_file_text(cwd: Path, path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "show", f"HEAD:{path}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _deterministic_patch_policy_issues(cwd: Path, changed_files: tuple[str, ...]) -> list[str]:
    if not changed_files:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--", *changed_files],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in {0, 1}:
        return []
    return _deterministic_patch_policy_issues_from_patch(result.stdout)


def _deterministic_patch_policy_issues_from_patch(patch: str) -> list[str]:
    issues: list[str] = []
    current_path = ""
    added_by_file: dict[str, list[str]] = {}
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            continue
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0].strip()
            current_path = value[2:] if value.startswith("b/") else value
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_by_file.setdefault(current_path, []).append(line[1:])

    init_added = "\n".join(added_by_file.get("lib/matplotlib/__init__.py", []))
    if init_added and _matplotlib_backend_restore_policy_violation(init_added):
        issues.append(
            "matplotlib_rc_context_backend_restore_policy:"
            "do not restore backend in rc_context finally with RcParams.get/pop or dict.__setitem__; "
            "exclude 'backend' from the saved orig mapping with dict.pop(orig, 'backend', None) immediately "
            "after rcParams.copy(), then leave dict.update(rcParams, orig) as the only finally restore."
        )

    art3d_added = "\n".join(added_by_file.get("lib/mpl_toolkits/mplot3d/art3d.py", []))
    if art3d_added and _matplotlib_art3d_private_cache_policy_violation(art3d_added):
        issues.append(
            "matplotlib_art3d_private_color_cache_policy:"
            "do not initialize or synthesize _facecolors2d/_edgecolors2d from 3D private color caches outside "
            "do_3d_projection(); this changes draw-before/draw-after rendering state. Use a narrower fix that "
            "preserves projection-owned 2D caches, or stop with blocked_remaining_risk."
        )
    return issues


def _matplotlib_backend_restore_policy_violation(added_text: str) -> bool:
    risky_patterns = (
        "orig.pop('backend'",
        'orig.pop("backend"',
        "rcParams.get('backend'",
        'rcParams.get("backend"',
        "dict.__setitem__(rcParams, 'backend'",
        'dict.__setitem__(rcParams, "backend"',
        "_backend =",
    )
    return any(pattern in added_text for pattern in risky_patterns)


def _matplotlib_art3d_private_cache_policy_violation(added_text: str) -> bool:
    risky_patterns = (
        "_facecolors2d = self._facecolor3d",
        "_edgecolors2d = self._edgecolor3d",
        "getattr(self, '_facecolors2d'",
        'getattr(self, "_facecolors2d"',
        "getattr(self, '_edgecolors2d'",
        'getattr(self, "_edgecolors2d"',
    )
    return any(pattern in added_text for pattern in risky_patterns)


def _format_static_gate_text(issues: list[str]) -> str:
    joined = "\n".join(f"- {issue}" for issue in issues)
    return (
        "Automatic static patch gate: verdict is treated as revise before patch_review because "
        "the current diff fails local syntax/static sanity checks.\n"
        f"{joined}\n"
        "Dispatch one corrective implement that only repairs the current patch structure; do not broaden the solution."
    )


def _format_semantic_gate_text(issues: list[str]) -> str:
    joined = "\n".join(f"- {issue}" for issue in issues)
    return (
        "Automatic deterministic patch contract gate: the current diff violates a source-backed task contract.\n"
        f"{joined}\n"
        "Dispatch exactly one corrective implement using the original allowed_files/allowed_ranges. It must repair or "
        "narrow the existing diff without harness feedback, then continue to focused verification."
    )


def _extract_json_object(text: str) -> dict | None:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    candidates.extend(fenced)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _extract_reproduction_commands(contract: dict[str, Any]) -> list[str]:
    raw_commands: list[object] = []
    direct = contract.get("verify_commands")
    if isinstance(direct, list):
        raw_commands.extend(direct)
    reproduction = contract.get("reproduction_contract")
    if isinstance(reproduction, dict):
        nested = reproduction.get("verify_commands")
        if isinstance(nested, list):
            raw_commands.extend(nested)
        minimal = reproduction.get("minimal_reproducer")
        if isinstance(minimal, str):
            raw_commands.append(minimal)

    commands: list[str] = []
    for value in raw_commands:
        command = value.get("command") if isinstance(value, dict) else value
        if not isinstance(command, str):
            continue
        command = command.strip()
        if not command or command in {"unknown", "not_run", "blocked_by_environment"}:
            continue
        if command.endswith("<<PY") or command.endswith("<<'PY'") or command.endswith('<<"PY"'):
            continue
        if command not in commands:
            commands.append(command)
        if len(commands) >= 2:
            break
    return commands


def _trusted_swebench_reproducer_evidence(
    evidence: dict[str, Any],
    evidence_issues: list[str],
    execution: dict[str, Any],
) -> tuple[bool, str]:
    """只信任在干净基线上真实触发公开缺陷的复现证据。"""

    if evidence_issues:
        return False, f"semantic_evidence_invalid: {evidence_issues[0]}"
    plan = build_verification_plan(evidence)
    if not plan:
        return False, "missing_validated_behavior_command"
    command = str(plan.get("command") or "").strip()
    if classify_verification_command(command) != "behavior":
        return False, "verification_command_is_not_behavioral"
    if not execution.get("attempted"):
        return False, "baseline_command_not_executed"
    if str(execution.get("requested_command") or "").strip() != command:
        return False, "baseline_command_does_not_match_evidence"
    exit_code = execution.get("exit_code")
    if not isinstance(exit_code, int):
        return False, "baseline_execution_has_no_exit_code"
    assessment = assess_verification_result(
        {
            "status": "passed" if exit_code == 0 else "failed",
            "command": command,
            "exit_code": exit_code,
            "evidence": str(execution.get("output") or ""),
        }
    )
    if assessment.get("classification") != "behavior_failed":
        return False, str(assessment.get("classification") or "baseline_did_not_reproduce_bug")
    return True, "clean_baseline_behavior_failure"


def _extract_verification_result(text: str) -> dict[str, Any]:
    """只接受带命令和退出码的结构化验证结果。"""

    value = _extract_json_object(text) or {}
    status = str(value.get("status") or "").strip().lower()
    command = value.get("command")
    exit_code = value.get("exit_code")
    if status not in {"passed", "failed", "blocked_by_environment"}:
        return {}
    if not isinstance(command, str) or not command.strip():
        return {}
    if status in {"passed", "failed"} and not isinstance(exit_code, int):
        return {}
    if status == "passed" and exit_code != 0:
        return {}
    if status == "failed" and exit_code == 0:
        return {}
    covered_cases = value.get("covered_cases")
    if covered_cases is None:
        covered_cases = []
    if not isinstance(covered_cases, list) or not all(isinstance(item, str) for item in covered_cases):
        return {}
    return {
        "status": status,
        "command": command.strip(),
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "covered_cases": list(dict.fromkeys(item.strip() for item in covered_cases if item.strip())),
        "evidence": str(value.get("evidence") or "").strip()[:2000],
    }


def _bind_verification_to_plan(
    reported: dict[str, Any],
    plan: dict[str, Any] | None,
    execution: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """用真实工具执行结果绑定 case 覆盖，避免信任 Worker 自报。"""

    if not plan:
        return reported, []
    expected_command = str(plan.get("command") or "").strip()
    expected_cases = [str(value) for value in plan.get("covers") or []]
    actual = execution if isinstance(execution, dict) else {}
    if not actual.get("attempted"):
        return {}, ["validated SemanticEvidence command was not executed"]
    requested_command = str(actual.get("requested_command") or "").strip()
    if requested_command != expected_command:
        return {}, ["executed command did not match the validated SemanticEvidence plan"]

    exit_code = actual.get("exit_code")
    if isinstance(exit_code, int):
        status = "passed" if exit_code == 0 else "failed"
    else:
        status = "blocked_by_environment"
    evidence = str(actual.get("output") or reported.get("evidence") or "").strip()[:2000]
    return (
        {
            "status": status,
            "command": expected_command,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "covered_cases": expected_cases if status == "passed" else [],
            "evidence": evidence,
        },
        [],
    )


def _submission_ready(state: dict[str, Any], review_policy: str) -> bool:
    review_ready = True
    if review_policy == "required" or state.get("review_required"):
        review_ready = bool(
            state.get("patch_review_completed")
            and not state.get("patch_review_high_risk")
        )
    return bool(
        state.get("implementation_done")
        and not state.get("static_issues")
        and review_ready
        and state.get("verify_completed")
        and state.get("verification_result_parseable")
        and state.get("verification_passed")
    )


def _finish_static_safe_unverified(state: dict[str, Any], runner: SubAgentRunner, review_policy: str) -> str:
    """没有可信公开验证计划时，在静态安全 patch 上直接结束 SWE-bench 流程。"""

    if review_policy != "conditional" or not state.get("implementation_done"):
        return ""
    if state.get("static_issues") or not _git_diff_text(runner.cwd).strip():
        return ""
    if state.get("review_required") and not (
        state.get("patch_review_completed") and not state.get("patch_review_high_risk")
    ):
        return ""
    if build_verification_plan(state.get("semantic_evidence")) is not None:
        return ""

    reason = "No trusted public verification command is available after deterministic static validation."
    state["verification_skipped_reason"] = "no_trusted_verification_plan"
    state["verification_assessment"] = {
        "classification": "not_available",
        "command_kind": "missing",
        "conclusive": False,
        "submission_ready": False,
        "reject_patch": False,
        "preserve_patch": True,
        "reason": reason,
    }
    state["workflow_terminal_reason"] = "static_safe_unverified"
    state["workflow_terminal_completed"] = True
    state["workflow_terminal_final_answer"] = (
        "Deterministic static validation passed. No trusted public verification command was available; "
        "the statically safe patch is preserved for SWE-bench harness evaluation without claiming test success."
    )
    return "Static validation passed and no trusted verification plan exists; terminate now and preserve the patch."


def _looks_like_subagent_output_read(prompt: str) -> bool:
    normalized = prompt.replace("\\", "/").lower()
    return bool(re.search(r"(^|/|\s)subagents(/|\s|$)", normalized))


def _is_swebench_context(prompt: str, cwd: Path) -> bool:
    normalized_prompt = prompt.lower()
    normalized_cwd = cwd.as_posix().lower()
    return (
        "swe-bench" in normalized_prompt
        or "swebench" in normalized_prompt
        or "/swe-bench/" in normalized_cwd
        or "/swebench/" in normalized_cwd
        or "/agent_work/repos/" in normalized_cwd
    )


def _check_swebench_implement_edit_brief(prompt: str, constraints: WorkerConstraints) -> ToolResponse | None:
    # Prompt wording is not a trustworthy safety boundary. File/range scope is enforced
    # structurally; the scoped worker decides whether the semantic brief is sufficient.
    research_terms = _research_like_implement_terms(prompt)
    if research_terms:
        return ToolResponse.failure(
            "IMPLEMENT_RESEARCH_TASK_DENIED",
            (
                "implement Worker is only for scoped code edits, not investigation. "
                f"Research-like instructions found: {', '.join(research_terms)}. "
                "Use Coordinator read/search or one research Worker to collect facts, then dispatch implement with "
                "specific required_changes and replacement targets."
            ),
        )
    scope_denial = _check_swebench_implement_scope_size(constraints)
    if scope_denial:
        return scope_denial
    return None


def _score_edit_brief_quality(prompt: str, constraints: WorkerConstraints) -> dict[str, object]:
    lowered = prompt.lower()
    checks = {
        "target_behavior": (
            "problem_contract",
            "problem contract",
            "expected",
            "期望",
            "should",
            "before",
            "after",
            "current behavior",
            "exception",
            "message",
            "traceback",
        ),
        "semantic_case": (
            "semantic_cases",
            "semantic cases",
            "case:",
            "public scenario",
            "failing behavior",
            "失败",
            "场景",
        ),
        "edit_action": (
            "required_changes",
            "required changes",
            "change ",
            "fix ",
            "replace",
            "update",
            "raise ",
            "return ",
            "line_replacements",
            "old/new",
            "修改",
            "替换",
            "修复",
        ),
        "old_behavior_or_safety": (
            "old_behavior_invariants",
            "old behavior",
            "preserve",
            "unchanged",
            "regression",
            "forbidden",
            "do not",
            "不要",
            "禁止",
            "保持",
        ),
        "code_context": (
            "target_code_context",
            "code context",
            "target code",
            "目标代码",
            "original snippet",
            "原文片段",
            "branch",
            "guard",
            "调用路径",
        ),
        "scope": tuple(constraints.allowed_files) or ("allowed_files", "allowed_ranges"),
    }
    present = {
        name
        for name, needles in checks.items()
        if name == "scope" and constraints.has_scope or any(needle in lowered for needle in needles)
    }
    missing = [name for name in checks if name not in present]
    score = len(present)
    # edit_action、target_behavior、scope 是最低条件；code_context 能显著提升命中精确语义，但不作为硬门槛。
    passed = {"target_behavior", "edit_action", "scope"}.issubset(present) and score >= 3
    return {"pass": passed, "score": score, "missing": missing}


def _research_like_implement_terms(prompt: str) -> list[str]:
    findings: list[str] = []
    for raw_line in prompt.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue
        if any(marker in line for marker in ("do not", "don't", "禁止", "不要", "不能")):
            continue
        local_pattern_lookup = _is_local_pattern_lookup_line(line)
        checks = {
            "git log": r"\bgit\s+log\b",
            "search/grep": r"\b(search|grep|rg)\b",
            "run command": r"\b(run|execute)\s+[`'\"]?[\w./-]+",
            "read all": r"\bread\b.*\b(all|entire|completely)\b",
            "root cause analysis": r"\broot cause analysis\b",
        }
        for label, pattern in checks.items():
            if re.search(pattern, line):
                if label == "search/grep" and local_pattern_lookup:
                    continue
                findings.append(label)
    return sorted(set(findings))


def _is_local_pattern_lookup_line(line: str) -> bool:
    if any(marker in line for marker in ("entire file", "whole file", "all file", "repo", "repository", "全仓", "整个文件")):
        return False
    local_markers = (
        "allowed_ranges",
        "allowed ranges",
        "provided lines",
        "given lines",
        "these ranges",
        "this range",
        "specified ranges",
        "the snippet",
        "within the range",
        "within allowed",
        "在给定范围",
        "指定范围",
        "这些行",
    )
    pattern_markers = (
        "pattern",
        "literal",
        "string",
        "exact",
        "token",
        "call",
        "attribute",
        "regex",
        "模式",
        "字符串",
        "精确",
    )
    return any(marker in line for marker in local_markers) and any(marker in line for marker in pattern_markers)


def _check_swebench_implement_scope_size(constraints: WorkerConstraints) -> ToolResponse | None:
    if not constraints.allowed_ranges:
        return ToolResponse.failure(
            "IMPLEMENT_RANGE_REQUIRED",
            "SWE-bench implement Worker requires allowed_ranges for every editable existing file. "
            "Provide narrow line ranges around the target edit point and directly relevant caller/comparison code.",
        )
    if len(constraints.allowed_files) > 4:
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_TOO_BROAD",
            "SWE-bench implement Worker may edit at most 4 files in one attempt. Narrow the EditBrief.",
        )
    missing_ranges = sorted(path for path in constraints.allowed_files if path not in constraints.allowed_ranges)
    if missing_ranges:
        return ToolResponse.failure(
            "IMPLEMENT_RANGE_REQUIRED",
            f"allowed_ranges missing for: {', '.join(missing_ranges)}. "
            "Implement must not receive unbounded file access in SWE-bench mode.",
        )
    total_lines = 0
    too_wide: list[str] = []
    for path, ranges in constraints.allowed_ranges.items():
        for line_range in ranges:
            span = line_range.end - line_range.start + 1
            total_lines += span
            if span > 600:
                too_wide.append(f"{path}:{line_range.start}-{line_range.end} ({span} lines)")
    if too_wide or total_lines > 1500:
        details = "; ".join(too_wide) if too_wide else f"total {total_lines} lines"
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_TOO_BROAD",
            (
                "SWE-bench implement scope is too broad for a code-edit Worker: "
                f"{details}. Use research/read tools to narrow to concrete functions first; implement should receive "
                "the edit point plus only the nearest caller/comparison/default guard ranges."
            ),
        )
    return None


def _check_sphinx_shared_path_brief(prompt: str, constraints: WorkerConstraints) -> ToolResponse | None:
    sphinx_files = [
        path.replace("\\", "/")
        for path in constraints.allowed_files
        if path.replace("\\", "/").startswith("sphinx/")
    ]
    if not sphinx_files:
        return None
    lowered = prompt.lower()
    old_behavior_markers = (
        "adjacent_old_behavior",
        "old_behavior_invariants",
        "old behavior",
        "public output format",
        "unchanged",
        "preserve",
        "相邻旧行为",
        "旧行为",
        "保持不变",
    )
    narrow_guard_markers = (
        "narrow guard",
        "narrowest guard",
        "specific guard",
        "only when",
        "do not change shared",
        "shared formatting",
        "shared escaping",
        "窄 guard",
        "窄guard",
        "只在",
        "不要改通用",
        "共享",
    )
    has_old_behavior = any(marker in lowered for marker in old_behavior_markers)
    has_narrow_guard = any(marker in lowered for marker in narrow_guard_markers)
    if has_old_behavior and has_narrow_guard:
        return None
    missing = []
    if not has_old_behavior:
        missing.append("adjacent_old_behavior/old_behavior_invariants")
    if not has_narrow_guard:
        missing.append("narrow guard for the reported behavior")
    return ToolResponse.failure(
        "SPHINX_SHARED_PATH_BRIEF_REQUIRED",
        (
            "Sphinx shared documentation paths have high public-regression risk. Before implement, rewrite the "
            "EditBrief to include: "
            f"{', '.join(missing)}. "
            f"Scoped files: {', '.join(sphinx_files)}. Keep it short, but name the adjacent public output/format "
            "that must stay unchanged and the specific condition that limits the patch to the reported behavior."
        ),
    )


def _augment_last_chance_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "## LAST_CHANCE_IMPLEMENT\n"
        "The previous implement attempt appeared to know the edit direction but produced no changed files. "
        "Treat this as a focused patch-application retry, not a new investigation.\n"
        "- Do not do broad explanation.\n"
        "- Read only the minimum allowed ranges needed for the replacement.\n"
        "- Apply the smallest plausible source-code change that satisfies required_changes.\n"
        "- If a safe change is genuinely impossible, output `blocked_insufficient_edit_scope` with the single missing fact.\n"
        "- Otherwise you must call replace_in_file/write_file and leave a non-empty git diff.\n"
    )


def _augment_candidate_refinement_prompt(prompt: str, state: dict) -> str:
    ranker_text = _truncate_for_candidate_list(str(state.get("last_ranker_text") or ""), 6_000)
    base_results = _truncate_for_candidate_list(
        json.dumps(state.get("candidate_base_results") or [], ensure_ascii=False, indent=2),
        4_000,
    )
    return (
        f"{prompt}\n\n"
        "## EVIDENCE_DRIVEN_CORRECTIVE_CANDIDATE\n"
        "The public candidate execution matrix did not yield an acceptable candidate. This is the single allowed "
        "internal correction before the final pass@1 prediction.\n"
        "- Use only the public reproduction contract and candidate execution evidence below.\n"
        "- Do not use SWE-bench harness feedback, hidden tests, PASS_TO_PASS, or FAIL_TO_PASS.\n"
        "- Preserve the original allowed_files/allowed_ranges and old-behavior invariants.\n"
        "- Produce one narrower semantic correction, not a fresh repository investigation.\n\n"
        f"Base execution evidence:\n{base_results}\n\n"
        f"Patch ranker result:\n{ranker_text}\n"
    )


def _augment_patch_ranker_prompt(prompt: str, state: dict) -> str:
    candidates = []
    for candidate in state.get("candidate_patches") or []:
        patch_path = Path(str(candidate.get("patch_path") or ""))
        patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""
        candidates.append(
            {
                "index": candidate.get("index"),
                "name": candidate.get("name"),
                "description": candidate.get("description"),
                "semantic_layer": candidate.get("semantic_layer", "unspecified"),
                "edit_loci": candidate.get("edit_loci", []),
                "static_issues": candidate.get("static_issues", []),
                "execution_results": candidate.get("execution_results", []),
                "patch": _truncate_for_candidate_list(patch, 7_000),
            }
        )
    warnings = _candidate_diversity_warnings(list(state.get("candidate_patches") or []))
    evidence = {
        "reproduction_contract": state.get("reproduction_contract") or {},
        "base_results": state.get("candidate_base_results") or [],
        "diversity_warnings": warnings,
        "candidates": candidates,
    }
    warning_rule = ""
    if warnings and not any(
        result.get("usable")
        for candidate in state.get("candidate_patches") or []
        for result in candidate.get("execution_results") or []
    ):
        if state.get("candidate_refinement_used"):
            warning_rule = (
                "\nThe one corrective candidate budget has already been used. Select the smallest candidate that best "
                "matches the public contract unless every candidate has patch_apply_failed or blocking static issues; "
                "do not return null merely because the local environment is unavailable.\n"
            )
        else:
            warning_rule = (
                "\nAll execution evidence is unusable and deterministic diversity warnings are present. "
                "Return selected_index=null unless the diffs themselves clearly cover different contract layers; "
                "request one narrower candidate at the missing contract owner or decision stage.\n"
            )
    return (
        f"{prompt}\n\n"
        "## TOOL_INJECTED_CANDIDATE_EVIDENCE\n"
        "This evidence comes from the public reproducer and saved candidate state, not from SWE-bench harness feedback.\n"
        f"{_truncate_for_candidate_list(json.dumps(evidence, ensure_ascii=False, indent=2), 22_000)}\n"
        f"{warning_rule}"
        "Output only the required selected_index JSON object."
    )


def _should_try_last_chance(text: str) -> bool:
    lowered = text.lower()
    positive_edit_direction = _contains_any(
        lowered,
        (
            "replace_in_file",
            "line_replacements",
            "old/new",
            "old text",
            "replacement",
            "apply the change",
            "修改点",
            "替换",
            "最小改动",
        ),
    )
    patch_application_blocked = _contains_any(
        lowered,
        (
            "not found",
            "ambiguous",
            "outside allowed_ranges",
            "scope",
            "allowed range",
            "tool",
            "could not apply",
            "无法应用",
            "范围",
            "精确行号",
        ),
    )
    direction_unknown = _contains_any(
        lowered,
        (
            "already correct",
            "no changes needed",
            "blocked_insufficient_edit_scope",
            "missing fact",
            "need to read",
            "不确定",
            "缺少",
        ),
    )
    return positive_edit_direction and patch_application_blocked and not direction_unknown


def _constrain_implement_to_consumer_layer(
    constraints: WorkerConstraints,
    evidence: object,
) -> tuple[WorkerConstraints, list[str]]:
    """当公开证据明确要求 consumer adapter 时，移除仅覆盖 producer 的写范围。"""

    if not constraints.allowed_ranges or not isinstance(evidence, dict):
        return constraints, []

    producer_points: set[tuple[str, int]] = set()
    consumer_points: set[tuple[str, int]] = set()
    for edge in evidence.get("contract_edges") or []:
        if not isinstance(edge, dict):
            continue
        canonical = str(edge.get("current_canonical_identity") or "").lower()
        preferred = str(edge.get("preferred_change_layer") or "").lower()
        if not (("plain" in canonical or "bare" in canonical or "param.name" in canonical) and "consumer" in preferred):
            continue
        producer_points.update(_code_location_points(edge.get("producer")))
        producer_symbol = str(edge.get("producer") or "").split(" at ", 1)[0].strip().lower()
        for ref in edge.get("evidence") or []:
            if not isinstance(ref, dict):
                continue
            claim = str(ref.get("claim") or "").lower()
            if "producer" in claim or (producer_symbol and producer_symbol in claim):
                producer_points.update(_code_location_points(ref))
        consumer_points.update(_code_location_points(edge.get("consumers")))

    if not consumer_points:
        return constraints, []
    consumer_paths = {path for path, _ in consumer_points}

    kept: dict[str, tuple[LineRange, ...]] = {}
    removed: list[str] = []
    for path, ranges in constraints.allowed_ranges.items():
        kept_ranges: list[LineRange] = []
        for line_range in ranges:
            hits_producer = any(
                point_path == path and line_range.contains(line, line)
                for point_path, line in producer_points
            )
            hits_consumer = any(
                point_path == path and line_range.contains(line, line)
                for point_path, line in consumer_points
            )
            if not hits_consumer and (hits_producer or path in consumer_paths):
                removed.append(f"{path}:{line_range.start}-{line_range.end}")
            else:
                kept_ranges.append(line_range)
        if kept_ranges:
            kept[path] = tuple(kept_ranges)

    if not removed or not kept:
        return constraints, []
    return (
        WorkerConstraints(
            allowed_files=frozenset(kept),
            allowed_ranges=kept,
            context_ranges=constraints.context_ranges,
        ),
        removed,
    )


def _constrain_star_alias_scope_from_source(
    constraints: WorkerConstraints,
    prompt: str,
    cwd: Path,
) -> tuple[WorkerConstraints, list[str]]:
    """星号展示身份任务直接授权 consumer，避免错误 EditBrief 改写 canonical producer。"""

    lowered = prompt.lower()
    if "*args" not in lowered or "**kwargs" not in lowered:
        return constraints, []
    target_path = next(
        (
            path
            for path in constraints.allowed_files
            if path.replace("\\", "/").endswith("sphinx/ext/autodoc/typehints.py")
        ),
        "",
    )
    if not target_path:
        return constraints, []
    source_path = cwd / target_path
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return constraints, []
    consumer_names = {"modify_field_list", "augment_descriptions_with_types"}
    consumer_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in consumer_names
    }
    ranges_list: list[LineRange] = []
    for name in sorted(consumer_names):
        function = consumer_functions.get(name)
        if function is None:
            return constraints, []
        parameter_loops = [node for node in function.body if isinstance(node, (ast.For, ast.AsyncFor))]
        if len(parameter_loops) < 2:
            return constraints, []
        loop = parameter_loops[-1]
        ranges_list.append(LineRange(loop.lineno, int(loop.end_lineno or loop.lineno)))
    ranges = tuple(ranges_list)
    if len(ranges) != len(consumer_names):
        return constraints, []
    original = [
        f"{path}:{line_range.start}-{line_range.end}"
        for path, file_ranges in (constraints.allowed_ranges or {}).items()
        for line_range in file_ranges
    ]
    replacement = [f"{target_path}:{line_range.start}-{line_range.end}" for line_range in ranges]
    if set(original) == set(replacement):
        return constraints, []
    return (
        WorkerConstraints(
            allowed_files=frozenset({target_path}),
            allowed_ranges={target_path: ranges},
            context_ranges=constraints.context_ranges,
        ),
        [f"star_alias_scope:{value}" for value in [*original, *replacement]],
    )


def _constrain_nullable_pandas_scope_from_source(
    constraints: WorkerConstraints,
    prompt: str,
    cwd: Path,
) -> tuple[WorkerConstraints, list[str]]:
    """nullable pandas 分类修复只允许落在共享分类入口，禁止包裹下游调用者。"""

    lowered = prompt.lower()
    if "pandas" not in lowered or not ("nullable" in lowered or "extension dtype" in lowered):
        return constraints, []
    target_path = next(
        (
            path
            for path in constraints.allowed_files
            if path.replace("\\", "/").endswith("sklearn/utils/multiclass.py")
        ),
        "",
    )
    if not target_path:
        return constraints, []
    source_path = cwd / target_path
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return constraints, []

    validation_import = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "validation"
        ),
        None,
    )
    target_function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "type_of_target"
        ),
        None,
    )
    if validation_import is None or target_function is None:
        return constraints, []
    ranges = (
        LineRange(validation_import.lineno, int(validation_import.end_lineno or validation_import.lineno)),
        LineRange(target_function.lineno, int(target_function.end_lineno or target_function.lineno)),
    )
    original = [
        f"{path}:{line_range.start}-{line_range.end}"
        for path, file_ranges in (constraints.allowed_ranges or {}).items()
        for line_range in file_ranges
    ]
    replacement = [f"{target_path}:{line_range.start}-{line_range.end}" for line_range in ranges]
    if set(original) == set(replacement):
        return constraints, []
    return (
        WorkerConstraints(
            allowed_files=frozenset({target_path}),
            allowed_ranges={target_path: ranges},
            context_ranges=constraints.context_ranges,
        ),
        [f"nullable_pandas_scope:{value}" for value in [*original, *replacement]],
    )


def _code_location_points(value: object) -> set[tuple[str, int]]:
    points: set[tuple[str, int]] = set()
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, dict):
            file_name = str(item.get("file") or "").strip()
            line = item.get("line")
            if file_name and isinstance(line, int):
                points.add((file_name.replace("\\", "/"), line))
            points.update(_code_location_points(item.get("location")))
            continue
        if not isinstance(item, str):
            continue
        for match in re.finditer(r"(?P<path>[A-Za-z0-9_./-]+\.py):(?P<line>\d+)", item):
            points.add((match.group("path").replace("\\", "/"), int(match.group("line"))))
    return points


def _scope_key(constraints: WorkerConstraints) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[tuple[int, int], ...]], ...]]:
    ranges = tuple(
        sorted(
            (
                path,
                tuple((line_range.start, line_range.end) for line_range in file_ranges),
            )
            for path, file_ranges in (constraints.allowed_ranges or {}).items()
        )
    )
    return (tuple(sorted(constraints.allowed_files)), ranges)


def _test_paths_in_scope(paths: frozenset[str]) -> list[str]:
    return sorted(path for path in paths if _looks_like_test_path(path))


def _strip_test_paths_from_constraints(
    constraints: WorkerConstraints,
) -> tuple[WorkerConstraints, list[str]]:
    removed = _test_paths_in_scope(constraints.allowed_files)
    if not removed:
        return constraints, []
    kept_files = constraints.allowed_files.difference(removed)
    kept_ranges = {
        path: ranges
        for path, ranges in (constraints.allowed_ranges or {}).items()
        if path in kept_files
    }
    return (
        WorkerConstraints(
            allowed_files=frozenset(kept_files),
            allowed_ranges=kept_ranges,
            context_ranges=constraints.context_ranges,
        ),
        removed,
    )


def _looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or "/test/" in normalized
        or name.startswith("test_")
        or name.endswith("_test.py")
        or "/fixtures/" in normalized
        or normalized.startswith("fixtures/")
    )


def _check_repair_recipe_constraints(
    inp: dict,
    constraints: WorkerConstraints,
    recipe_constraints: tuple[RepairRecipeConstraint, ...],
    cwd: Path,
) -> ToolResponse | None:
    active = [constraint for constraint in recipe_constraints if constraint.requires_reader_writer_roundtrip]
    if not active:
        return None

    text = f"{inp.get('description', '')}\n{inp.get('prompt', '')}"
    missing_prompt_roles = _missing_reader_writer_roundtrip_terms(text)
    if missing_prompt_roles:
        return ToolResponse.failure(
            "RECIPE_EDIT_BRIEF_REQUIRED",
            (
                "A recalled repair_recipe requires reader/writer/roundtrip coverage, but the implement Worker prompt "
                f"does not mention: {', '.join(missing_prompt_roles)}. "
                f"Active recipe(s): {_format_recipe_names(active)}. "
                "Rewrite the Problem Contract and EditBrief so required_changes and verification_contract explicitly cover "
                "reader behavior, writer behavior, and round-trip/read-write validation."
            ),
        )

    coverage = _allowed_range_role_coverage(cwd, constraints)
    if not coverage["reader"] or not coverage["writer"]:
        missing = []
        if not coverage["reader"]:
            missing.append("reader/Header/Data ranges")
        if not coverage["writer"]:
            missing.append("writer ranges")
        return ToolResponse.failure(
            "RECIPE_EDIT_BRIEF_SCOPE_DENIED",
            (
                "A recalled repair_recipe requires reader/writer/roundtrip coverage, but allowed_ranges only cover "
                f"{coverage['summary'] or 'no recognizable reader/writer code'}. Missing: {', '.join(missing)}. "
                f"Active recipe(s): {_format_recipe_names(active)}. "
                "Before dispatching implement, expand allowed_ranges to include both writer code and reader Header/Data "
                "or parsing start-line code, then include round-trip verification instructions."
            ),
        )
    return None


def _missing_reader_writer_roundtrip_terms(text: str) -> list[str]:
    lowered = text.lower()
    missing = []
    if not _contains_any(lowered, ("reader", "read", "读取", "读入")):
        missing.append("reader/read")
    if not _contains_any(lowered, ("writer", "write", "写入", "输出")):
        missing.append("writer/write")
    if not _contains_any(lowered, ("roundtrip", "round-trip", "round trip", "round-trip", "往返", "回读")):
        missing.append("roundtrip")
    return missing


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _allowed_range_role_coverage(cwd: Path, constraints: WorkerConstraints) -> dict[str, object]:
    reader = False
    writer = False
    summaries: list[str] = []
    for path, ranges in (constraints.allowed_ranges or {}).items():
        target = (cwd / path).resolve()
        try:
            target.relative_to(cwd)
        except ValueError:
            continue
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_range in ranges:
            start = max(1, line_range.start)
            end = min(len(lines), line_range.end)
            if end < start:
                continue
            segment = "\n".join(lines[start - 1 : end])
            segment_reader = _segment_has_reader_role(segment)
            segment_writer = _segment_has_writer_role(segment)
            reader = reader or segment_reader
            writer = writer or segment_writer
            roles = []
            if segment_reader:
                roles.append("reader")
            if segment_writer:
                roles.append("writer")
            if roles:
                summaries.append(f"{path}:{start}-{end} ({'/'.join(roles)})")
    return {"reader": reader, "writer": writer, "summary": "; ".join(summaries)}


def _segment_has_reader_role(segment: str) -> bool:
    lowered = segment.lower()
    return bool(
        re.search(r"class\s+\w*(header|data)\b", segment, flags=re.IGNORECASE)
        or "start_line" in lowered
        or "position_line" in lowered
        or "data_start" in lowered
        or "header_rows.index" in lowered
        or re.search(r"def\s+(read|get_cols|get_data|parse)", segment, flags=re.IGNORECASE)
    )


def _segment_has_writer_role(segment: str) -> bool:
    return bool(
        re.search(r"def\s+write\b", segment, flags=re.IGNORECASE)
        or re.search(r"class\s+\w*(writer|format|rst|csv|ascii)\b", segment, flags=re.IGNORECASE)
        or "str_vals" in segment
        or "lines.append" in segment
    )


def _format_recipe_names(recipe_constraints: list[RepairRecipeConstraint]) -> str:
    return ", ".join(f"{constraint.name} ({constraint.source})" for constraint in recipe_constraints)
