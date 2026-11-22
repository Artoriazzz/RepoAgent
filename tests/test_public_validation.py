import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo_agent.cli import build_parser
from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.public_validation import (
    PublicValidationAgent, build_public_validation_prompt, evaluate_public_plan, public_plan_error,
)
from repo_agent.core.subagent import SubAgentRunner
from repo_agent.core.subagent import WorkerResult
from repo_agent.tools.agent_tools import build_agent_tools
from repo_agent.core.tool import ToolRegistry, ToolResponse, build_tool
from repo_agent.core.verification import test_overlay_digest as overlay_digest


def case(identifier="target", kind="target", command="python -c 'assert 1 == 2'"):
    return {"id": identifier, "kind": kind, "command": command,
            "expected_behavior": "The public operation returns 2", "basis": "Issue example: expected 2"}


def execution(command, code, output="AssertionError: expected 2"):
    return {"command": command, "exit_code": code, "status": "passed" if code == 0 else "failed", "evidence": output}


def probe_input(plan):
    return {"files": plan.get("files", []), "commands": [item["command"] for item in plan["cases"]]}


def test_test_design_ignores_coordinator_context(monkeypatch):
    monkeypatch.delenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", raising=False)
    plain = build_public_validation_prompt("Public issue", {})
    injected = build_public_validation_prompt("Public issue", {
        "problem_contract": "SECRET_CONTRACT", "source_files": ["SECRET_PATH"],
        "hypotheses": ["SECRET_CHOICE"], "confirmed_facts": ["SECRET_FACT"],
        "candidate_patch": "SECRET_PATCH", "memory": "SECRET_MEMORY",
    })
    assert plain == injected
    assert "SECRET" not in injected
    assert plain.index("## Execution Environment") < plain.index("## Public Issue")


def test_input_contract_design_has_two_extra_steps_but_no_write_permission():
    from repo_agent.core.public_validation import PUBLIC_VALIDATION_PROMPT
    from repo_agent.core.subagent import WORKER_SPECS

    spec = WORKER_SPECS['public_validation']
    assert spec.max_steps == 12
    assert spec.allowed_tools == {'list_files', 'read_file', 'search_repo'}
    assert '原拟目标已通过时，可保留为 regression' in PUBLIC_VALIDATION_PROMPT


def test_baseline_probe_is_bounded_and_does_not_replace_final_plan(tmp_path):
    commands = []
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = ToolRegistry()
    registry.register(build_tool(name="run_command", description="run", parameters=[], handler=lambda _: ""))

    def execute(tool, inp):
        commands.append(inp)
        assert Path(inp["public_test_overlay"], "tests/test_probe.py").exists()
        return ToolResponse.success("Ran 1 test\nOK", {"exit_code": 0, "verification_runtime": "swebench_docker"})

    runner = SimpleNamespace(base_tools=registry, cwd=repo, _run_swebench_verification_command=execute)
    agent = PublicValidationAgent(runner, tmp_path / "out")
    agent.plan = {"status": "unavailable", "cases": [], "unknowns": ["not frozen"]}
    original = agent.plan
    agent.baseline = {"existing": {"exit_code": 0}}
    item = case("optional", "regression", "python -m unittest tests.test_probe")
    plan = {"status": "ready", "execution_mode": "native", "cases": [item], "unknowns": [],
            "files": [{"path": "tests/test_probe.py", "content": "def test_optional():\n    assert True\n"}]}
    tool = agent._baseline_probe_tool()
    for remaining in (1, 0):
        response = tool.run_with_timing(probe_input(plan))
        assert response.status.value == "success"
        assert response.data["remaining_probes"] == remaining
        assert response.data["observations"][0]["classification"] == "behavior_passed"
    assert tool.run_with_timing(probe_input(plan)).error["code"] == "BASELINE_PROBE_BUDGET_EXHAUSTED"
    assert len(commands) == 2
    assert all(c["timeout"] == 120 for c in commands)
    assert agent.plan is original
    assert agent.baseline == {"existing": {"exit_code": 0}}
    assert agent.test_overlay is None
    assert not (repo / "tests/test_probe.py").exists()
    assert len(agent.save()["baseline_probes"]) == 2


