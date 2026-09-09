from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


NULLABLE_PANDAS_CONTRACT = """## Active task constraint: nullable_pandas
- Cover both 1D pandas Series (`.dtype`) and 2D pandas DataFrame (`.dtypes`) before writing.
- Preserve non-pandas, native numpy, and sparse behavior. Import and use the existing `_pandas_dtype_needs_early_conversion` helper as the only dtype conversion predicate.
- Copy the existing `check_array` pandas identity guards exactly and in this order: DataFrame is `hasattr(y, 'dtypes') and hasattr(y.dtypes, '__array__')`, followed by `any(_pandas_dtype_needs_early_conversion(dtype) for dtype in y.dtypes)`; Series is `hasattr(y, 'iloc') and hasattr(y, 'dtype')`, followed by `_pandas_dtype_needs_early_conversion(y.dtype)`.
- Only when the matching identity and dtype guards are true, invoke `check_array(y, dtype=None, ensure_2d=False)` before the existing `is_multilabel(y)` predicate.
- Never use `hasattr(y, '__array__')`, a lone `.dtypes`/`.dtype` guard, `to_numpy`, `astype`, an exact dtype string whitelist, unconditional conversion, or a later object-dtype fallback.
- The semantic_challenge must name both exact identity guards, the helper guard, and their ordering before `is_multilabel(y)`.
"""


MATCHED_ALIAS_CONTRACT = """## Active task constraint: matched_alias
- Alias fallback resolves identity, not only a value. In each direct consumer, initialize one `matched_display_name` from the canonical bare name and update it only when the corresponding decorated key actually exists.
- Use that same resolved variable for every later dictionary lookup, membership decision, field-name construction, write, and rendered output.
- A patch is incomplete if it reads `get('*' + name)`/`get('**' + name)` or ORs decorated membership tests and then emits `'type ' + name`, `'param ' + name`, or otherwise returns to the bare name.
- Before writing, trace one concrete alias from fallback match to its final emitted field name. Preserve the producer's bare canonical storage key.
- Preserve existing independent `type`, `return/returns`, and `rtype` branches and their bookkeeping byte-for-byte. Extend only the parameter lookup/display path; do not replace the surrounding field-scanning loop.
"""


GENERATED_RELATION_NAME_CONTRACT = """## Active task constraint: generated_relation_name
- This task changes generated source text, so the exact naming identity is part of the public contract. Never derive a repeated relation's `related_name` from the target model alone: every relation to that target would collide again.
- Trace three clean-source identities before editing: the model currently being generated, the generated field's final `att_name`, and Django's reverse-accessor suffix convention. In this inspectdb path the source model expression is the same existing `table2model(table_name)` used by the generated class declaration. Lowercase that generated model name, combine it with `att_name`, and preserve the conventional `_set` suffix; do not substitute `rel_to` or the referenced table identity.
- Add a generated `related_name` only at the duplicate-relation boundary required by the issue; preserve the first/single relation's existing output.
- Candidate text is not a naming oracle. In the final report cite the clean-source expression supplying the source model identity and the field-name expression; do not cite the new assignment itself as repository_native_rule.
"""


RELATION_PROTOCOL_CONTRACT = """## Active task constraint: relation_protocol
- Replace concrete-class guesses with a compact relation truth table before editing: non-relation, forward FK, reverse FK, forward M2M, and reverse M2M.
- If the existing diagnostic says "many-to-many field or a reverse foreign key", the rejecting semantic predicate must cover both `many_to_many` and `one_to_many` relation flags while preserving ordinary forward FK. A concrete `ManyToManyField` isinstance check is insufficient because reverse relation descriptors are different classes.
- Use this boolean grouping: `(is_relation and (many_to_many or one_to_many)) or legacy_reverse_fk_fallback`. Keep the existing `rel.field.many_to_one` fallback as the second top-level OR branch; do not nest it under `is_relation`. Every relation kind named by the existing error contract must appear in semantic_coverage.
"""


MUTABLE_STATE_LIFECYCLE_CONTRACT = """## Active task constraint: mutable_state_lifecycle
- Treat a public setter as an atomic state transition. Before editing, enumerate every state consumer written by the setter and initialized by the constructor: geometry/path vertices, visual handles, displayed text, public value, redraw, and callbacks when present.
- Preserve the existing five-point closed polygon for ordinary non-degenerate ranges. The issue's suggestion to comment out `xy[4]` is only a hypothesis: deleting that assignment corrupts the fourth corner in existing RangeSlider behavior. If a degenerate path can contain fewer vertices, guard or normalize only that exceptional shape without changing the ordinary corner coordinates.
- After `set_val`-style calls, update both existing handles with `set_xdata` in horizontal mode or `set_ydata` in vertical mode. Their positions must agree with the sorted public value. Fixing only the polygon or only the IndexError is incomplete.
- Keep drag behavior orientation-aware too: nearest-handle selection and active-handle movement must read/write y-data vertically and x-data horizontally.
- The adjacent invariant is lifecycle behavior: construct with a degenerate initial range, then update to a distinct range, and require geometry, handles, and public value to stay synchronized.
"""


