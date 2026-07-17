from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class ConstraintProfile:
    name: str
    issue_all: tuple[str, ...]
    source_all: tuple[str, ...]
    implement_prompt: str
    issue_any: tuple[str, ...] = ()
    priority: int = 0

    def matches_issue(self, problem_statement: str) -> bool:
        lowered = problem_statement.lower()
        return all(marker.lower() in lowered for marker in self.issue_all) and (
            not self.issue_any or any(marker.lower() in lowered for marker in self.issue_any)
        )

    def matches_source(self, source_excerpt: str) -> bool:
        lowered = source_excerpt.lower()
        return all(marker.lower() in lowered for marker in self.source_all)


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
)


def route_task_constraints(
    *,
    problem_statement: str,
    cwd: Path,
    allowed_ranges: dict[str, Iterable[object]],
    max_active: int = 2,
) -> ConstraintRouting:
    candidates = [profile for profile in PROFILES if profile.matches_issue(problem_statement)]
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


def _range_bounds(value: object) -> tuple[int, int]:
    if hasattr(value, "start") and hasattr(value, "end"):
        return int(getattr(value, "start")), int(getattr(value, "end"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    return 0, -1