def test_probe_of_existing_tests_needs_only_commands(tmp_path, monkeypatch):
    agent = PublicValidationAgent(SimpleNamespace(cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(PublicValidationAgent, "execute", lambda self, cmd: execution(cmd, 0, "Ran 1 test\nOK"))
    response = agent._baseline_probe_tool().run_with_timing({"commands": ["python -m unittest tests.existing"]})
    assert response.status.value == "success"
    assert response.data["observations"][0]["classification"] == "behavior_passed"
    assert response.data["remaining_probes"] == 1


@pytest.mark.parametrize("invalid", ["production_file", "pipeline", "too_many_commands", "inline"])
def test_baseline_probe_rejects_unsafe_or_unbounded_plans(tmp_path, monkeypatch, invalid):
    runner = SimpleNamespace(cwd=tmp_path)
    agent = PublicValidationAgent(runner, tmp_path / "out")
    monkeypatch.setattr(PublicValidationAgent, "execute", lambda *args: pytest.fail("Invalid probe must not execute"))
    plan = {"status": "ready", "execution_mode": "native", "cases": [case(command="python -m unittest tests.test_probe")],
            "files": [{"path": "tests/test_probe.py", "content": "assert True"}], "unknowns": []}
    if invalid == "production_file":
        plan["files"][0]["path"] = "production.py"
    elif invalid == "pipeline":
        plan["cases"][0]["command"] += " | tail"
    elif invalid == "too_many_commands":
        plan["cases"] = [case(str(i), command="python -m unittest tests.test_probe") for i in range(3)]
    else:
        plan["cases"][0]["command"] = "python -c 'assert True'"
    response = agent._baseline_probe_tool().run_with_timing(probe_input(plan))
    assert response.error["code"] == "INVALID_BASELINE_PROBE"
    assert not (tmp_path / "production.py").exists()


def test_probe_budget_is_shared_with_baseline_design_retry(tmp_path, monkeypatch):
    (tmp_path / "messages.json").write_text("[]")
    item = case(command="python -m unittest tests.test_probe")
    plan = {"status": "ready", "execution_mode": "native", "cases": [item], "unknowns": []}
    seen = []

    def run(**kwargs):
        tool = kwargs["test_design_tool"]
        seen.append(tool)
        # 无效方案同样消耗试跑次数，重试不能重置预算。
        response = tool.run_with_timing({"commands": []})
        assert response.error["code"] == "INVALID_BASELINE_PROBE"
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=0, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: execution(cmd, 0, "OK"))
    agent.design("issue", {})
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[-1].run_with_timing(probe_input(plan)).error["code"] == "BASELINE_PROBE_BUDGET_EXHAUSTED"


def test_probe_tool_is_only_exposed_to_the_design_role(tmp_path):
    runner = SubAgentRunner(llm=SimpleNamespace(model="fake"), base_tools=ToolRegistry(),
                            cwd=tmp_path, output_dir=tmp_path / "out")
    tool = PublicValidationAgent(runner, tmp_path / "out")._baseline_probe_tool()
    with pytest.raises(ValueError, match="only available to public_validation"):
        runner.run("implement", "must not get test execution", test_design_tool=tool)
    assert runner.base_tools.get("run_baseline_tests") is None


def test_new_design_resets_probe_budget_but_not_within_a_design(tmp_path):
    def run(**kwargs):
        response = kwargs["test_design_tool"].run_with_timing({"commands": []})
        assert response.error["code"] == "INVALID_BASELINE_PROBE"
        return SimpleNamespace(text=json.dumps({"status": "unavailable", "cases": [], "unknowns": ["No oracle"]}),
                               completed=True, output_dir=tmp_path, total_tokens=0, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    for task in ("first", "second", "third"):
        artifact = agent.design(task, {})
        assert len(artifact["baseline_probes"]) == 1


def test_design_loop_can_probe_and_then_freeze_native_tests(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {"status": "ready", "execution_mode": "native", "unknowns": [],
            "files": [{"path": "tests/test_probe.py", "content":
                       "import unittest\nclass ProbeTest(unittest.TestCase):\n"
                       "    def test_optional(self):\n        self.assertEqual(str(None), 'None')\n"}],
            "cases": [case("optional", "regression", "python -m unittest discover -s tests -p test_probe.py")]}

    class Model:
        model = "fake"
        calls = 0

        def invoke_with_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                assert {t["function"]["name"] for t in tools} == {"run_baseline_tests"}
                call = {"id": "probe1", "type": "function", "function": {
                    "name": "run_baseline_tests", "arguments": json.dumps(probe_input(plan))}}
                return LLMToolResponse(content="", tool_calls=[ToolCall(
                    id="probe1", name="run_baseline_tests", arguments=json.dumps(probe_input(plan)))],
                                       raw_message={"role": "assistant", "content": "", "tool_calls": [call]}, usage={})
            assert any(m.get("role") == "tool" and "behavior_passed" in m["content"] for m in messages)
            text = json.dumps(plan)
            return LLMToolResponse(content=text, tool_calls=[], raw_message={"role": "assistant", "content": text}, usage={})

    executions = []

    def execute(inp):
        executions.append(inp["command"])
        result = subprocess.run(inp["command"], cwd=repo, shell=True, capture_output=True, text=True, timeout=30)
        return ToolResponse.success(result.stdout + result.stderr, {"exit_code": result.returncode})

    tools = ToolRegistry()
    tools.register(build_tool(name="run_command", description="run", parameters=[], handler=execute))
    model = Model()
    runner = SubAgentRunner(llm=model, base_tools=tools, cwd=repo, output_dir=tmp_path / "out")
    artifact = PublicValidationAgent(runner, tmp_path / "out").design("confirm an optional input", {})
    assert model.calls == 3
    assert len(executions) == 3  # 试跑、首版基线、复核后的正式基线各执行一次。
    assert len(artifact["baseline_probes"]) == 1
    assert artifact["baseline"]["optional"]["exit_code"] == 0
    assert not (repo / "tests/test_probe.py").exists()


def test_valid_baseline_still_gets_one_coverage_review_with_original_history(tmp_path, monkeypatch):
    history = [{"role": "user", "content": "PUBLIC ISSUE"},
               {"role": "tool", "content": "actual input type and existing extension example"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    target = case()
    regression = case("extension", "regression", "python -c 'assert True'")
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        plan = {"status": "ready", "cases": [target] + ([regression] if len(calls) == 2 else []), "unknowns": []}
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, prompt_tokens=8, completion_tokens=2, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: execution(cmd, 1 if cmd == target["command"] else 0))
    artifact = agent.design("PUBLIC ISSUE: ideally also support configured input", {})
    assert len(calls) == 2
    assert calls[1]["test_design_history"] == history
    assert "ideally also support configured input" in calls[1]["prompt"]
    feedback = json.loads(calls[1]["prompt"].split("\n", 1)[1])
    observation = feedback["baseline_observations"][target["id"]]
    assert observation["command"] == target["command"]
    assert observation["classification"] == "behavior_failed"
    assert observation["evidence"]
    assert feedback["remaining_probes"] == 2
    assert '"baseline_mismatches": []' in calls[1]["prompt"]
    assert artifact["worker"]["baseline_repair"]["reason"] == "coverage_review"
    assert artifact["worker"]["total_tokens"] == 20
    assert [item["id"] for item in artifact["plan"]["cases"]] == ["target", "extension"]


@pytest.mark.parametrize("retain_as_regression", [True, False])
def test_baseline_mismatch_repairs_once_with_history_and_counts_tokens(tmp_path, monkeypatch, retain_as_regression):
    history = [{"role": "system", "content": "rules"},
               {"role": "user", "content": "issue"},
               {"role": "assistant", "content": "source evidence and original plan"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    calls = []
    plan = {"status": "ready", "cases": [case()], "unknowns": []}
    def run(**kwargs):
        calls.append(kwargs)
        revised = {**plan, "cases": [{**plan["cases"][0], "kind": "regression"}],
                   "unknowns": ["Target was not reproduced."]}
        return SimpleNamespace(text=json.dumps(revised if len(calls) == 2 and retain_as_regression else plan),
                               completed=True, output_dir=tmp_path,
                               total_tokens=10, prompt_tokens=8, completion_tokens=2, model="fake")
    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    # 两次均意外通过，第二次后仍停止，不能无限修正或把无效目标当作复现。
    monkeypatch.setattr(agent, "execute", lambda command: execution(command, 0, "OK"))
    artifact = agent.design("issue", {})
    assert len(calls) == 2
    assert calls[1]["test_design_history"] == history
    assert "OK" in calls[1]["prompt"]
    assert artifact["worker"]["total_tokens"] == 20
    assert (tmp_path / "out/public_validation_initial.json").exists()
    evaluation = agent.evaluate()
    assert evaluation["status"] == "inconclusive"
    assert evaluation["cases"][0]["usable"] is retain_as_regression
    assert not evaluation["target_reproduced"]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_invalid_plan_gets_one_repair_with_original_history(tmp_path, monkeypatch, repair_succeeds):
    history = [{"role": "user", "content": "public issue"},
               {"role": "tool", "content": "source and existing test assertions"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    valid = {"status": "ready", "cases": [case()], "unknowns": []}
    invalid = {**valid, "files": [{"path": ".placeholder.txt", "content": "a note"}]}
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        plan = valid if repair_succeeds and len(calls) == 2 else invalid
        return SimpleNamespace(text=json.dumps(plan), completed=False if plan is invalid else True,
                               output_dir=tmp_path, total_tokens=10, prompt_tokens=8,
                               completion_tokens=2, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: execution(cmd, 1))
    artifact = agent.design("issue", {})
    assert len(calls) == 2
    assert calls[1]["test_design_history"] == history
    assert "Generate new test_*.py modules" in calls[1]["prompt"]
    assert artifact["worker"]["total_tokens"] == 20
    assert artifact["plan"]["status"] == ("ready" if repair_succeeds else "unavailable")
    assert (tmp_path / "out/public_validation_initial.json").exists()


def test_declared_unavailable_plan_does_not_trigger_a_retry(tmp_path):
    calls = []
    (tmp_path / "messages.json").write_text("[]")

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps({"status": "unavailable", "cases": [],
                                               "unknowns": ["No public expected behavior"]}),
                               completed=True, output_dir=tmp_path, total_tokens=0, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    assert agent.design("issue", {})["plan"]["status"] == "unavailable"
    assert len(calls) == 1


def test_missing_docker_image_neither_retries_design_nor_triggers_correction(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "messages.json").write_text("[]")
    item = case()
    plan = {"status": "ready", "cases": [item], "unknowns": []}

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: {
        **execution(cmd, 125, "Unable to find image 'sweb.eval.example:latest' locally\n"
                    "docker: Error response from daemon: pull access denied"),
        "runtime": "swebench_docker",
    })
    agent.design("issue", {})
    result = agent.evaluate()
    assert len(calls) == 1
    assert result["status"] == "inconclusive"
    assert not result["feedback_eligible"]
    assert not result["target_reproduced"]


@pytest.mark.parametrize("skipped,expected", [(2, "environment_blocked"), (1, "behavior_passed")])
def test_unittest_skip_summary_requires_executed_cases(skipped, expected):
    item = case(kind="regression", command="python -m unittest tests.test_cli")
    output = f"Ran 2 tests in 0.001s\n\nOK (skipped={skipped})\n"
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 0, output)},
                                  lambda cmd: execution(cmd, 0, output))
    assert result["cases"][0]["baseline_classification"] == expected
    assert result["cases"][0]["usable"] == (expected == "behavior_passed")


def test_missing_pytest_runner_reuses_baseline_review(tmp_path, monkeypatch):
    (tmp_path / "messages.json").write_text("[]")
    calls = []
    first = case(command="python -m pytest tests/test_cli.py::test_run")
    revised = case(command="python -m unittest tests.test_cli.TestCLI.test_run")

    def run(**kwargs):
        calls.append(kwargs)
        plan = {"status": "ready", "cases": [first if len(calls) == 1 else revised], "unknowns": []}
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=0, model="fake")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: execution(
        cmd, 1, "python: No module named pytest" if cmd == first["command"] else "AssertionError: wrong result"))
    artifact = agent.design("issue", {})
    assert len(calls) == 2
    feedback = json.loads(calls[1]["prompt"].rsplit("\n", 1)[-1])
    assert feedback["baseline_observations"][first["id"]]["classification"] == "environment_blocked"
    assert feedback["baseline_mismatches"][0]["execution"]["evidence"] == "python: No module named pytest"
    assert artifact["plan"]["cases"] == [revised]


@pytest.mark.parametrize("output,expected", [
    ("AssertionError: Database queries to 'default' are not allowed in SimpleTestCase subclasses.", "invalid_test"),
    ("django.db.utils.OperationalError: no such table: app_model", "behavior_failed"),
])
def test_database_fixture_restriction_is_distinct_from_missing_production_table(output, expected):
    item = case(command="python tests/runtests.py app.test_generated")
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == expected
    assert result["feedback_eligible"] == (expected == "behavior_failed")


@pytest.mark.parametrize("terminal_output,expected", [
    ("E       AssertionError: unexpected CLI status\n"
     "E         tool: [Errno 2] No such file or directory: 'package/__init__.py'\n"
     "E       assert 1 == 0\n", "behavior_failed"),
    ("E       FileNotFoundError: [Errno 2] No such file or directory: 'fixture.json'\n",
     "environment_blocked"),
])
def test_pytest_reported_diagnostic_is_distinct_from_missing_test_file(terminal_output, expected):
    item = case(command="python -m pytest tests/test_cli.py::test_run")
    output = terminal_output + "\nFAILED tests/test_cli.py::test_run\n"
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 0))
    assert result["cases"][0]["baseline_classification"] == expected
    assert result["target_reproduced"] == (expected == "behavior_failed")


