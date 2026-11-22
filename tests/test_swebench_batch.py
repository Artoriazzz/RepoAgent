import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest


def load_batch_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "swebench_batch.py"
    spec = importlib.util.spec_from_file_location("swebench_batch", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_statement_move_is_not_a_whitespace_only_patch():
    batch = load_batch_module()
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n+append_options()\n append_database()\n-append_options()\n"
    )
    assert not batch.patch_change_stats(patch)["whitespace_only"]


def test_regenerated_public_suite_returns_to_review_without_spending_code_budget(tmp_path, monkeypatch):
    batch = load_batch_module()
    old = {"plan": {"files": [{"path": "tests/test_generated.py", "content": "old"}]},
           "validation": {"feedback_eligible": True}}
    (tmp_path / "public_validation.json").write_text(json.dumps(old))
    calls = []

    def review(row, repo, out, task, patch, validation, report, args, env):
        calls.append((out.name, patch, validation["generated_test_revision_available"]))
        return {"route": "test_regeneration", "attempted": False, "feedback": {"test_revision_request": "Use unique usernames"}}

    def regenerate(row, repo, out, task, artifact, feedback, args, env, image):
        assert artifact["plan"]["files"][0]["content"] == "old"
        assert feedback["test_revision_request"] == "Use unique usernames"
        return {"plan": {"cases": [{"id": "target"}]}}

    monkeypatch.setattr(batch, "run_experience_correction", review)
    monkeypatch.setattr(batch, "regenerate_public_suite", regenerate)
    monkeypatch.setattr(batch, "evaluate_public_plan", lambda *a: {"status": "failed", "feedback_eligible": True})
    summary = {}
    assert batch.run_public_test_corrections({}, tmp_path, tmp_path, "issue", "patch", summary, SimpleNamespace(), {}) == "patch"
    assert calls == [("public-correction-1", "patch", True), ("public-correction-1-revised-suite", "patch", False)]
    assert summary["public_test_regeneration"]["status"] == "evaluated"


def test_generated_fixture_error_routes_to_test_agent_only():
    from repo_agent.core.experience import parse_failure_feedback

    batch = load_batch_module()
    payload = {
        "failure": {"kind": "invalid_test", "quality": "exception_detail",
                    "observations": ["setUpTestData raised IntegrityError before any assertions"],
                    "cause": "Both fixtures created the same unique username."},
        "repair": {"edits": [], "preserve": []},
        "evidence": {"items": [{"id": "E1", "source": "failure_log", "support": "direct",
                                "fact": "UNIQUE constraint failed: auth_user.username; Ran 0 tests"}],
                     "used_ids": ["E1"]},
        "decision": {"action": "regenerate_tests", "request": "Give each User a different username."},
    }
    feedback = parse_failure_feedback(json.dumps(payload))
    assert feedback is not None
    assert batch.validated_failure_feedback_route(feedback.to_dict()) == "test_regeneration"
    payload["evidence"]["items"][0]["support"] = "inference"
    assert parse_failure_feedback(json.dumps(payload)) is None


def test_api_failure_is_not_an_invalid_patch(tmp_path):
    batch = load_batch_module()
    stdout = "Traceback (most recent call last):\nopenai.APITimeoutError: Request timed out.\n"
    result = subprocess.CompletedProcess([], 1, stdout=stdout)
    assert batch.agent_process_status(result) == "api_error"
    assert batch.agent_process_status(subprocess.CompletedProcess([], 1, stdout="AssertionError: failed")) == "agent_error"
    assert batch.agent_process_status(subprocess.CompletedProcess([], 0, stdout=stdout)) == "agent_ok"
    assert batch.api_error_type("The task mentions openai.APITimeoutError: as an example") is None
    stage = tmp_path / "pass-1" / "experience-correction" / "corrective-implement"
    stage.mkdir(parents=True)
    (stage / "agent_stdout.txt").write_text(stdout)
    errors = batch.collect_api_errors(tmp_path)
    assert errors == [{"type": "APITimeoutError", "log_path": str(stage / "agent_stdout.txt"),
                       "stage": "pass-1/experience-correction/corrective-implement"}]


@pytest.mark.parametrize("line", [
    "RuntimeError: Incomplete LLM stream: finish_reason=network_error",
    "repo_agent.core.llm.LLMStreamInterruptedError: Incomplete LLM stream: finish_reason=network_error",
])
def test_failed_review_stream_is_counted_as_api_error(tmp_path, line):
    batch = load_batch_module()
    stage = tmp_path / "experience-correction/failure-review"
    stage.mkdir(parents=True)
    (stage / "review_stdout.txt").write_text("Traceback (most recent call last):\n" + line + "\n")
    assert batch.collect_api_errors(tmp_path)[0]["type"] == "LLMStreamInterruptedError"
    assert batch.api_error_type("The task mentions " + line) is None


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


