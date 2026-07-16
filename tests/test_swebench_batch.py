import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace


def load_batch_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "swebench_batch.py"
    spec = importlib.util.spec_from_file_location("swebench_batch", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_find_report_path_resolves_relative_path(tmp_path):
    batch = load_batch_module()

    path = batch.find_report_path("Report written to report.json\n", tmp_path)

    assert path == tmp_path / "report.json"


def test_read_failure_excerpt_prefers_failure_section(tmp_path):
    batch = load_batch_module()
    output = tmp_path / "test_output.txt"
    output.write_text("noise\nFAILURES\nimportant failure\n" + ("x" * 100), encoding="utf-8")

    excerpt = batch.read_failure_excerpt(output, max_chars=80)

    assert "important failure" in excerpt
    assert "noise" not in excerpt


def test_parser_defaults_to_single_pass_official_mode(tmp_path):
    batch = load_batch_module()

    args = batch.build_parser().parse_args(
        [
            "--dataset",
            str(tmp_path / "data.parquet"),
            "--repo-agent-bin",
            str(tmp_path / "repo-agent"),
            "--model",
            "test-model",
            "--work-dir",
            str(tmp_path / "work"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert args.max_steps == 16
    assert args.implement_model is None
    assert args.evolve_memory is False
    assert args.no_repair_memory is False
    assert args.full_agent_profile is False
    assert args.candidate_mode == "off"
    assert args.offline_cache is False
    assert args.diagnostic_mode == "none"
    assert args.corrective_source_dir is None
    assert not hasattr(args, "max_feedback_rounds")
    assert not hasattr(batch, "build_feedback_task")


def test_format_memory_scope_supports_repo_placeholder(tmp_path):
    batch = load_batch_module()
    args = batch.build_parser().parse_args(
        [
            "--dataset",
            str(tmp_path / "data.parquet"),
            "--repo-agent-bin",
            str(tmp_path / "repo-agent"),
            "--model",
            "test-model",
            "--work-dir",
            str(tmp_path / "work"),
            "--output-dir",
            str(tmp_path / "out"),
            "--memory-scope",
            "swebench-lite:{repo}",
        ]
    )

    assert batch.format_memory_scope({"repo": "astropy/astropy", "instance_id": "x"}, args) == "swebench-lite:astropy/astropy"


def test_swebench_eval_image_uses_instance_id_and_rejects_shell_text():
    batch = load_batch_module()

    assert (
        batch.swebench_eval_image("sphinx-doc__sphinx-10451")
        == "sweb.eval.x86_64.sphinx-doc__sphinx-10451:latest"
    )

    import pytest

    with pytest.raises(ValueError, match="Invalid SWE-bench instance_id"):
        batch.swebench_eval_image("repo__issue; touch /tmp/unsafe")


def test_harness_receives_git_proxy_for_docker_build(tmp_path, monkeypatch):
    batch = load_batch_module()
    captured = {}
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    def fake_run(command, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    args = SimpleNamespace(
        swebench_dir=tmp_path,
        dataset=tmp_path / "data.parquet",
        run_id="proxy-test",
        model="test-model",
        namespace="none",
        cache_level="env",
        harness_timeout=10,
        docker_network="host",
        proxy="http://127.0.0.1:7897",
    )

    batch.run_harness_for_instance(
        {"instance_id": "proj__repo-1"},
        "diff --git a/a.py b/a.py\n",
        output_dir,
        args,
        {},
        0,
    )

    assert captured["env"]["SWEBENCH_GIT_PROXY"] == "http://127.0.0.1:7897"
    assert captured["env"]["SWEBENCH_DOCKER_NETWORK"] == "host"


def test_checkout_repo_offline_cache_skips_fetch(tmp_path, monkeypatch):
    batch = load_batch_module()
    cache_root = tmp_path / "cache"
    mirror = cache_root / "owner__repo.git"
    mirror.mkdir(parents=True)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(batch, "run", fake_run)
    batch.checkout_repo(
        "owner/repo",
        "abc123",
        tmp_path / "checkout",
        cache_root,
        {},
        offline_cache=True,
    )

    assert not any("fetch" in command for command in commands)
    assert any(command[:2] == ["git", "clone"] for command in commands)


def test_finalize_stage_attribution_distinguishes_semantic_and_regression_failures():
    batch = load_batch_module()
    summary = {
        "status": "agent_ok",
        "patch_chars": 120,
        "patch_validation": {"invalid": False, "reasons": []},
        "stage_attribution": {"pre_harness_attribution": "awaiting_harness"},
        "harness": {
            "resolved": False,
            "error": False,
            "instance_report": {
                "tests_status": {
                    "FAIL_TO_PASS": {"success": ["target_a"], "failure": ["target_b"]},
                    "PASS_TO_PASS": {"success": ["old_a"], "failure": ["old_b", "old_c"]},
                }
            },
        },
    }

    attribution = batch.finalize_stage_attribution(summary)

    assert attribution["harness_attribution"]["primary"] == "semantic_fix_and_regression"
    assert attribution["harness_attribution"]["tests"]["fail_to_pass_failed"] == 1
    assert attribution["harness_attribution"]["tests"]["pass_to_pass_failed"] == 2


def test_summarize_stage_attributions_aggregates_primary_outcomes_and_tokens():
    batch = load_batch_module()
    summaries = [
        {
            "passes": [
                {
                    "stage_attribution": {
                        "pre_harness_attribution": "awaiting_harness",
                        "harness_attribution": {"primary": "success"},
                        "stages": {"conditional_review": {"required": True, "completed": True}},
                        "token_usage": {"total": 100},
                    }
                }
            ]
        },
        {
            "passes": [
                {
                    "stage_attribution": {
                        "pre_harness_attribution": "implementation_delivery",
                        "harness_attribution": {"primary": "implementation_delivery"},
                        "stages": {"conditional_review": {"required": False, "completed": False}},
                        "token_usage": {"total": 50},
                    }
                }
            ]
        },
    ]

    result = batch.summarize_stage_attributions(summaries)

    assert result["primary_counts"] == {"success": 1, "implementation_delivery": 1}
    assert result["conditional_review_required"] == 1
    assert result["average_tokens"] == 75.0


def test_build_task_contains_pass1_execution_deadlines(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "proj__repo-1",
            "problem_statement": "TypeError: Writer.__init__() got an unexpected keyword argument 'header_rows'",
            "hints_text": "secret hint that must not be used",
        },
        tmp_path / "repo",
    )

    assert "hints_text" in task
    assert "secret hint" not in task
    assert "Hints From Issue Discussion" not in task
    assert "at most 5 narrow calls" in task
    assert "Never install dependencies" in task
    assert "semantic precision matters" in task
    assert "allowed_ranges" in task
    assert "semantic_coverage" in task
    assert "simplified Coordinator path" in task
    assert "deterministic validation" in task
    assert "optional soft-evidence reproducer" in task
    assert "Reproducer is optional and non-blocking" in task
    assert "one implement Worker" in task
    assert "Conditional patch review required" in task
    assert "target_code_context" in task
    assert "exact original snippet or line_replacements" in task
    assert "adjacent old behavior" in task
    assert "Nullable pandas dtype constraint" not in task
    assert "mandatory patch_review" not in task
    assert "verdict=accept" not in task
    assert "public regression risks inferred only from changed files" not in task
    assert "corrective implement is not a fresh solution attempt" not in task
    assert "dict.pop(orig, 'backend', None)" not in task
    assert "mplot3d/art3d.py" not in task
    assert "second solution attempt" in task
    assert "Run at most one focused verification" in task
    assert "source-backed SemanticEvidence as supplemental context" in task
    assert "covered_cases" in task


def test_build_task_adds_narrow_nullable_pandas_constraint(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "proj__repo-2",
            "problem_statement": "Support nullable pandas dtypes in unique_labels",
        },
        tmp_path / "repo",
    )

    assert "Nullable pandas dtype constraint" in task
    assert "`_pandas_dtype_needs_early_conversion` helper" in task
    assert "`hasattr(y, 'dtypes') and hasattr(y.dtypes, '__array__')`" in task
    assert "for dtype in y.dtypes" in task
    assert "`hasattr(y, 'iloc') and hasattr(y, 'dtype')`" in task
    assert "`_pandas_dtype_needs_early_conversion(y.dtype)`" in task
    assert "`check_array(y, dtype=None, ensure_2d=False)`" in task
    assert "before the existing `is_multilabel(y)` predicate" in task
    assert "parallel `to_numpy`" in task
    assert "forbid `hasattr(y, '__array__')`" in task
    assert "forbid `hasattr(y, 'dtypes')` alone" in task
    assert "forbid `hasattr(y, 'dtype')` alone" in task
    assert "two exact identity guards plus helper-guarded normalization execute before it" in task
    assert "producer code is read-only context even without Reproducer evidence" in task


def test_diagnostic_gold_oracles_expose_scope_without_patch_content(tmp_path):
    batch = load_batch_module()
    row = {
        "instance_id": "proj__repo-1",
        "problem_statement": "Fix public behavior.",
        "patch": (
            "diff --git a/pkg/core.py b/pkg/core.py\n"
            "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
            "@@ -10,3 +10,4 @@\n-old\n+secret_gold_line\n"
        ),
    }

    files_task = batch.build_task(row, tmp_path / "repo", diagnostic_mode="gold_files")
    loci_task = batch.build_task(row, tmp_path / "repo", diagnostic_mode="gold_loci")

    assert 'production files: ["pkg/core.py"]' in files_task
    assert '"pkg/core.py": [[10, 12]]' in loci_task
    assert "secret_gold_line" not in files_task
    assert "secret_gold_line" not in loci_task

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("\n".join(["line"] * 30), encoding="utf-8")
    file_scope = batch.diagnostic_scope_for_row(row, repo, "gold_files")
    loci_scope = batch.diagnostic_scope_for_row(row, repo, "gold_loci")
    assert file_scope == {"allowed_files": ["pkg/core.py"], "allowed_ranges": {"pkg/core.py": [[1, 30]]}}
    assert loci_scope == {"allowed_files": ["pkg/core.py"], "allowed_ranges": {"pkg/core.py": [[10, 12]]}}


def test_unparseable_verification_preserves_statically_safe_patch():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "submission_ready": False,
        "stages": {
            "focused_verification": {"attempts": 1, "result_parseable": False, "passed": False}
        },
    }

    result = batch.validate_model_patch(patch, stage_attribution=attribution, require_patch_review=False)

    assert result["invalid"] is False
    assert result["discard_patch"] is False
    assert "verification_result_unparseable" in result["warnings"]


def test_semantic_stage_gate_discards_patch_before_harness():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "submission_ready": False,
        "workflow_terminal_reason": "semantic_patch_gate_failed",
        "stages": {
            "reproduction": {"semantic_evidence_issues": []},
            "deterministic_validation": {
                "issues": ["semantic_wrong_layer:consumer_compatibility_required_but_producer_changed"]
            },
            "focused_verification": {"attempts": 0},
        },
    }

    result = batch.validate_model_patch(patch, stage_attribution=attribution, require_patch_review=False)

    assert result["invalid"] is True
    assert result["discard_patch"] is True
    assert (
        "semantic_patch_gate_failed:semantic_wrong_layer:consumer_compatibility_required_but_producer_changed"
        in result["reasons"]
    )
    assert "workflow_terminal_gate:semantic_patch_gate_failed" in result["reasons"]


def test_degraded_reproducer_evidence_preserves_static_patch():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "submission_ready": False,
        "workflow_terminal_reason": "semantic_evidence_invalid",
        "stages": {
            "reproduction": {"semantic_evidence_issues": ["semantic_evidence JSON is missing"]},
            "deterministic_validation": {"issues": []},
            "focused_verification": {"attempts": 0},
        },
    }

    result = batch.validate_model_patch(patch, stage_attribution=attribution, require_patch_review=False)

    assert result["invalid"] is False
    assert result["discard_patch"] is False
    assert "semantic_evidence_degraded" in result["warnings"]