@pytest.mark.parametrize("output,expected", [
    ("/opt/miniconda3/envs/testbed/bin/python: No module named pytest\n", "environment_blocked"),
    ("E       AssertionError: python: No module named pytest\n"
     "FAILED tests/test_cli.py::test_run\n", "behavior_failed"),
])
def test_missing_pytest_runner_does_not_create_behavior_feedback(output, expected):
    item = case(command="python -m pytest tests/test_cli.py::test_run")
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == expected
    assert result["feedback_eligible"] == (expected == "behavior_failed")


@pytest.mark.parametrize("path,message,expected", [
    ("/testbed/django/forms/forms.py", "The empty_permitted and use_required_attribute arguments may not both be True.", "invalid_test"),
    ("/testbed/django/forms/forms.py", "Unexpected value in submitted form.", "behavior_failed"),
    ("/testbed/app/forms.py", "The empty_permitted and use_required_attribute arguments may not both be True.", "behavior_failed"),
])
def test_mutually_exclusive_form_fixture_arguments(path, message, expected):
    item = case(command="python tests/runtests.py app.test_public")
    output = f'  File "{path}", line 115, in __init__\nValueError: {message}\n'
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                 lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == expected
    assert result["feedback_eligible"] == (expected == "behavior_failed")


@pytest.mark.parametrize("message", [
    "test_stdin_read_with_function() takes 1 positional argument but 2 were given",
    "ShellTests.test_stdin_read_with_function() takes 1 positional argument but 2 were given",
    "test_stdin_read_with_function() missing 1 required positional argument: 'mock_select'",
])
def test_mock_argument_mismatch_is_a_test_design_error(message):
    item = case(command="python tests/runtests.py shell.test_public.TestShell.test_stdin_read_with_function")
    output = ('Traceback (most recent call last):\n'
              '  File "/opt/python3.6/unittest/mock.py", line 1183, in patched\n'
              '    return func(*args, **keywargs)\n'
              f'TypeError: {message}\n')
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == "invalid_test"
    assert not result["feedback_eligible"]


