from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from repo_agent.worker.models import RepairTaskContext


_FEATURE_NAMES = (
    "object",
    "operation",
    "symptom",
    "expectedBehavior",
    "triggerConditions",
)

_FEATURE_LIMITS = {
    "object": 512,
    "operation": 512,
    "symptom": 2_000,
    "expectedBehavior": 2_000,
    "triggerConditions": 2_000,
}


@dataclass(frozen=True)
class MemoryQuery:
    """传给后端的记忆查询：稳定语义特征优先，Issue 文本始终作为回退。"""

    fallback_text: str
    features: dict[str, str]


def build_memory_query(context: RepairTaskContext) -> MemoryQuery:
    """
    Coordinator 若已在上下文中留下结构化问题特征，就只用这些事实生成向量；
    首次召回或信息不足时保持原有的 Issue 标题/正文路径，不额外调用 LLM。
    """
    fallback = f"{context.issue_title}\n{context.issue_body}".strip()[:12_000]
    if not fallback:
        fallback = "repository repair issue"
    features = normalize_memory_query_features(context.memory_query_features) or {}
    return MemoryQuery(fallback_text=fallback, features=features)


def normalize_memory_query_features(value: object) -> dict[str, str] | None:
    """容忍后端尚未提供该字段的旧任务上下文。"""
    if not isinstance(value, Mapping):
        return None
    result = {
        name: str(value[name]).strip()[:_FEATURE_LIMITS[name]]
        for name in _FEATURE_NAMES
        if value.get(name) is not None and str(value[name]).strip()
    }
    return result or None


def extract_memory_query_features(output_dir: Path) -> dict[str, str] | None:
    """从 Coordinator 已保存的公开语义证据提取下一轮可复用的查询事实。

    不读取最终自然语言回答，也不把模型猜测的根因写入检索文本。只有 Reproducer/Coordinator
    已结构化保存的触发条件、当前行为和期望契约会进入这里，因此该函数本身不增加 LLM 调用。
    """
    attribution_path = output_dir / "stage_attribution.json"
    try:
        attribution = json.loads(attribution_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(attribution, Mapping):
        return None

    stages = attribution.get("stages")
    reproduction = stages.get("reproduction") if isinstance(stages, Mapping) else None
    if not isinstance(reproduction, Mapping):
        return None
    evidence = reproduction.get("semantic_evidence") or reproduction.get("contract")
    brief = reproduction.get("repair_brief")
    evidence_contract = _mapping_value(evidence, "reproduction_contract")
    brief_contract = _mapping_value(brief, "problem_contract")
    contract = evidence_contract or brief_contract
    if not contract:
        return None

    # object / operation 只有 Coordinator 明确给出时才使用；避免从长 Issue 文本猜测。
    features = {
        "object": _first_text(contract, "object", "component", "subject"),
        "operation": _first_text(contract, "operation", "action"),
        "symptom": _first_text(contract, "symptom", "current_behavior", "actual_behavior"),
        "expectedBehavior": _first_text(contract, "expected_behavior", "expectedBehavior"),
        "triggerConditions": _first_text(contract, "trigger", "trigger_conditions", "triggerConditions"),
    }
    normalized = normalize_memory_query_features(features)
    # 仅有一小段无上下文的模型文字不足以替换首次 Issue 检索。
    return normalized if normalized is not None and len(normalized) >= 2 else None


def merge_memory_query_features(
    existing: object, extracted: object
) -> dict[str, str] | None:
    """保留调用方已提供的稳定事实，再补齐 Coordinator 新确认的字段。"""
    result = dict(normalize_memory_query_features(existing) or {})
    for name, value in (normalize_memory_query_features(extracted) or {}).items():
        result.setdefault(name, value)
    return result or None


def _mapping_value(value: object, key: str) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    nested = value.get(key)
    return nested if isinstance(nested, Mapping) else None


def _first_text(value: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, (list, tuple)):
            parts = [str(item).strip() for item in raw if str(item).strip()]
            if parts:
                return "；".join(parts)
    return ""