def test_failed_behavior_verification_rejects_patch():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "submission_ready": False,
        "stages": {
            "focused_verification": {
                "attempts": 1,
                "result_parseable": True,
                "passed": False,
                "result": {
                    "status": "failed",
                    "command": "python -m pytest tests/test_app.py -q",
                    "exit_code": 1,
                    "evidence": "1 failed",
                },
            }
        },
    }

    result = batch.validate_model_patch(patch, stage_attribution=attribution, require_patch_review=False)

    assert result["invalid"] is True
    assert result["discard_patch"] is True
    assert "verification_evidence_rejected_patch:behavior_failed" in result["reasons"]


def test_static_sanity_rejects_name_made_undefined_by_patch(tmp_path):
    batch = load_batch_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo, check=True)
    app = repo / "app.py"
    app.write_text(
        "def render(value):\n    has_description = set()\n    return value in has_description\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    app.write_text("def render(value):\n    return value in has_description\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
    ).stdout

    reasons = batch.validate_repo_static_sanity(repo, patch)

    assert "python_new_undefined_name:app.py:render:1:has_description" in reasons


def test_corrective_constraints_expand_and_merge_patch_hunks(tmp_path):
    batch = load_batch_module()
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("\n".join(str(i) for i in range(1, 301)), encoding="utf-8")
    patch = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
        "@@ -100,2 +100,3 @@\n-old\n+new\n"
    )

    result = batch.corrective_constraints_from_patch(patch, repo, padding=20)

    assert result["allowed_files"] == ["pkg/core.py"]
    assert result["allowed_ranges"] == {"pkg/core.py": [[80, 122]]}


