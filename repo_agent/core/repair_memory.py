from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from repo_agent.core.memory import MemoryStore, RecalledMemory, SideQueryFn


MAX_REPAIR_RECIPE_CHARS = 6_000
MAX_CASE_FIELD_CHARS = 8_000


@dataclass(frozen=True)
class RepairRecipeRecall:
    task: str
    memories: tuple[RecalledMemory, ...]
    applicability: tuple["RepairRecipeApplicability", ...] = ()

    @property
    def injected(self) -> bool:
        return bool(self.memories)


@dataclass(frozen=True)
class RepairRecipeConstraint:
    source: str
    name: str
    description: str
    requires_reader_writer_roundtrip: bool = False


@dataclass(frozen=True)
class RepairRecipeApplicability:
    source: str
    name: str
    applicable: bool
    confidence: float
    matched_terms: tuple[str, ...]
    reason: str


def augment_task_with_repair_recipes(
    *,
    task: str,
    memory_store: MemoryStore,
    side_query: SideQueryFn | None = None,
    limit: int = 3,
) -> RepairRecipeRecall:
    query = repair_recipe_query_from_task(task)
    memories = tuple(memory_store.recall_repair_recipes(query, limit=limit, side_query=side_query))
    if not memories:
        return RepairRecipeRecall(task=task, memories=())
    applicability = evaluate_repair_recipe_applicability(query, memories)
    applicable_sources = {result.source for result in applicability if result.applicable}
    applicable_memories = tuple(
        memory for memory in memories if memory.header.filename in applicable_sources
    )
    if not applicable_memories:
        return RepairRecipeRecall(task=task, memories=(), applicability=applicability)
    return RepairRecipeRecall(
        task=(
            f"{_format_repair_recipe_applicability_section(tuple(result for result in applicability if result.applicable))}"
            f"\n\n{_format_repair_recipe_section(applicable_memories)}\n\n{task}"
        ),
        memories=applicable_memories,
        applicability=applicability,
    )


def evaluate_repair_recipe_applicability(
    task: str,
    memories: tuple[RecalledMemory, ...],
) -> tuple[RepairRecipeApplicability, ...]:
    task_terms = _signature_terms(repair_recipe_query_from_task(task))
    results: list[RepairRecipeApplicability] = []
    for memory in memories:
        recipe_text = _repair_recipe_text(memory)
        required_terms = _recipe_required_terms(recipe_text)
        matched_terms = tuple(sorted(term for term in required_terms if term in task_terms))
        exact_identifier_match = any("_" in term or "." in term for term in matched_terms)
        required_count = 1 if exact_identifier_match else 2
        applicable = len(matched_terms) >= required_count
        denominator = max(required_count, min(5, len(required_terms) or required_count))
        confidence = min(1.0, len(matched_terms) / denominator)
        if applicable:
            reason = f"matched recipe-specific task terms: {', '.join(matched_terms)}"
        elif matched_terms:
            reason = (
                "only weak overlap with recalled recipe; keep it as soft context, "
                f"matched: {', '.join(matched_terms)}"
            )
        else:
            reason = "no recipe-specific task signature matched; keep it as soft context only"
        results.append(
            RepairRecipeApplicability(
                source=memory.header.filename,
                name=memory.header.name,
                applicable=applicable,
                confidence=confidence,
                matched_terms=matched_terms,
                reason=reason,
            )
        )
    return tuple(results)


def hard_constraints_from_repair_recipes(
    memories: tuple[RecalledMemory, ...],
    *,
    task: str = "",
) -> tuple[RepairRecipeConstraint, ...]:
    applicability = {
        result.source: result
        for result in evaluate_repair_recipe_applicability(task, memories)
    } if task.strip() else {}
    constraints: list[RepairRecipeConstraint] = []
    for memory in memories:
        if applicability and not applicability[memory.header.filename].applicable:
            continue
        text = _repair_recipe_text(memory).lower()
        requires_reader_writer_roundtrip = (
            "reader" in text
            and "writer" in text
            and ("roundtrip" in text or "round-trip" in text or "round trip" in text)
        )
        if requires_reader_writer_roundtrip:
            constraints.append(
                RepairRecipeConstraint(
                    source=memory.header.filename,
                    name=memory.header.name,
                    description=memory.header.description,
                    requires_reader_writer_roundtrip=True,
                )
            )
    return tuple(constraints)


