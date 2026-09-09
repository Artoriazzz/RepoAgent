from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from repo_agent.core.memory import (
    MAX_SEMANTIC_INSIGHTS_PER_ROLE,
    MemoryStore,
    RecalledMemory,
)
from repo_agent.core.semantic_evidence import extract_json_object
from repo_agent.core.subagent import WorkerConstraints
from repo_agent.core.verification import assess_verification_result, verification_command_safety_reason


MAX_EPISODIC_PROMPT_CHARS = 3_000
MAX_SEMANTIC_PROMPT_CHARS = 2_500
MAX_EXPERIENCE_FIELD_CHARS = 6_000


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

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.identifier,
            "source": self.source,
            "fact": self.fact,
            "support": self.support,
        }


@dataclass(frozen=True)
class FailureFeedback:
    """FailureReview 的结构化结论，也是纠正流程的唯一控制面输入。

    自然语言根因本身不能触发改代码；只有 evidence ledger 中可追溯的证据
    与 correction_evidence_ids 对齐后，路由才会授权 corrective implement。
    """

    failure_stage: str
    issue_contract: tuple[str, ...]
    observed_failure: tuple[str, ...]
    failure_observation_quality: str
    root_cause: str
    invalidated_assumption: str
    required_correction: str
    evidence_ledger: tuple[FailureEvidence, ...]
    correction_evidence_ids: tuple[str, ...]
    scope_expansion: tuple[str, ...]
    regression_guards: tuple[str, ...]
    correction_readiness: str
    next_evidence_source: str
    confidence: float

    @property
    def requires_coordinator_replan(self) -> bool:
        return self.correction_route == "coordinator_replan"

    @property
    def correction_route(self) -> str:
        if self.failure_stage in {"complete", "invalid_test"}:
            return "none"
        if (
            self.correction_readiness == "needs_replan"
            and self.failure_stage != "validation_environment"
        ):
            return "coordinator_replan"
        if (
            self.correction_readiness == "actionable"
            and self.failure_stage in {"implementation_semantics", "regression"}
            and self._has_authorizing_correction_evidence()
        ):
            return "corrective_implement"
        return "none"

    def _has_authorizing_correction_evidence(self) -> bool:
        ledger = {item.identifier: item for item in self.evidence_ledger}
        if not self.correction_evidence_ids:
            return False
        selected = [ledger.get(identifier) for identifier in self.correction_evidence_ids]
        # inference 可以解释因果关系，但不能单独授权改代码；至少需要一条
        # 直接失败证据和一条直接任务/代码证据，unknown 或悬空引用一律拒绝。
        if any(item is None or item.support == "unknown" for item in selected):
            return False
        direct_sources = {
            item.source
            for item in selected
            if item is not None and item.support == "direct"
        }
        return bool(
            direct_sources & {"failure_log", "validation_summary"}
            and direct_sources & {"public_task", "failed_patch", "semantic_evidence"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_stage": self.failure_stage,
            "issue_contract": list(self.issue_contract),
            "observed_failure": list(self.observed_failure),
            "failure_observation_quality": self.failure_observation_quality,
            "root_cause": self.root_cause,
            "invalidated_assumption": self.invalidated_assumption,
            "required_correction": self.required_correction,
            "evidence_ledger": [item.to_dict() for item in self.evidence_ledger],
            "correction_evidence_ids": list(self.correction_evidence_ids),
            "scope_expansion": list(self.scope_expansion),
            "regression_guards": list(self.regression_guards),
            "correction_readiness": self.correction_readiness,
            "next_evidence_source": self.next_evidence_source,
            "correction_route": self.correction_route,
            "confidence": self.confidence,
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
            coordinator_replan=False,
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
    if validation.get("validation_kind") == "swebench_harness_feedback":
        return {
            "validation_kind": "swebench_harness_feedback",
            "status": validation.get("status"),
            "reason": validation.get("reason"),
            "test_counts": validation.get("test_counts") or {},
            "failure_facts": validation.get("failure_facts") or {},
            "failure_attribution": validation.get("failure_attribution") or {},
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


def build_failure_review_prompt(
    *,
    task: str,
    failed_patch: str,
    failure_log: str,
    validation: dict[str, Any] | str | None = None,
) -> str:
    compact_validation = compact_failure_validation(validation)
    validation_text = (
        json.dumps(compact_validation, ensure_ascii=False, indent=2)
        if isinstance(compact_validation, dict)
        else str(compact_validation or "")
    )
    failure_log = _sanitize_harness_labels(failure_log)
    validation_text = _sanitize_harness_labels(validation_text)
    return f"""## Goal
把一次失败修复压缩成有证据边界的 FailureFeedback。只做归因，不修改代码。

## Public Task
{_clip(task, MAX_EXPERIENCE_FIELD_CHARS)}

## Failed Patch
```diff
{_clip(failed_patch, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Explicit Failure Evidence
```text
{_clip(failure_log, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Validation Summary
{_clip(validation_text, 10_000)}

## Review Protocol
参考 ExpeRepair 的 review/refine 分离原则：公开任务、首次 harness 失败日志和候选 patch 都可能不完整，先归因再修正。
1. 先单独提取 Public Task 明确承诺的行为，写入 `issue_contract`；不要扩展到“类似”“对称”或未点名的输入。
2. 对 Public Task 给出的具体触发输入，逐个判断它实际会经过代码片段中的哪个分支。代码块只是定位上下文，不代表其中每个 disjunct 都是 direct issue contract。
   必须区分 `issue_contract` 与 `code-derived validation obligation`：后者不必在题目中逐字点名。若一个 case 隔离了 failed patch 所修改的复合 guard 中某个 predicate，且 baseline/candidate 已执行出逐 case 行为，它就是可裁决的代码差分证据；“题目未逐字提到该 case”本身不能把它判成 `invalid_test` 或 `scope_expansion`。
3. 再记录 patch 后日志真正观察到的事实，写入 `observed_failure` 和 `failure_observation_quality`。当 Validation Summary 的 `validation_kind=swebench_harness_feedback` 时，它是 experience-build 允许使用的一次真实评测反馈；可以使用失败测试、异常和 expected/actual 差异，但不得推断日志未展示的隐藏测试源码或 gold patch：
   - `expected_actual`：日志有可核对的期望值和实际值；
   - `exception_detail`：日志有明确异常类型、message 和触发位置；
   - `behavior_delta`：同一公开差分脚本在 baseline/candidate 上给出逐 case 结构化输出；
   - `failure_only`：只有测试失败、裸 AssertionError 或数量；
   - `environment_only`：只有环境/依赖/网络错误。
   `failure_only` 只能证明仍有失败，不能证明期望值、公式、额外分支或根因，因此不能直接 actionable。
   Validation Summary 中 `oracle_strength=differential_only` 只能证明行为发生变化，不能支持 complete；`exact` 必须逐字段匹配 expected_after，`invariant` 必须保持 baseline observation。`invalid_target_cases` 表示 clean baseline 已满足目标，不能据此要求 Corrective。
4. 建立 `evidence_ledger`。每条事实标注来源和支持强度：
   - `direct`：输入中原样出现或可直接释义；
   - `inference`：合理推断但输入没有直接证明；
   - `unknown`：缺失或冲突。
   `semantic_evidence` 只可引用 Validation Summary 中已注册、在干净基线执行过的 case/contract；不能把新推测包装成该来源。带 code/public_test 引用的旧行为不变量可以作为 direct regression evidence。
5. 比较 issue contract、failed patch 和 post-patch failure，定位第一个错误 predicate / identity / conversion / state transition。
   先检查验证实际观察的是不是 Public Task 要求的最终公开结果。命名/文本/格式任务必须包含精确输出，分类任务必须包含最终分类或诊断，容器任务必须包含公开 type/shape/dtype；只有“未抛异常”、退出码或内部中间值时，不能据此要求 Corrective 改写语义。
   必须先逐项读取 `case_matrix` 和 `relation_violations`，不得用通过的 case 概括或覆盖失败 case。存在 `must_change + changed=false` 或 `must_preserve + changed=true` 时，证据已经是 `behavior_delta`；不得输出 `complete`、`insufficient_evidence` 或要求额外 validation logs。
   对 `public_differential_trace`，先过滤无效输入，再逐 case 判断 baseline/candidate 差异是否符合题意、未变化是否意味着 bug 仍在、变化是否破坏题意、当前生产代码或公开测试支持的旧行为。由 changed guard 推导的结构类型/语义属性、True/False/None、符号假设和正负边界，是验证该 guard 是否充分且不过宽的代码义务，不因题目省略这些词而失效。若所有有效 case 都支持完整修复，不要编造 correction。
   - 所有有效 case 都支持修复且没有公开回归证据：`failure_stage=complete`、`correction_readiness=not_needed`。
   - 只有控制器给出 `oracle_complete=true` 时才能使用 complete。存在 oracle_gaps 时使用 insufficient_evidence；存在 invalid_target_cases 时使用 invalid_test。
   - 只有差分输入与 Public Task、当前生产代码、公开测试或已执行 baseline 事实明确冲突，或没有隔离其声称覆盖的 predicate 时，才可使用 `failure_stage=invalid_test`。必须在 `root_cause` 中写出具体冲突；仅凭 Public Task 没有逐字列出该输入，不足以判无效。
   - 对普通 assert/pytest 验证，如果断言要求了 Public Task 未承诺的具体表示、类型或规范形式，也使用 `invalid_test/not_needed`；不要因此批准当前 patch，控制器会改用公开差分行为重新审查。
   - 对有效差分中的复合 guard，若某 case 隔离了 patch 直接涉及的 predicate，且违反 `must_change`/`must_preserve`，默认视为 implementation semantics 证据。若要否定该 relation，必须引用 direct evidence 证明 relation 与题意、生产代码或 baseline 事实冲突，不能只说“题目未提及”。
6. 逐项列出需要修改当前 guard/hunk 以外的新行为或新契约到 `scope_expansion`。同一 changed guard 的 predicate truth table、结构/语义属性和符号/正负边界不是 scope expansion。
7. 只有同时满足以下条件才可写 `correction_readiness=actionable`：
   - 能从 direct evidence 精确说明要改变的 predicate / identity / conversion / state transition；
   - 能指出 failed patch 中哪一项具体假设被观察结果否定；不能因为验证不充分就让 Corrective 重写整个方案；
   - `correction_evidence_ids` 中的 direct 子集同时引用失败事实，以及 Public Task/failed patch/已校验 SemanticEvidence 中至少一项事实；inference 可以辅助解释因果，但不能替代这两类 direct 授权证据，unknown 不得被引用；
   - `failure_observation_quality` 是 `expected_actual`、`exception_detail` 或有逐 case 直接证据的 `behavior_delta`；
   - 不需要新增语义范围，`scope_expansion` 为空。
8. 如果 patch 已覆盖公开触发路径，而 post-patch 日志只有 `failure_only`，根因仍未知：不要为相邻分支编造修法，使用 `needs_replan` 或 `insufficient_evidence`。
9. “实现正确公式/算法”“全面覆盖边界情况”这类方向性建议不是 actionable；需要重新读源码或确认领域契约时写 `needs_replan`。
   固定默认值、宽泛异常 fallback 或下游症状处理若与题意要求的精确结果冲突，可以直接判为 implementation_semantics；但 required_correction 应指向首个错误语义决策或仓库原生规则，不能只要求“增加更多 special case”。
10. 如果缺失事实能通过当前仓库源码、公开测试或公开文档补齐，写 `needs_replan`，并用 `next_evidence_source` 指定唯一来源；不要写 `insufficient_evidence`。
11. 只有没有具体公开补证路径，或只有无法在修复流程解决的环境问题时，才写 `insufficient_evidence`。不得为了触发下一轮而提高 confidence。
12. 不得使用 gold patch、隐藏测试源码或题目答案。测试分组只用于区分目标仍失败与公开回归，不能反推出隐藏实现。

最终只输出 JSON：
{{
  "failure_stage": "complete|invalid_test|localization|problem_contract|implementation_semantics|regression|validation_environment|insufficient_evidence",
  "issue_contract": ["Public Task 直接规定的行为"],
  "observed_failure": ["patch 后直接观察到的失败；未知部分明确写 unknown"],
  "failure_observation_quality": "expected_actual|exception_detail|behavior_delta|failure_only|environment_only",
  "root_cause": "失败的首个因果点；证据不足时明确说明未知",
  "invalidated_assumption": "原 patch 中被失败证据实际否定的假设",
  "required_correction": "由证据支持的精确修正义务，或 Coordinator 需要补查的唯一事实",
  "evidence_ledger": [
    {{"id": "E1", "source": "public_task|failed_patch|failure_log|validation_summary|semantic_evidence", "fact": "输入中可核对的事实", "support": "direct|inference|unknown"}}
  ],
  "correction_evidence_ids": ["E1"],
  "scope_expansion": ["超出 Public Task、或需离开当前 patch 证据范围才能证明的语义义务；没有则为空"],
  "regression_guards": ["修正时必须保持的、由公开事实支持的相邻行为"],
  "correction_readiness": "not_needed|actionable|needs_replan|insufficient_evidence",
  "next_evidence_source": "none|repository_code|public_test|public_documentation|environment",
  "confidence": 0.0
}}
"""


def parse_failure_feedback(text: str) -> FailureFeedback | None:
    payload = extract_json_object(text)
    if not isinstance(payload, dict):
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
    next_evidence_source = str(payload.get("next_evidence_source") or "none").strip()
    if next_evidence_source not in {
        "none",
        "repository_code",
        "public_test",
        "public_documentation",
        "environment",
    }:
        return None
    if not readiness:
        readiness = (
            "needs_replan"
            if stage in {"localization", "problem_contract"}
            else "insufficient_evidence"
        )
    if readiness not in {
        "not_needed",
        "actionable",
        "needs_replan",
        "insufficient_evidence",
    }:
        return None
    public_replan_sources = {"repository_code", "public_test", "public_documentation"}
    if stage in {"complete", "invalid_test"}:
        readiness = "not_needed"
        next_evidence_source = "none"
    elif stage == "validation_environment":
        readiness = "insufficient_evidence"
        next_evidence_source = "environment"
    elif stage == "insufficient_evidence" and next_evidence_source in public_replan_sources:
        readiness = "needs_replan"
        stage = (
            "localization"
            if next_evidence_source == "repository_code"
            else "problem_contract"
        )
    elif stage == "insufficient_evidence":
        readiness = "insufficient_evidence"
    elif stage in {"localization", "problem_contract"} or scope_expansion:
        readiness = "needs_replan"
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
        and direct_sources & {"public_task", "failed_patch", "semantic_evidence"}
    )
    if readiness == "actionable" and not has_authorizing_correction_evidence:
        readiness = "needs_replan"
    if readiness == "actionable" and observation_quality in {
        "failure_only",
        "environment_only",
    }:
        readiness = "needs_replan"
    if readiness == "needs_replan" and next_evidence_source not in public_replan_sources:
        readiness = "insufficient_evidence"
    if readiness == "actionable":
        next_evidence_source = "none"
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return FailureFeedback(
        failure_stage=stage,
        issue_contract=_string_tuple(payload.get("issue_contract")),
        observed_failure=_string_tuple(payload.get("observed_failure")),
        failure_observation_quality=observation_quality,
        root_cause=str(payload.get("root_cause") or "").strip(),
        invalidated_assumption=str(payload.get("invalidated_assumption") or "").strip(),
        required_correction=str(payload.get("required_correction") or "").strip(),
        evidence_ledger=evidence_ledger,
        correction_evidence_ids=correction_evidence_ids,
        scope_expansion=scope_expansion,
        regression_guards=_string_tuple(payload.get("regression_guards")),
        correction_readiness=readiness,
        next_evidence_source=next_evidence_source,
        confidence=confidence,
    )


def _parse_failure_evidence(raw_items: Any) -> tuple[FailureEvidence, ...]:
    if not isinstance(raw_items, list):
        return ()
    allowed_sources = {
        "public_task",
        "failed_patch",
        "failure_log",
        "validation_summary",
        "semantic_evidence",
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
    return f"""## Goal
将一次已验证成功的修复更新到双记忆。先维护 Episodic Memory，再按 ExpeRepair 的 ADD / EDIT / REMOVE 方式维护 Semantic Memory。

## Provenance
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
{json.dumps(feedback_data, ensure_ascii=False, indent=2)}
```

## Successful Patch
```diff
{_clip(successful_patch, MAX_EXPERIENCE_FIELD_CHARS)}
```

## Independent Validation
{_clip(validation_text, MAX_EXPERIENCE_FIELD_CHARS)}

## Episodic Memory Policy
1. 只有验证明确成功时才保存 `repair_episode`。
2. 保存紧凑 demonstration：stage、problem_signature、input_evidence、failed_attempt、feedback、successful_output、validation。
3. `successful_output` 只描述结构化 delta，例如“在第一个依赖 predicate 前复用 normalization helper”；不要保存完整 diff、具体 instance id、文件路径、gold patch 或 hidden test。
4. 保存前搜索相似 `repair_episode`。相似且没有新增失败模式时 no-op；有新增证据时 update，不重复新增。
5. role 使用 `implement`，provenance 必须原样写入。

## Semantic Memory Policy
分别维护 `coordinator`、`implement`、`review` 角色的 `semantic_insight`：
- ADD：新经验明显不同、实用且可泛化时新增。
- EDIT：与已有 insight 重叠时合并、澄清或提高可操作性。
- REMOVE：删除矛盾、过时、重复或被新证据否定的 insight。
- 每个角色最多 {MAX_SEMANTIC_INSIGHTS_PER_ROLE} 条；达到上限时，ADD 前必须先 REMOVE。
- 每个 insight 只写一条短小、可执行规则，包含 applicability 和 failure signal。
- 单次修复每个角色最多执行 4 个操作；没有新增价值时 no-op。
- Semantic insight 不保存当前任务的标识符、路径或具体 patch。

## Required Tool Order
1. search_memories(type="repair_episode")，必要时 read/update/save。
2. list_memories 并按角色检查 `semantic_insight`；必要时 read。
3. 对 semantic insight 执行 ADD / EDIT / REMOVE。REMOVE 使用 delete_memory。

最终输出 JSON：
{{
  "episode_action": "save|update|noop",
  "episode_filename": "",
  "semantic_operations": [
    {{"role": "implement", "operation": "ADD|EDIT|REMOVE", "filename": "", "reason": ""}}
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