def test_corrective_failure_attribution_identifies_representation_gap_and_regression():
    batch = load_batch_module()
    patch = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
        "@@ -1 +1,2 @@\n-old\n+if hasattr(value, 'dtype'):\n+    value = convert(value)\n"
    )
    failure = (
        "E       AssertionError: assert 'unknown' == 'continuous-multioutput'\n"
        "FAILED tests/test_core.py::test_dataframe_nullable\n"
    )
    report = {
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": ["target"]},
            "PASS_TO_PASS": {"success": ["old"], "failure": ["regression"]},
        }
    }

    result = batch.build_corrective_failure_attribution(
        patch,
        failure,
        instance_report=report,
    )

    assert result["failure_class"] == "target_incomplete_and_public_regression"
    assert "input_representation_partition_gap" in result["hypothesis_flags"]
    assert "lookup_identity_not_propagated" not in result["hypothesis_flags"]
    assert "patch_overgeneralized_shared_behavior" in result["hypothesis_flags"]
    assert result["first_divergence_candidates"][0]["expected_expression"] == "'continuous-multioutput'"


def test_corrective_task_requires_causal_diagnosis_before_editing():
    batch = load_batch_module()
    task = batch.build_corrective_task(
        {"instance_id": "proj__repo-1", "problem_statement": "Support all public container variants."},
        "diff --git a/app.py b/app.py\n",
        "E assert 'actual' == 'expected'",
        {"failure_class": "target_semantics_incomplete", "correction_obligations": ["trace first divergence"]},
    )

    assert "Deterministic Failure Attribution" in task
    assert "corrective_diagnosis" in task
    assert "invalidated_patch_assumption" in task
    assert "first wrong predicate/identity/state transition" in task


