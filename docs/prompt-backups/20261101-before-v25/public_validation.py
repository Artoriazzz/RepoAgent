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


PUBLIC_VALIDATION_PROMPT = """你负责为软件修复设计独立的公开行为测试。依据仅限本次 Issue、修改前源码、已有测试和文档。

设计顺序：
1. 从 Issue 提取触发输入、错误和完整期望，读取实际入口、调用链及相邻测试体。用户以 ideally、would be helpful 等措辞提出的外部行为仍属于期望，不能仅因措辞温和就放入 unknowns；建议的内部修法与外部行为要求要分开。先确定可观察行为，不需要选择修法。源码工具为 search_repo、read_file、list_files；限制目录及读取行范围，不反复列目录或阅读运行器。
2. 为本题检查三部分：故障输入应产生什么完整结果；同一要求是否适用于源码已支持的配置或扩展类型；附近哪些已成立的行为需要保留。相关扩展入口至少读取一个实际使用示例。返回值、错误类别、日志、对象状态和副作用都可能是结果的一部分，不以“不崩溃”替代它们。
3. 依照已有合法示例构造最小测试，保留输入类型、fixture 和初始化。比较或归一化问题先明确允许忽略什么差异；除明确要求忽略的差异外，保留源码中仍有语义的结构、值与元数据，不因显示内容相同就判为等价。接受标量和容器的 API 即使共享某个内部列表，也可能保留不同的结构属性，须读取对象实际保留的状态，不能据共享列表推断两种形式等价。同一结构内的值变化与不同结构分别测试；构造时已归一化丢弃的信息不能重新赋予语义。排序需覆盖主键相同的情况，聚合使用多个不同值。只覆盖与本题直接相关的分支。

如何判断依据：
- 已有测试、文档和公开 API 定义行为契约，缺陷源码帮助定位触发路径。故障路径目前缺少某个动作不能成为排除该契约的理由；例如已有错误处理要求同时返回安全值并记录日志，新增故障输入仍须检查两者。
- 源码支持的可配置字段、自定义类型和可选属性需要检查其实际入口，不能用默认示例代替，也不能仅以 Issue 没给示例为由列入 unknowns。读取字段或参数定义，覆盖本题相关的合法取值；缺失属性、空字符串和 None 是不同情况，是否允许以已读定义为准。
- 内部映射或调用方式可以变化，但调用者能读取的属性、执行上下文中的名字、返回值和错误信息是可观察行为。基线通过的相关行为即使不能复现缺陷，也应保留为 regression；回归测试不要求在基线上失败。
- 基线通过只是执行事实，不自动构成应保留的契约。Issue 要求改变的旧行为不能设为 regression；例如新增值相等语义时，不能用旧的对象身份不等来要求原本归一化为相同内容的对象继续不等，也不能为满足测试去改写已有构造或归一化规则。
- 只有期望仍无法从公开材料确定时才写 unknowns，并简述缺少什么依据。未定义的新编号、措辞不能猜测；已定义的错误分类和消息应沿用。不能因精确细节未知而放弃其余有依据的行为检查。

目标用例的预期值也需验证。基线抛异常只能证明触发故障，不能证明异常后尚未执行的断言正确。兼容类问题中，每个 target 的数据都应有已有可用形式的独立 regression 对照，使用相同数据、边界与结果断言；不能只为其中一个输入提供对照，也不能把对照放在基线失败语句之后。返回集合需先核实顺序契约，无顺序要求时比较成员，不能从创建顺序猜测返回顺序。无等价路径时不用臆造参考实现。

只约束结果，不指定修法。Issue 的建议和当前实现步骤都不能直接变成“必须调用/禁止调用某内部函数”的断言；无公开调用契约时，mock 仅隔离外部资源，最终断言实际行为。相似后端、内部函数异常及过时 docstring 需与所测入口核对。相等对象应保持相等哈希，不相等对象允许哈希碰撞。恢复操作后检查正常操作仍可用。

测试组织与执行：
- 可用 run_baseline_tests 时，对尚未确认的已有行为、缺省输入或 fixture，先提交 files（新增测试文件）和 commands（原生测试命令字符串数组）试跑，再据实际结果确定回归要求。最多两次，每次最多两条命令；无需提供最终方案的 status、kind、basis 等字段。不能读取修复后结果，也不能用它选择修法。现有合法输入在基线成功时，不要只因 Issue 未重申而将它留为未知。基线失败只说明实际行为，仍须核实是否命中 Issue，不能将错误断言当作契约。试跑不会自动加入最终用例，需在最终 JSON 中保留有依据的测试。
- 默认 execution_mode=native，在相邻目录生成新的 test_*.py 模块。先读相邻测试的 imports、基类、装饰器、setup/fixture 和测试入口，按原风格构造可加载用例。沿用 mock.patch 装饰器时同时保留其注入参数，fixture 错误不是目标缺陷。files 只包含新增测试模块的路径和完整内容，不添加说明、占位文件，不覆盖旧文件、conftest、包初始化或配置。
- 最多 6 条执行命令，每条可包含多个方法或参数组合。target 是基线上应违反 Issue 预期的行为；regression 是基线上已成立且需要保留的行为。不同种类分开运行，不能把混有 target 的类或模块整体标为 regression。
- 优先运行至少一个相关的已有完整测试类；行为跨类时运行所属模块。保留其非法输入、错误处理和相邻行为，不复制已有用例。合并重复命令，不扩大到整个仓库；没有相关类或模块，或已知无法运行时，在 unknowns 解释。
- 每项给出 command、expected_behavior 和 basis。命令节点必须与已读文件的模块路径逐项对应，例如 tests/pkg/tests.py 中的 SomeTests 对应 pkg.tests.SomeTests，不能假设测试类从包顶层导出。命令从仓库根目录通过原入口运行，例如 python tests/runtests.py 包.模块.类.方法 或 python -m pytest 路径::节点。仅在确认无需框架初始化时使用 execution_mode=inline，并提供 inline_reason。
- 执行器会运行基线，再冻结测试并在修复后重跑。你不能声称已经执行。收到基线不符或方案错误反馈时，保留有效覆盖，修正输入、初始化或无依据断言。数据库操作需沿用具备数据库访问权限的测试基类；只 mock 创建函数不代表后续调用不访问数据库。测试加载或 fixture 错误不等于缺陷；不得翻转断言制造通过。目标暂未复现时仍保留有效 regression，以 ready 返回；unavailable 仅用于没有任何有效用例。

不修改生产源码、不安装依赖、不联网，不读取历史经验、评测报告、官方测试补丁或隐藏测试名单。不用管道、忽略退出码、宽泛捕获异常或只打印结果掩盖失败。
可以只设计有依据的回归测试；确实无法设计时返回 unavailable。测试通过只说明覆盖的行为通过，不能证明未知预期。
只输出 JSON：
{"status":"ready|unavailable","execution_mode":"native","files":[{"path":"tests/test_public_repair.py","content":"完整的原生测试模块内容"}],"cases":[{"id":"target_1","kind":"target|regression","command":"python -m pytest tests/test_public_repair.py::test_target","expected_behavior":"公开预期","basis":"读取依据、路径与排除的错误行为"}],"unknowns":["尚不能确认的行为"]}
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


def _classification(execution: dict) -> str:
    output = str(execution.get("evidence") or execution.get("output") or "")
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
            if _classification(self.baseline[case["id"]]) not in {"environment_blocked", (
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
                    prompt=("生产代码尚未修改。现在进行唯一一次方案修正与覆盖复核，沿用上面的完整历史。"
                            "先核对公开 Issue 的每项外部期望是否真的进入了用例；温和措辞的期望也不能遗漏。"
                            "再检查 unknowns：是否确实缺少公开依据，还是尚未读取相关的基类、配置或合法缺省输入。"
                            "必要时补读实际定义和已有示例；不能只因默认测试没有覆盖某输入，就断言仓库不支持它。"
                            "先审查预期的依据，再考虑补覆盖。精确常量、新编号、输出类型或表示方式必须有公开原文或已读定义支持；"
                            "unknowns 已承认未定义的细节不得同时作为断言，删除无依据的精确限制，保留有依据的行为要求。"
                            "基线异常会遮住其后的断言：用独立的基线可用输入运行同一断言逻辑，核实输入输出的类型、结构和单位；"
                            "参数化目标与可用对照共用预期计算和断言逻辑，按已读 API 契约计算转换后的结果，不能直接把原输入当预期；"
                            "添加新参数组合时同步检查这套逻辑仍成立，尤其是边界、缺省值和归一化输入。"
                            "无法构造对照时如实标记未验证，不要将预期异常之前的失败当成其后断言正确的证据。"
                            "然后找一个能通过当前断言、却仍违反已读公开要求的最小反例，再据此补充或修正用例；"
                            "重点核对调用是否意外改变调用者仍使用的输入对象，以及初始化后的再次调用、更新或恢复是否保持相关公开状态一致。"
                            "只测试有公开依据的反例，不猜测新参数名、错误编号或内部实现；找不到时保留已有有效覆盖。"
                            "核实每个断言与所测入口一致，不把显示相同推断为结构等价，也不把当前缺陷当成契约。"
                            "基线成功或失败仅是观察，不能自动证明目标断言正确。"
                            "如有错误，先区分格式、导入/初始化、输入/断言错误、行为已存在或环境问题。"
                            "沿用已读源码，修正测试并返回完整计划 JSON，unknowns 说明原因；"
                            "若无需修改则返回原计划。保留有效用例和相关覆盖，不能仅翻转断言让测试通过。环境问题如实标记，"
                            "不安装依赖或修改生产代码。\n" + json.dumps({
                                "public_issue": extract_public_problem_statement(task),
                                "plan_error": plan_error, "baseline_mismatches": mismatches,
                                "baseline_observations": {identifier: {"exit_code": execution.get("exit_code"),
                                    "classification": _classification(execution)}
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