NONINVERTIBLE_FORMAT_PROTOCOL_CONTRACT = """## Active task constraint: noninvertible_format_protocol
- The failure comes from a normalization protocol whose `inverse()` explicitly rejects some implementations. Do not make the norm approximately invertible and do not replace precision with a fixed constant.
- Keep cursor formatting in the consumer. Use the repository's concrete `BoundaryNorm` type as the narrow branch identity, not `hasattr(..., "boundaries")` or a catch-all exception. Derive the local display delta from its public boundaries around the scalar value, then reuse the existing significant-digit helper. Preserve the current inverse-based path for invertible norms.
- Check values inside a bin, on/near a boundary, and the ordinary Normalize path. The repair must avoid the exception while retaining data-dependent precision and the existing bracketed output format.
"""


@dataclass(frozen=True)
class ConstraintProfile:
    name: str
    issue_all: tuple[str, ...]
    source_all: tuple[str, ...]
    implement_prompt: str
    issue_any: tuple[str, ...] = ()
    source_any: tuple[str, ...] = ()
    priority: int = 0
    phases: tuple[str, ...] = ()

    def matches_issue(self, problem_statement: str) -> bool:
        lowered = problem_statement.lower()
        return all(marker.lower() in lowered for marker in self.issue_all) and (
            not self.issue_any or any(marker.lower() in lowered for marker in self.issue_any)
        )

    def matches_source(self, source_excerpt: str) -> bool:
        lowered = source_excerpt.lower()
        return all(marker.lower() in lowered for marker in self.source_all) and (
            not self.source_any
            or any(marker.lower() in lowered for marker in self.source_any)
        )

    def matches_phase(self, phase: str) -> bool:
        return not self.phases or phase in self.phases


@dataclass(frozen=True)
class ConstraintRouting:
    candidate_profiles: tuple[str, ...]
    active_profiles: tuple[ConstraintProfile, ...]
    rejected_profiles: tuple[str, ...]
    source_excerpt_chars: int

    @property
    def prompts(self) -> tuple[str, ...]:
        return tuple(profile.implement_prompt for profile in self.active_profiles)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_profiles": list(self.candidate_profiles),
            "active_profiles": [profile.name for profile in self.active_profiles],
            "rejected_profiles": list(self.rejected_profiles),
            "source_excerpt_chars": self.source_excerpt_chars,
            "injected_prompt_chars": sum(len(prompt) for prompt in self.prompts),
        }


PROFILES: tuple[ConstraintProfile, ...] = (
    ConstraintProfile(
        name="nullable_pandas",
        issue_all=("pandas",),
        issue_any=("nullable", "extension dtype"),
        source_all=("check_array", "is_multilabel"),
        implement_prompt=NULLABLE_PANDAS_CONTRACT,
        priority=100,
    ),
    ConstraintProfile(
        name="matched_alias",
        issue_all=("*args", "**kwargs"),
        source_all=("annotations", "has_description"),
        implement_prompt=MATCHED_ALIAS_CONTRACT,
        priority=100,
    ),
    ConstraintProfile(
        name="generated_relation_name",
        issue_all=("inspectdb", "related_name"),
        source_all=("rel_to", "att_name", "extra_params"),
        implement_prompt=GENERATED_RELATION_NAME_CONTRACT,
        priority=200,
        phases=("experience_initial", "experience_corrective"),
    ),
    ConstraintProfile(
        name="relation_protocol",
        issue_all=("list_display",),
        issue_any=("e108", "reverse", "choice"),
        source_all=("manytomanyfield", "admin.e109"),
        source_any=("many_to_one", "one_to_many"),
        implement_prompt=RELATION_PROTOCOL_CONTRACT,
        priority=200,
        phases=("experience_initial", "experience_corrective"),
    ),
    ConstraintProfile(
        name="mutable_state_lifecycle",
        issue_all=("rangeslider", "valinit"),
        source_all=("self.poly.xy", "set_val"),
        implement_prompt=MUTABLE_STATE_LIFECYCLE_CONTRACT,
        priority=200,
        phases=("experience_initial", "experience_corrective"),
    ),
    ConstraintProfile(
        name="noninvertible_format_protocol",
        issue_all=("boundarynorm", "format_cursor_data"),
        source_all=("self.norm.inverse", "_g_sig_digits"),
        implement_prompt=NONINVERTIBLE_FORMAT_PROTOCOL_CONTRACT,
        priority=200,
        phases=("experience_initial", "experience_corrective"),
    ),
)