def test_extract_failure_facts_finds_pytest_expected_and_actual_l031_messages():
    batch = load_batch_module()
    failure = r"""FAILURES ===================================
_________________________ test__cli__command_directed __________________________
>       assert result.output.replace("\\", "/").startswith(expected_output)
E       AssertionError: assert False
E        +  where False = <built-in method startswith of str object at 0xabc>('== [test/fixtures/linter/indentation_error_simple.sql] FAIL\nL:   5 | P:  13 | L031 | Avoid aliases in from clauses and join conditions.\n')
E        +    where <built-in method startswith of str object at 0xabc> = '== [test/fixtures/linter/indentation_error_simple.sql] FAIL\nL:   5 | P:  13 | L031 | Avoid using table aliases\nAll Finished!\n'.startswith

test/cli/commands_test.py:70: AssertionError
----------------------------- Captured stdout call -----------------------------
== [test/fixtures/linter/indentation_error_simple.sql] FAIL
L:   5 | P:  13 | L031 | Avoid using table aliases
All Finished!
"""

    facts = batch.extract_failure_facts(failure)

    assert facts["lint_messages"]["expected_l031"] == [
        "Avoid aliases in from clauses and join conditions."
    ]
    assert facts["lint_messages"]["actual_l031"] == ["Avoid using table aliases"]
    assert "expected_output" in facts["assertion_outputs"]
    assert "actual_output" in facts["assertion_outputs"]


