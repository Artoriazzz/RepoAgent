import json
import shlex

from repo_agent.core.differential_validation import (
    CASE_PREFIX,
    assess_differential_validation,
    differential_plan_validation_errors,
    differential_command_reason,
    extract_differential_obligations,
    extract_guard_obligations,
    extract_impact_obligations,
    extract_case_observations,
    parse_differential_test_plan,
    patch_source_context,
)
from repo_agent.core.subagent import (
    SWEBENCH_TEST_EXPANDER_PROMPT,
    SWEBENCH_WORKER_MAX_STEPS,
    TEST_EXPANDER_TOOLS,
)


def _command(case_ids: list[str]) -> str:
    source = "\n".join(
        [
            "import json",
            *[
                (
                    f"print('{CASE_PREFIX}' + json.dumps("
                    f"{{'case': '{case_id}', 'status': 'ok', "
                    f"'value_type': 'str', 'value_repr': '{case_id}'}}))"
                )
                for case_id in case_ids
            ],
        ]
    )
    return f"python -c {shlex.quote(source)}"


def _output(values: dict[str, str]) -> str:
    return "\n".join(
        CASE_PREFIX
        + json.dumps(
            {
                "case": case_id,
                "status": "ok",
                "value_type": "str",
                "value_repr": value,
            }
        )
        for case_id, value in values.items()
    )


def test_differential_plan_accepts_structured_print_only_command():
    command = _command(["reported", "symbolic_boundary"])
    plan = parse_differential_test_plan(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "reported",
                        "input": "public issue input",
                        "why": "exposes the issue",
                        "evidence": "issue",
                        "expected_relation": "must_change",
                    },
                    {
                        "id": "symbolic_boundary",
                        "input": "symbolic assumption input",
                        "why": "checks the changed guard",
                        "evidence": "repository_code:mod.py",
                        "expected_relation": "observe",
                    },
                ],
                "command": command,
                "limitations": [],
            }
        )
    )

    assert plan is not None
    assert plan["command"] == command
    assert differential_command_reason(command, {"reported", "symbolic_boundary"}) is None


def test_differential_trace_routes_exit_zero_outputs_to_review():
    plan = {
        "cases": [
            {
                "id": "reported",
                "input": "x",
                "why": "issue",
                "evidence": "issue",
                "expected_relation": "must_change",
            },
            {
                "id": "symbolic_boundary",
                "input": "l**p",
                "why": "guard boundary",
                "evidence": "repository_code:mod.py",
                "expected_relation": "must_change",
            },
        ],
        "command": _command(["reported", "symbolic_boundary"]),
        "limitations": [],
    }
    result = assess_differential_validation(
        plan=plan,
        baseline={
            "exit_code": 0,
            "evidence": _output({"reported": "0", "symbolic_boundary": "0"}),
        },
        patched={
            "exit_code": 0,
            "evidence": _output(
                {
                    "reported": "Mod(x**2, x)",
                    "symbolic_boundary": "Mod(l**p, l)",
                }
            ),
        },
    )

    assert result["status"] == "differential_review_required"
    assert result["feedback_eligible"] is True
    assert all(case["changed"] for case in result["case_matrix"])
    assert "public_differential_trace" in result["failure_evidence"]


def test_differential_trace_rejects_missing_case_output():
    plan = {
        "cases": [
            {"id": "reported", "expected_relation": "must_change"},
            {"id": "boundary", "expected_relation": "observe"},
        ],
        "command": _command(["reported", "boundary"]),
    }
    result = assess_differential_validation(
        plan=plan,
        baseline={"exit_code": 0, "output": _output({"reported": "0"})},
        patched={"exit_code": 0, "output": _output({"reported": "fixed"})},
    )

    assert result["status"] == "invalid_differential_probe"
    assert result["feedback_eligible"] is False


def test_case_observation_parser_ignores_unstructured_noise():
    output = "warning\n" + _output({"case_a": "value"}) + "\nnot-json"

    assert extract_case_observations(output)["case_a"]["value_repr"] == "value"


def test_case_observation_parser_normalizes_common_status_aliases():
    output = "\n".join(
        [
            CASE_PREFIX
            + json.dumps(
                {
                    "case": "success_case",
                    "status": "success",
                    "value_type": "Integer",
                    "value_repr": "0",
                }
            ),
            CASE_PREFIX
            + json.dumps(
                {
                    "case": "exception_case",
                    "status": "exception",
                    "exception_type": "ValueError",
                    "exception_message": "bad input",
                }
            ),
        ]
    )

    observations = extract_case_observations(output)

    assert observations["success_case"]["status"] == "ok"
    assert observations["exception_case"]["status"] == "error"