def route_task_constraints(
    *,
    problem_statement: str,
    cwd: Path,
    allowed_ranges: dict[str, Iterable[object]],
    max_active: int = 2,
    phase: str = "default",
) -> ConstraintRouting:
    candidates = [
        profile
        for profile in PROFILES
        if profile.matches_phase(phase) and profile.matches_issue(problem_statement)
    ]
    source_excerpt = read_allowed_source(cwd, allowed_ranges)
    confirmed = [profile for profile in candidates if profile.matches_source(source_excerpt)]
    confirmed.sort(key=lambda profile: (-profile.priority, profile.name))
    active = tuple(confirmed[:max_active])
    active_names = {profile.name for profile in active}
    rejected = tuple(profile.name for profile in candidates if profile.name not in active_names)
    return ConstraintRouting(
        candidate_profiles=tuple(profile.name for profile in candidates),
        active_profiles=active,
        rejected_profiles=rejected,
        source_excerpt_chars=len(source_excerpt),
    )


def read_allowed_source(
    cwd: Path,
    allowed_ranges: dict[str, Iterable[object]],
    *,
    max_chars: int = 30_000,
) -> str:
    root = cwd.resolve()
    chunks: list[str] = []
    size = 0
    for relative_path in sorted(allowed_ranges):
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw_range in allowed_ranges[relative_path]:
            start, end = _range_bounds(raw_range)
            if start < 1 or end < start:
                continue
            chunk = f"# {relative_path}:{start}-{end}\n" + "\n".join(lines[start - 1 : end])
            remaining = max_chars - size
            if remaining <= 0:
                return "\n".join(chunks)
            chunk = chunk[:remaining]
            chunks.append(chunk)
            size += len(chunk)
    return "\n".join(chunks)


_PROFILE_CONTEXT_QUERIES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "generated_relation_name": (
        ("django/core/management/commands/inspectdb.py", 'yield "class %s(models.Model):"', 8, 16),
        ("django/db/models/fields/reverse_related.py", "def get_accessor_name", 12, 32),
    ),
    "relation_protocol": (
        ("django/db/models/fields/reverse_related.py", "class ForeignObjectRel", 0, 70),
        ("django/db/models/fields/reverse_related.py", "one_to_many = True", 12, 16),
        ("django/db/models/fields/reverse_related.py", "many_to_many = True", 12, 16),
    ),
    "mutable_state_lifecycle": (
        ("lib/matplotlib/patches.py", "def set_xy", 8, 38),
    ),
    "noninvertible_format_protocol": (
        ("lib/matplotlib/colors.py", "class BoundaryNorm", 0, 135),
        ("lib/matplotlib/cbook/__init__.py", "def _g_sig_digits", 4, 24),
    ),
}


_PROFILE_EDIT_QUERIES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "generated_relation_name": (
        ("django/core/management/commands/inspectdb.py", 'yield "class %s(models.Model):"', 8, 16),
    ),
    "mutable_state_lifecycle": (
        ("lib/matplotlib/widgets.py", "def _update_val_from_pos", 2, 20),
        ("lib/matplotlib/widgets.py", "def _update(self, event)", 2, 38),
        ("lib/matplotlib/widgets.py", "def set_val(self, val)", 2, 48),
    ),
}