def test_extract_failure_facts_pairs_pytest_expected_and_actual_diff_fragments():
    batch = load_batch_module()
    failure = """E       AssertionError: rendered output differs
E         - ***args** (*int*) -- Additional arguments.
E         + ***args** -- Additional arguments.
"""

    facts = batch.extract_failure_facts(failure)

    assert facts["diff_mismatches"] == [
        {
            "actual_fragment": "***args** -- Additional arguments.",
            "expected_fragment": "***args** (*int*) -- Additional arguments.",
        }
    ]


def test_extract_failure_facts_handles_long_django_assertion_line_quickly():
    batch = load_batch_module()
    generated_model = "# generated\\n" + "field = models.CharField(max_length=10)\\n" * 300
    failure = (
        'AssertionError: "from_field = models.ForeignKey('
        "'InspectdbPeople', models.DO_NOTHING, db_column='from_id')\" "
        f'not found in "{generated_model}"'
    )

    facts = batch.extract_failure_facts(failure)

    assert facts == {"exception_types": ["AssertionError"]}


def test_validate_model_patch_rejects_empty_patch():
    batch = load_batch_module()

    validation = batch.validate_model_patch("")

    assert validation["invalid"] is True
    assert validation["reasons"] == ["empty_patch"]


def test_validate_model_patch_rejects_whitespace_only_patch():
    batch = load_batch_module()
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 print("hi")
+
"""

    validation = batch.validate_model_patch(patch)

    assert validation["invalid"] is True
    assert "whitespace_only_patch" in validation["reasons"]


def test_validate_model_patch_accepts_python_indentation_semantic_change():
    batch = load_batch_module()
    patch = """diff --git a/rule.py b/rule.py
--- a/rule.py
+++ b/rule.py
@@ -1,4 +1,4 @@
 if segment:
     if not prev_newline:
         prev_whitespace = seg
-    prev_newline = False
+        prev_newline = False
"""

    validation = batch.validate_model_patch(patch)

    assert validation["invalid"] is False
    assert validation["semantic_changes"] > 0


def test_validate_model_patch_accepts_semantic_patch():
    batch = load_batch_module()
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-raise AssertionError("bad")
+raise ValueError("bad")
"""

    validation = batch.validate_model_patch(patch)

    assert validation["invalid"] is False
    assert validation["semantic_changes"] > 0


def test_validate_model_patch_skips_patch_review_trace_when_not_required(tmp_path):
    batch = load_batch_module()
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-raise AssertionError("bad")
+raise ValueError("bad")
"""

    validation = batch.validate_model_patch(patch, agent_output_dir=tmp_path, require_patch_review=False)

    assert validation["invalid"] is False
    assert "missing_patch_review" not in validation["reasons"]


def test_validate_model_patch_requires_patch_review_trace_in_full_profile(tmp_path):
    batch = load_batch_module()
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-raise AssertionError("bad")
+raise ValueError("bad")
"""

    validation = batch.validate_model_patch(patch, agent_output_dir=tmp_path, require_patch_review=True)

    assert validation["invalid"] is True
    assert "missing_patch_review" in validation["reasons"]


def test_validate_model_patch_rejects_test_edits():
    batch = load_batch_module()
    patch = """diff --git a/tests/test_bug.py b/tests/test_bug.py
--- a/tests/test_bug.py
+++ b/tests/test_bug.py
@@ -1 +1 @@
-assert old()
+assert new()
"""

    validation = batch.validate_model_patch(patch, require_patch_review=False)

    assert validation["invalid"] is True
    assert any(reason.startswith("test_edit_denied:tests/test_bug.py") for reason in validation["reasons"])


