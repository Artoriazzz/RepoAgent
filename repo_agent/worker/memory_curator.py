from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from repo_agent.core.stage_history import StageConversation, prompt_history
from repo_agent.core.prompts import with_public_evidence_constraint
from repo_agent.worker.models import PatchArtifacts, RecalledMemory, RepairTaskContext
from repo_agent.worker.evaluator import BenchmarkEvaluation
from repo_agent.worker.memory_query import build_memory_query


class TextLLM(Protocol):
    def invoke_text(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class MemoryCurationResult:
    operation: str
    proposal_status: str
    memory_id: str | None = None
    revision: int | None = None


class SemanticMemoryCurator:
    """把已验证成功的修复抽象成后端可并发合并的语义记忆 proposal。"""

    def __init__(self, backend, llm: TextLLM, candidate_limit: int = 5) -> None:
        self._backend = backend
        self._llm = llm
        self._candidate_limit = max(1, min(candidate_limit, 10))

    def curate(
        self,
        context: RepairTaskContext,
        artifacts: PatchArtifacts,
        evaluation: BenchmarkEvaluation,
        observation_id: UUID,
    ) -> MemoryCurationResult:
        memory_query = build_memory_query(context)
        candidates = self._backend.search_memories(
            context.task_id,
            memory_query.fallback_text,
            query_features=memory_query.features,
            limit=self._candidate_limit,
            track_recall=False,
        )
        decision = self._decision(context, artifacts, evaluation, candidates)
        response = self._submit(context, observation_id, decision, candidates, suffix="initial")
        # 后端会把 UPDATE 固定在 Curator 召回时看到的版本上。版本过期意味着这条经验的
        # 推理前提已经变化，不能让同一个任务静默 rebase 后覆盖新的共享记忆；保留 STALE
        # 提案供审计，之后由基于新版本的成功任务重新提出建议。
        return MemoryCurationResult(
            operation=str(decision["operation"]),
            proposal_status=str(response.get("status") or "UNKNOWN"),
            memory_id=None if response.get("memoryId") is None else str(response["memoryId"]),
            revision=response.get("appliedRevision"),
        )

    def _decision(
        self,
        context: RepairTaskContext,
        artifacts: PatchArtifacts,
        evaluation: BenchmarkEvaluation,
        candidates: tuple[RecalledMemory, ...],
    ) -> dict[str, Any]:
        prompt = {
            "task": {"title": context.issue_title, "body": context.issue_body[:8_000]},
            "successful_change": {
                "changed_files": list(artifacts.changed_files),
                "patch": artifacts.patch[:12_000],
            },
            "validation": {
                "status": evaluation.status,
                "f2p_passed": evaluation.fail_to_pass_passed,
                "f2p_failed": evaluation.fail_to_pass_failed,
                "p2p_passed": evaluation.pass_to_pass_passed,
                "p2p_failed": evaluation.pass_to_pass_failed,
            },
            "similar_memories": [self._candidate_payload(memory) for memory in candidates],
        }
        conversation = StageConversation(scope="experience-curator")
        previous = prompt_history(conversation.previous)
        if previous:
            prompt["prior_conversation"] = previous
        if conversation.material:
            prompt["previous_stage_evidence"] = conversation.material
        user = json.dumps(prompt, ensure_ascii=False)
        system = with_public_evidence_constraint(_CURATOR_SYSTEM_PROMPT)
        raw = self._llm.invoke_text(system, user)
        conversation.save([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": raw},
        ])
        decision = self._validate_decision(_parse_json_object(raw), candidates)
        self._reject_task_leakage(decision, context, artifacts.changed_files)
        return decision

    def _submit(
        self,
        context: RepairTaskContext,
        observation_id: UUID,
        decision: dict[str, Any],
        candidates: tuple[RecalledMemory, ...],
        *,
        suffix: str,
    ) -> dict[str, Any]:
        operation = decision["operation"]
        target = self._target(decision, candidates)
        return self._backend.submit_memory_proposal(
            observation_id=observation_id,
            idempotency_key=f"memory-curation:{context.task_id}:{suffix}",
            operation=operation,
            memory_id=None if target is None else target.memory_id,
            base_version=None if target is None else target.revision,
            scope_type=decision.get("scopeType") if operation == "CREATE" else None,
            content=decision.get("content") if operation != "NO_CHANGE" else None,
            change_reason=str(decision.get("changeReason") or "validated repair evidence"),
        )

    @staticmethod
    def _validate_decision(
        decision: dict[str, Any], candidates: tuple[RecalledMemory, ...]
    ) -> dict[str, Any]:
        operation = str(decision.get("operation") or "").upper()
        if operation not in {"CREATE", "UPDATE", "NO_CHANGE"}:
            raise ValueError("Memory Curator returned an invalid operation")
        decision["operation"] = operation
        if operation == "CREATE":
            decision["scopeType"] = (
                "GLOBAL" if str(decision.get("scopeType")).upper() == "GLOBAL" else "REPOSITORY"
            )
        else:
            SemanticMemoryCurator._target(decision, candidates)
        if operation != "NO_CHANGE":
            content = decision.get("content")
            required = ("title", "summary", "applicability", "repairStrategy")
            if not isinstance(content, dict) or any(not str(content.get(key) or "").strip() for key in required):
                raise ValueError("Memory Curator returned incomplete semantic content")
            allowed = {
                "title", "summary", "retrievalText", "applicability", "repairStrategy", "verificationStrategy",
                "antiPatterns", "languageName", "frameworkName", "problemType", "dependencyRange",
            }
            limits = {
                "title": 255, "summary": 2_000, "retrievalText": 2_000, "applicability": 4_000,
                "repairStrategy": 4_000, "verificationStrategy": 3_000,
                "antiPatterns": 3_000, "languageName": 64, "frameworkName": 128,
                "problemType": 128, "dependencyRange": 128,
            }
            decision["content"] = {
                key: str(value).strip()[:limits[key]]
                for key, value in content.items()
                if key in allowed and value is not None
            }
            # 老模型或重试回合可能没有该字段；用摘要降级，不让一次格式遗漏阻断经验沉淀。
            decision["content"].setdefault("retrievalText", decision["content"]["summary"])
        return decision

    @staticmethod
    def _reject_task_leakage(
        decision: dict[str, Any], context: RepairTaskContext, changed_files: tuple[str, ...]
    ) -> None:
        content = json.dumps(decision.get("content") or {}, ensure_ascii=False).lower()
        forbidden = [context.issue_external_id or "", context.base_sha]
        forbidden.extend(path for path in changed_files if len(path) > 3)
        if any(value and value.lower() in content for value in forbidden):
            raise ValueError("Memory Curator output contains task-specific identifiers or paths")

    @staticmethod
    def _target(
        decision: dict[str, Any], candidates: tuple[RecalledMemory, ...]
    ) -> RecalledMemory | None:
        if decision["operation"] == "CREATE":
            return None
        target_id = UUID(str(decision.get("memoryId")))
        for candidate in candidates:
            if candidate.memory_id == target_id:
                return candidate
        raise ValueError("Memory Curator may update only a supplied candidate memory")

    @staticmethod
    def _candidate_payload(memory: RecalledMemory) -> dict[str, Any]:
        return {
            "memoryId": str(memory.memory_id),
            "revision": memory.revision,
            "title": memory.title,
            "summary": memory.summary,
            "retrievalText": memory.retrieval_text,
            "applicability": memory.applicability,
            "repairStrategy": memory.repair_strategy,
            "verificationStrategy": memory.verification_strategy,
            "antiPatterns": memory.anti_patterns,
            "scopeType": memory.scope_type,
        }


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Memory Curator did not return JSON")
        text = text[start:end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Memory Curator JSON must be an object")
    return value


_CURATOR_SYSTEM_PROMPT = """你负责从独立评测已经证明成功的修复案例中维护长期经验。
你没有其他会话或调用方架构的隐含上下文，只能使用本提示、当前成功案例和已召回的现有经验。
你的任务不是复述 patch，而是把经验归入全局经验或仓库经验，并与同类候选去重。

规则：
1. 候选已覆盖新证据且无需澄清时返回 NO_CHANGE。
2. 候选主题相同但新案例补充了适用条件、验证策略或反模式时返回 UPDATE。
3. GLOBAL 只保存问题机制、适用条件、通用修复步骤、验证思路和错误做法；它必须能帮助其他仓库处理同类问题。
4. REPOSITORY 保存当前仓库的相关模块/API、调用关系、可复用工具函数、仓库特有契约和公开验证方法；默认选择 REPOSITORY，只有明确跨仓库成立时才选 GLOBAL。
5. 禁止保存 instance id、issue 编号、完整 diff、gold patch、隐藏测试名或具体常量答案。GLOBAL 不保存仓库名、路径或私有 API；REPOSITORY 可保留稳定模块/API 名，但不保存一次性行号。
6. retrievalText 只写“对象、操作、故障特征、预期契约和适用条件”，用于向量召回；不要写具体 patch、验证步骤或反模式。applicability 写何时适用；repairStrategy 写抽象步骤；verificationStrategy 写公开行为验证；antiPatterns 写已证伪做法。
7. UPDATE/NO_CHANGE 的 memoryId 必须来自 similar_memories/current_memory。不要输出 markdown。

严格输出一个 JSON 对象：
{"operation":"CREATE|UPDATE|NO_CHANGE","memoryId":null,"scopeType":"REPOSITORY|GLOBAL",
 "content":{"title":"","summary":"","retrievalText":"","applicability":"","repairStrategy":"",
 "verificationStrategy":"","antiPatterns":"","languageName":"","frameworkName":"",
 "problemType":"","dependencyRange":""},"changeReason":""}
NO_CHANGE 不需要 content；UPDATE 不需要 scopeType。"""