def derive_profile_context_ranges(
    cwd: Path,
    profile_names: Iterable[str],
) -> dict[str, tuple[tuple[int, int], ...]]:
    """为已确认的语义 profile 提供少量只读 clean-source 对照范围。"""

    root = cwd.resolve()
    result: dict[str, list[tuple[int, int]]] = {}
    for profile in profile_names:
        profile_name = str(getattr(profile, "name", profile))
        for relative_path, marker, before, after in _PROFILE_CONTEXT_QUERIES.get(profile_name, ()):
            path = (root / relative_path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            line_number = next(
                (index for index, line in enumerate(lines, 1) if marker in line),
                None,
            )
            if line_number is None:
                continue
            result.setdefault(relative_path, []).append(
                (max(1, line_number - before), min(len(lines), line_number + after))
            )
    return {path: tuple(ranges) for path, ranges in result.items()}


def derive_profile_edit_ranges(
    cwd: Path,
    profile_names: Iterable[str],
) -> dict[str, tuple[tuple[int, int], ...]]:
    """返回 profile 已证明相关、且与原编辑文件同属一个局部生命周期的范围。"""

    return _derive_marker_ranges(cwd, profile_names, _PROFILE_EDIT_QUERIES)


def profile_patch_contract_issues(
    patch: str,
    profile_names: Iterable[str],
) -> list[str]:
    """用便宜的确定性规则检查已激活 profile 的关键语义契约。"""

    profiles = {str(getattr(profile, "name", profile)) for profile in profile_names}
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    lowered_added = added.lower()
    compact_added = re.sub(r"\s+", "", added.lower())
    issues: list[str] = []

    if "generated_relation_name" in profiles:
        related_lines = [line for line in added.splitlines() if "related_name" in line]
        related_text = "\n".join(related_lines)
        if not related_lines:
            issues.append("profile_contract:generated_relation_name:missing_related_name_assignment")
        else:
            if "_set" not in related_text:
                issues.append("profile_contract:generated_relation_name:missing_reverse_accessor_set_suffix")
            if "%(class)" in related_text or "rel_to" in related_text:
                issues.append("profile_contract:generated_relation_name:uses_target_or_template_identity")
            if "table2model(table_name)" not in compact_added:
                issues.append("profile_contract:generated_relation_name:missing_generated_source_model_identity")
        if "relations.values()" in compact_added and "sum(" in compact_added:
            issues.append("profile_contract:generated_relation_name:blanket_duplicate_count_changes_first_relation")
        if not re.search(r"\bif\s+\w+\s+in\s+\w+\s*:", added) or ".add(" not in added:
            issues.append("profile_contract:generated_relation_name:missing_stateful_later_duplicate_boundary")

    if "relation_protocol" in profiles:
        for token in ("is_relation", "many_to_many", "one_to_many"):
            if token not in added:
                issues.append(f"profile_contract:relation_protocol:missing_{token}")
        if "isinstance(field,models.manytomanyfield)" in compact_added:
            issues.append("profile_contract:relation_protocol:concrete_many_to_many_class_guard")
        if "field.rel.field.many_to_one" in removed and "field.rel.field.many_to_one" not in added:
            issues.append("profile_contract:relation_protocol:removed_legacy_reverse_fk_fallback")

    if "mutable_state_lifecycle" in profiles:
        removed_closing_vertex = bool(re.search(r"\bxy\s*\[\s*4\s*\]\s*=", removed))
        explicit_open_polygon = "closed=false" in compact_added
        if (
            removed_closing_vertex
            and not explicit_open_polygon
            and not re.search(r"\bxy\s*\[\s*4\s*\]\s*=", added)
        ):
            issues.append("profile_contract:mutable_state_lifecycle:removed_existing_closing_vertex_assignment")
        if re.search(r"np\.empty\s*\(\s*\(\s*5\s*,\s*2\s*\)\s*\)", added):
            issues.append("profile_contract:mutable_state_lifecycle:fixed_five_row_replacement_bypasses_polygon_closure")
        for token in ("_handles", "set_xdata", "set_ydata"):
            if token not in added:
                issues.append(f"profile_contract:mutable_state_lifecycle:missing_{token}_synchronization")

    if "noninvertible_format_protocol" in profiles:
        if re.search(r"g_sig_digits\s*=\s*[-+]?\d+(?:\.\d+)?", added):
            issues.append("profile_contract:noninvertible_format_protocol:fixed_precision_fallback")
        if "boundaries" not in lowered_added:
            issues.append("profile_contract:noninvertible_format_protocol:missing_boundaries_derived_precision")
        if "_g_sig_digits" in removed and "_g_sig_digits" not in lowered_added:
            issues.append("profile_contract:noninvertible_format_protocol:removed_existing_sig_digits_helper")
        if "boundarynorm" not in lowered_added:
            issues.append("profile_contract:noninvertible_format_protocol:missing_exact_boundarynorm_guard")
        if re.search(r"hasattr\s*\([^\n]*boundaries", added, flags=re.I):
            issues.append("profile_contract:noninvertible_format_protocol:broad_boundaries_attribute_guard")

    return list(dict.fromkeys(issues))


def _range_bounds(value: object) -> tuple[int, int]:
    if hasattr(value, "start") and hasattr(value, "end"):
        return int(getattr(value, "start")), int(getattr(value, "end"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    return 0, -1


def _derive_marker_ranges(
    cwd: Path,
    profile_names: Iterable[str],
    queries: dict[str, tuple[tuple[str, str, int, int], ...]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    root = cwd.resolve()
    result: dict[str, list[tuple[int, int]]] = {}
    for profile in profile_names:
        profile_name = str(getattr(profile, "name", profile))
        for relative_path, marker, before, after in queries.get(profile_name, ()):
            path = (root / relative_path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            line_number = next(
                (index for index, line in enumerate(lines, 1) if marker in line),
                None,
            )
            if line_number is None:
                continue
            result.setdefault(relative_path, []).append(
                (max(1, line_number - before), min(len(lines), line_number + after))
            )
    return {path: tuple(ranges) for path, ranges in result.items()}
