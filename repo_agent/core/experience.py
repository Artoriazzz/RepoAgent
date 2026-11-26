from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from repo_agent.core.failure_feedback_format import flatten_failure_feedback, format_failure_handoff
from repo_agent.core.memory import (
    MAX_SEMANTIC_INSIGHTS_PER_ROLE,
    MemoryStore,
    MemoryType,
    RecalledMemory,
)
from repo_agent.core.semantic_evidence import extract_json_object
from repo_agent.core.subagent import WorkerConstraints, _validate_failure_feedback_fields
from repo_agent.core.verification import assess_verification_result, verification_command_safety_reason


MAX_EPISODIC_PROMPT_CHARS = 3_000
MAX_SEMANTIC_PROMPT_CHARS = 2_500
MAX_EXPERIENCE_FIELD_CHARS = 6_000
MAX_COORDINATOR_MEMORY_CHARS = 5_000


class RepairRunMode(str, Enum):
    STANDARD = "standard"
    EXPERIENCE_BUILD = "experience-build"
    BENCHMARK = "benchmark"


@dataclass(frozen=True)
class RepairModePolicy:
    """把模式差异编码成能力开关，防止 benchmark 意外使用反馈或写入记忆。"""

    mode: RepairRunMode
    semantic_recall: bool
    episodic_recall: bool
    memory_write: bool
    feedback_correction: bool
    coordinator_replan: bool
    max_corrections: int


@dataclass(frozen=True)
class LightweightValidation:
    """同一公开行为命令在基线和候选 patch 上的差分结果。"""

    status: str
    command: str
    command_kind: str
    baseline: dict[str, Any]
    patched: dict[str, Any]
    feedback_eligible: bool
    reason: str

    @property
    def passed(self) -> bool:
        return self.status == "fixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "command": self.command,
            "command_kind": self.command_kind,
            "baseline": dict(self.baseline),
            "patched": dict(self.patched),
            "feedback_eligible": self.feedback_eligible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FailureEvidence:
    identifier: str
    source: str
    fact: str
    support: str
    round: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "source": self.source,
            "fact": self.fact,
            "support": self.support,
            **({"round": self.round} if self.round is not None else {}),
        }


@dataclass(frozen=True)
class FailureFeedback:
    """FailureReview 的结构化结论，也是纠正流程的唯一控制面输入。

    自然语言根因本身不能触发改代码；只有 evidence ledger 中可追溯的证据
    与 correction_evidence_ids 对齐后，路由才会授权 corrective implement。
    """

    failure_stage: str
    observed_failure: tuple[str, ...]
    failure_observation_quality: str
    root_cause: str
    evidence_ledger: tuple[FailureEvidence, ...]
    correction_evidence_ids: tuple[str, ...]
    scope_expansion: tuple[str, ...]
    regression_guards: tuple[str, ...]
    correction_readiness: str
    replan_reason: str = ""
    required_edits: tuple[dict[str, Any], ...] = ()
    stop_reason: str = ""
    test_revision_request: str = ""

    @property
    def requires_coordinator_replan(self) -> bool:
        return self.correction_route == "coordinator_replan"

    @property
    def correction_route(self) -> str:
        if (self.failure_stage == "invalid_test" and self.test_revision_request
                and not self.required_edits and not self.scope_expansion
                and self.failure_observation_quality not in {"failure_only", "environment_only"}
                and self._has_authorizing_correction_evidence(require_task_context=False)):
            return "test_regeneration"
        if self.failure_stage in {"complete", "invalid_test"}:
            return "none"
        if (self.correction_readiness == "needs_replan" and self.replan_reason
                and not self.scope_expansion
                and self.failure_stage != "validation_environment"
                and self._has_authorizing_correction_evidence(require_task_context=False)):
            return "coordinator_replan"
        if (
            self.correction_readiness == "actionable"
            and self.failure_stage in {"implementation_semantics", "regression"}
            and self._has_authorizing_correction_evidence()
        ):
            return "corrective_implement"
        return "none"

    def _has_authorizing_correction_evidence(self, *, require_task_context: bool = True) -> bool:
        ledger = {item.identifier: item for item in self.evidence_ledger}
        if not self.correction_evidence_ids:
            return False
        selected = [ledger.get(identifier) for identifier in self.correction_evidence_ids]
        # 已读源码也能与直接失败事实形成修正依据，不强制重复引用任务或补丁。
        if any(item is None or item.support == "unknown" for item in selected):
            return False
        direct_sources = {
            item.source
            for item in selected
            if item is not None and item.support == "direct"
        }
        return bool(
            direct_sources & {"failure_log", "validation_summary"}
            and (not require_task_context or direct_sources & {"public_task", "failed_patch", "semantic_evidence", "repository_code"})
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_stage": self.failure_stage,
            "observed_failure": list(self.observed_failure),
            "failure_observation_quality": self.failure_observation_quality,
            "root_cause": self.root_cause,
            "required_edits": list(self.required_edits),
            "evidence_ledger": [item.to_dict() for item in self.evidence_ledger],
            "correction_evidence_ids": list(self.correction_evidence_ids),
            "scope_expansion": list(self.scope_expansion),
            "regression_guards": list(self.regression_guards),
            "correction_readiness": self.correction_readiness,
            "correction_route": self.correction_route,
            "replan_reason": self.replan_reason,
            "stop_reason": self.stop_reason,
            "test_revision_request": self.test_revision_request,
        }