@pytest.mark.parametrize("status,risk,completed,accepted", [
    ("completed", False, True, True), ("partial", False, True, False),
    ("blocked", False, True, False), ("completed", True, True, True),
    ("completed", False, False, False),
])
def test_corrective_delivery_rejects_partial_patch(tmp_path, monkeypatch, status, risk, completed, accepted):
    batch = load_batch_module()
    scope = {"allowed_files": ["module.py"], "allowed_ranges": {"module.py": [[1, 20]]}}
    monkeypatch.setattr(batch, "corrective_constraints_from_patch", lambda *a, **k: scope)
    monkeypatch.setattr(batch, "add_reasoning_cli_args", lambda *a: None)
    monkeypatch.setattr(batch, "add_memory_cli_args", lambda *a: None)
    monkeypatch.setattr(batch, "git_diff", lambda *a: "partial-or-final-patch")
    restored = []
    monkeypatch.setattr(batch, "restore_worktree_patch", lambda repo, patch: restored.append(patch))
    out = tmp_path / "corrective"
    def run(command, **kwargs):
        report = {"status": status, "remaining_changes": [] if status == "completed" else ["caller"],
                  "block_reason": "" if status == "completed" else "scope",
                  "known_risks": [{"overlaps_changed_contract": risk}]}
        (out / "worker_result.json").write_text(json.dumps({"completed": completed, "text": json.dumps(report)}))
        return subprocess.CompletedProcess(command, 0, stdout="done")
    monkeypatch.setattr(batch.subprocess, "run", run)
    result = batch.run_inline_experience_corrective({}, tmp_path, out, "task", "old-patch", {},
        SimpleNamespace(repo_agent_bin="agent", model="test", implement_model=None, timeout=10), {})
    assert result["model_patch"] == ("partial-or-final-patch" if accepted else "old-patch")
    assert result["status"] == ("agent_ok" if accepted else "corrective_incomplete")
    assert restored == ([] if accepted else ["old-patch"])
    assert (out / "model.patch").read_text() == "partial-or-final-patch"


def test_corrective_routes_same_file_missing_range_before_edit(tmp_path, monkeypatch):
    batch = load_batch_module()
    monkeypatch.setattr(batch, "corrective_constraints_from_patch", lambda *a, **k: {
        "allowed_files": ["module.py"], "allowed_ranges": {"module.py": [[198, 444]]}})
    monkeypatch.setattr(batch, "add_reasoning_cli_args", lambda *a: None)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        replan = Path(command[command.index("--output-dir") + 1])
        (replan / "replan.json").write_text(json.dumps({"status": "blocked"}))
        return subprocess.CompletedProcess(command, 0, stdout="blocked")
    monkeypatch.setattr(batch.subprocess, "run", run)
    feedback = {"required_edits": [{"path": "module.py", "start_line": 580, "end_line": 585, "change": "caller"}]}
    result = batch.run_inline_experience_corrective({}, tmp_path, tmp_path / "corrective", "task", "old", feedback,
        SimpleNamespace(repo_agent_bin="agent", model="test", timeout=10), {})
    assert commands[0][1] == "coordinator-replan"
    assert len(commands) == 1
    assert result["implement_attempted"] is False


def test_read_failure_excerpt_prioritizes_target_over_early_environment_noise(tmp_path):
    batch = load_batch_module()
    output = tmp_path / "test_output.txt"
    output.write_text(
        "FAILURES\n"
        "____________________________ test_clipping ____________________________\n"
        "/site-packages/tool.py: DeprecationWarning: old configuration\n"
        + ("environment stack\n" * 300)
        + "________________ test_format_cursor_data_BoundaryNorm ________________\n"
        "assert img.format_cursor_data(v) == label\n"
        "E AssertionError: assert '[0.900]' == '[0.9]'\n"
        "=============================== PASSES ===============================\n"
        "PASSED unrelated_test\n",
        encoding="utf-8",
    )
    report = {
        "tests_status": {
            "FAIL_TO_PASS": {
                "success": [],
                "failure": ["lib/matplotlib/tests/test_artist.py::test_format_cursor_data_BoundaryNorm"],
            },
            "PASS_TO_PASS": {
                "success": [],
                "failure": ["lib/matplotlib/tests/test_artist.py::test_clipping"],
            },
        }
    }

    excerpt = batch.read_failure_excerpt(
        output,
        max_chars=2_000,
        instance_report=report,
        changed_files=["lib/matplotlib/artist.py"],
    )

    assert excerpt.startswith("## TARGET FAILURE")
    assert "assert '[0.900]' == '[0.9]'" in excerpt
    assert "environment stack\nenvironment stack\nenvironment stack" not in excerpt
    assert "PASSED unrelated_test" not in excerpt
    assert "OTHER FAILURE SUMMARY" in excerpt
    assert "DeprecationWarning" in excerpt


def test_read_failure_excerpt_keeps_changed_path_regression_after_target(tmp_path):
    batch = load_batch_module()
    output = tmp_path / "test_output.txt"
    output.write_text(
        "FAILURES\n"
        "________________________ test_same_relations _________________________\n"
        "E AssertionError: expected related_name, got none\n"
        "________________ test_attribute_name_not_python_keyword _____________\n"
        "django/core/management/commands/inspectdb.py:190\n"
        "E AssertionError: unexpected related_name on from_field\n",
        encoding="utf-8",
    )
    report = {
        "tests_status": {
            "FAIL_TO_PASS": {
                "success": [],
                "failure": ["test_same_relations (inspectdb.tests.InspectDBTestCase)"],
            },
            "PASS_TO_PASS": {
                "success": [],
                "failure": ["test_attribute_name_not_python_keyword (inspectdb.tests.InspectDBTestCase)"],
            },
        }
    }

    excerpt = batch.read_failure_excerpt(
        output,
        instance_report=report,
        changed_files=["django/core/management/commands/inspectdb.py"],
    )

    assert "## TARGET FAILURE" in excerpt
    assert "expected related_name, got none" in excerpt
    assert "## REGRESSION FAILURE" in excerpt
    assert "unexpected related_name on from_field" in excerpt


