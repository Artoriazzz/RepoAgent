from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from repo_agent.core.verification import classify_verification_command, verification_command_safety_reason


SUPPORTED_EVIDENCE_TYPES = {"issue", "code", "public_test", "execution"}


def normalize_semantic_evidence(
    evidence: dict[str, Any] | None,
    problem_statement: str,
) -> dict[str, Any] | None:
    """归一化能由公开题意和源码证据确定的 schema 值，不改写模型的修复结论。"""

    if not isinstance(evidence, dict):
        return evidence
    normalized = deepcopy(evidence)
    for item in normalized.get("verify_commands") or []:
        if isinstance(item, dict) and isinstance(item.get("command"), str):
            item["command"] = _strip_absolute_repo_cd(item["command"])
    lowered = problem_statement.lower()
    star_identity_problem = ("*args" in lowered or "**kwargs" in lowered) and "autodoc_typehints" in lowered
    if not star_identity_problem:
        return normalized

    for edge in normalized.get("contract_edges") or []:
        if not isinstance(edge, dict):
            continue
        canonical_identity = str(edge.get("current_canonical_identity") or "").lower()
        if "plain" in canonical_identity or "bare" in canonical_identity or "param.name" in canonical_identity:
            # canonical storage 已由代码证据证明为无星号键时，星号只能作为 consumer/display alias。
            edge["preferred_change_layer"] = "consumer"
    return normalized


def extract_public_problem_statement(task: str) -> str:
    """从 batch task 中取出公开题意，避免通用说明触发语义规则。"""

    match = re.search(
        r"## Problem Statement\s*\n(.*?)(?=\n## Instructions\s*\n|\Z)",
        task,
        flags=re.S | re.I,
    )
    return (match.group(1) if match else task).strip()


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I))
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                # 部分模型会在 JSON 字符串里的内联验证命令中输出未转义换行。
                # strict=False 仍要求完整 JSON 结构，只放宽字符串控制字符，避免丢掉整份证据。
                value = json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            nested = value.get("semantic_evidence")
            return nested if isinstance(nested, dict) else value
    return None


