from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from repo_agent.core.semantic_evidence import extract_json_object
from repo_agent.core.verification import verification_command_safety_reason


CASE_PREFIX = "REPO_AGENT_CASE="
MAX_DIFFERENTIAL_CASES = 4
GUARD_COVERAGE_BEGIN = "<required_guard_coverage>"
GUARD_COVERAGE_END = "</required_guard_coverage>"
TRUSTED_ORACLE_BEGIN = "<trusted_oracle_evidence>"
TRUSTED_ORACLE_END = "</trusted_oracle_evidence>"
EXPECTED_RELATIONS = {"must_change", "must_preserve", "observe"}
CASE_ROLES = {"target", "invariant", "exploratory"}
ORACLE_STRENGTHS = {"exact", "invariant", "differential_only"}


def _hint_present(text: str, hint: str) -> bool:
    normalized = hint.lower()
    if normalized == "integer base":
        return bool(
            re.search(r"(?<!non-)(?<!non )\binteger base\b", text)
        )
    if normalized in text:
        return True

    # 模型常会在语义短语中插入限定词，例如用
    # “positive symbolic integer exponent” 表达 “positive exponent”。
    # 允许少量有序插词，避免把已经明确表达的边界案例误判为缺失。
    words = re.findall(r"[a-z0-9_]+", normalized)
    if len(words) < 2 or any(character in normalized for character in "=*"):
        return False
    pattern = r"\b" + r"\b(?:\W+\w+){0,2}\W+\b".join(
        re.escape(word) for word in words
    ) + r"\b"
    return bool(re.search(pattern, text))