def test_failure_excerpt_handles_two_underscore_header_and_stops_before_warnings(tmp_path):
    batch = load_batch_module()
    name = "NumpyDocstringTest.test_underscore_in_attribute_strip_signature_backslash"
    failure = (
        f"__ {name} ___\n"
        "    self.assertEqual(expected, actual)\n"
        "E AssertionError: expected arg\\_, actual arg_\n"
        "tests/test_ext_napoleon_docstring.py:1431: AssertionError\n"
    )
    output = tmp_path / "test_output.txt"
    output.write_text(
        "=================================== FAILURES ===================================\n"
        + failure
        + "=============================== warnings summary ===============================\n"
        "/site-packages/pkg_resources/__init__.py: DeprecationWarning: deprecated\n"
        "==================================== PASSES ====================================\n"
        "PASSED unrelated_test\n",
        encoding="utf-8",
    )
    report = {"tests_status": {"PASS_TO_PASS": {"failure": [
        "tests/test_ext_napoleon_docstring.py::" + name.replace(".", "::", 1)
    ]}}}

    blocks = batch._harness_failure_blocks(output.read_text())
    assert len(blocks) == 1
    assert blocks[0][1] == failure.strip()
    excerpt = batch.read_failure_excerpt(output, instance_report=report)
    assert excerpt.startswith("## REGRESSION FAILURE:")
    assert "expected arg\\_, actual arg_" in excerpt
    assert "DeprecationWarning" not in excerpt
    assert "PASSED unrelated_test" not in excerpt


def test_failure_excerpt_preserves_assertion_with_warning_in_fallback(tmp_path):
    batch = load_batch_module()
    output = tmp_path / "test_output.txt"
    output.write_text(
        "test_attribute_regression\n"
        "E AssertionError: expected escaped, actual unescaped\n"
        "/site-packages/pkg_resources/__init__.py: DeprecationWarning: deprecated\n",
        encoding="utf-8",
    )
    report = {"tests_status": {"PASS_TO_PASS": {"failure": ["test_attribute_regression"]}}}

    excerpt = batch.read_failure_excerpt(output, instance_report=report)
    assert excerpt.startswith("## REGRESSION FAILURE:")
    assert "AssertionError: expected escaped, actual unescaped" in excerpt


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
    assert args.repair_mode == "benchmark"
    assert args.memory_provenance == ""
    assert args.implement_model is None
    assert args.evolve_memory is False
    assert args.no_repair_memory is False
    assert args.full_agent_profile is False
    assert args.candidate_mode == "off"
    assert args.offline_cache is False
    assert args.diagnostic_mode == "none"
    assert args.corrective_source_dir is None
    assert args.harness_python is None
    assert args.prebuild_instance_image is None
    assert args.image_build_timeout == 3600
    assert not hasattr(args, "max_feedback_rounds")
    assert not hasattr(batch, "build_feedback_task")


def test_batch_validates_cli_normalized_failure_routes():
    batch = load_batch_module()
    actionable = {
        "correction_route": "corrective_implement",
        "correction_readiness": "actionable",
        "failure_observation_quality": "expected_actual",
        "scope_expansion": [],
        "evidence_ledger": [
            {
                "id": "E1",
                "source": "public_task",
                "fact": "Expected canonical identity.",
                "support": "direct",
            },
            {
                "id": "E2",
                "source": "failure_log",
                "fact": "Observed alias identity.",
                "support": "direct",
            },
        ],
        "correction_evidence_ids": ["E1", "E2"],
    }
    assert batch.validated_failure_feedback_route(actionable) == "corrective_implement"
    replan = {**actionable, "correction_route": "coordinator_replan",
              "correction_readiness": "needs_replan", "next_evidence_source": "repository_code",
              "replan_reason": "Read and authorize producer.py outside old scope"}
    assert batch.validated_failure_feedback_route(replan) == "coordinator_replan"
    failure_only_replan = {**replan, "failure_observation_quality": "failure_only"}
    assert batch.validated_failure_feedback_route(failure_only_replan) == "coordinator_replan"
    assert batch.validated_failure_feedback_route({**failure_only_replan, "replan_reason": ""}) is None
    assert batch.validated_failure_feedback_route({**failure_only_replan, "next_evidence_source": "none"}) is None
    assert batch.validated_failure_feedback_route({**failure_only_replan, "scope_expansion": ["New feature"]}) is None
    assert batch.validated_failure_feedback_route({**failure_only_replan, "failure_observation_quality": "unknown"}) is None
    assert batch.validated_failure_feedback_route({**failure_only_replan, "correction_evidence_ids": ["E1"]}) is None
    assert batch.validated_failure_feedback_route({**failure_only_replan, "correction_evidence_ids": ["E2"]}) == "coordinator_replan"
    assert batch.validated_failure_feedback_route({**actionable, "correction_evidence_ids": ["E2"]}) is None
    assert batch.validated_failure_feedback_route({**replan, "scope_expansion": ["New feature"]}) is None
    actionable["evidence_ledger"].append(
        {
            "id": "E3",
            "source": "failed_patch",
            "fact": "The alias assignment is the likely causal link.",
            "support": "inference",
        }
    )
    actionable["correction_evidence_ids"].append("E3")
    assert batch.validated_failure_feedback_route(actionable) == "corrective_implement"
    actionable["evidence_ledger"][-1]["support"] = "unknown"
    assert batch.validated_failure_feedback_route(actionable) is None
    actionable["evidence_ledger"][-1]["support"] = "inference"
    actionable["failure_observation_quality"] = "failure_only"
    assert batch.validated_failure_feedback_route(actionable) is None
    actionable["evidence_ledger"].append({"id": "E4", "source": "repository_code",
        "fact": "producer.py:20 lacks the required conversion", "support": "direct"})
    actionable["correction_evidence_ids"].append("E4")
    assert batch.validated_failure_feedback_route(actionable) == "corrective_implement"


