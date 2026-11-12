from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable

from repo_agent.core.semantic_evidence import extract_json_object, extract_public_problem_statement
from repo_agent.core.tool import ToolParameter, ToolResponse, build_tool
from repo_agent.core.verification import (
    assess_verification_result,
    classify_verification_command,
    verification_command_safety_reason,
    test_overlay_digest,
)


PUBLIC_VALIDATION_PROMPT = """你负责为软件修复设计独立的公开测试，检查目标问题及直接相关的既有行为。依据仅限本次公开 Issue、修改前源码、文档和已有测试。

1. 明确预期：区分外部行为要求与报告者建议的修法。读取实际入口和相邻测试，必要时查阅定义或调用者。断言须有公开依据，不把当前缺陷或内部修法当成正确契约；无法确定的细节写入 unknowns。
2. 编写测试：沿用仓库的合法输入、测试基类、fixture、初始化和原生入口。优先复用已有测试，新增用例检查完整的可观察结果及相关回归，不扩大到无关需求。
3. 检查运行反馈：可用 run_baseline_tests 在未应用补丁的原始代码上试跑 files 和 commands。区分目标缺陷、测试编写错误和环境阻塞；测试写错就修正，不要求生产代码迁就测试。跳过或未执行不算通过；异常不能证明其后尚未执行的断言正确。试跑不会自动进入最终方案，需在最终 JSON 保留所选用例。
4. 分类并保留：修复前因本题缺陷违反公开预期的标为 target；修复前已通过且仍应成立的标为 regression。原拟目标已通过时，可保留为 regression，但不能声称复现问题，也不能保留 Issue 要求改变的旧行为。两类分开执行，混合命令应拆分。
5. 输出方案：执行器提供一次修复前反馈复核，沿用当前会话修正测试或分类，再在原始代码上复跑。随后冻结测试、命令和预期，在补丁上重跑，不迎合补丁改变断言。只有回归用例也可返回 ready，但说明目标未复现；没有有效用例才返回 unavailable。测试通过仅说明覆盖的行为通过。

不修改生产代码、不安装依赖、不联网，不读取历史经验、官方评测报告或隐藏测试。不掩盖退出码或吞掉异常。这些是自行设计的公开检查，不是官方 F2P/P2P 清单。
默认 execution_mode=native。files 最多六个新增 test_*.py 的相对路径和完整内容，不覆盖旧文件或测试配置。命令从仓库根目录经原生入口运行；仅确认无需框架初始化时用 inline 并提供 inline_reason。cases 最多六项，id 唯一，basis 写明公开依据、已读文件位置及已有运行观察。
最终只输出完整 JSON：
{"status":"ready|unavailable","execution_mode":"native","files":[{"path":"tests/test_public_repair.py","content":"完整测试模块内容"}],"cases":[{"id":"case_1","kind":"target|regression","command":"python -m pytest tests/test_public_repair.py::test_behavior","expected_behavior":"公开预期","basis":"依据、文件位置及已有基线观察"}],"unknowns":["未确定的预期、未复现的目标或环境阻塞"]}
"""