def repair_recipe_query_from_task(task: str) -> str:
    """提取当前问题本身，避免通用提示词污染 recipe 召回和硬约束判断。"""
    problem = _extract_markdown_section(task, "Problem Statement")
    if problem:
        return problem
    cleaned = re.split(r"\n##\s+(Instructions|Recalled Repair Recipes|Repair Recipe Applicability Gate)\b", task, maxsplit=1)[
        0
    ]
    return cleaned.strip() or task


def _repair_recipe_text(memory: RecalledMemory) -> str:
    return f"{memory.header.name}\n{memory.header.description}\n{_strip_frontmatter(memory.content)}"


def build_case_curator_prompt(
    *,
    task: str,
    patch: str,
    result: str,
    validation: dict[str, Any] | str | None = None,
    final_answer: str = "",
) -> str:
    validation_text = json.dumps(validation, ensure_ascii=False, indent=2) if isinstance(validation, dict) else str(validation or "")
    return f"""## Goal
判断这次成功修复是否值得沉淀为长期 repair_recipe，并在值得时保存一条抽象策略记忆。

## Success Evidence
{_clip(result, MAX_CASE_FIELD_CHARS)}

## Original Task / Problem
{_clip(task, MAX_CASE_FIELD_CHARS)}

## Final Patch
```diff
{_clip(patch, MAX_CASE_FIELD_CHARS)}
```

## Validation / Harness Summary
{_clip(validation_text, MAX_CASE_FIELD_CHARS)}

## Final Agent Answer
{_clip(final_answer, 2_000)}

## Required Decision
你必须先判断是否值得记录。只有满足以下条件才调用 save_memory：
- 修复已成功，且成功证据来自用户确认、验证命令成功或 SWE-bench harness resolved。
- 修复方式有跨问题泛化价值，不是 typo、一次性配置、项目私有路径或临时 hack。
- 能抽象成“如何识别 / 如何修改 / 如何验证 / 避免什么”的策略。

## Similarity / Update Policy
在保存前必须先调用 search_memories：
- query 应包含问题签名、错误类型、行为类别和核心修复策略。
- type 必须是 repair_recipe。

如果找到相似 recipe：
- 调用 read_memory 读取最相近的一条。
- 如果新案例提供了新的抽象步骤、验证契约或反模式，用 update_memory 改进原记忆。
- 如果新案例没有新增可迁移知识，什么也不保存。
- 不要为同一修复模式创建第二条 repair_recipe。

如果 save_memory 返回 SIMILAR_REPAIR_RECIPE_EXISTS：
- 读取提示中的相似 memory。
- 改用 update_memory 或 no-op，不要换名字强行新增。

## What To Save
如果没有相似项且值得记录，调用 save_memory：
- type: repair_recipe
- name: 4 到 8 个英文或中文词，描述抽象问题类型，不要包含 instance id。
- description: 一句话，包含问题签名和核心策略关键词，便于检索。
- content: Markdown 或 YAML 风格，必须包含以下小节：
  - problem_signatures
  - applicability
  - repair_strategy
  - edit_brief_rules
  - verification_contract
  - anti_patterns

## Redaction Rules
- 不要保存完整 diff、具体 instance id、gold patch、hidden tests、PASS_TO_PASS、FAIL_TO_PASS。
- 尽量不要保存一次性文件路径；如果路径能帮助抽象，只写“format class / reader data class / parser table”这类角色名。
- 可以保存通用 API 名、错误类型、参数名、行为类别，例如 `header_rows`、`unexpected keyword argument`、mutable buffer、case-insensitive parser。

## Final Output
无论是否保存，最终回复一段 JSON：
```json
{{"is_worth_recording": true, "action": "save", "saved_filename": "...", "updated_filename": "", "reason": "..."}}
```
或：
```json
{{"is_worth_recording": true, "action": "update", "saved_filename": "", "updated_filename": "...", "reason": "..."}}
```
或：
```json
{{"is_worth_recording": false, "action": "noop", "saved_filename": "", "updated_filename": "", "reason": "..."}}
```
"""