@pytest.mark.parametrize("context_source", ["public_task", "failed_patch", "semantic_evidence", "repository_code"])
@pytest.mark.parametrize("quality", ["exception_detail", "expected_actual", "failure_only"])
def test_batch_correction_accepts_direct_source_evidence_without_redundant_task_reference(context_source, quality):
    from repo_agent.core.experience import parse_failure_feedback

    batch = load_batch_module()
    feedback = {
        "failure_stage": "implementation_semantics",
        "correction_route": "corrective_implement", "correction_readiness": "actionable",
        "failure_observation_quality": quality, "scope_expansion": [],
        "required_edits": [{"path": "value.py", "start_line": 10, "end_line": 12, "change": "Restore hashing consistent with equality."}],
        "evidence_ledger": [
            {"id": "E1", "source": "failure_log", "support": "direct", "fact": "hash(value) raises TypeError"},
            {"id": "E2", "source": context_source, "support": "direct", "fact": "The class defines __eq__ without __hash__"},
            {"id": "E3", "source": "semantic_evidence", "support": "inference", "fact": "Equal objects need equal hashes"},
        ],
        "correction_evidence_ids": ["E1", "E2", "E3"],
    }
    expected = "corrective_implement" if quality != "failure_only" or context_source == "repository_code" else None
    assert batch.validated_failure_feedback_route(feedback) == expected
    parsed = parse_failure_feedback(json.dumps(feedback))
    assert parsed is not None
    assert parsed.correction_route == (expected or "none")
    feedback["evidence_ledger"][1]["support"] = "inference"
    assert batch.validated_failure_feedback_route(feedback) is None
    feedback["evidence_ledger"][1]["support"] = "unknown"
    assert batch.validated_failure_feedback_route(feedback) is None


def test_output_limit_is_not_reported_as_invalid_patch():
    batch = load_batch_module()
    error = "repo_agent.core.llm.LLMOutputLimitError: Incomplete LLM stream: finish_reason=length\n"
    result = subprocess.CompletedProcess([], 1, stdout=error)
    assert batch.agent_process_status(result) == "output_limit"
    assert batch.agent_process_status(subprocess.CompletedProcess([], 1, stdout="task mentions LLMOutputLimitError")) == "agent_error"
    attribution = batch.finalize_stage_attribution({"status": "output_limit", "patch_chars": 0})
    assert attribution["harness_attribution"]["primary"] == "model_output_limit"


def test_batch_extracts_reproducer_before_after_lightweight_validation():
    batch = load_batch_module()
    attribution = {
        "stages": {
            "reproduction": {
                "contract": {
                    "target_partitions": [
                        {
                            "id": "symbolic_invariant",
                            "expected": "symbolic integer assumptions preserve the old branch",
                            "evidence": [
                                {
                                    "type": "code",
                                    "location": "src/mod.py:40",
                                    "claim": "the original guard accepts integer exponents",
                                }
                            ],
                        }
                    ]
                },
                "baseline_execution": {
                    "requested_command": "python -m pytest tests/test_public.py -q",
                    "exit_code": 1,
                    "output": "expected canonical, got alias",
                }
            },
            "focused_verification": {
                "result": {
                    "status": "failed",
                    "command": "python -m pytest tests/test_public.py -q",
                    "exit_code": 1,
                    "evidence": "expected canonical, got requested alias",
                }
            },
        },
    }

    result = batch.lightweight_validation_from_attribution(attribution)

    assert result["status"] == "still_failing"
    assert result["feedback_eligible"] is True
    assert result["semantic_evidence"]["target_partitions"][0]["id"] == "symbolic_invariant"


def test_experience_harness_feedback_routes_only_real_unresolved_failures():
    batch = load_batch_module()
    report = {
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": ["test_target"]},
            "PASS_TO_PASS": {"success": ["test_old"], "failure": []},
        }
    }
    feedback = batch.build_experience_harness_feedback(
        {
            "resolved": False,
            "unresolved": True,
            "error": False,
            "failure_excerpt": "AssertionError: expected 2, got 1",
            "failure_facts": {"exception_types": ["AssertionError"]},
            "instance_report": report,
        },
        "diff --git a/app.py b/app.py\n",
    )
    error_feedback = batch.build_experience_harness_feedback(
        {
            "resolved": False,
            "unresolved": False,
            "error": True,
            "failure_excerpt": "Docker timeout",
            "instance_report": {},
        },
        "diff --git a/app.py b/app.py\n",
    )

    assert feedback["feedback_eligible"] is True
    assert feedback["test_counts"]["fail_to_pass_failed"] == 1
    assert feedback["failure_attribution"]["failure_class"] == "target_semantics_incomplete"
    assert error_feedback["feedback_eligible"] is False
    assert "infrastructure errors" in error_feedback["reason"]