def repair_mode_policy(mode: str | RepairRunMode) -> RepairModePolicy:
    """返回模式能力矩阵。

    experience-build 允许一次失败反馈和记忆写入，但关闭召回以避免循环自证；
    benchmark 只读召回、禁止反馈重试，从而保持一次提交的 pass@1 语义。
    """
    parsed = mode if isinstance(mode, RepairRunMode) else RepairRunMode(mode)
    if parsed == RepairRunMode.EXPERIENCE_BUILD:
        return RepairModePolicy(
            mode=parsed,
            semantic_recall=False,
            episodic_recall=False,
            memory_write=True,
            feedback_correction=True,
            coordinator_replan=True,
            max_corrections=1,
        )
    if parsed == RepairRunMode.BENCHMARK:
        return RepairModePolicy(
            mode=parsed,
            semantic_recall=True,
            episodic_recall=True,
            memory_write=False,
            feedback_correction=False,
            coordinator_replan=False,
            max_corrections=0,
        )
    return RepairModePolicy(
        mode=parsed,
        semantic_recall=True,
        episodic_recall=False,
        memory_write=False,
        feedback_correction=False,
        coordinator_replan=False,
        max_corrections=0,
    )


def independent_validation_succeeded(validation: dict[str, Any] | str | None) -> bool:
    if not isinstance(validation, dict):
        return False
    if validation.get("error"):
        return False
    if validation.get("resolved") is True:
        return True
    status = str(validation.get("status") or "").lower()
    return validation.get("exit_code") == 0 and status in {"success", "passed", "ok"}


def assess_lightweight_validation(
    baseline: dict[str, Any] | None,
    patched: dict[str, Any] | None,
) -> LightweightValidation:
    """只让真实复现过公开 bug 的行为命令进入失败反馈循环。"""

    base = _normalize_execution(baseline)
    candidate = _normalize_execution(patched)
    command = str(candidate.get("command") or base.get("command") or "").strip()
    command_kind = str(candidate.get("command_kind") or base.get("command_kind") or "missing")

    if not command or base.get("command") != candidate.get("command"):
        return LightweightValidation(
            "invalid_reproducer",
            command,
            command_kind,
            base,
            candidate,
            False,
            "baseline and patched executions must use the same command",
        )
    unsafe = verification_command_safety_reason(command)
    if unsafe:
        return LightweightValidation(
            "invalid_reproducer",
            command,
            command_kind,
            base,
            candidate,
            False,
            unsafe,
        )

    baseline_assessment = assess_verification_result(base)
    patched_assessment = assess_verification_result(candidate)
    if baseline_assessment.get("classification") != "behavior_failed":
        return LightweightValidation(
            "invalid_reproducer",
            command,
            command_kind,
            base,
            candidate,
            False,
            "the public behavior command did not reproduce the bug on the clean baseline",
        )
    patched_class = str(patched_assessment.get("classification") or "")
    if patched_class == "behavior_passed":
        return LightweightValidation(
            "fixed",
            command,
            command_kind,
            base,
            candidate,
            False,
            "the candidate changed the same public behavior command from failing to passing",
        )
    if patched_class == "behavior_failed":
        return LightweightValidation(
            "still_failing",
            command,
            command_kind,
            base,
            candidate,
            True,
            "the candidate still fails the independently reproduced public behavior",
        )
    return LightweightValidation(
        "blocked",
        command,
        command_kind,
        base,
        candidate,
        False,
        str(patched_assessment.get("reason") or "patched execution was inconclusive"),
    )