def _format_repair_recipe_section(memories: tuple[RecalledMemory, ...]) -> str:
    blocks = []
    for index, memory in enumerate(memories, 1):
        blocks.append(
            f"### Recipe {index}: {memory.header.name}\n"
            f"- source: {memory.header.filename}\n"
            f"- description: {memory.header.description}\n\n"
            f"{memory.content}"
        )
    body = _clip("\n\n".join(blocks), MAX_REPAIR_RECIPE_CHARS)
    return f"""## Recalled Repair Recipes
下面是长期记忆中检索到的抽象修复策略。它们是软参考，不是答案，也不是当前仓库事实；必须先用当前代码验证后才能采用。
只有当 recipe 的 problem_signatures/applicability 与当前任务强匹配时，Coordinator 才应把它转换为 Problem Contract、EditBrief 和 Verify Contract；不适用的 recipe 只能作为反例或忽略，不能扩大修改范围。

{body}"""


def _format_repair_recipe_applicability_section(applicability: tuple[RepairRecipeApplicability, ...]) -> str:
    lines = [
        "## Repair Recipe Applicability Gate",
        "下面是确定性过滤层对召回 recipe 的判断。`hard_applicable=false` 的 recipe 只能作为软参考，不能触发硬约束或扩大 allowed_ranges。",
        "",
    ]
    for result in applicability:
        matched = ", ".join(result.matched_terms) if result.matched_terms else "(none)"
        lines.append(
            f"- {result.source}: hard_applicable={str(result.applicable).lower()}, "
            f"confidence={result.confidence:.2f}, matched_terms={matched}, reason={result.reason}"
        )
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(1, (limit - 48) // 2)
    return f"{text[:keep]}\n[... clipped {len(text) - keep * 2} chars ...]\n{text[-keep:]}"


_RECIPE_SIGNATURE_STOPWORDS = {
    "abstract",
    "agent",
    "anti",
    "anti_patterns",
    "apply",
    "applicability",
    "avoid",
    "behavior",
    "behaviour",
    "brief",
    "case",
    "cases",
    "code",
    "contract",
    "cover",
    "covers",
    "current",
    "created",
    "created_at",
    "data",
    "description",
    "edit",
    "edit_brief_rules",
    "ensure",
    "expected",
    "failure",
    "format",
    "generic",
    "handle",
    "issue",
    "logic",
    "mention",
    "must",
    "only",
    "output",
    "problem",
    "problem_signatures",
    "python",
    "read",
    "reader",
    "reading",
    "repair",
    "repair_recipe",
    "repair_strategy",
    "requires",
    "result",
    "results",
    "round",
    "roundtrip",
    "round_trip",
    "round-trip",
    "semantic",
    "should",
    "strategy",
    "support",
    "supports",
    "table",
    "test",
    "tests",
    "update",
    "validation",
    "verification",
    "verification_contract",
    "when",
    "write",
    "writer",
    "writing",
}


def _signature_terms(text: str) -> set[str]:
    lowered = text.lower().replace("-", "_")
    terms = {term for term in re.findall(r"[a-z0-9_\.]+", lowered) if len(term) >= 4}
    phrase_aliases = {
        "header rows": "header_rows",
        "case insensitive": "case_insensitive",
        "unexpected keyword": "unexpected_keyword",
        "mutable buffer": "mutable_buffer",
        "order by": "order_by",
    }
    spaced = text.lower().replace("_", " ").replace("-", " ")
    terms.update(alias for phrase, alias in phrase_aliases.items() if phrase in spaced)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {term for term in terms if term not in _RECIPE_SIGNATURE_STOPWORDS}


def _recipe_required_terms(recipe_text: str) -> set[str]:
    terms = _signature_terms(recipe_text)
    # 保留带下划线或点号的 API/文件/参数签名；普通词只保留更具体的长词，避免
    # `reader/writer/roundtrip` 这类泛词把所有任务都升级成 hard constraint。
    return {
        term
        for term in terms
        if "_" in term or "." in term or len(term) >= 6
    }


def _extract_markdown_section(text: str, title: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$", flags=re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    match = re.match(r"---\s*\n[\s\S]*?\n---\s*\n?", text)
    if not match:
        return text
    return text[match.end() :]