def test_mock_decorated_test_keeps_real_production_type_error():
    item = case(command="python tests/runtests.py app.test_public.TestFeature.test_callback")
    output = ('Traceback (most recent call last):\n'
              '  File "/opt/python3.6/unittest/mock.py", line 1183, in patched\n'
              '    return func(*args, **keywargs)\n'
              '  File "/testbed/tests/app/test_public.py", line 20, in test_callback\n'
              '    run_callback()\n'
              '  File "/testbed/app/callback.py", line 10, in run_callback\n'
              '    test_callback(1)\n'
              'TypeError: test_callback() takes 0 positional arguments but 1 was given\n')
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == "behavior_failed"
    assert result["feedback_eligible"]


@pytest.mark.parametrize("initial_error", [
    '  File "/opt/python3.6/unittest/mock.py", line 1183, in patched\n'
    'TypeError: test_stdin() takes 1 positional argument but 2 were given\n',
    '  File "/testbed/django/forms/forms.py", line 115, in __init__\n'
    'ValueError: The empty_permitted and use_required_attribute arguments may not both be True.\n',
])
def test_initialization_error_is_sent_to_designer_before_patch(tmp_path, monkeypatch, initial_error):
    history = [{"role": "tool", "content": "@mock.patch injects a select argument"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    calls = []
    item = case(command="python tests/runtests.py shell.test_public.TestShell.test_stdin")
    plan = {"status": "ready", "cases": [item], "unknowns": []}

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, model="fake")

    def execute(command):
        return execution(command, 1, initial_error if len(calls) == 1 else "NameError: value is not defined")

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", execute)
    agent.design("issue", {})
    assert len(calls) == 2
    assert calls[1]["test_design_history"] == history
    assert initial_error.splitlines()[-1] in calls[1]["prompt"]
    assert agent.evaluate()["feedback_eligible"]


@pytest.mark.parametrize("missing,expected", [
    ("app.WrongTests", "invalid_test"),
    ("optional_dependency", "environment_blocked"),
])
def test_wrong_unittest_node_is_distinct_from_missing_dependency(missing, expected):
    item = case(command="python tests/runtests.py app.WrongTests.test_existing")
    output = ("ERROR: WrongTests (unittest.loader._FailedTest)\n"
              f"ModuleNotFoundError: No module named '{missing}'\n")
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["cases"][0]["baseline_classification"] == expected
    assert not result["feedback_eligible"]


def test_wrong_regression_node_is_sent_to_test_designer_once(tmp_path, monkeypatch):
    history = [{"role": "tool", "content": "tests/app/tests.py defines ExistingTests"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        command = "python tests/runtests.py app.WrongTests" if len(calls) == 1 else "python tests/runtests.py app.tests.ExistingTests"
        plan = {"status": "ready", "cases": [case("regression", "regression", command)], "unknowns": []}
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, model="fake")
    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=tmp_path), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", lambda cmd: execution(cmd, 1,
        "ERROR: WrongTests (unittest.loader._FailedTest)\nModuleNotFoundError: No module named 'app.WrongTests'")
        if "WrongTests" in cmd else execution(cmd, 0, "OK"))
    artifact = agent.design("issue", {})
    assert len(calls) == 2
    assert calls[1]["test_design_history"] == history
    assert "app.WrongTests" in calls[1]["prompt"]
    assert artifact["baseline"]["regression"]["exit_code"] == 0


def test_same_input_control_exposes_wrong_oracle_before_fix(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "example.py"
    implementation = "    return [x for x in range(1, 4) if bounds[0] <= x <= bounds[1]]\n"
    source.write_text("def select(bounds):\n    if hasattr(bounds, '_fields'):\n        raise TypeError('unsupported tuple type')\n" + implementation)
    history = [{"role": "tool", "content": "read range input handling and existing list examples"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    body = (
        "import unittest\nfrom collections import namedtuple\nfrom example import select\n"
        "Bounds = namedtuple('Bounds', 'lower upper')\n"
        "class GeneratedTests(unittest.TestCase):\n"
        "    def test_target(self):\n        self.assertEqual(select(Bounds(3, 1)), EXPECTED)\n"
        "    def test_control(self):\n        self.assertEqual(select([3, 1]), EXPECTED)\n"
    )
    items = [case("target", "target", "python -m unittest test_generated.GeneratedTests.test_target"),
             case("control", "regression", "python -m unittest test_generated.GeneratedTests.test_control")]
    plan = {"status": "ready", "cases": items, "unknowns": []}
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        (repo / "test_generated.py").write_text(body.replace("EXPECTED", "[3]" if len(calls) == 1 else "[]"))
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, model="fake")
    def execute(cmd):
        result = subprocess.run([sys.executable, *shlex.split(cmd)[1:]], cwd=repo,
                                capture_output=True, text=True, timeout=10)
        return execution(cmd, result.returncode, result.stdout + result.stderr)
    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=repo), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", execute)
    artifact = agent.design("Support named tuples with existing list semantics", {})
    assert len(calls) == 2
    assert "AssertionError" in calls[1]["prompt"]
    assert calls[1]["test_design_history"] == history
    assert artifact["baseline"]["control"]["exit_code"] == 0
    assert "TypeError: unsupported tuple type" in artifact["baseline"]["target"]["evidence"]
    # 修复生产路径后，已固定的正确预期能够通过，测试文件不再变化。
    source.write_text("def select(bounds):\n" + implementation)
    assert agent.evaluate()["resolved"]


@pytest.mark.parametrize("scope", ["<module>", "ShellExecGlobalsTestCase", "setUp", "setUpClass"])
def test_test_initialization_error_cannot_reproduce_issue(scope):
    item = case(command="python -m unittest tests.test_generated")
    output = (
        'Traceback (most recent call last):\n'
        '  File "/usr/lib/python3.11/unittest/loader.py", line 162, in loadTestsFromName\n'
        '    module = __import__(module_name)\n'
        f'  File "/testbed/tests/test_generated.py", line 49, in {scope}\n'
        "    @unittest.skipIf(sys.platform == 'win32', 'Windows')\n"
        "NameError: name 'sys' is not defined\n"
    )
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 0, "OK"))
    assert result["cases"][0]["baseline_classification"] == "invalid_test"
    assert not result["target_reproduced"]
    assert not result["resolved"]
    assert not result["feedback_eligible"]