def test_harness_dependency_contamination_is_not_code_feedback():
    batch = load_batch_module()
    report = {
        "tests_status": {
            "FAIL_TO_PASS": {"success": ["test_target"], "failure": []},
            "PASS_TO_PASS": {"success": [], "failure": ["test_image[png]", "test_image[pdf]"]},
        }
    }
    output = """
________________ test_image[png] _________________
E DeprecationWarning: Version scheme 'release-branch-semver' has been renamed
________________ test_image[pdf] _________________
E ResourceWarning: unclosed file '/tmp/image.pdf'
E pytest.PytestUnraisableExceptionWarning: Exception ignored in FileIO
================ short test summary info ================
"""

    contamination = batch.classify_harness_environment_contamination(report, output)
    feedback = batch.build_experience_harness_feedback(
        {
            "resolved": False,
            "unresolved": True,
            "error": False,
            "failure_excerpt": output,
            "instance_report": report,
            "environment_contamination": contamination,
        },
        "diff --git a/app.py b/app.py\n",
    )

    assert contamination["contaminated"] is True
    assert feedback["feedback_eligible"] is False
    assert "toolchain contamination" in feedback["reason"]


def test_differential_expansion_is_opt_in_and_accepts_safe_diagnostic_validation():
    batch = load_batch_module()

    assert not batch.should_run_experience_differential({"status": "fixed"})
    assert batch.should_run_experience_differential({"status": "fixed"}, enabled=True)
    assert batch.should_run_experience_differential(
        {
            "status": "invalid_reproducer",
            "command": "python -c 'assert 1 == 1'",
            "baseline": {
                "command": "python -c 'assert 1 == 1'",
                "exit_code": 0,
                "status": "passed",
            },
        },
        enabled=True,
    )


def test_differential_expansion_rejects_real_failure_and_unsafe_baseline():
    batch = load_batch_module()

    assert not batch.should_run_experience_differential(
        {
            "status": "still_failing",
            "baseline": {
                "command": "python -c 'raise AssertionError'",
                "exit_code": 1,
            },
        },
        enabled=True,
    )
    assert not batch.should_run_experience_differential(
        {
            "status": "invalid_reproducer",
            "baseline": {
                "command": "python -c 'import subprocess'",
                "exit_code": 0,
            },
        },
        enabled=True,
    )


def test_differential_expansion_replaces_executed_import_error_reproducer():
    batch = load_batch_module()

    assert batch.should_run_experience_differential(
        {
            "status": "invalid_reproducer",
            "command": "python -c 'from package import missing_name'",
            "baseline": {
                "attempted": True,
                "status": "failed",
                "command": "python -c 'from package import missing_name'",
                "exit_code": 1,
                "evidence": "ImportError: cannot import name 'missing_name'",
            },
        },
        enabled=True,
    )


def test_differential_expansion_does_not_replace_docker_environment_failure():
    batch = load_batch_module()

    assert not batch.should_run_experience_differential(
        {
            "status": "invalid_reproducer",
            "command": "python -c 'print(1)'",
            "baseline": {
                "attempted": True,
                "status": "blocked_by_environment",
                "command": "python -c 'print(1)'",
                "exit_code": None,
                "evidence": "Cannot connect to the Docker daemon",
            },
        },
        enabled=True,
    )


def test_invalid_validation_review_routes_to_differential_replacement():
    batch = load_batch_module()

    assert batch.correction_rejected_invalid_validation(
        {
            "attempted": False,
            "route": "none",
            "failure_stage": "invalid_test",
            "feedback": {"correction_readiness": "not_needed"},
        }
    )
    assert not batch.correction_rejected_invalid_validation(
        {
            "attempted": True,
            "route": "corrective_implement",
            "failure_stage": "implementation_semantics",
            "feedback": {"correction_readiness": "actionable"},
        }
    )


def test_complete_gate_does_not_correct_from_relation_without_oracle():
    batch = load_batch_module()
    validation = {
        "validation_kind": "differential_trace",
        "test_plan": {
            "guard_coverage_complete": True,
            "guard_coverage": [
                {
                    "obligation_id": "exponent_negative_boundary",
                    "case_ids": ["negative_exp"],
                    "required_relation": "must_change",
                }
            ],
        },
        "case_matrix": [{"case": "negative_exp", "changed": False}],
    }
    feedback = {
        "failure_stage": "complete",
        "issue_contract": ["Do not take the zero branch outside its valid domain."],
        "regression_guards": [],
    }

    gated = batch.enforce_differential_complete_gate(
        feedback,
        validation,
        "diff --git a/mod.py b/mod.py\n",
    )

    assert gated["failure_stage"] == "insufficient_evidence"
    assert gated["correction_route"] == "none"
    assert gated["controller_oracle_gate"] is True
    assert batch.validated_failure_feedback_route(gated) == "none"


def test_complete_gate_preserves_complete_when_all_relations_hold():
    batch = load_batch_module()
    validation = {
        "validation_kind": "differential_trace",
        "test_plan": {
            "guard_coverage_complete": True,
            "guard_coverage": [
                {
                    "obligation_id": "target",
                    "case_ids": ["reported"],
                    "required_relation": "must_change",
                },
                {
                    "obligation_id": "invariant",
                    "case_ids": ["old"],
                    "required_relation": "must_preserve",
                },
            ],
            "cases": [
                {
                    "id": "reported",
                    "role": "target",
                    "expected_relation": "must_change",
                    "oracle_strength": "exact",
                },
                {
                    "id": "old",
                    "role": "invariant",
                    "expected_relation": "must_preserve",
                    "oracle_strength": "invariant",
                },
            ],
        },
        "case_matrix": [
            {"case": "reported", "role": "target", "oracle_strength": "exact", "oracle_passed": True, "changed": True},
            {"case": "old", "role": "invariant", "oracle_strength": "invariant", "oracle_passed": True, "changed": False},
        ],
    }
    feedback = {"failure_stage": "complete"}

    assert (
        batch.enforce_differential_complete_gate(feedback, validation, "patch")
        == feedback
    )