def validate_semantic_evidence(evidence: dict[str, Any] | None, problem_statement: str) -> list[str]:
    """验证证据结构与公开题意覆盖；不把模型推理本身当作事实。"""

    if not evidence:
        return ["semantic_evidence JSON is missing"]

    issues: list[str] = []
    contract = evidence.get("reproduction_contract")
    if not isinstance(contract, dict):
        issues.append("reproduction_contract must be an object")
    else:
        if not _nonempty(contract.get("trigger")):
            issues.append("reproduction_contract.trigger is missing")
        if not _nonempty(contract.get("expected_behavior")):
            issues.append("reproduction_contract.expected_behavior is missing")

    partitions = evidence.get("target_partitions")
    if not isinstance(partitions, list) or not partitions:
        issues.append("target_partitions must contain at least one public behavior case")
        partitions = []

    seen_ids: set[str] = set()
    for index, partition in enumerate(partitions, 1):
        if not isinstance(partition, dict):
            issues.append(f"target_partitions[{index}] must be an object")
            continue
        case_id = str(partition.get("id") or "").strip()
        if not case_id:
            issues.append(f"target_partitions[{index}].id is missing")
        elif case_id in seen_ids:
            issues.append(f"duplicate target partition id: {case_id}")
        else:
            seen_ids.add(case_id)
        if not _nonempty(partition.get("expected")):
            issues.append(f"target partition {case_id or index} has no expected behavior")
        if not _valid_evidence_refs(partition.get("evidence")):
            issues.append(
                f"target partition {case_id or index} needs issue/code/public_test/execution evidence with a location"
            )

    lowered = problem_statement.lower()
    star_identity_problem = ("*args" in lowered or "**kwargs" in lowered) and "autodoc_typehints" in lowered
    if "pandas" in lowered and "nullable" in lowered and "dtype" in lowered:
        representations = {
            _partition_representation(item)
            for item in partitions
            if isinstance(item, dict)
        }
        if not any("series" in value or "1d" in value for value in representations):
            issues.append("nullable pandas evidence is missing the issue Series representation")
        if not any("dataframe" in value or "2d" in value for value in representations):
            issues.append("nullable pandas evidence is missing the adjacent DataFrame/2D representation")
        missing_covered = any(
            _meaningful_missing_behavior(item.get("missing_value_behavior"))
            for item in partitions
            if isinstance(item, dict)
        )
        if not missing_covered:
            issues.append("nullable pandas evidence must state the missing-value behavior")
        for index, item in enumerate(partitions, 1):
            if not isinstance(item, dict):
                continue
            case_id = str(item.get("id") or index)
            if not _first_predicate(item):
                issues.append(f"nullable pandas target partition {case_id} must locate its first dependent predicate")
            expected = str(item.get("expected") or "").lower()
            if _ambiguous_expected_behavior(expected):
                issues.append(
                    f"nullable pandas target partition {case_id} must state one exact expected public classification"
                )

    if star_identity_problem:
        edges = evidence.get("contract_edges")
        if not isinstance(edges, list) or not edges:
            issues.append("star-argument identity changes require producer/consumer contract_edges")
        else:
            consumers_by_producer: dict[str, set[str]] = {}
            for index, edge in enumerate(edges, 1):
                if not isinstance(edge, dict):
                    issues.append(f"contract_edges[{index}] must be an object")
                    continue
                producer = str(edge.get("producer") or "").strip()
                if not producer:
                    issues.append(f"contract_edges[{index}].producer is missing")
                if not _nonempty(edge.get("current_canonical_identity")):
                    issues.append(f"contract_edges[{index}].current_canonical_identity is missing")
                canonical_identity = str(edge.get("current_canonical_identity") or "").lower()
                preferred_layer = str(edge.get("preferred_change_layer") or "").lower()
                if ("plain" in canonical_identity or "param.name" in canonical_identity) and "consumer" not in preferred_layer:
                    issues.append(
                        f"contract_edges[{index}] must preserve the plain canonical producer identity and prefer a consumer adapter"
                    )
                if (
                    ("plain" in canonical_identity or "param.name" in canonical_identity)
                    and "consumer" in preferred_layer
                    and _partition_requires_decorated_producer(partitions, str(edge.get("producer") or ""))
                ):
                    issues.append(
                        f"contract_edges[{index}] conflicts with target_partitions: producer canonical identity is plain "
                        "and the preferred change layer is consumer, so no producer partition may require decorated storage keys"
                    )
                consumers = edge.get("consumers")
                if not isinstance(consumers, list) or not consumers:
                    issues.append(f"contract_edges[{index}] must cite at least one direct consumer")
                elif not all(isinstance(value, dict) and _nonempty(value.get("location")) for value in consumers):
                    issues.append(f"contract_edges[{index}] consumer locations are incomplete")
                else:
                    producer_key = producer.lower()
                    consumer_keys = consumers_by_producer.setdefault(producer_key, set())
                    for consumer_index, consumer in enumerate(consumers, 1):
                        consumer_keys.add(
                            str(consumer.get("symbol") or consumer.get("location") or "").strip().lower()
                        )
                        if not _nonempty(consumer.get("lookup_identity")):
                            issues.append(
                                f"contract_edges[{index}].consumers[{consumer_index}].lookup_identity is missing"
                            )
                        if not _nonempty(consumer.get("output_identity")):
                            issues.append(
                                f"contract_edges[{index}].consumers[{consumer_index}].output_identity is missing"
                            )
                if not _valid_evidence_refs(edge.get("evidence"), require_code=True):
                    issues.append(f"contract_edges[{index}] needs code evidence for the current producer contract")
            for producer, consumers in consumers_by_producer.items():
                consumers.discard("")
                if len(consumers) < 2:
                    issues.append(
                        f"contract_edges for producer {producer} must collectively cite both direct consumers"
                    )

    required_ids = required_case_ids(evidence)
    commands = evidence.get("verify_commands")
    if not isinstance(commands, list) or not commands:
        issues.append("verify_commands must contain a directly executable public behavior command")
    else:
        covered: set[str] = set()
        executable = False
        single_command = len(commands) == 1
        for index, item in enumerate(commands, 1):
            command = item.get("command") if isinstance(item, dict) else item
            covers = item.get("covers") if isinstance(item, dict) else []
            if not isinstance(command, str) or not command.strip():
                issues.append(f"verify_commands[{index}].command is missing")
                continue
            reason = verification_command_safety_reason(command)
            if reason:
                issues.append(f"verify_commands[{index}] is unsafe: {reason}")
                continue
            if classify_verification_command(command) != "behavior":
                issues.append(f"verify_commands[{index}] does not directly execute a public behavior check")
                continue
            if _looks_like_simulated_internal_verification(command):
                issues.append(
                    f"verify_commands[{index}] simulates internal data flow or asserts pre-fix behavior instead of "
                    "verifying the requested post-fix behavior"
                )
                continue
            if _asserts_nullable_unknown_behavior(command, lowered):
                issues.append(
                    f"verify_commands[{index}] asserts the pre-fix nullable unknown/error result; it must assert "
                    "the requested post-fix classification and successful public call"
                )
                continue
            if star_identity_problem and _expects_bare_star_param_to_exist(command):
                issues.append(
                    f"verify_commands[{index}] collapses lookup and rendered identities: bare param args/kwargs "
                    "must be absent while starred param fields remain"
                )
                continue
            if star_identity_problem and _expects_bare_star_type_output(command):
                issues.append(
                    f"verify_commands[{index}] drops the matched display identity: generated type fields for "
                    "starred parameters must remain type *args/type **kwargs, not bare type args/type kwargs"
                )
                continue
            if star_identity_problem and _only_checks_star_field_helpers(command):
                issues.append(
                    f"verify_commands[{index}] only checks intermediate field-list helpers; starred parameter "
                    "verification must execute the final Sphinx/rendering consumer and assert the rendered type"
                )
                continue
            executable = True
            if single_command:
                # covers 是调度元数据，不是行为证据。唯一命令已经通过安全与 behavior
                # 分类后，由框架绑定全部 required case，避免模型在 partition/differential
                # 两套等价 ID 之间产生纯格式失败。
                covered.update(required_ids)
            elif isinstance(covers, list):
                covered.update(str(value) for value in covers)
        if not executable:
            issues.append("no verify command directly executes behavioral assertions")
        missing = sorted(required_ids - covered)
        if missing:
            issues.append("verify_commands do not cover required cases: " + ", ".join(missing))
        if executable and build_verification_plan(evidence) is None:
            issues.append("one verify command must directly cover every required semantic case")
    return _unique(issues)