@pytest.mark.parametrize("path,scope", [
    ("/testbed/app/runtime.py", "resolve"),
    ("/testbed/tests/test_generated.py", "test_function_scope"),
])
def test_behavior_name_error_remains_valid_failure(path, scope):
    item = case(command="python -m unittest tests.test_generated")
    output = (f'Traceback (most recent call last):\n  File "{path}", line 10, in {scope}\n'
              "    execute_user_script()\nNameError: name 'user_value' is not defined\n")
    result = evaluate_public_plan({"cases": [item]}, {item["id"]: execution(item["command"], 1, output)},
                                  lambda cmd: execution(cmd, 1, output))
    assert result["target_reproduced"]
    assert result["feedback_eligible"]


def test_real_import_failure_is_repaired_before_freezing_suite(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    module = repo / "test_generated.py"
    source = repo / "example.py"
    source.write_text("def value():\n    return 1\n")
    command = "python -m unittest test_generated"
    body = (
        "import unittest\n"
        "from example import value\n"
        "class GeneratedTests(unittest.TestCase):\n"
        "    @unittest.skipIf(sys.platform == 'win32', 'Windows')\n"
        "    def test_target(self):\n"
        "        self.assertEqual(value(), 2)\n"
    )
    plan = {"status": "ready", "cases": [case(command=command)], "unknowns": []}
    history = [{"role": "tool", "content": "read source and neighboring tests"}]
    (tmp_path / "messages.json").write_text(json.dumps(history))
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        module.write_text(("import sys\n" if len(calls) == 2 else "") + body)
        return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                               total_tokens=10, model="fake")

    def execute(cmd):
        result = subprocess.run([sys.executable, "-m", "unittest", "test_generated"],
                                cwd=repo, capture_output=True, text=True, timeout=10)
        return execution(cmd, result.returncode, result.stdout + result.stderr)

    agent = PublicValidationAgent(SimpleNamespace(run=run, cwd=repo), tmp_path / "out")
    monkeypatch.setattr(agent, "execute", execute)
    artifact = agent.design("A public behavior should return 2", {})
    assert len(calls) == 2
    assert "NameError: name 'sys' is not defined" in calls[1]["prompt"]
    assert calls[1]["test_design_history"] == history
    assert "AssertionError: 1 != 2" in artifact["baseline"]["target"]["evidence"]
    source.write_text("def value():\n    return 2  # fixed\n")
    assert agent.evaluate()["resolved"]