def test_differential_command_rejects_process_imports():
    command = (
        "python -c "
        + shlex.quote(
            "import subprocess\n"
            + "print("
            + repr(CASE_PREFIX)
            + " + '{\"case\":\"a\",\"status\":\"ok\"}')"
        )
    )

    assert differential_command_reason(command, {"a"}) == "network/process imports are forbidden"


def test_test_expander_uses_compact_evidence_budget():
    assert TEST_EXPANDER_TOOLS == set()
    assert SWEBENCH_WORKER_MAX_STEPS["test_expander"] == 1
    assert "你没有工具" in SWEBENCH_TEST_EXPANDER_PROMPT
    assert "第一回合必须直接输出最终 JSON" in SWEBENCH_TEST_EXPANDER_PROMPT
    assert "精确字符串" in SWEBENCH_TEST_EXPANDER_PROMPT
    assert "`must_change` 只能用于" in SWEBENCH_TEST_EXPANDER_PROMPT


def test_patch_impact_obligations_cover_removed_state_and_constant_fallback():
    patch = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,4 +1,6 @@
-xy[4] = xy[0]
+try:
+    value = compute()
+except ValueError:
+    digits = 3
"""

    obligations = extract_impact_obligations(patch)
    by_id = {item["id"]: item for item in obligations}

    assert by_id["state_integrity_xy_4"]["required_relation"] == "must_preserve"
    assert "get_extents" in by_id["state_integrity_xy_4"]["command_hints"]
    assert "_handles" in by_id["state_integrity_xy_4"]["command_hints"]
    assert by_id["state_consumer_sync_xy_4"]["required_relation"] == "must_preserve"
    assert "get_xdata" in by_id["state_consumer_sync_xy_4"]["command_hints"]
    assert by_id["exact_output_digits"]["required_oracle_strength"] == "exact"
    assert len(extract_differential_obligations(patch)) == 3


def test_exact_oracle_rejects_candidate_provenance_and_accepts_trusted_quote():
    command = _command(["target", "old"])
    base = {
        "cases": [
            {
                "id": "target",
                "input": "public input",
                "why": "target",
                "evidence": "public task",
                "role": "target",
                "observable": "exact output",
                "expected_relation": "must_change",
                "oracle_strength": "exact",
                "expected_after": {"status": "ok", "value_repr": "fixed"},
                "oracle_source": "clean_source:module.py:1",
                "oracle_quote": "return fixed",
            },
            {
                "id": "old",
                "input": "old input",
                "why": "invariant",
                "evidence": "baseline",
                "role": "invariant",
                "observable": "old output",
                "expected_relation": "must_preserve",
                "oracle_strength": "invariant",
            },
        ],
        "command": command,
        "limitations": [],
    }
    trusted = "module.py:1 return fixed"

    assert parse_differential_test_plan(json.dumps(base), trusted_oracle_evidence=trusted)
    base["cases"][0]["oracle_source"] = "repository_code:candidate hunk"
    assert parse_differential_test_plan(json.dumps(base), trusted_oracle_evidence=trusted) is None


def test_patch_source_context_reads_clean_head_not_candidate(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    path = tmp_path / "module.py"
    path.write_text("value = 'clean'\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    path.write_text("value = 'candidate'\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff"], cwd=tmp_path, text=True, stdout=subprocess.PIPE, check=True
    ).stdout

    context = patch_source_context(tmp_path, patch)

    assert "value = 'clean'" in context
    assert "value = 'candidate'" not in context


def test_exact_oracle_distinguishes_changed_from_correct():
    plan = {
        "cases": [
            {
                "id": "formatted",
                "input": "public formatter input",
                "why": "exact public output",
                "evidence": "repository_code:formatter.py:10",
                "role": "target",
                "observable": "exact formatted return string",
                "expected_relation": "must_change",
                "oracle_strength": "exact",
                "expected_after": {"status": "ok", "value_repr": "'[0.9]'"},
                "oracle_source": "repository_code:formatter.py:10",
            },
            {
                "id": "old",
                "input": "old formatter input",
                "why": "preserve output",
                "evidence": "repository_code:formatter.py:11",
                "role": "invariant",
                "observable": "exact formatted return string",
                "expected_relation": "must_preserve",
                "oracle_strength": "invariant",
                "expected_after": {},
                "oracle_source": "repository_code:formatter.py:11",
            },
        ],
        "command": _command(["formatted", "old"]),
    }

    result = assess_differential_validation(
        plan=plan,
        baseline={"exit_code": 0, "evidence": _output({"formatted": "ValueError", "old": "same"})},
        patched={"exit_code": 0, "evidence": _output({"formatted": "'[0.500]'", "old": "same"})},
    )

    assert result["oracle_complete"] is False
    assert result["oracle_violations"] == ["formatted"]
    assert result["oracle_gaps"] == []


def test_compound_guard_extracts_semantic_and_sign_obligations():
    patch = """diff --git a/sympy/core/mod.py b/sympy/core/mod.py