def extract_guard_obligations(candidate_patch: str) -> list[dict[str, Any]]:
    """从新增复合条件提取可机器校验的 predicate 与高风险边界。"""

    added = [
        line[1:].strip()
        for line in candidate_patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    expression = " ".join(line for line in added if " and " in line or " or " in line)
    if not expression:
        return []
    atoms = []
    for value in re.split(r"\s+(?:and|or)\s+", expression):
        predicate = re.sub(r"\s+(?:and|or)\s*$", "", value.strip(" ():"))
        if predicate:
            atoms.append(predicate)
    obligations: list[dict[str, Any]] = []
    for index, predicate in enumerate(dict.fromkeys(atoms), 1):
        obligations.append(
            {
                "id": f"guard_atom_{index}",
                "predicate": predicate,
                "variant": "predicate_path",
                "required_relation": "observe",
                "case_hints": [],
            }
        )

    lowered = expression.lower()
    if re.search(r"\bexp\b.*(?:is_integer|isinstance\s*\([^,]+,\s*integer)", lowered):
        obligations.extend(
            [
                {
                    "id": "exponent_semantic_integer",
                    "predicate": "integer exponent represented by semantic assumptions",
                    "variant": "positive symbolic integer exponent",
                    "required_relation": "must_change",
                    "case_hints": [
                        [
                            "integer=true",
                            "integer-assumed exponent",
                            "整数指数假设",
                        ],
                        ["**p", "** p", "symbolic exponent", "符号指数"],
                        [
                            "integer base",
                            "base integer=true",
                            "base with integer=true",
                            "整数底数",
                        ],
                        ["positive=true", "positive exponent", "正整数指数"],
                    ],
                },
                {
                    "id": "exponent_negative_boundary",
                    "predicate": "integer exponent sign",
                    "variant": "negative integer exponent",
                    "required_relation": "must_change",
                    "case_hints": [
                        [
                            "**-2",
                            "** -2",
                            "literal negative",
                            "concrete negative",
                            "字面负整数",
                        ],
                        [
                            "integer base",
                            "base integer=true",
                            "base with integer=true",
                            "整数底数",
                        ],
                    ],
                },
            ]
        )
    if re.search(r"\bbase\b.*is_integer", lowered):
        obligations.append(
            {
                "id": "base_noninteger_boundary",
                "predicate": "base.is_integer",
                "variant": "non-integer or unknown base",
                "required_relation": "must_change",
                "case_hints": [
                    [
                        "non-integer",
                        "no integer assumption",
                        "rational",
                        "非整数",
                        "无整数假设",
                    ]
                ],
            }
        )
    return obligations


def extract_impact_obligations(candidate_patch: str) -> list[dict[str, Any]]:
    """从状态写入和输出控制值变化中提取公开可观察义务。"""

    obligations: list[dict[str, Any]] = []
    removed_indexed: list[tuple[str, str]] = []
    added_lines = [
        line[1:].strip()
        for line in candidate_patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    for line in candidate_patch.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        match = re.search(r"\b([A-Za-z_]\w*)\s*\[\s*([^]]+)\s*\]\s*=", line[1:])
        if match:
            removed_indexed.append((match.group(1), match.group(2)))
    for target, index in dict.fromkeys(removed_indexed):
        normalized_index = re.sub(r"\W+", "_", index).strip("_") or "index"
        obligations.append(
            {
                "id": f"state_integrity_{target}_{normalized_index}",
                "predicate": f"removal of indexed state write {target}[{index}]",
                "variant": "state_integrity",
                "required_relation": "must_preserve",
                "case_hints": [["distinct", "non-degenerate", "normal", "普通", "非退化"]],
                "observable_hints": [
                    target,
                    "vertices",
                    "geometry",
                    "extent",
                    "shape",
                    "closure",
                    "cardinality",
                    "handle",
                    "consumer",
                    "状态",
                ],
                "command_hints": [
                    target,
                    ".xy",
                    "get_extents",
                    "vertices",
                    "_handles",
                    "get_xdata",
                    "get_ydata",
                ],
                "required_oracle_strength": "invariant",
            }
        )
        obligations.append(
            {
                "id": f"state_consumer_sync_{target}_{normalized_index}",
                "predicate": f"consumers synchronized after changing {target}[{index}] write",
                "variant": "state_consumer_sync",
                "required_relation": "must_preserve",
                "case_hints": [["update", "set_val", "second call", "再次更新", "更新"]],
                "observable_hints": [
                    "handle",
                    "consumer",
                    "get_xdata",
                    "get_ydata",
                    "public value",
                ],
                "command_hints": ["_handles", "get_xdata", "get_ydata"],
                "required_oracle_strength": "invariant",
            }
        )

    added_text = "\n".join(added_lines)
    fallback_assignments = re.findall(
        r"except\s+[^:\n]+:\s*\n\s*([A-Za-z_]\w*)\s*=\s*([-+]?\d+(?:\.\d+)?)",
        added_text,
    )
    for variable, value in dict.fromkeys(fallback_assignments):
        obligations.append(
            {
                "id": f"exact_output_{variable}",
                "predicate": f"new exception fallback assigns {variable}={value}",
                "variant": "output_exactness",
                "required_relation": "observe",
                "case_hints": [],
                "observable_hints": [
                    "exact return",
                    "exact string",
                    "formatted output",
                    "precision",
                    "精确字符串",
                    "格式",
                ],
                "command_hints": ["value_repr"],
                "required_oracle_strength": "exact",
            }
        )

    if any("related_name" in line for line in added_lines):
        obligations.append(
            {
                "id": "exact_generated_related_name",
                "predicate": "new generated related_name is public source output",
                "variant": "output_exactness",
                "required_relation": "observe",
                "case_hints": [["related_name"]],
                "observable_hints": ["exact generated line", "exact string", "related_name", "精确"],
                "command_hints": ["related_name", "value_repr"],
                "required_oracle_strength": "exact",
            }
        )
    return obligations


def extract_differential_obligations(candidate_patch: str) -> list[dict[str, Any]]:
    """合并 predicate 与 patch 影响面义务，并保持稳定顺序。"""

    values = [*extract_guard_obligations(candidate_patch), *extract_impact_obligations(candidate_patch)]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in values:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result


def patch_source_context(repo_dir: Path, candidate_patch: str, *, padding: int = 35, limit: int = 8_000) -> str:
    """读取 patch hunk 周围的 clean HEAD 源码，候选新增文本不能成为 oracle。"""

    current_file = ""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for line in candidate_patch.splitlines():
        match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if match:
            current_file = match.group(2)
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if current_file and hunk:
            start = int(hunk.group(1))
            length = max(1, int(hunk.group(2) or 1))
            ranges.setdefault(current_file, []).append((start, start + length - 1))
    chunks: list[str] = []
    for path, loci in ranges.items():
        full_path = (repo_dir / path).resolve()
        try:
            full_path.relative_to(repo_dir.resolve())
        except ValueError:
            continue
        try:
            clean = subprocess.run(
                ["git", "show", f"HEAD:{path}"],
                cwd=repo_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            clean = None
        if clean is not None and clean.returncode == 0:
            lines = clean.stdout.splitlines()
        elif full_path.is_file():
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            continue
        for start, end in loci:
            lo = max(1, start - padding)
            hi = min(len(lines), end + padding)
            body = "\n".join(f"{number}: {lines[number - 1]}" for number in range(lo, hi + 1))
            chunks.append(f"### {path}:{lo}-{hi}\n{body}")
    text = "\n\n".join(chunks)
    return text if len(text) <= limit else text[:limit] + "\n...<source context clipped>"


def guard_obligations_from_prompt(prompt: str) -> list[dict[str, Any]]:
    """读取 Test Expander prompt 中控制器注入的覆盖义务。"""

    match = re.search(
        re.escape(GUARD_COVERAGE_BEGIN)
        + r"\s*(.*?)\s*"
        + re.escape(GUARD_COVERAGE_END),
        prompt,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def trusted_oracle_evidence_from_prompt(prompt: str) -> str:
    match = re.search(
        re.escape(TRUSTED_ORACLE_BEGIN)
        + r"\s*(.*?)\s*"
        + re.escape(TRUSTED_ORACLE_END),
        prompt,
        re.DOTALL,
    )
    return match.group(1) if match else ""


def build_test_expander_prompt(
    *,
    task: str,
    candidate_patch: str,
    initial_validation: dict[str, Any],
    source_context: str = "",
) -> str:
    """构造公开差分输入生成任务。"""

    guard_obligations = extract_differential_obligations(candidate_patch)
    return f"""## Goal
为一个初始验证已通过、或 clean baseline 本来就通过而缺乏区分力的候选 patch，生成一份小型差分行为脚本。只设计测试输入，不修改代码，不判断最终分数。

## Trusted Oracle Evidence
只有本区块可以支持 exact oracle。Candidate Patch 及其新增源码绝不是 oracle。
{TRUSTED_ORACLE_BEGIN}
### Public Task
{task[:6000]}

### Clean Source Context
以下内容来自 git HEAD，候选 patch 的新增文本不在其中。
{source_context[:8000] or "(not available)"}
{TRUSTED_ORACLE_END}

## Candidate Patch
```diff
{candidate_patch[:6000]}
```

## Initial Public Validation
{json.dumps(initial_validation, ensure_ascii=False, indent=2)[:6000]}

## Required Guard Coverage
以下义务由控制器从候选 diff 确定性提取。每项都必须映射到至少一个真实 case；一个 case 可以覆盖多项。
{GUARD_COVERAGE_BEGIN}
{json.dumps(guard_obligations, ensure_ascii=False, indent=2)}
{GUARD_COVERAGE_END}

## Rules
- 只能依据公开题意、当前生产源码和仓库中的公开测试；禁止 hints、gold patch、hidden tests、harness 日志及其测试名称。
- Initial Public Validation 可能是无效 reproducer；不要沿用其单一输入或 oracle，必须从 Public Task 和源码条件重新提取能区分错误 guard 的输入维度。
- 不要求提前知道所有精确 oracle。生成 2-{MAX_DIFFERENTIAL_CASES} 个有区分度的输入，分别观察 baseline 与 candidate 输出；没有可信 exact oracle 时明确标记为 differential_only。
- 至少包含一个公开问题输入，以及一个由当前 patch 修改的 guard/predicate 直接影响的旧行为或边界输入。
- 若 patch 修改复合 guard，必须逐项检查决定该分支的语义维度；结构类型检查与语义属性判断不等价时，加入带 assumptions 的符号输入；正负性影响运算合法性时，加入相反符号边界。
- Required Guard Coverage 是控制器硬约束：`required_relation` 不得降级为 `observe`；`case_hints` 是必须同时满足的 AND 分组。测试某一 predicate 时，要让同一复合 guard 的其他 predicate 保持为真，只隔离该维度。
- 优先复用公开测试中的构造方式。涉及类型、属性假设、shape、正负、None/unknown 或生产者/消费者时，选择能区分这些路径的输入。
- 每个输入独立 try/except，单个失败不能阻止后续 case。
- 每个 case 标注 `role` 和 `oracle_strength`。`must_change` 只说明候选应改变 baseline，不能证明改变后的结果正确；没有精确 expected_after 时必须使用 `differential_only`，不得伪造 exact oracle。
- `exact` 必须给出可与结构化 observation 逐字段比较的 `expected_after`，`oracle_source` 只能以 `public_task:`、`public_test:` 或 `clean_source:` 开头，并在 `oracle_quote` 逐字摘录 Trusted Oracle Evidence 中支持该期望的短句。候选 patch、diff、hunk、proposed code 不能成为 exact oracle。`invariant` 表示 candidate 必须与 baseline 的完整 observation 一致。
- Required Guard Coverage 中的 `observable_hints` 描述必须观察的公开状态，`command_hints` 必须真实出现在脚本中。删除 indexed mutation 时，不能只检查“未抛异常”或另一个未被修改的属性；必须记录 shape/cardinality/closure/geometry 等被修改状态。
- 仓库邻近分支只能作为 oracle 前先证明适用条件相同。非有限值 fallback 不能直接证明有限值异常分支应使用同一常量。
- 命令必须是单条安全的 `python -c`，不能写文件、联网、安装依赖、使用 shell 管道或读取 SWE-bench 元数据。
- 每个 case 必须恰好打印一行：`REPO_AGENT_CASE=` 后紧跟 JSON。
- 打印 JSON 的 `case` 必须逐字等于对应 `cases[].id`，不得使用 1/2/3/4 等序号代替。
- JSON 必须包含 `case`、`status`；成功时 `status` 必须为 `ok` 并包含 `value_type`、`value_repr`，异常时必须为 `error` 并包含 `exception_type`、`exception_message`。
- 不要使用 assert；该脚本只采集行为，后续 Judge 会结合 issue、源码惯例和 baseline/candidate 差分判断有效性。

最终只输出 JSON：
{{
  "cases": [
    {{
      "id": "稳定 case id",
      "input": "输入/调用摘要",
      "why": "它区分哪个公开语义或回归风险",
      "evidence": "issue|repository_code|public_test:位置",
      "role": "target|invariant|exploratory",
      "observable": "脚本实际记录的公开返回值、异常、诊断或状态",
      "expected_relation": "must_change|must_preserve|observe",
      "oracle_strength": "exact|invariant|differential_only",
      "expected_after": {{"status":"ok","value_repr":"仅 exact 时填写真实期望"}},
      "oracle_source": "public_task:位置|public_test:位置|clean_source:位置|unknown",
      "oracle_quote": "仅 exact 时填写 Trusted Oracle Evidence 中的逐字短句"
    }}
  ],
  "guard_coverage": [
    {{
      "obligation_id": "Required Guard Coverage 中的 id",
      "case_ids": ["覆盖该义务的 case id"]
    }}
  ],
  "command": "python -c ...",
  "limitations": ["公开信息仍无法判断的行为"]
}}
"""


def _exact_oracle_provenance_errors(
    case: dict[str, Any],
    trusted_oracle_evidence: str,
) -> list[str]:
    source = str(case.get("oracle_source") or "").strip().lower()
    quote = str(case.get("oracle_quote") or "").strip()
    errors: list[str] = []
    if not source.startswith(("public_task:", "public_test:", "clean_source:")):
        errors.append(
            "exact oracle_source must start with public_task:, public_test:, or clean_source:"
        )
    if any(marker in source for marker in ("candidate", "patch", "diff", "hunk", "proposed")):
        errors.append("candidate-derived source cannot support an exact oracle")
    if not quote:
        errors.append("exact oracle needs a non-empty oracle_quote")
    elif trusted_oracle_evidence:
        normalized_quote = " ".join(quote.split()).lower()
        normalized_evidence = " ".join(trusted_oracle_evidence.split()).lower()
        if normalized_quote not in normalized_evidence:
            errors.append("oracle_quote is not present in Trusted Oracle Evidence")
    return errors


def parse_differential_test_plan(
    text: str,
    required_obligations: list[dict[str, Any]] | None = None,
    trusted_oracle_evidence: str = "",
) -> dict[str, Any] | None:
    payload = extract_json_object(text)
    if not isinstance(payload, dict):
        return None
    cases = payload.get("cases")
    command = str(payload.get("command") or "").strip()
    if not isinstance(cases, list) or not 2 <= len(cases) <= MAX_DIFFERENTIAL_CASES:
        return None
    normalized_cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in cases:
        if not isinstance(raw, dict):
            return None
        case_id = str(raw.get("id") or "").strip()
        if not case_id or case_id in seen:
            return None
        expected_relation = str(raw.get("expected_relation") or "").strip()
        if expected_relation not in EXPECTED_RELATIONS:
            return None
        role = str(raw.get("role") or _default_case_role(expected_relation)).strip()
        if role not in CASE_ROLES:
            return None
        oracle_strength = str(
            raw.get("oracle_strength") or _default_oracle_strength(expected_relation)
        ).strip()
        if oracle_strength not in ORACLE_STRENGTHS:
            return None
        if oracle_strength == "exact" and _exact_oracle_provenance_errors(
            raw,
            trusted_oracle_evidence,
        ):
            return None
        seen.add(case_id)
        normalized_cases.append(
            {
                "id": case_id,
                "input": str(raw.get("input") or "").strip(),
                "why": str(raw.get("why") or "").strip(),
                "evidence": str(raw.get("evidence") or "").strip(),
                "role": role,
                "observable": str(raw.get("observable") or "").strip(),
                "expected_relation": expected_relation,
                "oracle_strength": oracle_strength,
                "expected_after": _normalize_expected_after(raw.get("expected_after")),
                "oracle_source": str(raw.get("oracle_source") or "unknown").strip(),
                "oracle_quote": str(raw.get("oracle_quote") or "").strip(),
            }
        )
    reason = differential_command_reason(command, seen)
    if reason:
        return None
    coverage = _normalize_guard_coverage(
        payload.get("guard_coverage"),
        normalized_cases,
        required_obligations or [],
    )
    if coverage is None:
        return None
    command_lower = command.lower()
    for obligation in required_obligations or []:
        command_hints = [str(value).lower() for value in obligation.get("command_hints") or []]
        if command_hints and not any(value in command_lower for value in command_hints):
            return None
    return {
        "cases": normalized_cases,
        "guard_coverage": coverage,
        "guard_obligations": list(required_obligations or []),
        "guard_coverage_complete": True,
        "command": command,
        "limitations": [
            str(value).strip()
            for value in payload.get("limitations") or []
            if str(value).strip()
        ],
    }


def differential_plan_validation_errors(
    text: str,
    required_obligations: list[dict[str, Any]] | None = None,
    trusted_oracle_evidence: str = "",
) -> list[str]:
    """返回可直接反馈给 Test Expander 的精确 schema/coverage 错误。"""

    obligations = required_obligations or []
    payload = extract_json_object(text)
    if not isinstance(payload, dict):
        return ["output is not one JSON object"]
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 2 <= len(cases) <= MAX_DIFFERENTIAL_CASES:
        return [f"cases must contain 2-{MAX_DIFFERENTIAL_CASES} items"]
    case_by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] is not an object")
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in case_by_id:
            errors.append(f"cases[{index}] needs a unique non-empty id")
            continue
        case_by_id[case_id] = case
        relation = str(case.get("expected_relation") or "")
        if relation not in EXPECTED_RELATIONS:
            errors.append(f"{case_id}: expected_relation must be must_change, must_preserve, or observe")
        role = str(case.get("role") or _default_case_role(relation))
        if role not in CASE_ROLES:
            errors.append(f"{case_id}: role must be target, invariant, or exploratory")
        oracle_strength = str(
            case.get("oracle_strength") or _default_oracle_strength(relation)
        )
        if oracle_strength not in ORACLE_STRENGTHS:
            errors.append(f"{case_id}: oracle_strength must be exact, invariant, or differential_only")
        if oracle_strength == "exact":
            if not _normalize_expected_after(case.get("expected_after")):
                errors.append(f"{case_id}: exact oracle needs a non-empty expected_after object")
            errors.extend(
                f"{case_id}: {error}"
                for error in _exact_oracle_provenance_errors(
                    case,
                    trusted_oracle_evidence,
                )
            )
    command = str(payload.get("command") or "").strip()
    command_error = differential_command_reason(command, set(case_by_id))
    if command_error:
        errors.append(f"command: {command_error}")

    raw_coverage = payload.get("guard_coverage")
    if obligations and not isinstance(raw_coverage, list):
        errors.append("guard_coverage must be an array covering every required obligation")
        return errors
    coverage_by_id: dict[str, list[str]] = {}
    for item in raw_coverage or []:
        if not isinstance(item, dict):
            errors.append("every guard_coverage item must be an object")
            continue
        obligation_id = str(item.get("obligation_id") or "").strip()
        case_ids = [str(value).strip() for value in item.get("case_ids") or []]
        if not obligation_id or not case_ids:
            errors.append("every guard_coverage item needs obligation_id and non-empty case_ids")
            continue
        coverage_by_id[obligation_id] = list(dict.fromkeys(case_ids))
        unknown = [case_id for case_id in case_ids if case_id not in case_by_id]
        if unknown:
            errors.append(f"{obligation_id}: unknown case_ids {unknown}")
    obligation_by_id = {str(item.get("id") or ""): item for item in obligations}
    missing = sorted(set(obligation_by_id) - set(coverage_by_id))
    extra = sorted(set(coverage_by_id) - set(obligation_by_id))
    if missing:
        errors.append("missing guard obligations: " + ", ".join(missing))
    if extra:
        errors.append("unknown guard obligations: " + ", ".join(extra))
    for obligation_id, obligation in obligation_by_id.items():
        case_ids = coverage_by_id.get(obligation_id) or []
        if not case_ids or any(case_id not in case_by_id for case_id in case_ids):
            continue
        if obligation.get("variant") != "predicate_path" and len(case_ids) != 1:
            errors.append(f"{obligation_id}: specialized obligation must map to exactly one case")
            continue
        mapped_text = " ".join(
            f"{case_by_id[case_id].get('input', '')} {case_by_id[case_id].get('why', '')} "
            f"{case_by_id[case_id].get('observable', '')}".lower()
            for case_id in case_ids
        )
        for alternatives in obligation.get("case_hints") or []:
            if not any(_hint_present(mapped_text, str(value)) for value in alternatives):
                errors.append(
                    f"{obligation_id}: mapped case must include one of {alternatives}"
                )
        relation = str(obligation.get("required_relation") or "observe")
        if relation != "observe" and not any(
            str(case_by_id[case_id].get("expected_relation") or "") == relation
            for case_id in case_ids
        ):
            errors.append(f"{obligation_id}: mapped case must use expected_relation={relation}")
        observable_hints = [str(value).lower() for value in obligation.get("observable_hints") or []]
        if observable_hints and not any(_hint_present(mapped_text, value) for value in observable_hints):
            errors.append(
                f"{obligation_id}: mapped case observable must include one of {observable_hints}"
            )
        required_oracle = str(obligation.get("required_oracle_strength") or "")
        if required_oracle and not any(
            str(case_by_id[case_id].get("oracle_strength") or _default_oracle_strength(
                str(case_by_id[case_id].get("expected_relation") or "observe")
            )) == required_oracle
            for case_id in case_ids
        ):
            errors.append(f"{obligation_id}: mapped case must use oracle_strength={required_oracle}")
        command_hints = [str(value).lower() for value in obligation.get("command_hints") or []]
        command_lower = command.lower()
        if command_hints and not any(value in command_lower for value in command_hints):
            errors.append(f"{obligation_id}: command must observe one of {command_hints}")
    return errors


def _default_case_role(relation: str) -> str:
    if relation == "must_preserve":
        return "invariant"
    if relation == "must_change":
        return "target"
    return "exploratory"


def _default_oracle_strength(relation: str) -> str:
    return "invariant" if relation == "must_preserve" else "differential_only"


def _normalize_expected_after(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "status",
        "value_type",
        "value_repr",
        "exception_type",
        "exception_message",
    }
    return {
        str(key): str(item)
        for key, item in value.items()
        if key in allowed and str(item).strip()
    }


def _normalize_guard_coverage(
    raw_coverage: Any,
    cases: list[dict[str, Any]],
    obligations: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    if not obligations:
        return []
    if not isinstance(raw_coverage, list):
        return None
    case_by_id = {case["id"]: case for case in cases}
    coverage_by_id: dict[str, list[str]] = {}
    for item in raw_coverage:
        if not isinstance(item, dict):
            return None
        obligation_id = str(item.get("obligation_id") or "").strip()
        case_ids = [str(value).strip() for value in item.get("case_ids") or []]
        if (
            not obligation_id
            or not case_ids
            or any(case_id not in case_by_id for case_id in case_ids)
        ):
            return None
        coverage_by_id[obligation_id] = list(dict.fromkeys(case_ids))
    required_ids = {str(item.get("id") or "") for item in obligations}
    if not required_ids or set(coverage_by_id) != required_ids:
        return None
    obligation_by_id = {str(item["id"]): item for item in obligations}
    normalized: list[dict[str, Any]] = []
    for obligation_id in sorted(required_ids):
        case_ids = coverage_by_id[obligation_id]
        obligation = obligation_by_id[obligation_id]
        if obligation.get("variant") != "predicate_path" and len(case_ids) != 1:
            return None
        text = " ".join(
            f"{case_by_id[case_id]['input']} {case_by_id[case_id]['why']} "
            f"{case_by_id[case_id].get('observable', '')}".lower()
            for case_id in case_ids
        )
        for alternatives in obligation.get("case_hints") or []:
            if not any(_hint_present(text, str(value)) for value in alternatives):
                return None
        required_relation = str(obligation.get("required_relation") or "observe")
        if required_relation != "observe" and not any(
            case_by_id[case_id]["expected_relation"] == required_relation
            for case_id in case_ids
        ):
            return None
        observable_hints = [str(value).lower() for value in obligation.get("observable_hints") or []]
        if observable_hints and not any(_hint_present(text, value) for value in observable_hints):
            return None
        required_oracle = str(obligation.get("required_oracle_strength") or "")
        if required_oracle and not any(
            case_by_id[case_id].get("oracle_strength") == required_oracle
            for case_id in case_ids
        ):
            return None
        normalized.append(
            {
                "obligation_id": obligation_id,
                "case_ids": case_ids,
                "required_relation": required_relation,
            }
        )
    return normalized


def differential_command_reason(command: str, required_case_ids: set[str]) -> str | None:
    safety_reason = verification_command_safety_reason(command)
    if safety_reason:
        return safety_reason
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "invalid shell quoting"
    if not tokens or tokens[0] not in {"python", "python3"} or "-c" not in tokens:
        return "differential command must use python -c"
    source_index = tokens.index("-c") + 1
    if source_index >= len(tokens):
        return "python -c source is missing"
    source = tokens[source_index]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"invalid Python syntax: {exc.msg}"
    if CASE_PREFIX not in source:
        return f"command must print {CASE_PREFIX} records"
    dangerous_imports = {"requests", "socket", "subprocess", "urllib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".", 1)[0] in dangerous_imports for alias in node.names):
                return "network/process imports are forbidden"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".", 1)[0] in dangerous_imports:
                return "network/process imports are forbidden"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                if node.func.attr in {"system", "popen", "remove", "unlink", "rename"}:
                    return "filesystem/process calls are forbidden"
    missing = [case_id for case_id in required_case_ids if case_id not in source]
    if missing:
        return "command does not emit every declared case: " + ", ".join(sorted(missing))
    return None


def extract_case_observations(output: str) -> dict[str, dict[str, str]]:
    observations: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        marker = line.find(CASE_PREFIX)
        if marker < 0:
            continue
        raw = line[marker + len(CASE_PREFIX) :].strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        case_id = str(value.get("case") or "").strip()
        raw_status = str(value.get("status") or "").strip().lower()
        status = {
            "ok": "ok",
            "success": "ok",
            "passed": "ok",
            "error": "error",
            "exception": "error",
            "failed": "error",
        }.get(raw_status)
        if not case_id or status is None:
            continue
        value["status"] = status
        observations[case_id] = {
            str(key): str(item)
            for key, item in value.items()
            if key
            in {
                "case",
                "status",
                "value_type",
                "value_repr",
                "exception_type",
                "exception_message",
            }
        }
    return observations


def assess_differential_validation(
    *,
    plan: dict[str, Any],
    baseline: dict[str, Any],
    patched: dict[str, Any],
) -> dict[str, Any]:
    """把同一脚本的 baseline/candidate 输出压缩为供 FailureReview 判断的矩阵。"""

    command = str(plan.get("command") or "")
    declared = [str(case.get("id") or "") for case in plan.get("cases") or []]
    base_output = str(baseline.get("evidence") or baseline.get("output") or "")
    patch_output = str(patched.get("evidence") or patched.get("output") or "")
    base_cases = extract_case_observations(base_output)
    patch_cases = extract_case_observations(patch_output)
    missing = [
        case_id
        for case_id in declared
        if case_id not in base_cases or case_id not in patch_cases
    ]
    executable = (
        baseline.get("exit_code") == 0
        and patched.get("exit_code") == 0
        and not missing
        and bool(declared)
    )
    matrix: list[dict[str, Any]] = []
    for case in plan.get("cases") or []:
        case_id = str(case.get("id") or "")
        before = base_cases.get(case_id)
        after = patch_cases.get(case_id)
        relation = str(case.get("expected_relation") or "observe")
        role = str(case.get("role") or _default_case_role(relation))
        oracle_strength = str(
            case.get("oracle_strength") or _default_oracle_strength(relation)
        )
        expected_after = _normalize_expected_after(case.get("expected_after"))
        changed = bool(before is not None and after is not None and before != after)
        relation_passed = (
            changed
            if relation == "must_change"
            else not changed
            if relation == "must_preserve"
            else None
        )
        oracle_valid = bool(
            oracle_strength == "invariant"
            or (oracle_strength == "exact" and expected_after)
        )
        oracle_passed = (
            bool(before is not None and after is not None and before == after)
            if oracle_strength == "invariant"
            else _observation_matches(after, expected_after)
            if oracle_strength == "exact" and expected_after
            else None
        )
        baseline_matches_expected = bool(
            oracle_strength == "exact"
            and expected_after
            and _observation_matches(before, expected_after)
        )
        matrix.append(
            {
                "case": case_id,
                "input": case.get("input") or "",
                "why": case.get("why") or "",
                "evidence": case.get("evidence") or "",
                "role": role,
                "observable": case.get("observable") or "",
                "expected_relation": relation,
                "relation_passed": relation_passed,
                "oracle_strength": oracle_strength,
                "expected_after": expected_after,
                "oracle_source": case.get("oracle_source") or "unknown",
                "oracle_valid": oracle_valid,
                "oracle_passed": oracle_passed,
                "baseline_matches_expected": baseline_matches_expected,
                "baseline": before,
                "patched": after,
                "changed": changed,
            }
        )
    required_rows = [row for row in matrix if row.get("role") in {"target", "invariant"}]
    target_rows = [row for row in required_rows if row.get("role") == "target"]
    oracle_gaps = [
        str(row.get("case") or "")
        for row in required_rows
        if not row.get("oracle_valid")
    ]
    oracle_violations = [
        str(row.get("case") or "")
        for row in required_rows
        if row.get("oracle_valid") and row.get("oracle_passed") is False
    ]
    invalid_target_cases = [
        str(row.get("case") or "")
        for row in target_rows
        if row.get("baseline_matches_expected")
    ]
    oracle_complete = bool(
        executable
        and target_rows
        and not oracle_gaps
        and not oracle_violations
        and not invalid_target_cases
    )
    status = "differential_review_required" if executable else "invalid_differential_probe"
    failure_evidence = json.dumps(
        {
            "kind": "public_differential_trace",
            "case_matrix": matrix,
            "limitations": plan.get("limitations") or [],
            "guard_coverage": plan.get("guard_coverage") or [],
            "guard_coverage_complete": bool(plan.get("guard_coverage_complete")),
            "missing_cases": missing,
            "oracle_complete": oracle_complete,
            "oracle_gaps": oracle_gaps,
            "oracle_violations": oracle_violations,
            "invalid_target_cases": invalid_target_cases,
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "validation_kind": "differential_trace",
        "status": status,
        "command": command,
        "command_kind": "behavior_trace",
        "baseline": dict(baseline),
        "patched": dict(patched),
        "test_plan": dict(plan),
        "case_matrix": matrix,
        "oracle_complete": oracle_complete,
        "oracle_gaps": oracle_gaps,
        "oracle_violations": oracle_violations,
        "invalid_target_cases": invalid_target_cases,
        "feedback_eligible": executable,
        "reason": (
            "the same public differential script ran on baseline and candidate; "
            "FailureReview must classify complete, partial, regression, or invalid cases"
            if executable
            else "differential script did not produce a complete comparable case matrix"
        ),
        "failure_evidence": failure_evidence,
    }


def _observation_matches(
    observation: dict[str, str] | None,
    expected: dict[str, str],
) -> bool:
    if not observation or not expected:
        return False
    return all(str(observation.get(key) or "") == str(value) for key, value in expected.items())