def test_validate_repo_static_sanity_rejects_python_syntax_error(tmp_path):
    batch = load_batch_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    patch = """diff --git a/pkg.py b/pkg.py
--- a/pkg.py
+++ b/pkg.py
@@ -1 +1 @@
-print("old")
+def broken(:
"""

    reasons = batch.validate_repo_static_sanity(repo, patch)

    assert any(reason.startswith("python_syntax_error:pkg.py") for reason in reasons)


def test_validate_patch_review_trace_rejects_high_risk_review(tmp_path):
    batch = load_batch_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "type": "tool_result",
                "tool": "agent",
                "response": {
                    "data": {"type": "patch_review", "completed": True},
                    "text": json.dumps({"patch_risk": "high", "verdict": "revise"}),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    reasons = batch.validate_patch_review_trace(tmp_path)

    assert "patch_review_not_accepted:high:revise" in reasons


def test_validate_patch_review_trace_rejects_accept_with_suggested_fix(tmp_path):
    batch = load_batch_module()
    trace = tmp_path / "trace.jsonl"
    review = {
        "patch_risk": "medium",
        "verdict": "accept",
        "findings": [
            {
                "risk_type": "public_regression_risk",
                "suggested_fix": "Add a missing-key guard before pop().",
            }
        ],
    }
    trace.write_text(
        json.dumps(
            {
                "type": "tool_result",
                "tool": "agent",
                "response": {
                    "data": {"type": "patch_review", "completed": True},
                    "text": json.dumps(review),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    reasons = batch.validate_patch_review_trace(tmp_path)

    assert "patch_review_accept_with_suggested_fix" in reasons


def test_validate_patch_review_trace_allows_low_risk_high_fanout_path(tmp_path):
    batch = load_batch_module()
    trace = tmp_path / "trace.jsonl"
    review = {
        "patch_risk": "low",
        "verdict": "accept",
        "findings": [],
        "impact_surface": {
            "changed_core_paths": ["lib/mpl_toolkits/mplot3d/art3d.py"],
        },
    }
    trace.write_text(
        json.dumps(
            {
                "type": "tool_result",
                "tool": "agent",
                "response": {
                    "data": {"type": "patch_review", "completed": True},
                    "text": json.dumps(review),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    reasons = batch.validate_patch_review_trace(tmp_path)

    assert reasons == []


def test_validate_model_patch_rejects_rc_context_backend_restore_antipattern():
    batch = load_batch_module()
    patch = """diff --git a/lib/matplotlib/__init__.py b/lib/matplotlib/__init__.py
--- a/lib/matplotlib/__init__.py
+++ b/lib/matplotlib/__init__.py
@@ -1095,6 +1095,9 @@ def rc_context(rc=None, fname=None):
     finally:
+        _backend = rcParams.get('backend')
+        orig.pop('backend', None)
         dict.update(rcParams, orig)
+        dict.__setitem__(rcParams, 'backend', _backend)
"""

    validation = batch.validate_model_patch(patch)

    assert validation["invalid"] is True
    assert any(reason.startswith("matplotlib_rc_context_backend_restore_policy") for reason in validation["reasons"])


def test_validate_model_patch_rejects_art3d_private_color_cache_antipattern():
    batch = load_batch_module()
    patch = """diff --git a/lib/mpl_toolkits/mplot3d/art3d.py b/lib/mpl_toolkits/mplot3d/art3d.py
--- a/lib/mpl_toolkits/mplot3d/art3d.py
+++ b/lib/mpl_toolkits/mplot3d/art3d.py
@@ -765,6 +765,8 @@ class Poly3DCollection(PolyCollection):
         self._facecolor3d = PolyCollection.get_facecolor(self)
+        self._facecolors2d = self._facecolor3d
+        self._edgecolors2d = self._edgecolor3d
"""

    validation = batch.validate_model_patch(patch)

    assert validation["invalid"] is True
    assert any(reason.startswith("matplotlib_art3d_private_color_cache_policy") for reason in validation["reasons"])