--- a/sympy/core/mod.py
+++ b/sympy/core/mod.py
@@ -1 +1 @@
-p.is_Pow and p.exp.is_Integer and p.base == q
+p.is_Pow and p.exp.is_Integer and p.base == q and p.base.is_integer
"""

    obligations = extract_guard_obligations(patch)
    by_id = {item["id"]: item for item in obligations}

    assert by_id["exponent_semantic_integer"]["required_relation"] == "must_change"
    assert by_id["exponent_negative_boundary"]["required_relation"] == "must_change"
    assert by_id["base_noninteger_boundary"]["required_relation"] == "must_change"


def test_guard_coverage_requires_mapped_semantic_and_negative_cases():
    patch = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -1 +1 @@
-p.exp.is_Integer and p.base == q
+p.exp.is_Integer and p.base == q and p.base.is_integer
"""
    obligations = extract_guard_obligations(patch)
    cases = [
        {
            "id": "reported",
            "input": "non-integer base with no integer assumption",
            "why": "public trigger",
            "evidence": "issue",
            "expected_relation": "must_change",
        },
        {
            "id": "assumed_exp",
            "input": "integer base l with l**p and symbolic exponent p carrying integer=True, positive=True",
            "why": "positive symbolic integer-assumed exponent",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
        {
            "id": "negative_exp",
            "input": "l**-2 with integer base l and a negative exponent",
            "why": "negative sign boundary",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
    ]
    coverage = [
        {
            "obligation_id": item["id"],
            "case_ids": [
                "assumed_exp"
                if item["id"] == "exponent_semantic_integer"
                else "negative_exp"
                if item["id"] == "exponent_negative_boundary"
                else "reported"
            ],
        }
        for item in obligations
    ]
    payload = {
        "cases": cases,
        "guard_coverage": coverage,
        "command": _command([case["id"] for case in cases]),
        "limitations": [],
    }

    plan = parse_differential_test_plan(json.dumps(payload), obligations)

    assert plan is not None
    assert plan["guard_coverage_complete"] is True


def test_specialized_guard_obligation_rejects_coverage_laundering():
    patch = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -1 +1 @@
-p.exp.is_Integer and p.base == q
+p.exp.is_Integer and p.base == q and p.base.is_integer
"""
    obligations = extract_guard_obligations(patch)
    cases = [
        {
            "id": "reported",
            "input": "non-integer base with no integer assumption",
            "why": "public trigger",
            "evidence": "issue",
            "expected_relation": "must_change",
        },
        {
            "id": "assumed_exp",
            "input": "integer base l with l**p and symbolic exponent p carrying integer=True, positive=True",
            "why": "positive integer-assumed exponent",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
        {
            "id": "negative_exp",
            "input": "l**-2 with integer base l and a negative exponent",
            "why": "negative sign boundary",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
    ]
    coverage = []
    for item in obligations:
        case_ids = ["reported"]
        if item["id"] == "exponent_semantic_integer":
            case_ids = ["assumed_exp", "reported"]
        elif item["id"] == "exponent_negative_boundary":
            case_ids = ["negative_exp"]
        coverage.append({"obligation_id": item["id"], "case_ids": case_ids})

    assert (
        parse_differential_test_plan(
            json.dumps(
                {
                    "cases": cases,
                    "guard_coverage": coverage,
                    "command": _command([case["id"] for case in cases]),
                    "limitations": [],
                }
            ),
            obligations,
        )
        is None
    )


def test_plan_validation_errors_explain_relation_isolation_and_case_ids():
    patch = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -1 +1 @@
-p.exp.is_Integer and p.base == q
+p.exp.is_Integer and p.base == q and p.base.is_integer
"""
    obligations = extract_guard_obligations(patch)
    payload = {
        "cases": [
            {
                "id": "reported",
                "input": "non-integer base with no integer assumption",
                "why": "public trigger",
                "evidence": "issue",
                "expected_relation": "must_change",
            },
            {
                "id": "assumed_exp",
                "input": "x**n with symbolic exponent n carrying integer=True",
                "why": "symbolic exponent",
                "evidence": "repository_code",
                "expected_relation": "observe",
            },
            {
                "id": "negative_exp",
                "input": "2**m with symbolic negative exponent",
                "why": "negative boundary",
                "evidence": "repository_code",
                "expected_relation": "observe",
            },
        ],
        "guard_coverage": [
            {
                "obligation_id": item["id"],
                "case_ids": [
                    "assumed_exp"
                    if item["id"] == "exponent_semantic_integer"
                    else "negative_exp"
                    if item["id"] == "exponent_negative_boundary"
                    else "reported"
                ],
            }
            for item in obligations
        ],
        "command": _command(["1", "2", "3"]),
        "limitations": [],
    }

    errors = differential_plan_validation_errors(json.dumps(payload), obligations)

    assert any("command does not emit every declared case" in error for error in errors)
    assert any("exponent_semantic_integer" in error for error in errors)
    assert any("exponent_negative_boundary" in error for error in errors)
    assert any("integer base" in error for error in errors)


def test_differential_plan_accepts_semantic_words_between_hint_terms():
    patch = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -1 +1 @@
-p.exp.is_Integer and p.base == q
+p.exp.is_integer and p.base == q and p.base.is_integer
"""
    obligations = extract_guard_obligations(patch)
    cases = [
        {
            "id": "public_failure",
            "input": "Mod(x**2, x) with a non-integer or unknown base",
            "why": "public failure with no integer assumption",
            "evidence": "issue",
            "expected_relation": "must_change",
        },
        {
            "id": "negative_exp",
            "input": "2**-2 with integer base",
            "why": "literal negative exponent boundary",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
        {
            "id": "positive_symbolic_exp",
            "input": "x**p where p has integer=True and x is an integer base",
            "why": "symbolic positive integer exponent with integer base",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
    ]
    payload = {
        "cases": cases,
        "guard_coverage": [
            {
                "obligation_id": obligation["id"],
                "case_ids": [
                    "positive_symbolic_exp"
                    if obligation["id"] == "exponent_semantic_integer"
                    else "negative_exp"
                    if obligation["id"] == "exponent_negative_boundary"
                    else "public_failure"
                    if obligation["id"] == "base_noninteger_boundary"
                    else "positive_symbolic_exp"
                ],
            }
            for obligation in obligations
        ],
        "command": _command([case["id"] for case in cases]),
        "limitations": [],
    }

    errors = differential_plan_validation_errors(json.dumps(payload), obligations)

    assert errors == []


def test_differential_plan_still_rejects_missing_positive_exponent_boundary():
    patch = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -1 +1 @@
-p.exp.is_Integer and p.base == q
+p.exp.is_integer and p.base == q
"""
    obligations = extract_guard_obligations(patch)
    cases = [
        {
            "id": "symbolic_exp",
            "input": "x**p where p has integer=True and x is an integer base",
            "why": "symbolic integer exponent with unknown sign",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
        {
            "id": "negative_exp",
            "input": "2**-2 with integer base",
            "why": "literal negative exponent boundary",
            "evidence": "repository_code",
            "expected_relation": "must_change",
        },
    ]
    payload = {
        "cases": cases,
        "guard_coverage": [
            {
                "obligation_id": obligation["id"],
                "case_ids": [
                    "symbolic_exp"
                    if obligation["id"] == "exponent_semantic_integer"
                    else "negative_exp"
                ],
            }
            for obligation in obligations
        ],
        "command": _command([case["id"] for case in cases]),
        "limitations": [],
    }

    errors = differential_plan_validation_errors(json.dumps(payload), obligations)

    assert any(
        error.startswith("exponent_semantic_integer: mapped case must include")
        and "positive exponent" in error
        for error in errors
    )