def public_case_error(case: Any) -> str | None:
    if not isinstance(case, dict) or case.get("kind") not in {"target", "regression"}:
        return "Each case needs kind=target or regression."
    for key in ("id", "command", "expected_behavior", "basis"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            return f"Every case needs a nonempty {key}."
    unsafe = verification_command_safety_reason(case["command"])
    if unsafe:
        return unsafe
    if classify_verification_command(case["command"]) != "behavior":
        return "Each command must execute a behavioral test, not a static check or print-only probe."
    return None


def public_plan_error(text: str) -> str | None:
    plan = extract_json_object(text)
    if not isinstance(plan, dict) or plan.get("status") not in {"ready", "unavailable"}:
        return "Return the complete JSON object with status=ready or unavailable."
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) > 6:
        return "cases must be an array containing at most six cases."
    if not isinstance(plan.get("unknowns"), list):
        return "unknowns must be an array."
    if (plan["status"] == "ready") != bool(cases):
        return "ready requires cases; unavailable requires cases=[] and an explanation in unknowns."
    files = plan.get("files", [])
    if not isinstance(files, list) or len(files) > 6:
        return "files must contain at most six new native test files."
    paths = set()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("path"), str):
            return "Each file needs a repository-relative path and complete content."
        path = PurePosixPath(file["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in file["path"] or not path.parts:
            return "Test file paths must stay inside the repository."
        if path.name in {"__init__.py", "conftest.py"} or not path.name.startswith("test_") or path.suffix != ".py":
            return "Generate new test_*.py modules; reuse existing fixtures and configuration."
        if str(path) in paths or not isinstance(file.get("content"), str) or not file["content"].strip():
            return "Test files need unique paths and nonempty content."
        paths.add(str(path))
    if plan.get("execution_mode") == "inline" and not plan.get("inline_reason"):
        return "Inline mode requires a public basis explaining why no test-framework initialization is needed."
    if plan.get("execution_mode") == "native" and any(
        isinstance(case, dict) and re.search(r"\bpython[\d.]*\s+-c\b", str(case.get("command", ""))) for case in cases
    ):
        return "Native mode must invoke the repository test runner, not python -c."
    errors = [public_case_error(case) for case in cases]
    if cases and all(errors):
        return "No executable behavioral cases: " + "; ".join(errors)
    return None


def project_public_test_context(context: dict[str, Any]) -> dict[str, Any]:
    # 只投影定位线索，不把 EditBrief 的修改指令、历史对话或候选 patch 交给测试设计者。
    return {key: context[key] for key in (
        "problem_contract", "confirmed_facts", "behavior_cases", "old_behavior_invariants",
        "source_files", "hypotheses", "unknowns",
    ) if key in context}


def build_public_validation_prompt(task: str, context: dict[str, Any]) -> str:
    # 保留参数以兼容独立 CLI；测试预期仅来自 Issue 与自行读取的基线源码。
    return (
        # 固定环境说明在前，任务内容后置，跨题共享稳定前缀。
        "## Execution Environment\n"
        + json.dumps({
            "cwd": "repository root",
            "runtime": "docker /testbed" if os.getenv("REPO_AGENT_VERIFY_DOCKER_IMAGE") else "local Python environment",
            "python": "Use python; the executor resolves the interpreter.",
            "dependencies": "Use existing repository configuration; dependency availability is not yet confirmed.",
        }, ensure_ascii=False)
        + "\n\n## Public Issue\n" + extract_public_problem_statement(task)
    )


def _missing_pytest_runner(execution: dict) -> bool:
    command = str(execution.get("command") or "")
    output = str(execution.get("evidence") or execution.get("output") or "")
    return bool(
        execution.get("exit_code") == 1
        and re.search(r"(?:^|\s)-m\s+pytest(?:\s|$)", command)
        and re.search(r"(?m)^(?:\S*/)?python[\d.]*: No module named pytest\s*$", output)
    )


def _classification(execution: dict) -> str:
    output = str(execution.get("evidence") or execution.get("output") or "")
    if _missing_pytest_runner(execution):
        return "environment_blocked"
    if execution.get("exit_code") == 0 and "-m unittest" in str(execution.get("command", "")):
        summary = re.search(r"(?m)^Ran (\d+) tests? in [^\n]+\n\s*OK \(skipped=(\d+)\)\s*$", output)
        if summary and summary[1] == summary[2]:
            return "environment_blocked"
    if execution.get("exit_code") and (
        (execution.get("runtime") == "swebench_docker" and execution.get("exit_code") == 125)
        or re.search(r"docker: Error response from daemon|Cannot connect to the Docker daemon|Unable to find image .* locally", output)
    ):
        return "environment_blocked"
    if execution.get("exit_code") and re.search(r"Database queries to .+ are not allowed in SimpleTestCase", output):
        return "invalid_test"
    if execution.get("exit_code") and "unittest.loader._FailedTest" in output:
        missing = re.search(r"No module named ['\"]([^'\"]+)['\"]", output)
        # 错测试节点可修正；测试导入时缺少第三方依赖仍交由环境处理。
        try:
            labels = shlex.split(str(execution.get("command", "")))
        except ValueError:
            labels = []
        if missing is None or any(label == missing[1] or label.startswith(missing[1] + ".") for label in labels):
            return "invalid_test"
    # 测试模块或类定义中的异常尚未触达被测行为；测试体内的同类异常仍可复现缺陷。
    frames = re.findall(r'File "([^"]+)", line \d+, in ([^\n]+)', output)
    if execution.get("exit_code") and frames:
        path, scope = frames[-1]
        if path.replace("\\", "/").endswith("/django/forms/forms.py") and scope == "__init__" and (
            "ValueError: The empty_permitted and use_required_attribute arguments may not both be True." in output
        ):
            return "invalid_test"
        # patch 装饰器注入参数失败发生在测试体执行前，不能作为生产缺陷反馈。
        if path.replace("\\", "/").endswith("/unittest/mock.py") and scope == "patched" and re.search(
            r"TypeError: (?:[\w.]+\.)?test_\w+\(\) (?:takes .* (?:given|arguments?)|missing .*required .*argument)",
            output,
        ):
            return "invalid_test"
        name = PurePosixPath(path.replace("\\", "/")).name
        if (name.startswith("test_") or name == "tests.py") and (
            scope == "<module>" or scope[:1].isupper()
            or scope in {"setUp", "setUpClass", "setUpModule", "setup_method", "setup_class", "setup_module"}
        ):
            return "invalid_test"
    if "pytest" in str(execution.get("command", "")) and execution.get("exit_code") in {2, 3, 4, 5}:
        return "environment_blocked"
    if re.search(r"SyntaxError:|ImportError:|ERROR collecting|no tests ran|unrecognized arguments", output, re.I):
        return "environment_blocked"
    if execution.get("exit_code") == 1 and re.search(r"(?m)^FAILED\s+\S+::", output):
        # Pytest 可以把被测程序的诊断作为断言消息输出，不能当成执行器缺文件。
        evidence = re.sub(
            r"(?m)^E[ \t]+AssertionError:[^\n]*(?:\nE[ \t]+[^\n]*)*",
            lambda match: re.sub(r"no such file or directory", "file missing (assertion)", match[0], flags=re.I),
            output,
        )
        execution = {**execution, "evidence": evidence}
    return assess_verification_result(execution)["classification"]


def evaluate_public_plan(
    plan: dict, baseline: dict, execute: Callable[[str], dict],
) -> dict:
    """逐条对比固定用例；基线已通过的回归变坏也能触发反馈。"""
    rows = []
    failures = []
    target_reproduced = False
    all_passed = bool(plan.get("cases")) and not plan.get("rejected_cases")
    for case in plan.get("cases", []):
        before = baseline.get(case["id"], {})
        after = execute(case["command"])
        before_class = _classification(before)
        after_class = _classification(after)
        same_command = before.get("command") == after.get("command") == case["command"]
        same_runtime = before.get("runtime") == after.get("runtime")
        same_files = before.get("test_files_sha256") == after.get("test_files_sha256")
        usable = same_command and same_runtime and same_files and (
            (case["kind"] == "target" and before_class == "behavior_failed")
            or (case["kind"] == "regression" and before_class == "behavior_passed")
        )
        target_reproduced |= case["kind"] == "target" and usable
        all_passed &= usable and after_class == "behavior_passed"
        row = {**case, "baseline": before, "patched": after, "usable": usable,
               "baseline_classification": before_class, "patched_classification": after_class}
        rows.append(row)
        if usable and after_class == "behavior_failed":
            failures.append(
                f"## {case['id']} ({case['kind']})\nExpected: {case['expected_behavior']}\n"
                f"Basis: {case['basis']}\nCommand: {case['command']}\n"
                f"Baseline exit: {before.get('exit_code')}\nPatched exit: {after.get('exit_code')}\n"
                + str(after.get("evidence") or after.get("output") or "")[-4000:]
            )
    passed = bool(all_passed and target_reproduced)
    return {
        "validation_kind": "public_test_suite",
        "status": "passed" if passed else "failed" if failures else "inconclusive",
        "feedback_eligible": bool(failures),
        "target_reproduced": target_reproduced,
        "failure_evidence": "\n\n".join(failures),
        "cases": rows,
        "unknowns": plan.get("unknowns", []),
        "rejected_cases": plan.get("rejected_cases", []),
        "resolved": passed,
        "reason": "Public tests passed" if passed else "See per-case baseline and patched evidence; no official evaluation was used.",
    }


class PublicValidationAgent:
    """独立设计会话与固定用例；执行复用现有运行时，不把测试交给实现者修改。"""

    def __init__(self, runner, output_dir: Path, timeout: int = 300):
        self.runner = runner
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self.plan: dict = {"status": "unavailable", "cases": [], "unknowns": ["not invoked"]}
        self.baseline: dict = {}
        self.worker: dict = {}
        self.context: dict = {}
        self.test_overlay: Path | None = None
        self.baseline_probes: list[dict] = []

    def _baseline_probe_tool(self):
        def probe(inp):
            if len(self.baseline_probes) >= 2:
                return ToolResponse.failure("BASELINE_PROBE_BUDGET_EXHAUSTED", "Two baseline probes used; return the final test plan.")
            entry = {"index": len(self.baseline_probes) + 1}
            self.baseline_probes.append(entry)
            commands = inp["commands"]
            plan = {"status": "ready", "execution_mode": "native", "unknowns": [],
                    "files": inp.get("files") or [], "cases": [
                        {"id": f"probe_{index + 1}", "kind": "regression", "command": command,
                         "expected_behavior": "Observe baseline only; no repair contract is established.",
                         "basis": "Temporary baseline probe requested by the test designer."}
                        for index, command in enumerate(commands)
                    ]}
            error = public_plan_error(json.dumps(plan, ensure_ascii=False))
            if not error and len(commands) > 2:
                error = "Submit at most two native test commands."
            if error:
                entry["error"] = error
                return ToolResponse.failure("INVALID_BASELINE_PROBE", error)
            # 试跑使用独立的测试目录与状态，不能覆盖待冻结的正式计划。
            executor = PublicValidationAgent(self.runner, self.output_dir / "baseline-probes" / f"probe-{entry['index']}",
                                             timeout=min(self.timeout, 120))
            result = SimpleNamespace(text=json.dumps(plan, ensure_ascii=False), completed=True,
                                     output_dir=executor.output_dir, total_tokens=0, model="baseline-executor")
            error = executor._prepare_baseline(result)
            artifact = executor.save()
            entry.update(artifact=str(executor.output_dir / "public_validation.json"), error=error)
            if error:
                return ToolResponse.failure("INVALID_BASELINE_PROBE", error)
            observations = [{"id": identifier, **execution, "classification": _classification(execution)}
                            for identifier, execution in artifact["baseline"].items()]
            data = {"observations": observations, "rejected_cases": artifact["plan"].get("rejected_cases", []),
                    "remaining_probes": 2 - len(self.baseline_probes)}
            return ToolResponse.success(
                "Baseline observations only; confirm the public contract before retaining assertions. "
                f"Remaining probes: {data['remaining_probes']}.", data,
            )

        return build_tool(
            name="run_baseline_tests", description="试跑临时原生测试以确认修改前的行为和 fixture。最多两次，每次最多两条命令。每次独立暂存文件；重用新增模块时仍需传入完整 files。",
            parameters=[ToolParameter("commands", "array", "一到两条完整的原生测试命令字符串。"),
                        ToolParameter("files", "array", "新增测试模块的数组，每项为 {path, content}；仅运行已有测试时可省略。", required=False)],
            handler=probe, is_read_only=True, is_concurrency_safe=False, max_result_size_chars=16000,
        )

    def execute(self, command: str) -> dict:
        tool = self.runner.base_tools.get("run_command")
        if tool is None:
            return {"command": command, "status": "blocked_by_environment", "exit_code": None,
                    "evidence": "No execution tool is available"}
        response = self.runner._run_swebench_verification_command(
            tool, {"command": command, "timeout": self.timeout,
                   "public_test_overlay": str(self.test_overlay) if self.test_overlay else None},
        )
        code = response.data.get("exit_code")
        return {"command": command, "exit_code": code,
                "status": "passed" if code == 0 else "failed" if isinstance(code, int) else "blocked_by_environment",
                "evidence": response.text[-6000:],
                "runtime": response.data.get("verification_runtime"),
                "test_files_sha256": test_overlay_digest(self.test_overlay)}

    def design(self, task: str, context: dict) -> dict:
        self.context = {}
        self.baseline_probes = []
        probe_tool = self._baseline_probe_tool()
        result = self.runner.run(
            agent_type="public_validation", description="Design independent public behavior tests",
            prompt=build_public_validation_prompt(task, self.context),
            test_design_tool=probe_tool,
        )
        plan_error = self._prepare_baseline(result)
        mismatches = [
            {"case": case, "execution": self.baseline[case["id"]]}
            for case in self.plan["cases"]
            if _missing_pytest_runner(self.baseline[case["id"]])
            or _classification(self.baseline[case["id"]]) not in {"environment_blocked", (
                "behavior_failed" if case["kind"] == "target" else "behavior_passed"
            )}
        ]
        review_coverage = any(_classification(execution) in {"behavior_passed", "behavior_failed"}
                              for execution in self.baseline.values())
        if plan_error or mismatches or review_coverage:
            initial = self.save()
            (self.output_dir / "public_validation_initial.json").write_text(
                json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")
            history_path = Path(result.output_dir) / "messages.json"
            if history_path.exists():
                result = self.runner.run(
                    agent_type="public_validation", description="Review public test coverage and baseline once",
                    test_design_history=json.loads(history_path.read_text(encoding="utf-8")),
                    test_design_tool=probe_tool,
                    prompt=("你之前为当前 Issue 生成了一组测试。执行器已在尚未应用修复补丁的原始仓库中运行这些测试。"
                            "沿用当前会话的测试方案、源码和执行记录，结合下方反馈进行唯一一次复核。"
                            "优先修正已观察到的导入、fixture、节点或断言错误，区分测试问题、目标缺陷与环境阻塞。"
                            "已通过且仍应保留的目标用例改为 regression；因本题缺陷失败的用例标为 target。"
                            "保留有效覆盖，不扩大无关范围，不翻转断言迎合运行结果。"
                            "必要时使用剩余试跑机会确认修改；未执行或跳过不算通过。"
                            "返回完整方案 JSON，无法确认的事项写入 unknowns；无需修改时返回原方案。\n" + json.dumps({
                                "public_issue": extract_public_problem_statement(task),
                                "plan_error": plan_error, "baseline_mismatches": mismatches,
                                "remaining_probes": 2 - len(self.baseline_probes),
                                "baseline_observations": {identifier: {
                                    "command": execution.get("command"),
                                    "exit_code": execution.get("exit_code"),
                                    "classification": _classification(execution),
                                    "evidence": str(execution.get("evidence") or execution.get("output") or "")[-1500:],
                                }
                                    for identifier, execution in self.baseline.items()},
                            }, ensure_ascii=False)),
                )
                previous_worker = dict(self.worker)
                self._prepare_baseline(result)
                for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
                    self.worker[key] += previous_worker[key]
                self.worker["baseline_repair"] = {"attempts": 1, "initial_output_dir": previous_worker["output_dir"],
                                                  "reason": "baseline_mismatch" if plan_error or mismatches else "coverage_review"}
        return self.save()

    def _prepare_baseline(self, result) -> str | None:
        error = public_plan_error(result.text) or (None if result.completed else "Test design did not complete")
        self.plan = extract_json_object(result.text) if not error else {
            "status": "unavailable", "cases": [],
            "unknowns": [error or "Test design did not complete"],
        }
        # 单条命令无效时保留其他用例，拒绝原因随产物保存，禁止执行被拒绝的命令。
        accepted, rejected, ids = [], [], set()
        for case in self.plan["cases"]:
            reason = public_case_error(case)
            if not reason and case["id"] in ids:
                reason = "Duplicate case id"
            if reason:
                rejected.append({"case": case, "reason": reason})
            else:
                accepted.append(case)
                ids.add(case["id"])
        self.plan["cases"] = accepted
        if rejected:
            self.plan["rejected_cases"] = rejected
        self.worker = {"output_dir": str(result.output_dir), "completed": result.completed,
                       "total_tokens": result.total_tokens, "model": result.model,
                       "prompt_tokens": getattr(result, "prompt_tokens", 0),
                       "completion_tokens": getattr(result, "completion_tokens", 0)}
        self.test_overlay = None
        files = self.plan.get("files", [])
        if files:
            repo = Path(self.runner.cwd).resolve()
            for file in files:
                target = repo / file["path"]
                if not target.resolve().is_relative_to(repo) or target.exists():
                    error = f"Generated test file would overwrite or escape the repository: {file['path']}"
                    self.plan = {"status": "unavailable", "cases": [], "unknowns": [
                        error,
                    ]}
                    break
            else:
                suite_dir = self.output_dir / "public-test-files"
                suite_dir.mkdir(parents=True, exist_ok=True)
                self.test_overlay = Path(tempfile.mkdtemp(prefix="suite-", dir=suite_dir))
                for file in files:
                    target = self.test_overlay / file["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(file["content"], encoding="utf-8")
        self.baseline = {case["id"]: self.execute(case["command"]) for case in self.plan["cases"]}
        return error

    def evaluate(self) -> dict:
        validation = evaluate_public_plan(self.plan, self.baseline, self.execute)
        self.save(validation)
        return validation

    def save(self, validation: dict | None = None) -> dict:
        payload = {"plan": self.plan, "baseline": self.baseline, "worker": self.worker,
                   "test_context": self.context,
                   "baseline_probes": self.baseline_probes,
                   "test_files_dir": str(self.test_overlay) if self.test_overlay else None}
        if validation is not None:
            payload["validation"] = validation
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "public_validation.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return payload