def test_complete_gate_checks_every_declared_case_relation():
    batch = load_batch_module()
    validation = {
        "validation_kind": "differential_trace",
        "test_plan": {
            "guard_coverage_complete": True,
            "guard_coverage": [],
            "cases": [
                {
                    "id": "reported",
                    "expected_relation": "must_change",
                }
            ],
        },
        "case_matrix": [{"case": "reported", "changed": False}],
    }

    violations = batch.differential_complete_gate_violations(validation)

    assert violations == []
    assert batch.differential_oracle_gaps(validation) == ["reported"]


def test_relation_gate_overrides_invalid_failure_review_output():
    batch = load_batch_module()
    validation = {
        "validation_kind": "differential_trace",
        "test_plan": {
            "guard_coverage_complete": True,
            "guard_coverage": [],
            "cases": [
                {"id": "negative", "expected_relation": "must_change"}
            ],
        },
        "case_matrix": [{"case": "negative", "changed": False}],
    }
    invalid_feedback = {
        "parse_error": True,
        "raw": {"failure_observation_quality": "insufficient"},
    }

    gated = batch.enforce_differential_relation_gate(
        invalid_feedback,
        validation,
        "diff --git a/mod.py b/mod.py\n",
    )

    assert gated["controller_oracle_gate"] is True
    assert gated["correction_route"] == "none"
    assert batch.validated_failure_feedback_route(gated) == "none"


def test_baseline_matching_exact_target_is_invalid_probe_not_corrective():
    batch = load_batch_module()
    validation = {
        "validation_kind": "differential_trace",
        "invalid_target_cases": ["reverse_relation"],
        "case_matrix": [],
    }

    gated = batch.enforce_differential_relation_gate(
        {"failure_stage": "implementation_semantics"}, validation, "patch"
    )

    assert gated["failure_stage"] == "invalid_test"
    assert gated["correction_route"] == "none"


def test_experience_validation_preserves_failed_patch_for_lightweight_correction():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "stages": {
            "focused_verification": {
                "attempts": 1,
                "result_parseable": True,
                "result": {
                    "status": "failed",
                    "command": "python -m pytest tests/test_public.py -q",
                    "exit_code": 1,
                    "evidence": "1 failed",
                },
            }
        }
    }

    result = batch.validate_model_patch(
        patch,
        stage_attribution=attribution,
        require_patch_review=False,
        reject_failed_verification=False,
    )

    assert result["invalid"] is False
    assert result["discard_patch"] is False
    assert "verification_failed_feedback_eligible:behavior_failed" in result["warnings"]