def test_independent_tests_run_before_coordinator_and_receive_only_issue(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from repo_agent.core.coordinator import CoordinatorEngine

    engine = CoordinatorEngine(llm=SimpleNamespace(model="fake"), worker_tools=ToolRegistry(),
                               cwd=tmp_path, output_dir=tmp_path / "out", public_validation=True)
    events = []
    def design(task, context):
        events.append("design")
        assert task == "PUBLIC_ISSUE"
        assert context == {}
        engine.public_validator.worker = {"total_tokens": 7}
    def chat(task):
        events.append("coordinator")
        assert events == ["design", "coordinator"]
        assert task == "PUBLIC_ISSUE"
        return "done"
    engine.public_validator.design = design
    monkeypatch.setattr(engine.engine, "chat", chat)
    monkeypatch.setattr(engine, "_write_stage_attribution", lambda answer: None)
    engine.runtime_pool_registry = SimpleNamespace(
        pool_for=lambda **kwargs: SimpleNamespace(acquire=lambda: nullcontext(SimpleNamespace(llm=engine.llm))))
    assert engine.run("PUBLIC_ISSUE") == "done"
    assert "test_context" not in engine.engine.system_prompt
    assert engine._agent_state["worker_events"][0]["total_tokens"] == 7


@pytest.mark.parametrize("command", ["python -c 'print(2)'", "pytest tests/test_api.py | tail -20", "pytest || true"])
def test_plan_rejects_non_behavior_or_masked_exit(command):
    assert public_plan_error(json.dumps({"status": "ready", "cases": [case(command=command)], "unknowns": []}))


def test_plan_accepts_unknown_oracle_without_forcing_a_guess():
    assert public_plan_error(json.dumps({"status": "unavailable", "cases": [], "unknowns": ["Unknown formula"]})) is None


def test_regression_failure_can_trigger_feedback_without_a_target_reproducer():
    item = case("regression", "regression", "python -m pytest tests/test_api.py::test_empty")
    result = evaluate_public_plan({"cases": [item]}, {"regression": execution(item["command"], 0)},
                                  lambda command: execution(command, 1))
    assert result["feedback_eligible"]
    assert not result["target_reproduced"]
    assert "Baseline exit: 0" in result["failure_evidence"]


@pytest.mark.parametrize("baseline_output", ["ModuleNotFoundError: no module named x", "SyntaxError: invalid syntax", "ERROR collecting test_a.py"])
def test_invalid_baseline_never_authorizes_correction(baseline_output):
    item = case()
    result = evaluate_public_plan({"cases": [item]}, {"target": execution(item["command"], 1, baseline_output)},
                                  lambda command: execution(command, 1))
    assert result["status"] == "inconclusive"
    assert not result["feedback_eligible"]


def test_pass_requires_reproduced_target_and_all_regressions():
    target, regression = case(), case("regression", "regression", "python -c 'assert True'")
    baseline = {"target": execution(target["command"], 1), "regression": execution(regression["command"], 0)}
    result = evaluate_public_plan({"cases": [target, regression]}, baseline, lambda cmd: execution(cmd, 0))
    assert result["status"] == "passed" and result["resolved"]
    assert not result["feedback_eligible"]
    baseline["target"]["command"] = "different command"
    assert evaluate_public_plan({"cases": [target, regression]}, baseline, lambda cmd: execution(cmd, 0))["status"] == "inconclusive"


def test_unreproduced_target_or_empty_plan_does_not_claim_success():
    item = case()
    assert not evaluate_public_plan({"cases": [item]}, {"target": execution(item["command"], 0)}, lambda cmd: execution(cmd, 0))["resolved"]
    assert not evaluate_public_plan({"cases": []}, {}, lambda cmd: execution(cmd, 0))["resolved"]


def test_pytest_collection_exit_and_runtime_change_are_inconclusive():
    item = case(command="python -m pytest tests/test_api.py")
    baseline = {"target": execution(item["command"], 2, "Interrupted during collection")}
    assert not evaluate_public_plan({"cases": [item]}, baseline, lambda cmd: execution(cmd, 1))["feedback_eligible"]
    baseline["target"] = {**execution(item["command"], 1), "runtime": "local_interpreter"}
    assert not evaluate_public_plan({"cases": [item]}, baseline, lambda cmd: {
        **execution(cmd, 0), "runtime": "swebench_docker",
    })["resolved"]


def test_real_baseline_and_patch_execute_the_same_public_test(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", raising=False)
    source = tmp_path / "example.py"
    source.write_text("def value():\n    return 1\n")
    item = case(command="python -c 'from example import value; assert value() == 2'")
    plan = {"status": "ready", "cases": [item], "unknowns": []}
    registry = ToolRegistry()

    def execute(inp):
        result = subprocess.run(inp["command"], shell=True, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        return ToolResponse.success(result.stdout + result.stderr, {"exit_code": result.returncode})

    registry.register(build_tool(name="run_command", description="execute", parameters=[], handler=execute))
    runner = SubAgentRunner(llm=SimpleNamespace(model="fake"), base_tools=registry,
                            cwd=tmp_path, output_dir=tmp_path / "out")
    monkeypatch.setattr(runner, "run", lambda **kw: SimpleNamespace(
        text=json.dumps(plan), completed=True, output_dir=tmp_path, total_tokens=0, model="fake"))
    agent = PublicValidationAgent(runner, tmp_path / "out")
    agent.design("value should return 2", {})
    assert agent.baseline["target"]["exit_code"] == 1
    # 改变文件长度以避免同秒更新时命中旧的 Python 字节码缓存。
    source.write_text("def value():\n    return 2  # fixed\n")
    assert agent.evaluate()["resolved"]
    assert agent.plan == plan


def test_native_tests_reuse_repository_fixture_and_stay_outside_source(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", raising=False)
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    source = repo / "example.py"
    source.write_text("def value():\n    return 1\n")
    (tests / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef expected():\n    return 2\n")
    command = "python -m pytest tests/test_public_generated.py::test_target -q"
    plan = {"status": "ready", "execution_mode": "native", "unknowns": [],
            "files": [{"path": "tests/test_public_generated.py", "content":
                       "from example import value\ndef test_target(expected):\n    assert value() == expected\n"}],
            "cases": [case(command=command)]}
    registry = ToolRegistry()

    def execute(inp):
        result = subprocess.run(inp["command"], shell=True, cwd=repo,
                                capture_output=True, text=True, timeout=30)
        return ToolResponse.success(result.stdout + result.stderr, {"exit_code": result.returncode})

    registry.register(build_tool(name="run_command", description="execute", parameters=[], handler=execute))
    runner = SubAgentRunner(llm=SimpleNamespace(model="fake"), base_tools=registry,
                            cwd=repo, output_dir=tmp_path / "out")
    monkeypatch.setattr(runner, "run", lambda **kw: SimpleNamespace(
        text=json.dumps(plan), completed=True, output_dir=tmp_path, total_tokens=0, model="fake"))
    agent = PublicValidationAgent(runner, tmp_path / "out")
    artifact = agent.design("value must return 2", {})
    assert agent.baseline["target"]["exit_code"] == 1
    assert "assert 1 == 2" in agent.baseline["target"]["evidence"]
    assert not (tests / "test_public_generated.py").exists()
    assert agent.test_overlay.is_relative_to(tmp_path / "out")
    source.write_text("def value():\n    return 2  # fixed\n")
    assert agent.evaluate()["resolved"]
    assert not (tests / "test_public_generated.py").exists()
    generated = agent.test_overlay / "tests/test_public_generated.py"
    assert generated.read_text() == plan["files"][0]["content"]
    assert artifact["baseline"]["target"]["test_files_sha256"] == overlay_digest(agent.test_overlay)
    generated.write_text("def test_target():\n    assert True\n")
    assert not agent.evaluate()["resolved"]


def test_fixing_only_one_public_branch_keeps_feedback_eligible(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "example.py"
    source.write_text("def transform(x):\n    return 0\n")
    context = {"behavior_cases": [{"id": "positive", "expected_behavior": "1 becomes 2"},
                                  {"id": "negative", "expected_behavior": "-1 becomes -2"}]}
    plan = {"status": "ready", "execution_mode": "native", "unknowns": [],
            "files": [{"path": "tests/test_public_branches.py", "content":
                       "from example import transform\n"
                       "def test_positive():\n    assert transform(1) == 2\n"
                       "def test_negative():\n    assert transform(-1) == -2\n"}],
            "cases": [{**case(name, command=f"python -m pytest tests/test_public_branches.py::test_{name} -q"),
                       "expected_behavior": f"{value} becomes {2 * value}", "basis": "public task"}
                      for name, value in (("positive", 1), ("negative", -1))]}
    registry = ToolRegistry()

    def execute(inp):
        result = subprocess.run(inp["command"], shell=True, cwd=repo,
                                capture_output=True, text=True, timeout=30)
        return ToolResponse.success(result.stdout + result.stderr, {"exit_code": result.returncode})

    registry.register(build_tool(name="run_command", description="execute", parameters=[], handler=execute))
    runner = SubAgentRunner(llm=SimpleNamespace(model="fake"), base_tools=registry,
                            cwd=repo, output_dir=tmp_path / "out")
    monkeypatch.setattr(runner, "run", lambda **kwargs: SimpleNamespace(
        text=json.dumps(plan), completed=True, output_dir=tmp_path, total_tokens=0, model="fake"))
    agent = PublicValidationAgent(runner, tmp_path / "out")
    artifact = agent.design("Double positive and negative values", context)
    assert artifact["test_context"] == {}
    source.write_text("def transform(x):\n    return 2 if x > 0 else 0\n")
    partial = agent.evaluate()
    assert partial["feedback_eligible"]
    assert not partial["resolved"]
    assert "## negative" in partial["failure_evidence"]
    assert "## positive" not in partial["failure_evidence"]
    source.write_text("def transform(x):\n    return 2 * x  # fix both branches\n")
    assert agent.evaluate()["resolved"]


@pytest.mark.parametrize("path", ["../test_escape.py", "/tmp/test_escape.py", "tests/conftest.py"])
def test_generated_test_files_cannot_replace_fixture_configuration_or_escape(path):
    plan = {"status": "ready", "cases": [case()], "unknowns": [],
            "files": [{"path": path, "content": "assert True"}]}
    assert public_plan_error(json.dumps(plan))


def test_validation_role_has_only_read_tools_and_preserves_history_on_json_retry(tmp_path):
    class Model:
        model = "fake"

        def __init__(self):
            self.messages = []
            self.tools = []

        def invoke_with_tools(self, messages, tools):
            self.messages.append(list(messages))
            self.tools.append([t["function"]["name"] for t in tools])
            text = "invalid json" if len(self.messages) == 1 else json.dumps({"status": "unavailable", "cases": [], "unknowns": ["Unknown expected result"]})
            return LLMToolResponse(content=text, tool_calls=[], raw_message={"role": "assistant", "content": text}, usage={})

    model = Model()
    tools = ToolRegistry()
    for name in ("read_file", "search_repo", "write_file", "run_command", "search_memories", "get_issue"):
        tools.register(build_tool(name=name, description=name, parameters=[], handler=lambda _: ToolResponse.success("ok")))
    runner = SubAgentRunner(llm=model, base_tools=tools, cwd=tmp_path, output_dir=tmp_path / "out")
    result = runner.run("public_validation", "PUBLIC_ISSUE_SENTINEL")
    assert result.completed
    assert set(model.tools[0]) == {"read_file", "search_repo"}
    assert model.tools[-1] == []
    assert "PUBLIC_ISSUE_SENTINEL" in str(model.messages[-1])
    history = json.loads((result.output_dir / "messages.json").read_text())
    resumed = runner.run("public_validation", "BASELINE_FAILURE_SENTINEL", test_design_history=history)
    assert resumed.completed
    assert model.messages[-1][:len(history)] == history
    assert "BASELINE_FAILURE_SENTINEL" in str(model.messages[-1][-1])


def test_agent_freezes_commands_and_saves_baseline(tmp_path):
    commands = []
    item = case()
    plan = {"status": "ready", "cases": [item], "unknowns": []}

    class Runner:
        base_tools = SimpleNamespace(get=lambda _: object())

        def run(self, **kwargs):
            return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                                   total_tokens=12, model="fake")

        def _run_swebench_verification_command(self, tool, inp):
            commands.append(inp["command"])
            return ToolResponse.partial("AssertionError: expected 2", {"exit_code": 1})

    agent = PublicValidationAgent(Runner(), tmp_path)
    agent.design("issue", {})
    result = agent.evaluate()
    artifact = json.loads((tmp_path / "public_validation.json").read_text())
    assert commands == [item["command"], item["command"]]
    assert artifact["baseline"]["target"]["exit_code"] == 1
    assert result["feedback_eligible"]


def test_public_suite_revision_starts_fresh_and_keeps_issue_trigger(tmp_path):
    item = case()
    plan = {"status": "ready", "cases": [item], "unknowns": []}

    class Runner:
        base_tools = SimpleNamespace(get=lambda _: object())

        def run(self, **kwargs):
            assert "test_design_history" not in kwargs
            assert "keep the filtered query" in kwargs["prompt"]
            assert "original test source" in kwargs["prompt"]
            assert "unique constraint failed" in kwargs["prompt"]
            assert "use distinct fixture values" in kwargs["prompt"]
            assert "保留 Issue 的触发路径" in kwargs["prompt"]
            return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                                   total_tokens=12, model="fake")

        def _run_swebench_verification_command(self, tool, inp):
            return ToolResponse.partial("AssertionError: expected 2", {"exit_code": 1})

    agent = PublicValidationAgent(Runner(), tmp_path)
    result = agent.revise("keep the filtered query", {"plan": {"files": [{"content": "original test source"}]}},
                          {"failure_log_excerpts": ["unique constraint failed"],
                           "suggestion": "use distinct fixture values"})
    assert result["baseline"][item["id"]]["exit_code"] == 1


def test_invalid_case_is_preserved_as_rejected_without_discarding_valid_test(tmp_path):
    commands = []
    valid = case()
    invalid = case("invalid", command="python -c 'print(2)'")
    plan = {"status": "ready", "cases": [invalid, valid, valid], "unknowns": []}
    assert public_plan_error(json.dumps(plan)) is None

    class Runner:
        base_tools = SimpleNamespace(get=lambda _: object())

        def run(self, **kwargs):
            return SimpleNamespace(text=json.dumps(plan), completed=True, output_dir=tmp_path,
                                   total_tokens=12, model="fake")

        def _run_swebench_verification_command(self, tool, inp):
            commands.append(inp["command"])
            return ToolResponse.partial("AssertionError", {"exit_code": 1})

    agent = PublicValidationAgent(Runner(), tmp_path)
    agent.design("issue", {})
    result = agent.evaluate()
    assert commands == [valid["command"], valid["command"]]
    assert len(result["cases"]) == 1
    assert len(result["rejected_cases"]) == 2
    assert result["feedback_eligible"]
    assert not result["resolved"]


def test_public_validation_tokens_are_counted_in_stage_attribution(tmp_path):
    from repo_agent.core.coordinator import CoordinatorEngine
    from repo_agent.core.subagent import WorkerConstraints

    engine = CoordinatorEngine(llm=SimpleNamespace(model="fake"), worker_tools=ToolRegistry(),
                               cwd=tmp_path, output_dir=tmp_path / "out")
    engine.public_validator = SimpleNamespace(
        design=lambda *args: None,
        worker={"total_tokens": 123, "completed": True, "model": "fake"},
    )
    engine._design_public_tests(WorkerConstraints())
    engine._write_stage_attribution("done")
    result = json.loads((tmp_path / "out/stage_attribution.json").read_text())
    assert result["token_usage"]["workers"] == 123
    assert result["token_usage"]["total"] == engine.engine.total_tokens + 123
    assert result["worker_events"][0]["type"] == "public_validation"


def test_cli_exposes_standalone_and_repair_entry_points():
    args = build_parser().parse_args(["design-public-tests", "--repo", ".", "--task-file", "issue.md"])
    assert args.command == "design-public-tests"
    args = build_parser().parse_args(["run", "--repo", ".", "--task", "fix", "--coordinator", "--public-validation"])
    assert args.public_validation


def test_batch_public_correction_reuses_plan_and_curates_before_official_evaluation(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("public_batch", Path(__file__).parents[1] / "scripts/swebench_batch.py")
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    item = case()
    overlay = tmp_path / "public-test-files"
    overlay.mkdir()
    (overlay / "test_generated.py").write_text("assert True")
    (tmp_path / "public_validation.json").write_text(json.dumps({
        "plan": {"status": "ready", "cases": [item], "unknowns": []},
        "baseline": {"target": {**execution(item["command"], 1), "test_files_sha256": overlay_digest(overlay)}},
        "test_files_dir": str(overlay),
    }))
    events = []
    def execute(row, repo, command, *args, **kwargs):
        events.append("test")
        assert kwargs["public_test_overlay"] == overlay
        return {**execution(command, 1 if events.count("test") == 1 else 0),
                "test_files_sha256": overlay_digest(overlay)}
    monkeypatch.setattr(batch, "run_lightweight_feedback_command", execute)
    def correct(*args, **kwargs):
        events.append("correct")
        assert args[5]["validation_kind"] == "public_test_suite"
        return {"attempted": True, "model_patch": "fixed", "output_dir": str(tmp_path), "feedback_file": str(tmp_path / "feedback.json")}
    monkeypatch.setattr(batch, "run_experience_correction", correct)
    monkeypatch.setattr(batch, "validate_model_patch", lambda *a, **k: {})
    def curate(*args, **kwargs):
        events.append("curate")
        assert kwargs["validation"]["validation_kind"] == "public_test_suite"
        return {}
    monkeypatch.setattr(batch, "run_experience_curator_for_success", curate)
    monkeypatch.setattr(batch, "run_harness_for_instance", lambda *a, **k: pytest.fail("Harness must not drive public corrections"))
    summary = {}
    result = batch.run_public_test_corrections({}, tmp_path, tmp_path, "issue", "initial", summary, SimpleNamespace(), {})
    assert result == "fixed"
    assert events == ["test", "correct", "test", "curate"]
    assert summary["public_validation"]["resolved"]


@pytest.mark.parametrize("second_fixes_regression", [False, True])
def test_public_correction_preserves_passed_cases_after_budget(tmp_path, monkeypatch, second_fixes_regression):
    spec = importlib.util.spec_from_file_location("public_batch", Path(__file__).parents[1] / "scripts/swebench_batch.py")
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    target = case(command="python -m unittest tests.test_target")
    regression = case("regression", "regression", "python -m unittest tests.test_existing")
    (tmp_path / "public_validation.json").write_text(json.dumps({
        "plan": {"status": "ready", "cases": [target, regression], "unknowns": []},
        "baseline": {"target": execution(target["command"], 1),
                     "regression": execution(regression["command"], 0)},
    }))
    state = {"patch": "initial", "round": 0}
    def execute(row, repo, command, *args, **kwargs):
        failed = (command == target["command"] if state["patch"] == "initial"
                  else command == regression["command"] and not (
                      second_fixes_regression and state["round"] == 2))
        return execution(command, int(failed))
    def correct(*args, **kwargs):
        state["round"] += 1
        # 第一轮引入的回归仍有第二轮修正机会，第二次归因必须收到这个失败。
        if state["round"] == 2:
            assert "regression" in args[5]["failure_evidence"]
        state["patch"] = "correction-" + str(state["round"])
        return {"attempted": True, "model_patch": state["patch"], "output_dir": str(tmp_path),
                "feedback_file": str(tmp_path / "feedback.json")}
    monkeypatch.setattr(batch, "run_lightweight_feedback_command", execute)
    monkeypatch.setattr(batch, "run_experience_correction", correct)
    monkeypatch.setattr(batch, "validate_model_patch", lambda *a, **k: {})
    monkeypatch.setattr(batch, "restore_worktree_patch", lambda repo, patch: state.update(patch=patch))
    monkeypatch.setattr(batch, "run_experience_curator_for_success", lambda *a, **k: {})
    summary = {}
    patch = batch.run_public_test_corrections({}, tmp_path, tmp_path, "issue", "initial", summary, SimpleNamespace(), {})
    assert state["round"] == 2
    assert patch == state["patch"] == ("correction-2" if second_fixes_regression else "initial")
    if second_fixes_regression:
        assert summary["public_validation"]["resolved"]
        assert "public_regression_rollback" not in summary
    else:
        assert summary["public_regression_rollback"]["lost_passing_cases"] == ["regression"]
        assert not summary["public_validation"]["resolved"]
        assert "experience_curator" not in summary


@pytest.mark.parametrize("candidate_blocked", [False, True])
def test_public_correction_keeps_improvement_despite_existing_baseline_gap(tmp_path, monkeypatch, candidate_blocked):
    spec = importlib.util.spec_from_file_location("public_batch", Path(__file__).parents[1] / "scripts/swebench_batch.py")
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    target = case(command="python -m unittest tests.test_target")
    regression = case("regression", "regression", "python -m unittest tests.test_existing")
    baseline_gap = case("baseline_gap", "regression", "python -m unittest tests.test_baseline_gap")
    (tmp_path / "public_validation.json").write_text(json.dumps({
        "plan": {"cases": [target, regression, baseline_gap]},
        "baseline": {item["id"]: execution(item["command"], int(item != regression))
                     for item in (target, regression, baseline_gap)},
    }))
    state = {"patch": "initial"}

    def execute(row, repo, command, *args, **kwargs):
        if state["patch"] == "initial":
            return execution(command, int(command == target["command"]))
        if candidate_blocked and command == regression["command"]:
            return execution(command, 1, "ImportError: missing dependency")
        return execution(command, 0)

    def correct(*args, **kwargs):
        state["patch"] = "corrected"
        return {"attempted": True, "model_patch": "corrected", "output_dir": str(tmp_path),
                "feedback_file": str(tmp_path / "feedback.json")}

    monkeypatch.setattr(batch, "run_lightweight_feedback_command", execute)
    monkeypatch.setattr(batch, "run_experience_correction", correct)
    monkeypatch.setattr(batch, "validate_model_patch", lambda *a, **k: {})
    monkeypatch.setattr(batch, "restore_worktree_patch", lambda repo, patch: state.update(patch=patch))
    monkeypatch.setattr(batch, "run_experience_curator_for_success", lambda *a, **k: pytest.fail("not fully verified"))
    summary = {}
    patch = batch.run_public_test_corrections({}, tmp_path, tmp_path, "issue", "initial", summary, SimpleNamespace(), {})

    assert patch == state["patch"] == ("initial" if candidate_blocked else "corrected")
    assert summary["public_validation"]["status"] == ("failed" if candidate_blocked else "inconclusive")
    assert not summary["public_validation"]["resolved"]