def required_case_ids(evidence: dict[str, Any] | None) -> set[str]:
    if not isinstance(evidence, dict):
        return set()
    # target_partitions 描述语义空间，differential_cases 才是可直接执行的具体行为。
    # 模型经常用二者各自的 ID，强迫 verify 同时重复两套 ID 只会制造格式失败。
    differential_ids: set[str] = set()
    for case in evidence.get("differential_cases") or []:
        if not isinstance(case, dict) or case.get("required", True) is False:
            continue
        case_id = _case_id(case)
        if case_id:
            differential_ids.add(case_id)
    if differential_ids:
        return differential_ids

    partition_ids: set[str] = set()
    for partition in evidence.get("target_partitions") or []:
        if not isinstance(partition, dict) or partition.get("required", True) is False:
            continue
        case_id = _case_id(partition)
        if case_id:
            partition_ids.add(case_id)
    return partition_ids


def build_verification_plan(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 SemanticEvidence 选择一条可独立覆盖全部必需 case 的公开命令。"""

    if not isinstance(evidence, dict):
        return None
    required = required_case_ids(evidence)
    commands = evidence.get("verify_commands")
    if not required or not isinstance(commands, list):
        return None
    partitions = [value for value in evidence.get("target_partitions") or [] if isinstance(value, dict)]
    nullable_problem = _has_nullable_dtype_partition(partitions)
    star_identity_problem = _has_consumer_display_alias_contract(evidence)
    single_command = len(commands) == 1
    for item in commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        covers = {str(value).strip() for value in item.get("covers") or [] if str(value).strip()}
        if not command:
            continue
        if verification_command_safety_reason(command):
            continue
        if classify_verification_command(command) != "behavior":
            continue
        if _looks_like_simulated_internal_verification(command):
            continue
        if nullable_problem and _asserts_nullable_unknown_behavior(command, "pandas nullable"):
            continue
        if star_identity_problem and (
            _expects_bare_star_param_to_exist(command)
            or _expects_bare_star_type_output(command)
            or _only_checks_star_field_helpers(command)
        ):
            continue
        if not single_command and not required.issubset(covers):
            continue
        return {"command": command, "covers": sorted(required)}
    return None


def format_semantic_evidence(evidence: dict[str, Any] | None, issues: list[str], max_chars: int = 8_000) -> str:
    payload = {"semantic_evidence": evidence or {}, "validation_issues": issues}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text if len(text) <= max_chars else text[:max_chars] + "\n...<truncated>"


def patch_evidence_alignment_issues(patch: str, evidence: dict[str, Any] | None) -> list[str]:
    """捕获证据已经能证明的典型错层、表示遗漏与过宽转换。"""

    if not patch.strip() or not isinstance(evidence, dict):
        return []
    lowered = patch.lower()
    issues: list[str] = []
    partitions = [value for value in evidence.get("target_partitions") or [] if isinstance(value, dict)]
    representations = {_partition_representation(value) for value in partitions}
    if any("dataframe" in value or "2d" in value for value in representations):
        single_dtype_guard = bool(re.search(r"^\+.*hasattr\([^\n]*['\"]dtype['\"]", patch, flags=re.M))
        supports_table = ".dtypes" in lowered or "check_array" in lowered
        if single_dtype_guard and not supports_table:
            issues.append("semantic_representation_gap:dataframe_not_covered_by_single_dtype_guard")
    if "dtype=np.float64" in lowered and "dtype == object" in lowered:
        issues.append("semantic_overgeneralization:all_numeric_object_arrays_coerced_to_float64")
    if any(_meaningful_missing_behavior(value.get("missing_value_behavior")) for value in partitions):
        if "except (valueerror, typeerror)" in lowered and re.search(r"^\+\s*pass\s*$", patch, flags=re.M):
            issues.append("semantic_representation_gap:missing_values_fall_back_to_old_unknown_path")
    if _has_nullable_dtype_partition(partitions) and _adds_first_element_semantic_inference(patch):
        issues.append("semantic_representation_gap:first_element_used_as_container_dtype_proxy")

    predicate_locations = _first_predicate_locations(partitions)
    normalization_locations = _added_normalization_locations(patch)
    for path, predicate_line, symbol in predicate_locations:
        additions = normalization_locations.get(path, [])
        if additions and min(additions) > predicate_line:
            issues.append(
                "semantic_order_violation:normalization_after_first_dependent_predicate:"
                f"{path}:{symbol}:{predicate_line}"
            )

    for edge in evidence.get("contract_edges") or []:
        if not isinstance(edge, dict):
            continue
        preferred_layer = str(edge.get("preferred_change_layer") or "").lower()
        current_identity = str(edge.get("current_canonical_identity") or "").lower()
        producer = str(edge.get("producer") or "").lower()
        changes_star_storage = _producer_star_storage_changed(patch, producer)
        if changes_star_storage and ("plain" in current_identity or "param.name" in current_identity):
            issues.append("semantic_contract_violation:producer_canonical_identity_changed_for_display")
        if changes_star_storage and "consumer" in preferred_layer and producer:
            issues.append("semantic_wrong_layer:consumer_compatibility_required_but_producer_changed")
        if _consumer_alias_match_without_output_propagation(patch):
            issues.append("semantic_alias_violation:matched_alias_not_propagated_to_output")
    if _has_consumer_display_alias_contract(evidence) and _changes_sphinx_return_type_contract(patch):
        issues.append("semantic_contract_violation:parameter_alias_patch_changed_return_rtype_branches")
    return _unique(issues)


def blocking_patch_semantic_issues(issues: list[str]) -> list[str]:
    """只保留能由 diff 与证据直接证明的契约破坏。"""

    blocking_prefixes = (
        "semantic_contract_violation:producer_canonical_identity_changed_for_display",
        "semantic_wrong_layer:consumer_compatibility_required_but_producer_changed",
        "semantic_contract_violation:parameter_alias_patch_changed_return_rtype_branches",
    )
    return [issue for issue in issues if issue.startswith(blocking_prefixes)]


def patch_semantic_warnings(issues: list[str]) -> list[str]:
    """文本启发式只能提示风险，不能触发重写或丢弃候选 patch。"""

    blocking = set(blocking_patch_semantic_issues(issues))
    return [issue for issue in issues if issue not in blocking]


def implementation_semantic_issues(issues: list[str]) -> list[str]:
    """仅返回无法定义修复方向或证据自相矛盾的硬错误。"""

    return [issue for issue in issues if _is_implementation_blocker(issue)]


def semantic_evidence_warnings(issues: list[str]) -> list[str]:
    """覆盖、定位和验证质量问题只降低置信度，不阻止生成候选 patch。"""

    return [issue for issue in issues if not _is_implementation_blocker(issue)]


def _is_implementation_blocker(issue: str) -> bool:
    """把门禁限制在最小问题契约缺失和已证实的方向冲突。"""

    exact = {
        "semantic_evidence JSON is missing",
        "reproduction_contract must be an object",
        "reproduction_contract.trigger is missing",
        "reproduction_contract.expected_behavior is missing",
        "target_partitions must contain at least one public behavior case",
        "star-argument identity changes require producer/consumer contract_edges",
    }
    if issue in exact:
        return True
    if issue.startswith("target_partitions[") and "must be an object" in issue:
        return True
    if issue.startswith("target partition ") and (
        "has no expected behavior" in issue
        or "needs issue/code/public_test/execution evidence" in issue
    ):
        return True
    if "must state one exact expected public classification" in issue:
        return True
    if issue.startswith("contract_edges[") and any(
        marker in issue
        for marker in (
            "must be an object",
            ".producer is missing",
            ".current_canonical_identity is missing",
            "must preserve the plain canonical producer identity and prefer a consumer adapter",
            "conflicts with target_partitions",
            "needs code evidence for the current producer contract",
        )
    ):
        return True
    return False


def verification_coverage_issues(result: dict[str, Any], evidence: dict[str, Any] | None) -> list[str]:
    if str(result.get("status") or "").lower() != "passed":
        return []
    required = required_case_ids(evidence)
    covered = {str(value) for value in result.get("covered_cases") or []}
    missing = sorted(required - covered)
    return ["verification did not cover required semantic cases: " + ", ".join(missing)] if missing else []


def _valid_evidence_refs(value: object, require_code: bool = False) -> bool:
    refs = value if isinstance(value, list) else [value]
    valid_types: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        evidence_type = str(ref.get("type") or "").strip().lower()
        location = str(ref.get("location") or ref.get("command") or "").strip()
        if evidence_type in SUPPORTED_EVIDENCE_TYPES and location:
            valid_types.add(evidence_type)
    return "code" in valid_types if require_code else bool(valid_types)


def _strip_absolute_repo_cd(command: str) -> str:
    """Verify 已在仓库 cwd 执行，移除模型生成的不可移植绝对目录前缀。"""

    return re.sub(
        r"^\s*cd\s+(?:['\"]/[^'\"]*['\"]|/[^\s;&|]+)\s*&&\s*",
        "",
        command,
        count=1,
    )


def _first_predicate(partition: dict[str, Any]) -> bool:
    value = partition.get("first_dependent_predicate")
    if isinstance(value, dict):
        return _nonempty(value.get("symbol")) and _nonempty(value.get("location"))
    return _nonempty(value)


def _case_id(value: dict[str, Any]) -> str:
    """兼容模型常见的 case_id 别名，同时保持一个稳定覆盖 ID。"""

    return str(value.get("id") or value.get("case_id") or "").strip()


def _partition_representation(partition: dict[str, Any]) -> str:
    """联合 container/rank/dtype 字段判断表示，避免只看一个字段造成假阴性。"""

    return " ".join(
        str(partition.get(key) or "").strip().lower()
        for key in ("container", "rank", "dtype_protocol")
    )


def _partition_requires_decorated_producer(partitions: list[object], producer: str) -> bool:
    """识别 evidence 内部把 canonical producer 和 display alias 混为一谈的矛盾。"""

    producer = producer.strip().lower()
    if not producer:
        return False
    for partition in partitions:
        if not isinstance(partition, dict):
            continue
        location_text = " ".join(
            str(partition.get(key) or "").lower()
            for key in ("id", "container", "function", "file")
        )
        expected = str(partition.get("expected") or "").lower()
        if producer not in location_text:
            continue
        asks_for_decorated_key = any(
            marker in expected
            for marker in (
                "must include the asterisk",
                "must include asterisk",
                "asterisk-prefixed",
                "include the * prefix",
                "include the star prefix",
                "not just param.name",
            )
        )
        preserves_plain = "preserve" in expected and ("plain" in expected or "param.name" in expected)
        if asks_for_decorated_key and not preserves_plain:
            return True
    return False


def _ambiguous_expected_behavior(expected: str) -> bool:
    if not expected:
        return False
    return bool(
        re.search(r"\b(?:or|either|appropriate|consistent(?:ly)?)\b", expected)
        or "或" in expected
    )


def _looks_like_simulated_internal_verification(command: str) -> bool:
    lowered = command.lower()
    markers = (
        "simulate current",
        "simulation",
        "would append",
        "bug confirmed",
        "buggy behavior",
        "expected bug",
        "root cause confirmed",
        "demonstrate producer",
        "proposed fix",
        "annotations_fixed",
    )
    return any(marker in lowered for marker in markers)


def _asserts_nullable_unknown_behavior(command: str, problem_statement: str) -> bool:
    """Nullable 支持任务不能把旧 unknown/异常路径写成验证成功条件。"""

    if not all(marker in problem_statement for marker in ("pandas", "nullable")):
        return False
    lowered = command.lower()
    expects_unknown = bool(
        re.search(r"assert\s+[^;\n]+==\s*['\"]unknown['\"]", lowered)
        or "expected unknown" in lowered
    )
    expects_old_error = bool(
        "should have raised" in lowered
        or "expected bug" in lowered
    )
    return expects_unknown or expects_old_error


def _has_nullable_dtype_partition(partitions: list[dict[str, Any]]) -> bool:
    return any(
        "nullable" in _partition_representation(partition)
        or "nullable" in str(partition.get("id") or "").lower()
        for partition in partitions
    )


def _adds_first_element_semantic_inference(patch: str) -> bool:
    """识别用首元素类型代表整个容器 dtype/分类语义的新增判断。"""

    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    first_element = re.search(r"(?:\.flat\s*\[\s*0\s*\]|\bravel\s*\(\s*\)\s*\[\s*0\s*\])", added)
    semantic_type_check = re.search(r"\b(?:isinstance|type)\s*\(", added)
    return bool(first_element and semantic_type_check)


def _expects_bare_star_param_to_exist(command: str) -> bool:
    """拒绝把内部 bare lookup key 错当成应出现的 param 展示字段。"""

    return any(
        re.search(
            rf"assert\s+len\([^;\n]*['\"]param {name}['\"][^;\n]*\)\s*==\s*1",
            command,
            flags=re.I,
        )
        for name in ("args", "kwargs")
    )


def _expects_bare_star_type_output(command: str) -> bool:
    """拒绝把 canonical lookup key 直接写成关联字段的展示身份。"""

    for name in ("args", "kwargs"):
        field = rf"['\"]type {name}['\"]"
        positive_patterns = (
            rf"assert\s+len\([^;\n]*{field}[^;\n]*\)\s*(?:==\s*[1-9]\d*|>\s*0)",
            rf"assert\s+(?!not\b)any\([^;\n]*{field}",
            rf"assert\s+{field}\s+in\b",
        )
        if any(re.search(pattern, command, flags=re.I) for pattern in positive_patterns):
            return True
    return False


def _only_checks_star_field_helpers(command: str) -> bool:
    """识别绕过最终渲染消费者、只检查中间 field_list 的验证。"""

    lowered = command.lower()
    calls_internal_helper = (
        "nodes.field_list" in lowered
        and (
            "modify_field_list(" in lowered
            or "augment_descriptions_with_types(" in lowered
        )
    )
    executes_rendering = any(
        marker in lowered
        for marker in (
            "pytest",
            "sphinx-build",
            "app.build(",
            "sphinxtestapp",
        )
    )
    return calls_internal_helper and not executes_rendering


def _first_predicate_locations(partitions: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    locations: list[tuple[str, int, str]] = []
    for partition in partitions:
        predicate = partition.get("first_dependent_predicate")
        if not isinstance(predicate, dict):
            continue
        location = str(predicate.get("location") or "")
        match = re.search(r"(?P<path>[^\s:]+\.py):(?P<line>\d+)", location)
        if not match:
            continue
        locations.append(
            (match.group("path"), int(match.group("line")), str(predicate.get("symbol") or "predicate"))
        )
    return locations


def _added_normalization_locations(patch: str) -> dict[str, list[int]]:
    """用旧文件行号定位新增归一化，足以判断它是否落在证据声明的首个谓词之后。"""

    locations: dict[str, list[int]] = {}
    path = ""
    old_line: int | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            old_line = None
            continue
        hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
        if hunk:
            old_line = int(hunk.group(1))
            continue
        if old_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added = line[1:].lower()
            if re.search(r"\b(?:check_array|asarray|to_numpy|astype)\s*\(", added):
                locations.setdefault(path, []).append(old_line)
            continue
        if not line.startswith("-"):
            old_line += 1
    return locations


def _producer_star_storage_changed(patch: str, producer: str) -> bool:
    """仅在 producer hunk 内识别 canonical key 改写，避免误伤正确的 consumer alias。"""

    producer_symbol = producer.split("(", 1)[0].split(" at ", 1)[0].strip().split()[-1] if producer else ""
    active_hunk = ""
    hunk_lines: list[str] = []
    hunks: list[tuple[str, str]] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            if hunk_lines:
                hunks.append((active_hunk, "\n".join(hunk_lines)))
            active_hunk = line.lower()
            hunk_lines = []
        elif active_hunk:
            hunk_lines.append(line)
    if hunk_lines:
        hunks.append((active_hunk, "\n".join(hunk_lines)))

    for header, body in hunks:
        searchable = f"{header}\n{body}".lower()
        if producer_symbol and producer_symbol.lower() not in searchable:
            continue
        additions = "\n".join(line[1:] for line in body.splitlines() if line.startswith("+"))
        if "var_positional" in additions.lower() and re.search(
            r"(?:['\"]\*['\"]\s*\+|['\"]\*\*['\"]\s*\+)", additions
        ):
            return True
    return False


def _consumer_alias_match_without_output_propagation(patch: str) -> bool:
    added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    if not re.search(r"(?:get|in)\s*\([^\n]*(?:['\"]\*['\"]|['\"]\*\*['\"])", added):
        return False
    if re.search(r"matched_(?:name|key)|output_(?:name|key)|render_(?:name|key)", added):
        return False
    return bool(re.search(r"^[ +].*(?:type|param) [^\n]*\+\s*name\b", patch, flags=re.M))


def _has_consumer_display_alias_contract(evidence: dict[str, Any]) -> bool:
    for edge in evidence.get("contract_edges") or []:
        if not isinstance(edge, dict):
            continue
        canonical = str(edge.get("current_canonical_identity") or "").lower()
        preferred = str(edge.get("preferred_change_layer") or "").lower()
        consumers = json.dumps(edge.get("consumers") or [], ensure_ascii=False).lower()
        plain_identity = (
            "plain" in canonical
            or "bare" in canonical
            or "param.name" in canonical
            or (
                canonical.strip("'\" ") in {"args", "kwargs"}
                and any(marker in consumers for marker in ("*args", "**kwargs", "starred", "asterisk"))
            )
        )
        if plain_identity and "consumer" in preferred:
            return True
    return False


def _changes_sphinx_return_type_contract(patch: str) -> bool:
    """参数 display alias 修复不能改写独立的 type/return/rtype 收集分支。"""

    removed = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed_branch_patterns = (
        r"elif\s+parts\[0\]\s*==\s*['\"]type['\"]\s*:",
        r"elif\s+parts\[0\]\s+in\s*\(\s*['\"]return['\"]\s*,\s*['\"]returns['\"]\s*\)\s*:",
        r"elif\s+parts\[0\]\s*==\s*['\"]rtype['\"]\s*:",
    )
    removes_existing_branch = any(re.search(pattern, removed) for pattern in removed_branch_patterns)
    adds_return_bookkeeping = bool(
        re.search(r"has_(?:type|description)\.add\(\s*['\"]return['\"]\s*\)", added)
    )
    return removes_existing_branch or adds_return_bookkeeping


def _meaningful_missing_behavior(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and text not in {"unknown", "not_checked", "not applicable", "n/a"})


def _nonempty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple)):
        return bool(value)
    return value is not None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