@pytest.mark.parametrize("public_status", ["passed", "failed", "inconclusive"])
def test_public_preview_only_scores_a_publicly_passing_patch(tmp_path, monkeypatch, public_status):
    batch = load_batch_module()
    output = tmp_path / "out"
    output.mkdir()
    patch = "nonempty patch"
    calls = []
    monkeypatch.setattr(batch, "checkout_repo", lambda *a, **k: None)
    monkeypatch.setattr(batch, "git_diff", lambda *a: patch)
    monkeypatch.setattr(batch, "run_agent_pass", lambda *a, **k: {
        "model_patch": patch, "status": "agent_ok", "output_dir": str(output / "pass-1"),
    })
    monkeypatch.setattr(batch, "validate_model_patch", lambda *a, **k: {})

    def public_tests(*args, **kwargs):
        args[5]["public_validation"] = {"status": public_status}
        return patch

    def harness(*args, **kwargs):
        calls.append(args[1])
        return {"resolved": True}

    monkeypatch.setattr(batch, "run_public_test_corrections", public_tests)
    monkeypatch.setattr(batch, "run_harness_for_instance", harness)
    monkeypatch.setattr(batch, "run_experience_curator_for_success", lambda *a, **k: pytest.fail("memory write"))
    args = SimpleNamespace(
        work_dir=tmp_path / "work", diagnostic_mode="none", full_agent_profile=False,
        repair_mode="benchmark", require_public_pass=True, feedback_source="public-tests",
        evaluate=True, evolve_memory=False, timeout=60, offline_cache=False, prebuild_instance_image=False,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    result = batch.run_instance_once({"instance_id": "owner__repo-1", "repo": "owner/repo",
                                      "base_commit": "abc", "problem_statement": "fix public behavior"},
                                     repo, output, args, {})
    assert calls == ([patch] if public_status == "passed" else [])
    assert result["model_patch"] == patch
    assert not result["official_pass1"]
    if public_status != "passed":
        assert result["passes"][0]["harness"]["reason"] == "public_tests_not_passed"


@pytest.mark.parametrize("second_result", ["resolved", "failed", "error"])
def test_experience_batch_uses_initial_harness_failure_then_evaluates_correction(tmp_path, monkeypatch, second_result):
    batch = load_batch_module()
    repo = tmp_path / "work" / "repos" / "owner__repo-1"
    output = tmp_path / "out"
    output.mkdir()
    initial_patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+first\n"
    corrected_patch = initial_patch.replace("+first", "+corrected")
    final_patch = initial_patch.replace("+first", "+final")
    current = {"patch": initial_patch}
    harness_calls = []
    correction_feedback = []

    def fake_checkout(*args, **kwargs):
        repo.mkdir(parents=True, exist_ok=True)

    def fake_agent_pass(*args, **kwargs):
        return {
            "status": "agent_ok",
            "agent_exit_code": 0,
            "patch_chars": len(initial_patch),
            "model_patch": initial_patch,
            "output_dir": str(output / "pass-1"),
            "stage_attribution": {
                "problem_statement": "fix public behavior",
                "stages": {
                    "reproduction": {
                        "baseline_execution": {
                            "requested_command": "python -m pytest tests/test_public.py -q",
                            "exit_code": 1,
                            "output": "expected fixed, got old",
                        }
                    },
                    "focused_verification": {
                        "result": {
                            "status": "failed",
                            "command": "python -m pytest tests/test_public.py -q",
                            "exit_code": 1,
                            "evidence": "expected fixed, got first",
                        }
                    },
                }
            },
        }

    def fake_correction(*args, **kwargs):
        correction_feedback.append(args[5])
        selected_patch = corrected_patch if len(correction_feedback) == 1 else final_patch
        current["patch"] = selected_patch
        feedback_file = output / "pass-1" / "experience-correction" / "feedback.json"
        feedback_file.parent.mkdir(parents=True, exist_ok=True)
        feedback_file.write_text("{}", encoding="utf-8")
        return {
            "attempted": True,
            "model_patch": selected_patch,
            "output_dir": str(feedback_file.parent),
            "feedback_file": str(feedback_file),
            "stage_attribution": {},
        }

    def fake_harness(*args, **kwargs):
        harness_calls.append(args[1])
        if len(harness_calls) == 1 or (len(harness_calls) == 2 and second_result in {"failed", "error"}):
            return {
                "resolved": False,
                "unresolved": True,
                "error": len(harness_calls) == 2 and second_result == "error",
                "failure_excerpt": "AssertionError: expected fixed, got first",
                "failure_facts": {"exception_types": ["AssertionError"]},
                "instance_report": {
                    "tests_status": {
                        "FAIL_TO_PASS": {"success": [], "failure": ["test_public"]},
                        "PASS_TO_PASS": {"success": ["test_old"], "failure": []},
                    }
                },
            }
        return {
            "resolved": True,
            "unresolved": False,
            "error": False,
            "failure_excerpt": "",
            "failure_facts": {},
            "instance_report": {},
        }

    monkeypatch.setattr(batch, "checkout_repo", fake_checkout)
    monkeypatch.setattr(batch, "run_agent_pass", fake_agent_pass)
    monkeypatch.setattr(batch, "git_diff", lambda path: current["patch"])
    monkeypatch.setattr(
        batch,
        "validate_model_patch",
        lambda patch, **kwargs: {"invalid": False, "discard_patch": False, "reasons": [], "warnings": []},
    )
    monkeypatch.setattr(batch, "run_experience_correction", fake_correction)
    monkeypatch.setattr(
        batch,
        "run_experience_curator_for_success",
        lambda *args, **kwargs: {"status": "saved"},
    )
    monkeypatch.setattr(
        batch,
        "run_lightweight_feedback_command",
        lambda *args, **kwargs: {
            "status": "passed",
            "command": "python -m pytest tests/test_public.py -q",
            "exit_code": 0,
            "evidence": "1 passed",
        },
    )
    monkeypatch.setattr(batch, "run_harness_for_instance", fake_harness)

    args = SimpleNamespace(
        work_dir=tmp_path / "work",
        diagnostic_mode="none",
        corrective_source_dir=None,
        full_agent_profile=False,
        repair_mode="experience-build",
        evaluate=True,
        evolve_memory=False,
        timeout=60,
        offline_cache=False,
        prebuild_instance_image=False,
    )
    result = batch.run_instance_once(
        {
            "instance_id": "owner__repo-1",
            "repo": "owner/repo",
            "base_commit": "abc",
            "problem_statement": "fix public behavior",
        },
        repo,
        output,
        args,
        {},
    )

    assert result["model_patch"] == (final_patch if second_result == "failed" else corrected_patch), result
    assert harness_calls == ([initial_patch, corrected_patch, final_patch] if second_result == "failed" else [initial_patch, corrected_patch])
    assert len(correction_feedback) == (2 if second_result == "failed" else 1)
    assert correction_feedback[0]["validation_kind"] == "swebench_harness_feedback"
    assert correction_feedback[0]["feedback_eligible"] is True
    assert result["passes"][0]["initial_harness"]["unresolved"] is True
    assert result["passes"][0]["harness"]["resolved"] is (second_result != "error")


def test_experience_build_requires_evaluation_and_provenance(tmp_path):
    batch = load_batch_module()
    base = [
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
        "--repair-mode",
        "experience-build",
    ]

    import pytest

    with pytest.raises(SystemExit, match="requires --evaluate"):
        batch.main(base)
    with pytest.raises(SystemExit, match="requires --memory-provenance"):
        batch.main([*base, "--evaluate"])


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


def test_instance_image_prebuild_defaults_only_to_experience_mode():
    batch = load_batch_module()

    assert batch.should_prebuild_instance_image(
        SimpleNamespace(repair_mode="experience-build", prebuild_instance_image=None)
    )
    assert not batch.should_prebuild_instance_image(
        SimpleNamespace(repair_mode="benchmark", prebuild_instance_image=None)
    )
    assert batch.should_prebuild_instance_image(
        SimpleNamespace(repair_mode="benchmark", prebuild_instance_image=True)
    )


def test_prebuild_instance_image_uses_harness_python_and_records_ready_image(tmp_path, monkeypatch):
    batch = load_batch_module()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        result_file = Path(command[command.index("--result-file") + 1])
        result_file.write_text(
            json.dumps(
                {
                    "status": "ready",
                    "image": "sweb.eval.x86_64.proj__repo-1:latest",
                    "already_present": False,
                    "seconds": 4.2,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="built")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    args = SimpleNamespace(
        harness_python=tmp_path / "swebench-python",
        dataset=tmp_path / "data.parquet",
        namespace="none",
        swebench_dir=tmp_path,
        docker_network="host",
        proxy="http://127.0.0.1:7897",
        image_build_timeout=60,
    )

    result = batch.prebuild_swebench_instance_image(
        {"instance_id": "proj__repo-1"},
        tmp_path / "out",
        args,
        {},
    )

    assert captured["command"][0] == str(args.harness_python)
    assert captured["env"]["SWEBENCH_DOCKER_BUILD_NETWORK"] == "host"
    assert captured["env"]["SWEBENCH_GIT_PROXY"] == args.proxy
    assert result["status"] == "ready"
    assert result["image"] == "sweb.eval.x86_64.proj__repo-1:latest"


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


def test_harness_uses_explicit_python_and_preserves_startup_error(tmp_path, monkeypatch):
    batch = load_batch_module()
    captured = {}
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 1, stdout="ModuleNotFoundError: missing dependency")

    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    args = SimpleNamespace(
        swebench_dir=tmp_path,
        dataset=tmp_path / "data.parquet",
        harness_python=tmp_path / "swebench-python",
        run_id="python-test",
        model="test-model",
        namespace="none",
        cache_level="env",
        harness_timeout=10,
        docker_network="",
        proxy="",
    )

    result = batch.run_harness_for_instance(
        {"instance_id": "proj__repo-1"},
        "diff --git a/a.py b/a.py\n",
        output_dir,
        args,
        {},
        0,
    )

    assert captured["command"][0] == str(args.harness_python)
    assert result["error"] is True
    assert "ModuleNotFoundError" in result["failure_excerpt"]


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


def test_static_safe_unverified_patch_is_kept_with_precise_warning():
    batch = load_batch_module()
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    attribution = {
        "workflow_terminal_reason": "static_safe_unverified",
        "stages": {
            "deterministic_validation": {"passed": True, "issues": []},
            "focused_verification": {
                "attempts": 0,
                "skipped_reason": "no_trusted_verification_plan",
                "assessment": {
                    "classification": "not_available",
                    "submission_ready": False,
                    "reject_patch": False,
                    "preserve_patch": True,
                },
            },
        },
    }

    result = batch.validate_model_patch(patch, require_patch_review=False, stage_attribution=attribution)

    assert result["invalid"] is False
    assert result["discard_patch"] is False
    assert result["warnings"] == ["verification_not_available"]


def test_build_task_contains_only_public_task_and_location(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "proj__repo-1",
            "problem_statement": "TypeError: Writer.__init__() got an unexpected keyword argument 'header_rows'",
            "hints_text": "secret hint that must not be used",
        },
        tmp_path / "repo",
    )

    assert "hints_text" not in task
    assert "secret hint" not in task
    assert "Hints From Issue Discussion" not in task
    assert task == (
        "## Instance\nproj__repo-1\n\n"
        f"## Repository\n{tmp_path / 'repo'}\n\n"
        "## Problem Statement\nTypeError: Writer.__init__() got an unexpected keyword argument 'header_rows'\n"
    )
    assert "Nullable pandas dtype constraint" not in task
    assert "mandatory patch_review" not in task
    assert "verdict=accept" not in task


def test_build_task_does_not_duplicate_system_mode_policy(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "proj__repo-1",
            "problem_statement": "Public example returns the wrong value.",
        },
        tmp_path / "repo",
        repair_mode="experience-build",
    )

    assert "## Instructions" not in task
    assert "Public example returns the wrong value." in task
    assert "FailureReview" not in task
    assert "Corrective Implement" not in task
    assert "Memory is written only after" not in task
    assert "Benchmark is strict pass@1" not in task