def _normalize_execution(value: dict[str, Any] | None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    command = str(raw.get("command") or raw.get("requested_command") or "").strip()
    exit_code = raw.get("exit_code")
    status = str(raw.get("status") or "").strip().lower()
    if status not in {"passed", "failed", "blocked_by_environment"}:
        if isinstance(exit_code, int):
            status = "passed" if exit_code == 0 else "failed"
        else:
            status = "blocked_by_environment"
    return {
        "status": status,
        "command": command,
        "command_kind": str(raw.get("command_kind") or ""),
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "evidence": str(raw.get("evidence") or raw.get("output") or "")[-4_000:],
        "attempted": bool(raw.get("attempted", bool(command))),
    }


def augment_task_with_semantic_insights(
    *,
    task: str,
    memory_store: MemoryStore,
    role: str = "coordinator",
    limit: int = 1,
) -> tuple[str, tuple[RecalledMemory, ...]]:
    """召回角色级抽象规则，并作为必须由当前源码复核的软指导注入。"""
    memories = tuple(memory_store.recall_semantic_insights(task, role=role, limit=limit))
    if not memories:
        return task, ()
    body = _clip(
        "\n\n".join(
            f"### Insight {index}: {memory.header.name}\n"
            f"- role: {memory.header.role or role}\n"
            f"- provenance: {memory.header.provenance or 'unspecified'}\n"
            f"{memory.content}"
            for index, memory in enumerate(memories, 1)
        ),
        MAX_SEMANTIC_PROMPT_CHARS,
    )
    section = f"""## Recalled Semantic Repair Insights
以下是从已验证历史修复中提炼的角色级经验。它们是短小的软指导，不是当前任务事实，也不能扩大修改范围。
采用任何 insight 前，必须用当前 problem statement 和源码验证；冲突时以当前代码为准。

{body}"""
    return f"{section}\n\n{task}", memories


def augment_coordinator_with_experiences(
    *,
    task: str,
    memory_store: MemoryStore,
    limit_per_type: int = 1,
    retrieval_query: str | None = None,
) -> tuple[str, tuple[RecalledMemory, ...]]:
    """只给 Coordinator 注入全局经验和当前仓库经验。"""

    query = task if retrieval_query is None else retrieval_query
    global_memories = tuple(
        memory_store.recall_experiences(
            query,
            memory_type=MemoryType.GLOBAL_EXPERIENCE,
            limit=limit_per_type,
        )
    )
    repository_memories = tuple(
        memory_store.recall_experiences(
            query,
            memory_type=MemoryType.REPOSITORY_EXPERIENCE,
            limit=limit_per_type,
        )
    )
    memories = global_memories + repository_memories
    if not memories:
        return task, ()

    def render(title: str, items: tuple[RecalledMemory, ...], allowed: str) -> str:
        if not items:
            return f"### {title}\n（未召回）"
        body = "\n\n".join(
            f"#### {index}. {memory.header.name}\n"
            f"- provenance: {memory.header.provenance or 'unspecified'}\n"
            f"{memory.content}"
            for index, memory in enumerate(items, 1)
        )
        return f"### {title}\n仅用于：{allowed}\n\n{body}"

    section = _clip(
        "## Coordinator Recalled Experience\n"
        "以下内容只供 Coordinator 形成 Problem Contract 和 EditBrief。它们是历史假设，必须由当前任务与源码复核；"
        "不得原样转发给代码修改、失败归因或二次纠正阶段。\n\n"
        + render(
            "全局经验",
            global_memories,
            "问题机制、适用条件、通用修复步骤、验证思路和错误做法",
        )
        + "\n\n"
        + render(
            "仓库经验",
            repository_memories,
            "相关模块/API、调用关系、可复用工具函数、仓库特有契约和公开验证方法",
        ),
        MAX_COORDINATOR_MEMORY_CHARS,
    )
    return f"{section}\n\n{task}", memories


def augment_implement_with_episodes(
    *,
    prompt: str,
    memory_store: MemoryStore,
    feedback: str = "",
    limit: int = 1,
) -> tuple[str, tuple[RecalledMemory, ...]]:
    """给 Implement 注入紧凑历史示例，而非可复制的历史 patch。

    episode 的价值是暴露“问题签名 -> 错误假设 -> 反馈 -> 正确修法”的关系；
    文件名、标识符和具体 diff 都不得迁移到当前任务。
    """
    query = f"{prompt}\n{feedback}".strip()
    memories = tuple(memory_store.recall_repair_episodes(query, limit=limit))
    if not memories:
        return prompt, ()
    body = _clip(
        "\n\n".join(
            f"### Episode {index}: {memory.header.name}\n"
            f"- provenance: {memory.header.provenance or 'unspecified'}\n"
            f"{memory.content}"
            for index, memory in enumerate(memories, 1)
        ),
        MAX_EPISODIC_PROMPT_CHARS,
    )
    section = f"""## Retrieved Repair Episodes
这些紧凑示例只用于展示历史上的问题签名、失败假设、反馈和结构化修正方式。
不得复制其中的文件、标识符或 patch；先完成 applicability check，只迁移经当前源码证明成立的关系。
在执行 Coordinator 的精确改法前，必须逐项检查：
1. 当前问题签名是否与 episode 的 Problem Signature 一致；
2. allowed_ranges 中的当前 predicate/contract 是否与 Input Evidence 一致；
3. Coordinator 的 required change 是否与 Failed Attempt 同构。
若三项均成立，该 Failed Attempt 是已验证反模式，不能因“记忆只是软参考”而重复执行。应把 Successful Output 中可由当前源码独立证明的结构/语义、True/False/None 与边界关系转成当前 patch 的语义义务；如果无法证明，则阻塞并指出唯一冲突证据。

{body}"""
    return f"{prompt}\n\n{section}", memories


def compact_failure_validation(
    validation: dict[str, Any] | str | None,
) -> dict[str, Any] | str:
    """优先保留差分矩阵和关系违规，避免长执行日志挤掉关键证据。"""

    if not isinstance(validation, dict):
        return str(validation or "")
    if validation.get("validation_kind") == "public_test_suite":
        return {
            "validation_kind": "public_test_suite",
            "status": validation.get("status"),
            "target_reproduced": validation.get("target_reproduced"),
            "unknowns": validation.get("unknowns", []),
            "cases": [{
                **{key: case.get(key) for key in ("id", "kind", "command", "expected_behavior", "basis", "usable")},
                "baseline": {"exit_code": case.get("baseline", {}).get("exit_code"),
                             "classification": case.get("baseline_classification")},
                "patched": {"exit_code": case.get("patched", {}).get("exit_code"),
                            "classification": case.get("patched_classification")},
            } for case in validation.get("cases", [])],
        }
    if validation.get("validation_kind") == "swebench_harness_feedback":
        return {
            "validation_kind": "swebench_harness_feedback",
            "status": validation.get("status"),
            "reason": validation.get("reason"),
            "test_counts": validation.get("test_counts") or {},
            "failure_facts": validation.get("failure_facts") or {},
            "failure_attribution": validation.get("failure_attribution") or {},
            "failure_excerpt": str(validation.get("failure_excerpt") or "")[-6_000:],
        }
    if validation.get("validation_kind") != "differential_trace":
        return validation
    plan = validation.get("test_plan") or {}
    matrix = [
        {
            "case": item.get("case"),
            "input": item.get("input"),
            "why": item.get("why"),
            "evidence": item.get("evidence"),
            "expected_relation": item.get("expected_relation"),
            "baseline": item.get("baseline"),
            "patched": item.get("patched"),
            "changed": item.get("changed"),
        }
        for item in validation.get("case_matrix") or []
        if isinstance(item, dict)
    ]
    violations: list[str] = []
    for item in matrix:
        relation = str(item.get("expected_relation") or "observe")
        if relation == "must_change" and not item.get("changed"):
            violations.append(
                f"{item.get('case')}: must_change but baseline and candidate are unchanged"
            )
        elif relation == "must_preserve" and item.get("changed"):
            violations.append(
                f"{item.get('case')}: must_preserve but candidate changed"
            )
    return {
        "validation_kind": "differential_trace",
        "status": validation.get("status"),
        "guard_coverage_complete": plan.get("guard_coverage_complete"),
        "guard_obligations": plan.get("guard_obligations") or [],
        "guard_coverage": plan.get("guard_coverage") or [],
        "case_matrix": matrix,
        "relation_violations": violations,
        "limitations": plan.get("limitations") or [],
    }


def build_previous_failure_constraints(validation: dict, feedback: dict, *, round_index: int) -> dict:
    evidence = [
        {**item, "round": round_index}
        for item in feedback.get("evidence_ledger") or []
    ]
    failure = str(validation.get("failure_evidence") or "").strip()
    if failure:
        evidence.insert(0, {"source": "failure_log", "support": "direct", "round": round_index,
                            "fact": _sanitize_harness_labels(failure[:4_000])})
    return {"round": round_index, "evidence": evidence,
            "regression_guards": list(feedback.get("regression_guards") or [])}


def build_failure_review_prompt(
    *,
    task: str,
    failed_patch: str,
    failure_log: str,
    validation: dict[str, Any] | str | None = None,
    implementation_report: dict[str, Any] | None = None,
) -> str:
    compact_validation = compact_failure_validation(validation)
    validation_text = (
        json.dumps(compact_validation, ensure_ascii=False, indent=2)
        if isinstance(compact_validation, dict)
        else str(compact_validation or "")
    )
    failure_log = _sanitize_harness_labels(failure_log)
    validation_text = _sanitize_harness_labels(validation_text)
    report = implementation_report if isinstance(implementation_report, dict) else {}
    compact_report = {
        "changed_decision": str(report.get("changed_decision") or ""),
        "preserve_cases": list(report.get("preserve_cases") or [])[:6],
        "known_risks": list(report.get("known_risks") or [])[:6],
    }
    report_text = json.dumps(compact_report, ensure_ascii=False, indent=2)
    return f"""## Public Task
{_clip(task, MAX_EXPERIENCE_FIELD_CHARS)}

## Failed Patch
```diff
{_clip(failed_patch, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Initial Implementation Report
{_clip(report_text, 4_000)}

## Explicit Failure Evidence
```text
{_clip(failure_log, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Validation Summary
{"本次公开测试由独立测试 Agent 设计；可请求一次重新生成。" if isinstance(validation, dict) and validation.get("generated_test_revision_available") else "本次不提供测试重生成，测试无效时选择 stop。"}
{_clip(validation_text, 10_000)}

## Previous Failure Constraints
以下区块仅组织历史输入，不是新的证据来源。引用时沿用每项原始 source/support，round 标记历史轮次；不得把 previous_failure_constraints 当成 source。
{_clip(json.dumps((validation or {}).get('previous_failure_constraints', {}) if isinstance(validation, dict) else {}, ensure_ascii=False, indent=2), 6_000)}

## 输出 Schema
只返回下列四个对象；枚举用 | 分隔，输出时只填其中一个值。
{{
  "failure": {{
    "kind": "complete|invalid_test|localization|problem_contract|implementation_semantics|regression|validation_environment|insufficient_evidence",
    "observations": ["写日志的实际值、预期值或异常；只有失败测试名时不要猜输入"],
    "quality": "expected_actual|exception_detail|behavior_delta|failure_only|environment_only",
    "cause": "写哪个函数的哪个操作造成上述结果；未确认时写缺少的事实"
  }},
  "repair": {{
    "edits": [{{"path": "仓库相对路径", "start_line": 1, "end_line": 20, "change": "写当前代码做什么、应改成什么；当前行号包含两端"}}],
    "preserve": ["写相邻的合法输入及应保持的输出或副作用"]
  }},
  "evidence": {{
    "items": [{{"id": "E1", "source": "public_task|failed_patch|implementation_report|failure_log|validation_summary|semantic_evidence|repository_code", "support": "direct|inference|unknown", "fact": "写可核对的日志内容或源码位置；历史项可另加 round"}}],
    "used_ids": ["E1"]
  }},
  "decision": {{
    "action": "correct|replan|regenerate_tests|stop",
    "request": "replan：写待查位置和缺失事实；regenerate_tests：写测试错误、日志依据及改法；stop：写原因；correct：填空"
  }}
}}
"""


def parse_failure_feedback(text: str) -> FailureFeedback | None:
    payload = extract_json_object(text)
    if not isinstance(payload, dict):
        return None
    is_structured = "failure" in payload
    payload = flatten_failure_feedback(payload)
    if payload is None:
        return None
    if is_structured and _validate_failure_feedback_fields(payload):
        return None
    stage = str(payload.get("failure_stage") or "").strip()
    allowed_stages = {
        "complete",
        "invalid_test",
        "localization",
        "problem_contract",
        "implementation_semantics",
        "regression",
        "validation_environment",
        "insufficient_evidence",
    }
    if stage not in allowed_stages:
        return None
    legacy_evidence = payload.get("evidence") or []
    raw_ledger = payload.get("evidence_ledger")
    if raw_ledger is None:
        raw_ledger = [
            {
                "id": f"legacy-{index}",
                "source": "validation_summary",
                "fact": str(item),
                "support": "unknown",
            }
            for index, item in enumerate(legacy_evidence, 1)
        ]
    evidence_ledger = _parse_failure_evidence(raw_ledger)
    if raw_ledger and not evidence_ledger:
        return None
    scope_expansion = _string_tuple(payload.get("scope_expansion"))
    correction_evidence_ids = _string_tuple(payload.get("correction_evidence_ids"))
    readiness = str(payload.get("correction_readiness") or "").strip()
    observation_quality = str(
        payload.get("failure_observation_quality") or "failure_only"
    ).strip()
    if observation_quality not in {
        "expected_actual",
        "exception_detail",
        "behavior_delta",
        "failure_only",
        "environment_only",
    }:
        return None
    # Historical artifacts can carry a source that explicitly disallows source replanning.
    legacy_source = payload.get("next_evidence_source", "repository_code")
    if not readiness:
        readiness = "insufficient_evidence"
    if readiness not in {
        "not_needed",
        "actionable",
        "needs_replan",
        "insufficient_evidence",
    }:
        return None
    if stage in {"complete", "invalid_test"}:
        readiness = "not_needed"
    elif stage == "validation_environment":
        readiness = "insufficient_evidence"
    elif stage == "insufficient_evidence" and readiness != "needs_replan":
        readiness = "insufficient_evidence"
    elif scope_expansion or (stage in {"localization", "problem_contract"} and readiness != "needs_replan"):
        readiness = "insufficient_evidence"
    ledger_by_id = {item.identifier: item for item in evidence_ledger}
    selected_evidence = [ledger_by_id.get(identifier) for identifier in correction_evidence_ids]
    direct_sources = {
        item.source
        for item in selected_evidence
        if item is not None and item.support == "direct"
    }
    has_authorizing_correction_evidence = bool(
        correction_evidence_ids
        and all(item is not None and item.support != "unknown" for item in selected_evidence)
        and direct_sources & {"failure_log", "validation_summary"}
        and (readiness == "needs_replan" or direct_sources & {"public_task", "failed_patch", "semantic_evidence", "repository_code"})
    )
    if readiness in {"actionable", "needs_replan"} and not has_authorizing_correction_evidence:
        readiness = "insufficient_evidence"
    replan_reason = str(payload.get("replan_reason") or "").strip()
    if readiness == "needs_replan" and (not replan_reason or legacy_source != "repository_code"):
        readiness = "insufficient_evidence"
    if readiness == "actionable" and (
        observation_quality == "environment_only"
        or (observation_quality == "failure_only" and "repository_code" not in direct_sources)
        or (not payload.get("required_edits") and not payload.get("required_correction"))
    ):
        readiness = "insufficient_evidence"
    return FailureFeedback(
        failure_stage=stage,
        observed_failure=_string_tuple(payload.get("observed_failure")),
        failure_observation_quality=observation_quality,
        root_cause=str(payload.get("root_cause") or "").strip(),
        evidence_ledger=evidence_ledger,
        correction_evidence_ids=correction_evidence_ids,
        scope_expansion=scope_expansion,
        regression_guards=_string_tuple(payload.get("regression_guards")),
        correction_readiness=readiness,
        replan_reason=replan_reason,
        required_edits=tuple(payload.get("required_edits") or []),
        stop_reason=str(payload.get("stop_reason") or ""),
        test_revision_request=str(payload.get("test_revision_request") or "").strip(),
    )


def required_edit_scope_issues(required_edits: list | tuple, constraints: dict) -> list[str]:
    """核对必要修改是否全部落在本轮可编辑行范围，而非仅核对文件名。"""
    issues = []
    ranges = constraints.get("allowed_ranges") or {}
    for edit in required_edits:
        if (not isinstance(edit, dict) or not edit.get("path")
                or not isinstance(edit.get("start_line"), int)
                or not isinstance(edit.get("end_line"), int)
                or not 1 <= edit["start_line"] <= edit["end_line"]):
            issues.append("required_edits needs path and valid start_line/end_line")
            continue
        path, start, end = edit["path"], edit["start_line"], edit["end_line"]
        if path not in constraints.get("allowed_files", []) or not any(
            left <= start <= end <= right for left, right in ranges.get(path, [])
        ):
            issues.append(f"{path}:{start}-{end}: {edit.get('change', '')}")
    return issues


def _parse_failure_evidence(raw_items: Any) -> tuple[FailureEvidence, ...]:
    if not isinstance(raw_items, list):
        return ()
    allowed_sources = {
        "public_task",
        "failed_patch",
        "implementation_report",
        "failure_log",
        "validation_summary",
        "semantic_evidence",
        "repository_code",
    }
    allowed_support = {"direct", "inference", "unknown"}
    parsed: list[FailureEvidence] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            return ()
        identifier = str(raw.get("id") or "").strip()
        source = str(raw.get("source") or "").strip()
        fact = str(raw.get("fact") or "").strip()
        support = str(raw.get("support") or "").strip()
        if source == "implementation_report":
            support = "inference"
        if (
            not identifier
            or identifier in seen_ids
            or source not in allowed_sources
            or not fact
            or support not in allowed_support
        ):
            return ()
        seen_ids.add(identifier)
        parsed.append(
            FailureEvidence(
                identifier=identifier,
                source=source,
                fact=fact,
                support=support,
                round=raw.get("round"),
            )
        )
    return tuple(parsed)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _sanitize_harness_labels(text: str) -> str:
    replacements = {
        "FAIL_TO_PASS": "target_test",
        "PASS_TO_PASS": "regression_test",
    }
    for label, replacement in replacements.items():
        text = re.sub(label, replacement, text, flags=re.IGNORECASE)
    return text


def build_experience_curator_prompt(
    *,
    task: str,
    successful_patch: str,
    validation: dict[str, Any] | str,
    provenance: str,
    failed_patch: str = "",
    feedback: FailureFeedback | dict[str, Any] | None = None,
) -> str:
    """构造记忆提炼任务；调用方必须先证明最终 patch 已被独立验证成功。"""
    feedback_data = feedback.to_dict() if isinstance(feedback, FailureFeedback) else dict(feedback or {})
    validation_text = (
        json.dumps(validation, ensure_ascii=False, indent=2)
        if isinstance(validation, dict)
        else str(validation)
    )
    trajectory_kind = "failure_feedback_success" if failed_patch.strip() and feedback_data else "input_success"
    return f"""## Provenance
{provenance}

## Trajectory Kind
{trajectory_kind}

## Public Task
{_clip(task, MAX_EXPERIENCE_FIELD_CHARS)}

## Failed Patch
```diff
{_clip(failed_patch, MAX_EXPERIENCE_FIELD_CHARS) if failed_patch else "(first attempt succeeded)"}
```

## Failure Feedback
```json
{json.dumps(format_failure_handoff(feedback_data) if feedback_data else {}, ensure_ascii=False, indent=2)}
```

## Successful Patch
```diff
{_clip(successful_patch, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Independent Validation
{_clip(validation_text, MAX_EXPERIENCE_FIELD_CHARS)}

## 输出 Schema
type 和 operation 中用 | 分隔的是可选值，输出时选择单个值。
{{
  "operations": [
    {{"type": "global_experience|repository_experience", "operation": "ADD|EDIT|REMOVE|NOOP", "filename": "", "reason": ""}}
  ],
  "reason": ""
}}
"""


def constraints_from_patch(patch: str, repo_dir: Path, padding: int = 120) -> WorkerConstraints:
    ranges: dict[str, list[list[int]]] = {}
    current_file = ""
    for line in patch.splitlines():
        match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if match:
            current_file = match.group(2)
            ranges.setdefault(current_file, [])
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if not current_file or not hunk:
            continue
        start = int(hunk.group(1))
        length = max(1, int(hunk.group(2) or 1))
        target = repo_dir / current_file
        total_lines = (
            len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            if target.exists()
            else start + length + padding
        )
        ranges[current_file].append(
            [max(1, start - padding), min(total_lines, start + length - 1 + padding)]
        )
    merged = {path: _merge_ranges(values) for path, values in ranges.items() if values}
    return WorkerConstraints.from_tool_input(
        {"allowed_files": sorted(merged), "allowed_ranges": merged}
    )


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(1, (limit - 64) // 2)
    return f"{text[:keep]}\n[... clipped {len(text) - keep * 2} chars ...]\n{text[-keep:]}"