def test_build_task_leaves_matched_alias_details_to_constraint_router(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "sphinx-doc__sphinx-1",
            "problem_statement": "Autodoc duplicates *args and **kwargs parameter fields.",
            "hints_text": "",
        },
        tmp_path / "repo",
    )

    assert "Matched alias propagation constraint" not in task
    assert "matched_display_name" not in task
    assert "canonical lookup identity separate from rendered identity" not in task
    assert "dict.pop(orig, 'backend', None)" not in task
    assert "mplot3d/art3d.py" not in task
    assert "## Instructions" not in task


def test_build_task_leaves_nullable_pandas_details_to_constraint_router(tmp_path):
    batch = load_batch_module()
    task = batch.build_task(
        {
            "instance_id": "proj__repo-2",
            "problem_statement": "Support nullable pandas dtypes in unique_labels",
        },
        tmp_path / "repo",
    )

    assert "Nullable pandas dtype constraint" not in task
    assert "_pandas_dtype_needs_early_conversion" not in task
    assert "check_array(y, dtype=None, ensure_2d=False)" not in task
    assert "Split every representation named or directly implied" not in task


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


def test_semantic_alignment_does_not_discard_nonempty_patch():
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

    assert result["invalid"] is False
    assert result["discard_patch"] is False


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
    assert "Every directly observed relation violation is an acceptance criterion" in task
    assert "Public Task omission alone does not invalidate" in task
    assert "structural-versus-semantic" in task


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
