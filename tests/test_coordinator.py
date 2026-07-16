import json
import subprocess
import sys

from repo_agent.core.coordinator import (
    COORDINATOR_SYSTEM_PROMPT,
    CoordinatorEngine,
    _budgeted_agent_tools,
    _candidate_mode_required_for_task,
    _coordinator_completion_guard,
    _coordinator_read_only_tools,
)
from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.memory import MemoryStore
from repo_agent.core.repair_memory import (
    RepairRecipeConstraint,
    augment_task_with_repair_recipes,
    evaluate_repair_recipe_applicability,
    hard_constraints_from_repair_recipes,
    repair_recipe_query_from_task,
)
from repo_agent.core.subagent import (
    IMPLEMENT_PROMPT,
    PATCH_RANKER_PROMPT,
    PATCH_REVIEW_PROMPT,
    REPRODUCER_PROMPT,
    RESEARCH_PROMPT,
    SWEBENCH_IMPLEMENT_PROMPT,
    SWEBENCH_REPRODUCER_PROMPT,
    VERIFY_PROMPT,
    SubAgentRunner,
    WorkerConstraints,
    WorkerResult,
    get_worker_spec,
)
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, build_tool
from repo_agent.tools.agent_tools import (
    _augment_corrective_prompt,
    _augment_swebench_semantic_prompt,
    _bind_verification_to_plan,
    _candidate_diversity_warnings,
    _check_swebench_implement_edit_brief,
    _conditional_patch_review_reasons,
    _constrain_implement_to_consumer_layer,
    _constrain_nullable_pandas_scope_from_source,
    _constrain_star_alias_scope_from_source,
    _patch_review_requires_revision,
    _research_like_implement_terms,
    _run_candidate_commands,
    build_agent_tools,
)
from repo_agent.tools.repo_tools import RepoWorkspace, register_repo_tools


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tool_names_by_call = []
        self.messages_by_call = []

    def invoke_with_tools(self, messages, tools):
        self.messages_by_call.append(messages)
        self.tool_names_by_call.append([tool["function"]["name"] for tool in tools])
        return self.responses.pop(0)


def test_subagent_runner_uses_implement_model_override(tmp_path):
    flash = FakeLLM([])
    flash.model = "deepseek-v4-flash"
    pro = FakeLLM(
        [LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={})]
    )
    pro.model = "deepseek-v4-pro"
    runner = SubAgentRunner(
        llm=flash,
        llm_overrides={"implement": pro},
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
    )

    result = runner.run(
        agent_type="implement",
        prompt="Make the scoped change.",
        constraints=WorkerConstraints.from_tool_input(
            {"allowed_files": ["app.py"], "allowed_ranges": {"app.py": [[1, 1]]}}
        ),
    )

    assert result.model == "deepseek-v4-pro"
    assert len(pro.messages_by_call) == 1
    assert flash.messages_by_call == []


def _base_tools():
    registry = ToolRegistry()
    registry.register(
        build_tool(
            name="read_file",
            description="Read",
            parameters=[ToolParameter("path", "string", "Path")],
            handler=lambda inp: ToolResponse.success("content", {"path": inp["path"]}),
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        build_tool(
            name="write_file",
            description="Write",
            parameters=[
                ToolParameter("path", "string", "Path"),
                ToolParameter("content", "string", "Content"),
            ],
            handler=lambda inp: ToolResponse.success("written", {"path": inp["path"]}),
        )
    )
    registry.register(
        build_tool(
            name="search_repo",
            description="Search",
            parameters=[ToolParameter("query", "string", "Query")],
            handler=lambda inp: ToolResponse.success("matches", {"query": inp["query"]}),
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        build_tool(
            name="apply_patch",
            description="Patch",
            parameters=[ToolParameter("patch", "string", "Patch")],
            handler=lambda inp: ToolResponse.success("patched", {"patch": inp["patch"]}),
        )
    )
    registry.register(
        build_tool(
            name="git_diff",
            description="Diff",
            parameters=[],
            handler=lambda inp: ToolResponse.success("diff"),
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        build_tool(
            name="run_command",
            description="Run",
            parameters=[ToolParameter("command", "string", "Command")],
            handler=lambda inp: ToolResponse.success("ran"),
        )
    )
    return registry


def _write_rst_fixture(root):
    path = root / "astropy" / "io" / "ascii" / "rst.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# fixture"]
    while len(lines) < 15:
        lines.append("")
    lines.extend(
        [
            "class SimpleRSTHeader(FixedWidthHeader):",  # 16
            "    position_line = 0",
            "    start_line = 1",
            "    splitter_class = DefaultSplitter",
            "    position_char = '='",
            "",
            "    def get_fixedwidth_params(self, line):",
            "        vals, starts, ends = super().get_fixedwidth_params(line)",
            "        ends[-1] = None",
            "        return vals, starts, ends",
            "",
            "",
            "class SimpleRSTData(FixedWidthData):",  # 28
            "    start_line = 3",
            "    end_line = -1",
            "    splitter_class = FixedWidthTwoLineDataSplitter",
            "",
            "",
            "class RST(FixedWidth):",  # 34
        ]
    )
    while len(lines) < 59:
        lines.append("")
    lines.extend(
        [
            "    def __init__(self):",  # 60
            "        super().__init__(delimiter_pad=None, bookend=False)",
            "",
            "    def write(self, lines):",
            "        lines = super().write(lines)",
            "        lines = [lines[1]] + lines + [lines[1]]",
            "        return lines",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_coordinator_only_exposes_agent_and_research_worker_is_read_only(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_agent",
                        name="agent",
                        arguments=json.dumps(
                            {
                                "description": "探索代码",
                                "prompt": "读取 src/app.py 并总结。",
                                "type": "research",
                            }
                        ),
                    )
                ],
                raw_message={"tool_calls": [{"id": "call_agent"}]},
                usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            ),
            LLMToolResponse(
                content="研究完成",
                tool_calls=[],
                raw_message={"content": "研究完成"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="最终结果",
                tool_calls=[],
                raw_message={"content": "最终结果"},
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            ),
        ]
    )
    engine = CoordinatorEngine(llm=llm, worker_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")

    assert engine.run("分析仓库") == "最终结果"
    assert set(llm.tool_names_by_call[0]) == {"agent", "patch_candidate", "read_file", "search_repo", "git_diff"}
    assert "read_file" in llm.tool_names_by_call[1]
    assert "search_repo" in llm.tool_names_by_call[1]
    assert "write_file" not in llm.tool_names_by_call[1]
    assert (tmp_path / "out" / "subagents").exists()


def test_worker_spec_aliases_explore_to_research():
    assert get_worker_spec("explore").name == "research"
    assert get_worker_spec("reproducer").name == "reproducer"
    assert get_worker_spec("patch_ranker").name == "patch_ranker"
    assert get_worker_spec("patch_review").name == "patch_review"
    assert get_worker_spec("case_curator").name == "case_curator"
    assert get_worker_spec("unknown").name == "general"


def test_worker_specs_have_tight_budgets_and_stop_rules():
    assert get_worker_spec("research").max_steps == 4
    assert get_worker_spec("reproducer").max_steps == 5
    assert get_worker_spec("verify").max_steps == 4
    assert get_worker_spec("implement").max_steps == 8
    assert get_worker_spec("patch_ranker").max_steps == 3
    assert get_worker_spec("patch_review").max_steps == 6
    assert "够用即停" in RESEARCH_PROMPT
    assert "best_effort_edit_brief" in RESEARCH_PROMPT
    assert "problem_contract" in RESEARCH_PROMPT
    assert "semantic_cases" in RESEARCH_PROMPT
    assert "old_behavior_invariants" in RESEARCH_PROMPT
    assert "comparison_ranges" in RESEARCH_PROMPT
    assert "semantic_coverage" in IMPLEMENT_PROMPT
    assert "old_behavior_invariants_preserved" in IMPLEMENT_PROMPT
    assert "Repair Brief" in IMPLEMENT_PROMPT
    assert "target_code_context" in IMPLEMENT_PROMPT
    assert "minimal_fix_strategy" in IMPLEMENT_PROMPT
    assert "精确类型、精确字符串、顺序和格式" in IMPLEMENT_PROMPT
    assert "Sphinx/autodoc/autosummary/napoleon/domain" in IMPLEMENT_PROMPT
    assert "sklearn/sympy" in IMPLEMENT_PROMPT
    assert "Repair Brief" in SWEBENCH_IMPLEMENT_PROMPT
    assert "minimal_fix_strategy" in SWEBENCH_IMPLEMENT_PROMPT
    assert "窄 guard" in SWEBENCH_IMPLEMENT_PROMPT
    assert "regression-aware patch gate" in PATCH_REVIEW_PROMPT
    assert "patch_risk" in PATCH_REVIEW_PROMPT
    assert "最多额外调用 3 次 read_file" in PATCH_REVIEW_PROMPT
    assert "不能使用 search_repo" in PATCH_REVIEW_PROMPT
    assert "parent_default_behavior" in PATCH_REVIEW_PROMPT
    assert "wrong_layer_or_exact_contract" in PATCH_REVIEW_PROMPT
    assert "public_regression_risk" in PATCH_REVIEW_PROMPT
    assert "inferred_regression_test_families" in PATCH_REVIEW_PROMPT
    assert "coverage_gap" in PATCH_REVIEW_PROMPT
    assert "verdict" in PATCH_REVIEW_PROMPT
    assert "required_changes" in PATCH_REVIEW_PROMPT
    assert "默认最多运行 1 条命令" in VERIFY_PROMPT
    assert "blocked_by_environment" in VERIFY_PROMPT
    assert "git diff --check" in VERIFY_PROMPT
    assert "py_compile" in VERIFY_PROMPT
    assert "reproduction_contract" in REPRODUCER_PROMPT
    assert "differential_cases" in REPRODUCER_PROMPT
    assert "PASS_TO_PASS / FAIL_TO_PASS" in REPRODUCER_PROMPT
    assert "selected_index" in PATCH_RANKER_PROMPT
    assert "候选 patch 排序" in PATCH_RANKER_PROMPT
    assert "pass@1" in COORDINATOR_SYSTEM_PROMPT
    assert "不要请求、等待或使用 harness 失败日志做第二轮修改" in COORDINATOR_SYSTEM_PROMPT
    assert "最多派 1 个 research Worker" in COORDINATOR_SYSTEM_PROMPT
    assert "最多 5 个只读工具调用" in COORDINATOR_SYSTEM_PROMPT
    assert "不要安装依赖" in COORDINATOR_SYSTEM_PROMPT
    assert "semantic_cases" in COORDINATOR_SYSTEM_PROMPT
    assert "old_behavior_invariants" in COORDINATOR_SYSTEM_PROMPT
    assert "直接调用/参数转发路径" in COORDINATOR_SYSTEM_PROMPT
    assert "semantic_coverage" in COORDINATOR_SYSTEM_PROMPT
    assert "line_replacements" in COORDINATOR_SYSTEM_PROMPT
    assert "COORDINATOR_READ_BUDGET_EXHAUSTED" in COORDINATOR_SYSTEM_PROMPT
    assert "blocked_insufficient_edit_scope" in COORDINATOR_SYSTEM_PROMPT
    assert "SWE-bench 模式禁止修改" in COORDINATOR_SYSTEM_PROMPT
    assert "父类默认实现" in COORDINATOR_SYSTEM_PROMPT
    assert "verdict=accept" in COORDINATOR_SYSTEM_PROMPT
    assert "public_regression_risk" in COORDINATOR_SYSTEM_PROMPT
    assert "patch_review" in COORDINATOR_SYSTEM_PROMPT
    assert "patch_candidate(action=\"save_current\")" in COORDINATOR_SYSTEM_PROMPT
    assert "patch_ranker" in COORDINATOR_SYSTEM_PROMPT
    assert "reproducer" in COORDINATOR_SYSTEM_PROMPT
    assert "bare `param args`/`param kwargs` 必须为零" in SWEBENCH_REPRODUCER_PROMPT
    assert "`type *args`/`type **kwargs`" in SWEBENCH_REPRODUCER_PROMPT
    assert "最终 Sphinx 构建或公开集成测试" in SWEBENCH_REPRODUCER_PROMPT
    assert "框架会确定性绑定全部 required case ID" in SWEBENCH_REPRODUCER_PROMPT
    assert "最终公开输出写明确 `assert`" in SWEBENCH_REPRODUCER_PROMPT


def test_consumer_semantic_evidence_removes_producer_only_implement_range():
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sphinx/ext/autodoc/typehints.py"],
            "allowed_ranges": {
                "sphinx/ext/autodoc/typehints.py": [[14, 34], [88, 188]],
            },
        }
    )
    evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints at sphinx/ext/autodoc/typehints.py:15",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "sphinx/ext/autodoc/typehints.py:115",
                    }
                ],
                "evidence": [
                    {"type": "code", "location": "sphinx/ext/autodoc/typehints.py:30"}
                ],
            }
        ]
    }

    narrowed, removed = _constrain_implement_to_consumer_layer(constraints, evidence)

    assert removed == ["sphinx/ext/autodoc/typehints.py:14-34"]
    assert {
        path: [(line_range.start, line_range.end) for line_range in ranges]
        for path, ranges in narrowed.allowed_ranges.items()
    } == {"sphinx/ext/autodoc/typehints.py": [(88, 188)]}


def test_star_alias_scope_is_moved_from_producer_to_source_consumers(tmp_path):
    path = tmp_path / "sphinx" / "ext" / "autodoc" / "typehints.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def record_typehints():\n"
        "    pass\n\n"
        "def modify_field_list(fields, annotations):\n"
        "    for field in fields:\n"
        "        pass\n"
        "    for name, annotation in annotations.items():\n"
        "        write(name, annotation)\n"
        "    if 'return' in annotations:\n"
        "        write_return()\n\n"
        "def augment_descriptions_with_types(fields, annotations):\n"
        "    for field in fields:\n"
        "        pass\n"
        "    for name in annotations:\n"
        "        write_type(name)\n"
        "    if 'return' in annotations:\n"
        "        write_rtype()\n",
        encoding="utf-8",
    )
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sphinx/ext/autodoc/typehints.py"],
            "allowed_ranges": {"sphinx/ext/autodoc/typehints.py": [[1, 2]]},
        }
    )

    narrowed, adjustments = _constrain_star_alias_scope_from_source(
        constraints,
        "Fix duplicate *args and **kwargs by changing record_typehints.",
        tmp_path,
    )

    assert adjustments
    assert {
        key: [(item.start, item.end) for item in value]
        for key, value in narrowed.allowed_ranges.items()
    } == {"sphinx/ext/autodoc/typehints.py": [(15, 16), (7, 8)]}


def test_nullable_pandas_scope_is_limited_to_validation_import_and_type_of_target(tmp_path):
    path = tmp_path / "sklearn" / "utils" / "multiclass.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from .validation import check_array\n\n"
        "def unique_labels(*ys):\n"
        "    return ys\n\n"
        "def type_of_target(y):\n"
        "    if is_multilabel(y):\n"
        "        return 'multilabel-indicator'\n"
        "    return 'unknown'\n",
        encoding="utf-8",
    )
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sklearn/utils/multiclass.py"],
            "allowed_ranges": {"sklearn/utils/multiclass.py": [[1, 9]]},
        }
    )

    narrowed, adjustments = _constrain_nullable_pandas_scope_from_source(
        constraints,
        "Support nullable pandas extension dtype labels.",
        tmp_path,
    )

    assert adjustments
    assert {
        key: [(item.start, item.end) for item in value]
        for key, value in narrowed.allowed_ranges.items()
    } == {"sklearn/utils/multiclass.py": [(1, 1), (6, 9)]}


def test_nullable_pandas_implement_prompt_requires_series_and_dataframe_matrix():
    prompt = _augment_swebench_semantic_prompt(
        "Implement nullable pandas dtype support.",
        {
            "problem_statement": "Support nullable pandas dtypes in unique_labels",
            "semantic_evidence": {},
            "semantic_evidence_issues": [],
            "semantic_evidence_warnings": [],
        },
    )

    assert "1D pandas Series (`.dtype`)" in prompt
    assert "2D pandas DataFrame (`.dtypes`)" in prompt
    assert "hasattr(y, 'dtype')" in prompt
    assert "REQUIRED EDIT PATH FOR THIS TASK" in prompt
    assert "`_pandas_dtype_needs_early_conversion` helper" in prompt
    assert "`hasattr(y, 'dtypes') and hasattr(y.dtypes, '__array__')`" in prompt
    assert "for dtype in y.dtypes" in prompt
    assert "`hasattr(y, 'iloc') and hasattr(y, 'dtype')`" in prompt
    assert "`_pandas_dtype_needs_early_conversion(y.dtype)`" in prompt
    assert "`check_array(y, dtype=None, ensure_2d=False)`" in prompt
    assert "before the existing `is_multilabel(y)` predicate" in prompt
    assert "Never guard this conversion with `hasattr(y, '__array__')`" in prompt
    assert "`hasattr(y, 'dtypes')` alone misclassifies Series" in prompt
    assert "`hasattr(y, 'dtype')` alone captures numpy/sparse inputs" in prompt
    assert "Do not add a parallel `to_numpy`, `astype`" in prompt


def test_swebench_reproducer_has_focused_tools_and_budget(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
        swebench_profile=True,
    )
    spec = runner._worker_spec("reproducer")
    tools = runner._tools_for(spec)

    assert spec.max_steps == 6
    assert set(tools.names()) == {"read_file", "search_repo", "run_command"}
    assert "list_files" not in tools.names()
    assert runner._tools_for(runner._worker_spec("patch_ranker")).names() == []


def test_candidate_command_uses_current_python_and_preserves_explicit_exit_code():
    captured = {}
    run_tool = build_tool(
        name="run_command",
        description="Run",
        parameters=[
            ToolParameter("command", "string", "Command"),
            ToolParameter("timeout", "integer", "Timeout", required=False),
        ],
        handler=lambda inp: (
            captured.update(inp)
            or ToolResponse.success(
                "exit_code=0\n/bin/sh: 1: python: not found\nEXIT_CODE=127\n",
                {"exit_code": 0},
            )
        ),
    )

    result = _run_candidate_commands(run_tool, ["python -c 'print(1)'; echo EXIT_CODE=127"])[0]

    assert shlex_quote_for_test(sys.executable) in captured["command"]
    assert result["exit_code"] == 127
    assert result["status"] == "blocked_by_environment"
    assert result["usable"] is False


def shlex_quote_for_test(value):
    import shlex

    return shlex.quote(value)


def test_candidate_diversity_warns_for_same_layer_and_hunk():
    warnings = _candidate_diversity_warnings(
        [
            {"semantic_layer": "consumer_adapter", "edit_loci": ["src/app.py:10-12"]},
            {"semantic_layer": "consumer_adapter", "edit_loci": ["src/app.py:10-12"]},
        ]
    )

    assert any("same diff hunk" in warning for warning in warnings)
    assert any("same semantic_layer" in warning for warning in warnings)


def test_coordinator_read_budget_blocks_endless_reading_before_worker(tmp_path):
    budget_state = {
        "read_calls_before_worker": 0,
        "worker_calls": 0,
        "research_calls": 0,
        "research_failed": 0,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }
    registry = ToolRegistry()
    registry.register_many(
        _coordinator_read_only_tools(
            _base_tools(),
            budget_state=budget_state,
            read_budget_before_worker=2,
        )
    )

    assert registry.execute("search_repo", {"query": "one"}).status == "success"
    assert registry.execute("read_file", {"path": "app.py"}).status == "success"

    blocked = registry.execute("search_repo", {"query": "two"})

    assert blocked.status == "error"
    assert blocked.error["code"] == "COORDINATOR_TRANSITION_REQUIRED"
    assert budget_state["read_calls_before_worker"] == 2
    assert budget_state["required_transition"] == "implement"

    budget_state["worker_calls"] = 1
    budget_state["required_transition"] = ""
    assert registry.execute("search_repo", {"query": "after worker"}).status == "success"


def test_coordinator_blocks_reads_after_failed_research(tmp_path):
    budget_state = {
        "read_calls_before_worker": 0,
        "worker_calls": 1,
        "research_calls": 1,
        "research_failed": 1,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }
    registry = ToolRegistry()
    registry.register_many(_coordinator_read_only_tools(_base_tools(), budget_state=budget_state))

    blocked = registry.execute("search_repo", {"query": "keep digging"})

    assert blocked.status == "error"
    assert blocked.error["code"] == "COORDINATOR_RESEARCH_FAILED_IMPLEMENT_REQUIRED"


def test_swebench_profile_allows_limited_read_recovery_after_failed_research(tmp_path):
    budget_state = {
        "read_calls_before_worker": 0,
        "worker_calls": 1,
        "research_calls": 1,
        "research_failed": 1,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
        "post_failed_research_read_calls": 0,
    }
    registry = ToolRegistry()
    registry.register_many(
        _coordinator_read_only_tools(
            _base_tools(),
            budget_state=budget_state,
            swebench_profile=True,
            read_budget_after_failed_research=2,
        )
    )

    first = registry.execute("search_repo", {"query": "target"})
    second = registry.execute("read_file", {"path": "app.py"})
    blocked = registry.execute("search_repo", {"query": "keep digging"})

    assert first.status == "success"
    assert second.status == "success"
    assert blocked.status == "error"
    assert blocked.error["code"] == "COORDINATOR_TRANSITION_REQUIRED"
    assert budget_state["post_failed_research_read_calls"] == 2


def test_coordinator_allows_only_small_read_budget_after_research_before_implement(tmp_path):
    budget_state = {
        "read_calls_before_worker": 3,
        "worker_calls": 1,
        "research_calls": 1,
        "research_failed": 0,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }
    registry = ToolRegistry()
    registry.register_many(
        _coordinator_read_only_tools(
            _base_tools(),
            budget_state=budget_state,
            read_budget_after_research_before_implement=2,
        )
    )

    assert registry.execute("search_repo", {"query": "one"}).status == "success"
    assert registry.execute("read_file", {"path": "app.py"}).status == "success"
    blocked = registry.execute("search_repo", {"query": "three"})

    assert blocked.status == "error"
    assert blocked.error["code"] == "COORDINATOR_TRANSITION_REQUIRED"


def test_budgeted_agent_tools_force_implement_after_failed_research(tmp_path):
    budget_state = {
        "read_calls_before_worker": 8,
        "worker_calls": 0,
        "research_calls": 0,
        "research_failed": 0,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }
    raw_agent = build_tool(
        name="agent",
        description="Agent",
        parameters=[
            ToolParameter("type", "string", "Type", required=False),
            ToolParameter("description", "string", "Description"),
            ToolParameter("prompt", "string", "Prompt"),
        ],
        handler=lambda inp: ToolResponse.partial(
            "research stopped",
            {"type": "research", "completed": False, "stop_reason": "max_steps"},
        ),
    )
    [agent_tool] = _budgeted_agent_tools([raw_agent], budget_state=budget_state)

    first = agent_tool.run_with_timing({"type": "research", "description": "r", "prompt": "p"})
    second = agent_tool.run_with_timing({"type": "research", "description": "r2", "prompt": "p"})

    assert first.status == "partial"
    assert budget_state["research_failed"] == 1
    assert second.status == "error"
    assert second.error["code"] == "COORDINATOR_RESEARCH_FAILED_IMPLEMENT_REQUIRED"


def test_swebench_failed_research_allows_reproducer_and_blocks_more_research(tmp_path):
    budget_state = {
        "read_calls_before_worker": 8,
        "worker_calls": 1,
        "research_calls": 1,
        "research_failed": 1,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }

    def handler(inp):
        agent_type = inp.get("type") or "general"
        return ToolResponse.success(
            f"{agent_type} ok",
            {"type": agent_type, "completed": True, "stop_reason": "finished"},
        )

    raw_agent = build_tool(
        name="agent",
        description="Agent",
        parameters=[
            ToolParameter("type", "string", "Type", required=False),
            ToolParameter("description", "string", "Description"),
            ToolParameter("prompt", "string", "Prompt"),
        ],
        handler=handler,
    )
    [agent_tool] = _budgeted_agent_tools([raw_agent], budget_state=budget_state, swebench_profile=True)

    reproduced = agent_tool.run_with_timing(
        {"type": "reproducer", "description": "reproduce", "prompt": "p"}
    )
    blocked_research = agent_tool.run_with_timing(
        {"type": "research", "description": "research again", "prompt": "p"}
    )

    assert reproduced.status == "success"
    assert blocked_research.status == "error"
    assert blocked_research.error["code"] == "COORDINATOR_TRANSITION_REQUIRED"
    assert budget_state["required_transition"] == "implement"


def test_budgeted_agent_tools_allow_patch_review_after_failed_research_and_implement(tmp_path):
    budget_state = {
        "read_calls_before_worker": 8,
        "worker_calls": 1,
        "research_calls": 1,
        "research_failed": 1,
        "post_research_read_calls": 0,
        "implement_calls": 0,
        "patch_review_calls": 0,
    }

    def handler(inp):
        agent_type = inp.get("type") or "general"
        return ToolResponse.success(
            f"{agent_type} ok",
            {"type": agent_type, "completed": True, "stop_reason": "finished"},
        )

    raw_agent = build_tool(
        name="agent",
        description="Agent",
        parameters=[
            ToolParameter("type", "string", "Type", required=False),
            ToolParameter("description", "string", "Description"),
            ToolParameter("prompt", "string", "Prompt"),
        ],
        handler=handler,
    )
    [agent_tool] = _budgeted_agent_tools([raw_agent], budget_state=budget_state)

    review_before_implement = agent_tool.run_with_timing(
        {"type": "patch_review", "description": "review", "prompt": "p"}
    )
    verify_before_review = agent_tool.run_with_timing(
        {"type": "verify", "description": "verify", "prompt": "p"}
    )
    implement = agent_tool.run_with_timing({"type": "implement", "description": "impl", "prompt": "p"})
    patch_review = agent_tool.run_with_timing(
        {"type": "patch_review", "description": "review", "prompt": "p"}
    )
    verify = agent_tool.run_with_timing({"type": "verify", "description": "verify", "prompt": "p"})

    assert review_before_implement.status == "error"
    assert verify_before_review.status == "error"
    assert implement.status == "success"
    assert patch_review.status == "success"
    assert verify.status == "success"
    assert budget_state["implement_calls"] == 1
    assert budget_state["patch_review_calls"] == 1


def test_coordinator_injects_recalled_repair_recipe(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows roundtrip",
        description="header_rows table format must update writer reader roundtrip",
        type="repair_recipe",
        content="verification_contract: write, read, and round-trip header rows.",
    )
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="done",
                tool_calls=[],
                raw_message={"content": "done"},
                usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            )
        ]
    )
    engine = CoordinatorEngine(
        llm=llm,
        worker_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
        memory_store=store,
    )

    assert engine.run("fix header_rows table output") == "done"

    user_content = llm.messages_by_call[0][1]["content"]
    assert "Recalled Repair Recipes" in user_content
    assert "round-trip header rows" in user_content
    assert engine.last_recalled_repair_recipes == ("repair_recipe_header_rows_roundtrip.md",)


def test_unmatched_repair_recipe_is_not_injected(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows roundtrip",
        description="header_rows table format must update writer reader roundtrip",
        type="repair_recipe",
        content="verification_contract: write, read, and round-trip header rows.",
    )

    recall = augment_task_with_repair_recipes(
        task="Django SQLCompiler should deduplicate ordering expressions.",
        memory_store=store,
    )

    assert recall.memories == ()
    assert "Recalled Repair Recipes" not in recall.task
    assert not any(result.applicable for result in recall.applicability)


def test_coordinator_can_freeze_repair_memory_recall(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows roundtrip",
        description="header_rows table format must update writer reader roundtrip",
        type="repair_recipe",
        content="verification_contract: write, read, and round-trip header rows.",
    )
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="done",
                tool_calls=[],
                raw_message={"content": "done"},
                usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            )
        ]
    )
    engine = CoordinatorEngine(
        llm=llm,
        worker_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
        memory_store=store,
        repair_memory_recall=False,
    )

    assert engine.run("fix header_rows table output") == "done"

    user_messages = [message["content"] for message in llm.messages_by_call[0] if message["role"] == "user"]
    rendered_user_messages = "\n".join(user_messages)
    assert "## Recalled Repair Recipes" not in rendered_user_messages
    assert "round-trip header rows" not in rendered_user_messages
    assert engine.last_recalled_repair_recipes == ()


def test_implement_worker_requires_structured_scope(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {"type": "implement", "description": "改代码", "prompt": "修改 src/app.py"},
    )

    assert response.status == "error"
    assert response.error["code"] == "IMPLEMENT_SCOPE_REQUIRED"


def test_swebench_implement_scope_denies_test_files(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix Django bug",
            "prompt": _swebench_edit_brief_prompt("Update source and tests."),
            "allowed_files": ["django/core/checks/model_checks.py", "tests/check_framework/test_model_checks.py"],
            "allowed_ranges": {
                "django/core/checks/model_checks.py": [[35, 60]],
                "tests/check_framework/test_model_checks.py": [[40, 80]],
            },
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "SWEBENCH_TEST_EDIT_DENIED"
    assert "tests/check_framework/test_model_checks.py" in response.text


def test_swebench_implement_does_not_require_prompt_keyword_score(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM(
            [
                LLMToolResponse(
                    content="No safe source edit was needed.",
                    tool_calls=[],
                    raw_message={"content": "No safe source edit was needed."},
                    usage={},
                )
            ]
        ),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
    )
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix SWE-bench bug",
            "prompt": "You are solving a SWE-bench instance. Change django/core/checks/model_checks.py.",
            "allowed_files": ["django/core/checks/model_checks.py"],
            "allowed_ranges": {"django/core/checks/model_checks.py": [[35, 60]]},
        },
    )

    assert response.status == "success"
    assert response.error is None


def test_swebench_implement_denies_research_task_prompt(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Investigate SWE-bench bug",
            "prompt": _swebench_edit_brief_prompt("Run `git log --oneline -30` and search for permission."),
            "allowed_files": ["django/contrib/auth/management/__init__.py"],
            "allowed_ranges": {"django/contrib/auth/management/__init__.py": [[35, 120]]},
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "IMPLEMENT_RESEARCH_TASK_DENIED"


def test_swebench_implement_allows_local_pattern_lookup_inside_allowed_ranges():
    prompt = _swebench_edit_brief_prompt(
        "Within allowed_ranges only, grep/search the exact string `backend` in the provided lines, "
        "then replace the local guard."
    )

    assert _research_like_implement_terms(prompt) == []
    assert _research_like_implement_terms(_swebench_edit_brief_prompt("Search the repository for backend usage.")) == [
        "search/grep"
    ]


def test_swebench_implement_receives_reproducer_contract_and_semantic_proof():
    prompt = _augment_swebench_semantic_prompt(
        "Apply the scoped edit.",
        {
            "semantic_evidence": {
                "reproduction_contract": {
                    "trigger": ["nullable one-dimensional and two-dimensional containers"],
                    "expected_behavior": ["preserve multioutput classification"],
                },
                "target_partitions": [{"id": "nullable", "expected": "classified"}],
            },
            "semantic_evidence_issues": [],
        },
    )

    assert "nullable one-dimensional and two-dimensional containers" in prompt
    assert "semantic_challenge" in prompt
    assert "assumptions_checked" in prompt
    assert "first_semantic_decision" in prompt
    assert "non_target_case" in prompt
    assert "not an authority" in prompt
    assert "first predicate" in prompt
    assert "current producer canonical identity" in prompt


def test_swebench_implement_gate_no_longer_depends_on_container_keywords():
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sklearn/utils/multiclass.py"],
            "allowed_ranges": {"sklearn/utils/multiclass.py": [[140, 370]]},
        }
    )
    incomplete = _swebench_edit_brief_prompt(
        "Convert nullable pandas Series and DataFrame values before classification."
    )

    assert _check_swebench_implement_edit_brief(incomplete, constraints) is None


def test_swebench_implement_gate_no_longer_depends_on_contract_keywords():
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sphinx/ext/autodoc/typehints.py"],
            "allowed_ranges": {"sphinx/ext/autodoc/typehints.py": [[15, 34], [60, 150]]},
        }
    )
    incomplete = _swebench_edit_brief_prompt(
        "Change annotation mapping keys for *args/**kwargs so consumer lookup and rendering match."
    )

    assert _check_swebench_implement_edit_brief(incomplete, constraints) is None


def test_swebench_scope_gate_does_not_depend_on_repository_name():
    constraints = WorkerConstraints.from_tool_input(
        {
            "allowed_files": ["sphinx/ext/napoleon/docstring.py"],
            "allowed_ranges": {"sphinx/ext/napoleon/docstring.py": [[120, 180]]},
        }
    )

    denial = _check_swebench_implement_edit_brief(
        _swebench_edit_brief_prompt("Change escaping behavior for attribute names."),
        constraints,
    )

    assert denial is None


def test_swebench_implement_denies_overbroad_ranges(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix ORM SWE-bench bug",
            "prompt": _swebench_edit_brief_prompt("Apply required_changes in the narrowed edit point."),
            "allowed_files": ["django/db/models/sql/query.py"],
            "allowed_ranges": {"django/db/models/sql/query.py": [[1, 2500]]},
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "IMPLEMENT_SCOPE_TOO_BROAD"


def test_recipe_constraint_blocks_writer_only_implement_scope(tmp_path):
    _write_rst_fixture(tmp_path)
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            repair_recipe_constraints=lambda: (
                RepairRecipeConstraint(
                    source="repair_recipe_header_rows_table_roundtrip.md",
                    name="header rows table roundtrip",
                    description="header_rows table format must cover writer reader roundtrip",
                    requires_reader_writer_roundtrip=True,
                ),
            ),
        )
    )

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix RST reader writer roundtrip",
            "prompt": "Fix header_rows reader and writer behavior and include roundtrip verification contract.",
            "allowed_files": ["astropy/io/ascii/rst.py"],
            "allowed_ranges": {"astropy/io/ascii/rst.py": [[60, 66]]},
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "RECIPE_EDIT_BRIEF_SCOPE_DENIED"
    assert "reader/Header/Data ranges" in response.text


def test_recipe_constraint_requires_prompt_to_mention_roundtrip_contract(tmp_path):
    _write_rst_fixture(tmp_path)
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            repair_recipe_constraints=lambda: (
                RepairRecipeConstraint(
                    source="repair_recipe_header_rows_table_roundtrip.md",
                    name="header rows table roundtrip",
                    description="header_rows table format must cover writer reader roundtrip",
                    requires_reader_writer_roundtrip=True,
                ),
            ),
        )
    )

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix RST writer",
            "prompt": "Fix header_rows writer separator placement.",
            "allowed_files": ["astropy/io/ascii/rst.py"],
            "allowed_ranges": {"astropy/io/ascii/rst.py": [[16, 67]]},
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "RECIPE_EDIT_BRIEF_REQUIRED"
    assert "reader/read" in response.text
    assert "roundtrip" in response.text


def test_recipe_constraint_allows_reader_writer_roundtrip_scope(tmp_path):
    _write_rst_fixture(tmp_path)
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="范围满足 recipe，实施完成。",
                tool_calls=[],
                raw_message={"content": "done"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            )
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            repair_recipe_constraints=lambda: (
                RepairRecipeConstraint(
                    source="repair_recipe_header_rows_table_roundtrip.md",
                    name="header rows table roundtrip",
                    description="header_rows table format must cover writer reader roundtrip",
                    requires_reader_writer_roundtrip=True,
                ),
            ),
        )
    )

    response = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix RST reader writer roundtrip",
            "prompt": "Fix header_rows reader start lines, writer separator placement, and roundtrip verification contract.",
            "allowed_files": ["astropy/io/ascii/rst.py"],
            "allowed_ranges": {"astropy/io/ascii/rst.py": [[16, 67]]},
        },
    )

    assert response.status == "success"
    assert "实施完成" in response.text


def test_hard_constraints_detect_reader_writer_roundtrip_recipe(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows table roundtrip",
        description="header_rows table format must cover writer reader roundtrip",
        type="repair_recipe",
        content="edit_brief_rules: allowed_ranges must cover reader Header/Data and writer class.\nverification_contract: roundtrip.",
    )
    memories = tuple(store.recall_repair_recipes("header_rows writer reader roundtrip"))

    constraints = hard_constraints_from_repair_recipes(
        memories,
        task="Fix astropy ascii.rst header_rows writer reader roundtrip behavior.",
    )

    assert constraints
    assert constraints[0].requires_reader_writer_roundtrip is True


def test_recipe_applicability_keeps_unmatched_recipe_soft(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows table roundtrip",
        description="header_rows table format must cover writer reader roundtrip",
        type="repair_recipe",
        content="edit_brief_rules: allowed_ranges must cover reader Header/Data and writer class.\nverification_contract: roundtrip.",
    )
    memories = tuple(store.recall_repair_recipes("header_rows writer reader roundtrip"))

    applicability = evaluate_repair_recipe_applicability(
        "Django SQLCompiler.get_order_by() should avoid duplicate order_by columns.",
        memories,
    )
    constraints = hard_constraints_from_repair_recipes(
        memories,
        task="Django SQLCompiler.get_order_by() should avoid duplicate order_by columns.",
    )

    assert applicability
    assert applicability[0].applicable is False
    assert constraints == ()


def test_recipe_applicability_ignores_general_task_instructions(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="header rows table roundtrip",
        description="header_rows table format must cover writer reader roundtrip",
        type="repair_recipe",
        content="edit_brief_rules: allowed_ranges must cover reader Header/Data and writer class.\nverification_contract: roundtrip.",
    )
    task = """You are solving a SWE-bench instance.

## Problem Statement
Django SQLCompiler.get_order_by() should avoid duplicate ordering expressions.

## Instructions
- If the issue is specifically about table `header_rows`, patch writer separator placement and reader header/data start handling.
"""
    memories = tuple(store.recall_repair_recipes("header_rows writer reader roundtrip"))

    query = repair_recipe_query_from_task(task)
    applicability = evaluate_repair_recipe_applicability(task, memories)
    constraints = hard_constraints_from_repair_recipes(memories, task=task)

    assert "header_rows" not in query
    assert applicability[0].applicable is False
    assert constraints == ()


def test_agent_tool_does_not_block_on_bare_ready_to_implement_text(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="关键事实已确认。\nready_to_implement: true",
                tool_calls=[],
                raw_message={"content": "ready"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="第二次研究完成。",
                tool_calls=[],
                raw_message={"content": "second"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    first = registry.execute(
        "agent",
        {"type": "research", "description": "定位代码", "prompt": "读取 app.py"},
    )
    second = registry.execute(
        "agent",
        {"type": "research", "description": "重复研究", "prompt": "再次读取测试"},
    )

    assert first.status == "success"
    assert second.status == "success"


def test_agent_tool_blocks_research_after_structured_edit_brief(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=(
                    "关键事实已确认。\n"
                    "```json\n"
                    "{\"edit_brief\":{\"allowed_files\":[\"app.py\"],"
                    "\"allowed_ranges\":{\"app.py\":[[1,2]]},"
                    "\"required_changes\":[\"改返回值\"],"
                    "\"forbidden\":[\"不要改测试\"]}}\n"
                    "```"
                ),
                tool_calls=[],
                raw_message={"content": "ready"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            )
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    first = registry.execute(
        "agent",
        {"type": "research", "description": "定位代码", "prompt": "读取 app.py"},
    )
    second = registry.execute(
        "agent",
        {"type": "research", "description": "重复研究", "prompt": "再次读取测试"},
    )

    assert first.status == "success"
    assert second.status == "error"
    assert second.error["code"] == "IMPLEMENT_REQUIRED"


def test_agent_tool_blocks_general_after_structured_edit_brief(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=(
                    "关键事实已确认。\n"
                    "```json\n"
                    "{\"edit_brief\":{\"allowed_files\":[\"app.py\"],"
                    "\"allowed_ranges\":{\"app.py\":[[1,2]]},"
                    "\"required_changes\":[\"改返回值\"],"
                    "\"forbidden\":[\"不要改测试\"]}}\n"
                    "```"
                ),
                tool_calls=[],
                raw_message={"content": "ready"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            )
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    first = registry.execute(
        "agent",
        {"type": "research", "description": "定位代码", "prompt": "读取 app.py"},
    )
    second = registry.execute(
        "agent",
        {"type": "general", "description": "绕路读取", "prompt": "读取 research 输出"},
    )

    assert first.status == "success"
    assert second.status == "error"
    assert second.error["code"] == "IMPLEMENT_REQUIRED"


def test_implement_without_changed_files_does_not_count_as_done(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=(
                    "```json\n"
                    "{\"edit_brief\":{\"allowed_files\":[\"app.py\"],"
                    "\"allowed_ranges\":{\"app.py\":[[1,2]]},"
                    "\"required_changes\":[\"改返回值\"],"
                    "\"forbidden\":[\"不要改测试\"]}}\n"
                    "```"
                ),
                tool_calls=[],
                raw_message={"content": "ready"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="没有实际修改。",
                tool_calls=[],
                raw_message={"content": "no changes"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="重新研究完成。",
                tool_calls=[],
                raw_message={"content": "research again"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    first = registry.execute("agent", {"type": "research", "description": "定位代码", "prompt": "读取 app.py"})
    second = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "改代码",
            "prompt": "修改 app.py",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 2]]},
        },
    )
    third = registry.execute("agent", {"type": "research", "description": "重新定位", "prompt": "读取 app.py"})

    assert first.status == "success"
    assert second.status == "success"
    assert second.data["changed_files"] == []
    assert third.status == "success"


def test_last_chance_only_triggers_for_patch_application_failure(tmp_path):
    llm = FakeLLM(
        [
            LLMToolResponse(
                content="replace_in_file old text was not found inside allowed_ranges; use line_replacements for the same minimal replacement.",
                tool_calls=[],
                raw_message={"content": "no patch"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
            LLMToolResponse(
                content="line_replacements still could not apply; no changed files.",
                tool_calls=[],
                raw_message={"content": "still no patch"},
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    first = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "改代码",
            "prompt": "修改 app.py",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 2]]},
        },
    )
    second = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "重试落地",
            "prompt": "修改 app.py",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 2]]},
        },
    )
    third_same_scope = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "同范围再次重试",
            "prompt": "修改 app.py",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 2]]},
        },
    )

    assert first.status == "success"
    assert second.status == "success"
    rendered_last_chance_messages = "\n".join(
        str(message.get("content") or "") for message in llm.messages_by_call[-1]
    )
    assert "LAST_CHANCE_IMPLEMENT" in rendered_last_chance_messages
    assert third_same_scope.status == "error"
    assert third_same_scope.error["code"] == "LAST_CHANCE_EXHAUSTED"


def _init_git_repo_with_app(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "RepoAgent Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "repoagent@example.test"], cwd=repo, check=True)
    (repo / "app.py").write_text("value = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    return repo


def _replace_response(call_id: str, old: str, new: str) -> LLMToolResponse:
    return LLMToolResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id=call_id,
                name="replace_in_file",
                arguments=json.dumps({"path": "app.py", "old": old, "new": new}),
            )
        ],
        raw_message={"tool_calls": [{"id": call_id}]},
        usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    )


def _swebench_edit_brief_prompt(extra: str = "") -> str:
    return (
        "You are solving a SWE-bench instance.\n"
        "problem_contract: inputs call the public failing behavior; expected behavior is fixed; forbidden behavior is test edits.\n"
        "semantic_cases: before fails, after succeeds with exact public behavior.\n"
        "old_behavior_invariants: preserve existing valid API behavior and output formatting.\n"
        "required_changes: apply the minimal source-code change in allowed_ranges.\n"
        "forbidden: do not modify tests, do not install dependencies.\n"
        f"{extra}"
    )


def test_patch_review_is_required_before_verify_after_implement(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(
                content='{"patch_risk":"low","findings":[]}',
                tool_calls=[],
                raw_message={"content": "reviewed"},
                usage={},
            ),
            LLMToolResponse(content="verified", tool_calls=[], raw_message={"content": "verified"}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    verify_before_review = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )
    reviewed = registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    )
    verified = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert implemented.status == "success"
    assert implemented.data["changed_files"] == ["app.py"]
    assert verify_before_review.status == "error"
    assert verify_before_review.error["code"] == "PATCH_REVIEW_REQUIRED"
    assert reviewed.status == "success"
    assert verified.status == "success"


def test_swebench_profile_disables_patch_review_gate_and_worker_type(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(content="verified", tool_calls=[], raw_message={"content": "verified"}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            allowed_agent_types={"research", "reproducer", "implement", "patch_ranker", "verify"},
        )
    )

    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    patch_review = registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    )
    verified = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert implemented.status == "success"
    assert patch_review.status == "error"
    assert patch_review.error["code"] == "WORKER_TYPE_DISABLED"
    assert verified.status == "success"


def test_swebench_semantic_gate_allows_one_narrow_corrective_implement(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    source = (
        "def record_typehints(param):\n"
        "    annotation = {}\n"
        "    annotation[param.name] = 'int'\n"
        "    return annotation\n\n"
        "def modify_field_list(name):\n"
        "    return name\n"
    )
    (repo / "app.py").write_text(source, encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "record typehints"], cwd=repo, check=True, stdout=subprocess.PIPE)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    risky = (
        "    if param.kind == param.VAR_POSITIONAL:\n"
        "        key = '*' + param.name\n"
        "    annotation[key] = 'int'"
    )
    corrected = "    return '*' + name"
    llm = FakeLLM(
        [
            _replace_response("replace_1", "    annotation[param.name] = 'int'", risky),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={}, usage={}),
            _replace_response("replace_2", risky, "    annotation[param.name] = 'int'"),
            _replace_response("replace_3", "    return name", corrected),
            LLMToolResponse(content="corrected", tool_calls=[], raw_message={}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"implement", "verify"},
            state=state,
        )
    )
    state["semantic_evidence"] = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "evidence": [{"type": "code", "location": "app.py:1"}],
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "app.py:6",
                        "lookup_identity": "plain name",
                        "output_identity": "matched display name",
                    }
                ],
            }
        ]
    }

    first = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix SWE-bench star identity",
            "prompt": _swebench_edit_brief_prompt("Preserve canonical storage identity."),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 12]]},
        },
    )
    second = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Correct semantic layer",
            "prompt": _swebench_edit_brief_prompt("Undo the producer key change."),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 12]]},
        },
    )
    third = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Try another correction",
            "prompt": _swebench_edit_brief_prompt("Change it again."),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 12]]},
        },
    )

    assert first.status == "success"
    assert "Automatic SemanticEvidence patch gate" in first.text
    assert second.status == "success"
    assert "CORRECTIVE_IMPLEMENT" in "\n".join(
        str(message.get("content") or "") for message in llm.messages_by_call[-1]
    )
    corrective_prompt = "\n".join(str(message.get("content") or "") for message in llm.messages_by_call[-1])
    assert "AUTHORITATIVE TOOL-ENFORCED OBLIGATIONS" in corrective_prompt
    assert "Restore the producer's canonical storage assignment" in corrective_prompt
    assert corrective_prompt.index("Coordinator corrective prompt") < corrective_prompt.index(
        "AUTHORITATIVE TOOL-ENFORCED OBLIGATIONS"
    )
    assert state["corrective_implement_count"] == 1
    current = (repo / "app.py").read_text(encoding="utf-8")
    assert "annotation[param.name] = 'int'" in current
    assert "annotation[key] = 'int'" not in current
    assert "return '*' + name" in current
    assert "VAR_POSITIONAL" not in current
    assert third.status == "error"
    assert third.error["code"] == "FINAL_PATCH_ALREADY_EXISTS"


def test_corrective_prompt_explains_nullable_missing_value_gate():
    prompt = _augment_corrective_prompt(
        "Correct the current nullable pandas patch.",
        {
            "implemented_files": ["sklearn/utils/multiclass.py"],
            "semantic_patch_issues": [
                "semantic_representation_gap:missing_values_fall_back_to_old_unknown_path"
            ],
            "last_patch_gate_text": "missing-value gate",
        },
    )

    assert "Remove first-element dtype inference" in prompt
    assert "silently `pass`es" in prompt
    assert "whole Series/DataFrame" in prompt


def test_corrective_prompt_rejects_first_element_container_inference():
    prompt = _augment_corrective_prompt(
        "Correct the current nullable pandas patch.",
        {
            "implemented_files": ["sklearn/utils/multiclass.py"],
            "semantic_patch_issues": [
                "semantic_representation_gap:first_element_used_as_container_dtype_proxy"
            ],
            "last_patch_gate_text": "first-element gate",
        },
    )

    assert "Remove every new `flat[0]`" in prompt
    assert "whole-container dtype metadata" in prompt


def test_corrective_prompt_restores_return_contract_after_parameter_alias_edit():
    prompt = _augment_corrective_prompt(
        "Correct the starred parameter alias patch.",
        {
            "implemented_files": ["sphinx/ext/autodoc/typehints.py"],
            "semantic_patch_issues": [
                "semantic_contract_violation:parameter_alias_patch_changed_return_rtype_branches"
            ],
            "last_patch_gate_text": "return contract gate",
        },
    )

    assert "Restore the original `type`, `return/returns`, and `rtype` parsing branches byte-for-byte" in prompt
    assert "Remove every new return-bookkeeping line from the parameter loop" in prompt


def test_invalid_semantic_evidence_hard_stops_before_implement(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=repo,
        output_dir=tmp_path / "agents",
        swebench_profile=True,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"implement"},
            state=state,
        )
    )
    state.update(
        {
            "problem_statement": "Fix nullable pandas inputs",
            "semantic_evidence": {},
            "semantic_evidence_issues": ["semantic_evidence JSON is missing"],
        }
    )

    result = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Implement an incomplete semantic plan",
            "prompt": _swebench_edit_brief_prompt("Handle nullable pandas inputs."),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )

    assert result.status == "partial"
    assert result.data["terminate_loop"] is True
    assert result.data["terminal_reason"] == "semantic_evidence_invalid"
    assert state["workflow_terminal_reason"] == "semantic_evidence_invalid"
    assert state["semantic_gate_denials"] == 1
    assert state["worker_attempts"].get("implement", 0) == 0


def test_swebench_malformed_reproducer_is_soft_evidence(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "agents",
        swebench_profile=True,
    )
    runner.run = lambda **kwargs: WorkerResult(
        type="reproducer",
        description="Malformed optional evidence",
        text="not valid SemanticEvidence JSON",
        output_dir=tmp_path / "agents" / "reproducer",
        completed=True,
        stop_reason="finished",
        max_steps=6,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
    )
    state = {"problem_statement": "Fix a public behavior bug"}
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner, state=state))

    result = registry.execute(
        "agent",
        {
            "type": "reproducer",
            "description": "Optional public reproduction",
            "prompt": "SWE-bench public issue reproduction.",
        },
    )

    assert result.status == "success"
    assert state["semantic_evidence_issues"] == []
    assert state["semantic_evidence_warnings"] == ["semantic_evidence JSON is missing"]


def test_completion_guard_allows_static_patch_when_no_trustworthy_verify_plan():
    guard = _coordinator_completion_guard(
        "Implemented a statically safe patch; focused verification is unavailable.",
        budget_state={},
        agent_state={
            "implementation_done": True,
            "review_required": False,
            "verify_attempted": False,
            "semantic_evidence": {
                "target_partitions": [{"id": "public_case", "expected": "fixed"}],
                "verify_commands": [],
            },
            "submission_ready": False,
        },
        swebench_profile=True,
    )

    assert guard is None


def test_completion_guard_still_requires_available_focused_verification():
    guard = _coordinator_completion_guard(
        "Implemented the patch.",
        budget_state={},
        agent_state={
            "implementation_done": True,
            "review_required": False,
            "verify_attempted": False,
            "semantic_evidence": {
                "target_partitions": [{"id": "public_case", "expected": "fixed"}],
                "verify_commands": [
                    {"command": 'python -c "assert True"', "covers": ["public_case"]}
                ],
            },
            "submission_ready": False,
        },
        swebench_profile=True,
    )

    assert guard is not None
    assert "agent(type=verify)" in guard


def test_simplified_swebench_pipeline_requires_reproducer_and_skips_low_risk_review(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            LLMToolResponse(
                content='{"reproduction_contract":{"trigger":"old"},"verify_commands":["python3 -c \\"assert True\\""]}',
                tool_calls=[],
                raw_message={},
                usage={},
            ),
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(
                content='{"status":"passed","command":"python3 -c \\"assert True\\"","exit_code":0,"evidence":"ok"}',
                tool_calls=[],
                raw_message={},
                usage={},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            require_reproducer_before_implement=True,
            allowed_agent_types={"reproducer", "implement", "patch_review", "verify"},
            state=state,
        )
    )

    denied = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": _swebench_edit_brief_prompt(),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    reproduced = registry.execute(
        "agent", {"type": "reproducer", "description": "Reproduce", "prompt": "SWE-bench public contract for app.py"}
    )
    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": _swebench_edit_brief_prompt(),
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    review = registry.execute(
        "agent", {"type": "patch_review", "description": "Review", "prompt": "Review public behavior"}
    )
    verified = registry.execute(
        "agent", {"type": "verify", "description": "Verify", "prompt": "python3 -c \"assert True\""}
    )

    assert denied.error["code"] == "REPRODUCER_REQUIRED"
    assert reproduced.status == "success"
    assert implemented.status == "success"
    assert state["review_required"] is False
    assert review.error["code"] == "PATCH_REVIEW_NOT_REQUIRED"
    assert verified.status == "success"


def test_conditional_patch_review_detects_public_signature_change(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    (repo / "app.py").write_text("def public(value):\n    return value\n", encoding="utf-8")

    reasons = _conditional_patch_review_reasons(repo, ("app.py",))

    assert "public_signature_or_type_change" in reasons


def test_candidate_mode_auto_uses_only_problem_statement_uncertainty():
    task = (
        "You are solving a SWE-bench instance.\n\n"
        "## Problem Statement\nFix a simple local typo in one function.\n\n"
        "## Instructions\n"
        "Generic advice mentions case-insensitive parsing, expected output, actual output, messages, and roundtrip."
    )
    high_uncertainty_task = (
        "## Problem Statement\n"
        "The Sphinx parser should preserve exact warning message output for multiple exception cases.\n\n"
        "## Instructions\nGeneric instructions."
    )

    assert _candidate_mode_required_for_task(task, "auto") is False
    assert _candidate_mode_required_for_task(high_uncertainty_task, "auto") is True
    assert _candidate_mode_required_for_task(task, "required") is True
    assert _candidate_mode_required_for_task(high_uncertainty_task, "off") is False


def test_swebench_auto_exposes_candidate_pool_tools_for_runtime_state_machine(tmp_path):
    auto_llm = FakeLLM(
        [
            LLMToolResponse(
                content="done",
                tool_calls=[],
                raw_message={"content": "done"},
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        ]
    )
    auto_engine = CoordinatorEngine(
        llm=auto_llm,
        worker_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "auto",
        swebench_profile=True,
        candidate_mode="auto",
        repair_memory_recall=False,
    )

    assert auto_engine.run("Fix a SWE-bench bug") == "done"
    assert "patch_candidate" in auto_llm.tool_names_by_call[0]
    assert set(auto_llm.tool_names_by_call[0]) == {"agent", "patch_candidate", "read_file", "search_repo", "git_diff"}

    required_llm = FakeLLM(
        [
            LLMToolResponse(
                content="done",
                tool_calls=[],
                raw_message={"content": "done"},
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        ]
    )
    required_engine = CoordinatorEngine(
        llm=required_llm,
        worker_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "required",
        swebench_profile=True,
        candidate_mode="required",
        repair_memory_recall=False,
    )

    assert required_engine.run("Fix a SWE-bench bug") == "done"
    assert "patch_candidate" in required_llm.tool_names_by_call[0]


def test_candidate_mode_required_blocks_implement_until_reproducer(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            LLMToolResponse(
                content='{"reproduction_contract":{"trigger":["public bug"],"expected_behavior":["candidate_one"]}}',
                tool_calls=[],
                raw_message={},
                usage={},
            ),
            _replace_response("replace_1", "old", "candidate_one"),
            LLMToolResponse(content="candidate one implemented", tool_calls=[], raw_message={}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            allowed_agent_types={"research", "reproducer", "implement", "patch_ranker", "verify"},
            candidate_mode_required=lambda: True,
        )
    )

    blocked = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate one",
            "prompt": "Change app.py from old to candidate_one.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    reproduced = registry.execute(
        "agent",
        {
            "type": "reproducer",
            "description": "Build public contract",
            "prompt": "Use public facts to define the reproduction contract.",
        },
    )
    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate one",
            "prompt": "Change app.py from old to candidate_one.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )

    assert blocked.status == "error"
    assert blocked.error["code"] == "CANDIDATE_MODE_REPRODUCER_REQUIRED"
    assert reproduced.status == "success"
    assert implemented.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'candidate_one'\n"


def test_candidate_mode_accepts_one_partial_reproducer_attempt(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    results = iter(
        [
            WorkerResult(
                type="reproducer",
                description="Public reproducer",
                text="达到最大步数，任务未明确完成。",
                output_dir=tmp_path / "agents" / "reproducer",
                completed=False,
                stop_reason="max_steps",
                max_steps=8,
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
            ),
            WorkerResult(
                type="implement",
                description="Candidate one",
                text="No patch in this state-machine test.",
                output_dir=tmp_path / "agents" / "implement",
                completed=True,
                stop_reason="finished",
                max_steps=8,
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
            ),
        ]
    )
    runner.run = lambda **kwargs: next(results)
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            allowed_agent_types={"reproducer", "implement"},
            candidate_mode_required=lambda: True,
        )
    )

    reproduced = registry.execute(
        "agent",
        {"type": "reproducer", "description": "Public reproducer", "prompt": "Build a public contract."},
    )
    repeated = registry.execute(
        "agent",
        {"type": "reproducer", "description": "Repeat reproducer", "prompt": "Try again."},
    )
    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate one",
            "prompt": "Change app.py.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )

    assert reproduced.status == "partial"
    assert repeated.status == "error"
    assert repeated.error["code"] == "REPRODUCER_ALREADY_ATTEMPTED"
    assert implemented.error is None or implemented.error["code"] != "CANDIDATE_MODE_REPRODUCER_REQUIRED"


def test_swebench_slim_profile_allows_only_one_verify_worker(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    runner.run = lambda **kwargs: WorkerResult(
        type="verify",
        description="Verify once",
        text="blocked_by_environment",
        output_dir=tmp_path / "agents" / "verify",
        completed=False,
        stop_reason="max_steps",
        max_steps=3,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            allowed_agent_types={"verify"},
        )
    )

    first = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify once", "prompt": "Run one public check."},
    )
    repeated = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify twice", "prompt": "Try another check."},
    )

    assert first.status == "partial"
    assert repeated.status == "error"
    assert repeated.error["code"] == "VERIFY_ALREADY_ATTEMPTED"


def test_conditional_review_partial_result_terminates_without_retry(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    runner.run = lambda **kwargs: WorkerResult(
        type="patch_review",
        description="Review once",
        text="review reached max_steps",
        output_dir=tmp_path / "agents" / "patch_review",
        completed=False,
        stop_reason="max_steps",
        max_steps=3,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"patch_review", "verify"},
            state=state,
        )
    )
    state.update({"implementation_done": True, "review_required": True, "implemented_files": ("app.py",)})

    first = registry.execute(
        "agent", {"type": "patch_review", "description": "Review once", "prompt": "Review current public risk."}
    )
    repeated = registry.execute(
        "agent", {"type": "patch_review", "description": "Review twice", "prompt": "Retry review."}
    )

    assert first.status == "partial"
    assert first.data["terminate_loop"] is True
    assert first.data["terminal_completed"] is False
    assert first.data["terminal_reason"] == "conditional_review_incomplete"
    assert repeated.error["code"] == "WORKFLOW_ALREADY_TERMINAL"
    assert state["worker_attempts"]["patch_review"] == 1
    assert state["worker_requests"]["patch_review"] == 2
    assert state["patch_review_completed"] is False


def test_conditional_verify_attempt_terminates_even_when_worker_is_partial(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    runner.run = lambda **kwargs: WorkerResult(
        type="verify",
        description="Verify once",
        text="blocked_by_environment",
        output_dir=tmp_path / "agents" / "verify",
        completed=False,
        stop_reason="max_steps",
        max_steps=3,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"verify"},
            state=state,
        )
    )

    result = registry.execute(
        "agent", {"type": "verify", "description": "Verify once", "prompt": "Run one public check."}
    )

    assert result.status == "partial"
    assert result.data["terminate_loop"] is True
    assert result.data["terminal_reason"] == "focused_verification_incomplete"
    assert result.data["terminal_completed"] is False
    assert state["worker_attempts"]["verify"] == 1
    assert state["verify_completed"] is False


def test_conditional_verify_requires_behavioral_passing_evidence(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    runner.run = lambda **kwargs: WorkerResult(
        type="verify",
        description="Verify once",
        text='{"status":"passed","command":"python -m pytest tests/test_app.py -q","exit_code":0,"evidence":"1 passed"}',
        output_dir=tmp_path / "agents" / "verify",
        completed=True,
        stop_reason="finished",
        max_steps=3,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"verify"},
            state=state,
        )
    )
    state.update({"implementation_done": True, "static_issues": []})

    result = registry.execute(
        "agent", {"type": "verify", "description": "Verify once", "prompt": "Run one public check."}
    )

    assert result.status == "success"
    assert result.data["terminal_reason"] == "focused_verification_passed"
    assert result.data["terminal_completed"] is True
    assert state["verify_completed"] is True
    assert state["verification_passed"] is True
    assert state["submission_ready"] is True


def test_verification_plan_coverage_comes_from_actual_execution():
    plan = {
        "command": 'python -c "assert True"',
        "covers": ["series", "dataframe"],
    }
    reported = {
        "status": "passed",
        "command": plan["command"],
        "exit_code": 0,
        "covered_cases": ["invented_case"],
        "evidence": "model report",
    }
    execution = {
        "attempted": True,
        "requested_command": plan["command"],
        "exit_code": 0,
        "output": "exit_code=0",
    }

    bound, issues = _bind_verification_to_plan(reported, plan, execution)

    assert issues == []
    assert bound["covered_cases"] == ["series", "dataframe"]
    assert bound["evidence"] == "exit_code=0"


def test_verification_plan_rejects_unexecuted_model_report():
    plan = {"command": 'python -c "assert True"', "covers": ["behavior"]}

    bound, issues = _bind_verification_to_plan(
        {"status": "passed", "command": plan["command"], "exit_code": 0},
        plan,
        {},
    )

    assert bound == {}
    assert issues == ["validated SemanticEvidence command was not executed"]


def test_invalid_semantic_evidence_verify_request_terminates_workflow(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "agents",
        swebench_profile=True,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"verify"},
            state=state,
        )
    )
    state.update(
        {
            "implementation_done": True,
            "semantic_evidence": {},
            "semantic_evidence_issues": ["semantic_evidence JSON is missing"],
        }
    )

    result = registry.execute(
        "agent",
        {
            "type": "verify",
            "description": "Verify SWE-bench patch",
            "prompt": "SWE-bench focused verification for the current patch.",
        },
    )

    assert result.status == "partial"
    assert result.data["terminate_loop"] is True
    assert result.data["terminal_reason"] == "focused_verification_unavailable"
    assert result.data["terminal_completed"] is False
    assert state["workflow_terminal_reason"] == "focused_verification_unavailable"
    assert state["verification_assessment"]["preserve_patch"] is True
    assert state["worker_attempts"].get("verify", 0) == 0


def test_conditional_verify_marks_pycompile_as_inconclusive(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "agents")
    runner.run = lambda **kwargs: WorkerResult(
        type="verify",
        description="Verify once",
        text='{"status":"passed","command":"python -m py_compile app.py","exit_code":0,"evidence":"ok"}',
        output_dir=tmp_path / "agents" / "verify",
        completed=True,
        stop_reason="finished",
        max_steps=3,
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
    )
    state = {}
    registry = ToolRegistry()
    registry.register_many(
        build_agent_tools(
            runner,
            require_patch_review=False,
            conditional_patch_review=True,
            allowed_agent_types={"verify"},
            state=state,
        )
    )
    state.update({"implementation_done": True, "static_issues": []})

    result = registry.execute(
        "agent", {"type": "verify", "description": "Verify once", "prompt": "Run one public check."}
    )

    assert result.status == "partial"
    assert result.data["terminal_reason"] == "focused_verification_inconclusive"
    assert state["verification_passed"] is False
    assert state["verification_assessment"]["classification"] == "static_passed"
    assert state["submission_ready"] is False


def test_patch_candidate_pool_requires_reproducer_before_save(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "candidate_one"),
            LLMToolResponse(content="candidate one implemented", tool_calls=[], raw_message={}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate one",
            "prompt": "Change app.py from old to candidate_one.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    saved_without_reproducer = registry.execute(
        "patch_candidate",
        {"action": "save_current", "name": "candidate-one", "description": "first strategy"},
    )

    assert implemented.status == "success"
    assert saved_without_reproducer.status == "error"
    assert saved_without_reproducer.error["code"] == "CANDIDATE_REPRODUCER_REQUIRED"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'candidate_one'\n"


def test_patch_candidate_pool_saves_resets_and_applies_selected_candidate(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            LLMToolResponse(
                content=json.dumps(
                    {
                        "reproduction_contract": {
                            "trigger": ["public bug"],
                            "current_behavior": ["old"],
                            "expected_behavior": ["candidate_two"],
                        },
                        "differential_cases": [],
                        "verify_commands": [
                            "python3 -c \"from pathlib import Path; print(Path('app.py').read_text().strip())\""
                        ],
                    }
                ),
                tool_calls=[],
                raw_message={},
                usage={},
            ),
            _replace_response("replace_1", "old", "candidate_one"),
            LLMToolResponse(content="candidate one implemented", tool_calls=[], raw_message={}, usage={}),
            _replace_response("replace_2", "old", "candidate_two"),
            LLMToolResponse(content="candidate two implemented", tool_calls=[], raw_message={}, usage={}),
            LLMToolResponse(
                content='{"selected_index":2,"confidence":"high","ranking":[],"selected_reason":"better","required_changes_if_none":[]}',
                tool_calls=[],
                raw_message={},
                usage={},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    reproduced = registry.execute(
        "agent",
        {
            "type": "reproducer",
            "description": "Build public contract",
            "prompt": "Use only public problem facts to define the reproduction and differential contract.",
        },
    )
    first = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate one",
            "prompt": "Change app.py from old to candidate_one.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    saved_first = registry.execute(
        "patch_candidate",
        {
            "action": "save_current",
            "name": "candidate-one",
            "description": "first strategy",
            "semantic_layer": "producer_contract",
        },
    )
    assert first.status == "success"
    assert saved_first.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'old'\n"

    apply_too_early = registry.execute("patch_candidate", {"action": "apply", "index": 1})

    second = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Candidate two",
            "prompt": "Change app.py from old to candidate_two.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    saved_second = registry.execute(
        "patch_candidate",
        {
            "action": "save_current",
            "name": "candidate-two",
            "description": "second strategy",
            "semantic_layer": "consumer_adapter",
        },
    )
    assert second.status == "success"
    assert saved_second.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'old'\n"

    apply_before_evaluation = registry.execute("patch_candidate", {"action": "apply", "index": 2})
    list_before_evaluation = registry.execute("patch_candidate", {"action": "list"})
    evaluated_and_listed = registry.execute("patch_candidate", {"action": "evaluate_and_list"})
    apply_before_ranker = registry.execute("patch_candidate", {"action": "apply", "index": 2})
    ranked = registry.execute(
        "agent",
        {
            "type": "patch_ranker",
            "description": "Rank candidate patches",
            "prompt": "Rank the candidates from patch_candidate list and select candidate 2.",
        },
    )
    apply_wrong_index = registry.execute("patch_candidate", {"action": "apply", "index": 1})
    applied = registry.execute("patch_candidate", {"action": "apply", "index": 2})
    verify_before_review = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert reproduced.status == "success"
    assert apply_too_early.status == "error"
    assert apply_too_early.error["code"] == "PATCH_CANDIDATE_MIN_COUNT_REQUIRED"
    assert apply_before_evaluation.status == "error"
    assert apply_before_evaluation.error["code"] == "PATCH_CANDIDATE_EVALUATION_REQUIRED"
    assert list_before_evaluation.status == "error"
    assert list_before_evaluation.error["code"] == "PATCH_CANDIDATE_EVALUATION_REQUIRED"
    assert evaluated_and_listed.status == "success"
    evaluation = evaluated_and_listed.data["evaluation"]
    assert "value = 'old'" in evaluation["base_results"][0]["output"]
    assert "value = 'candidate_one'" in evaluation["candidates"][0]["execution_results"][0]["output"]
    assert evaluation["candidates"][0]["execution_results"][0]["differs_from_base"] is True
    assert "candidate-one" in evaluated_and_listed.text
    assert "candidate-two" in evaluated_and_listed.text
    assert apply_before_ranker.status == "error"
    assert apply_before_ranker.error["code"] == "PATCH_RANKER_REQUIRED"
    assert ranked.status == "success"
    assert apply_wrong_index.status == "error"
    assert apply_wrong_index.error["code"] == "PATCH_CANDIDATE_SELECTION_MISMATCH"
    assert applied.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'candidate_two'\n"
    assert verify_before_review.status == "error"
    assert verify_before_review.error["code"] == "PATCH_REVIEW_REQUIRED"


def test_high_risk_patch_review_requires_corrective_implement_before_verify(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(
                content='{"patch_risk":"high","findings":[{"risk_type":"parent_default_behavior","file":"app.py","reason":"risk","suggested_fix":"use better"}]}',
                tool_calls=[],
                raw_message={"content": "reviewed"},
                usage={},
            ),
            _replace_response("replace_2", "new", "better"),
            LLMToolResponse(content="corrected", tool_calls=[], raw_message={"content": "corrected"}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    assert registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    ).status == "success"
    assert registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    ).status == "success"
    verify_blocked = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )
    corrective = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Correct risk",
            "prompt": "Apply review suggested_fix.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    verify_still_needs_review = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert verify_blocked.status == "error"
    assert verify_blocked.error["code"] == "PATCH_REVIEW_FINDINGS_REQUIRE_FIX"
    assert corrective.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'better'\n"
    assert verify_still_needs_review.status == "error"
    assert verify_still_needs_review.error["code"] == "PATCH_REVIEW_REQUIRED"


def test_corrective_implement_cannot_expand_to_new_files(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    (repo / "other.py").write_text("value = 'other'\n", encoding="utf-8")
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(
                content='{"patch_risk":"high","verdict":"revise","findings":[],"required_changes":["fix current patch"]}',
                tool_calls=[],
                raw_message={"content": "reviewed"},
                usage={},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    assert registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    ).status == "success"
    assert registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    ).status == "success"
    too_wide = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Expand fix",
            "prompt": "Fix by editing another file.",
            "allowed_files": ["other.py"],
            "allowed_ranges": {"other.py": [[1, 1]]},
        },
    )

    assert too_wide.status == "error"
    assert too_wide.error["code"] == "CORRECTIVE_SCOPE_TOO_WIDE"


def test_static_patch_gate_triggers_corrective_implement(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "value = 'old'", "def broken(:"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            _replace_response("replace_2", "def broken(:", "value = 'new'"),
            LLMToolResponse(content="corrected", tool_calls=[], raw_message={"content": "corrected"}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    implemented = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Break app",
            "prompt": "Change app.py from old to a syntactically invalid line.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    verify_blocked = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )
    corrective = registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Fix syntax",
            "prompt": "Repair only the syntax error in current patch.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    )
    verify_needs_review = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert implemented.status == "success"
    assert "Automatic static patch gate" in implemented.text
    assert verify_blocked.status == "error"
    assert verify_blocked.error["code"] == "PATCH_REVIEW_FINDINGS_REQUIRE_FIX"
    rendered_corrective_messages = "\n".join(
        str(message.get("content") or "") for message in llm.messages_by_call[-1]
    )
    assert "CORRECTIVE_IMPLEMENT" in rendered_corrective_messages
    assert corrective.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 'new'\n"
    assert verify_needs_review.status == "error"
    assert verify_needs_review.error["code"] == "PATCH_REVIEW_REQUIRED"


def test_patch_review_revise_verdict_requires_corrective_implement(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(
                content='{"patch_risk":"medium","verdict":"revise","findings":[],"regression_risks":[{"file":"app.py","reason":"shared behavior","likely_tests":[]}],"coverage_gaps":[],"required_changes":["narrow guard"]}',
                tool_calls=[],
                raw_message={"content": "reviewed"},
                usage={},
            ),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    assert registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    ).status == "success"
    assert registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    ).status == "success"
    verify_blocked = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert verify_blocked.status == "error"
    assert verify_blocked.error["code"] == "PATCH_REVIEW_FINDINGS_REQUIRE_FIX"


def test_patch_review_medium_accept_allows_verify(tmp_path):
    repo = _init_git_repo_with_app(tmp_path)
    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    llm = FakeLLM(
        [
            _replace_response("replace_1", "old", "new"),
            LLMToolResponse(content="implemented", tool_calls=[], raw_message={"content": "implemented"}, usage={}),
            LLMToolResponse(
                content='{"patch_risk":"medium","verdict":"accept","findings":[],"regression_risks":[],"coverage_gaps":[],"required_changes":[]}',
                tool_calls=[],
                raw_message={"content": "reviewed"},
                usage={},
            ),
            LLMToolResponse(content="verified", tool_calls=[], raw_message={"content": "verified"}, usage={}),
        ]
    )
    runner = SubAgentRunner(llm=llm, base_tools=base_tools, cwd=repo, output_dir=tmp_path / "agents")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    assert registry.execute(
        "agent",
        {
            "type": "implement",
            "description": "Change app",
            "prompt": "Change app.py from old to new.",
            "allowed_files": ["app.py"],
            "allowed_ranges": {"app.py": [[1, 1]]},
        },
    ).status == "success"
    assert registry.execute(
        "agent",
        {"type": "patch_review", "description": "Review patch risk", "prompt": "Review current diff."},
    ).status == "success"
    verified = registry.execute(
        "agent",
        {"type": "verify", "description": "Verify", "prompt": "Run a check."},
    )

    assert verified.status == "success"


def test_patch_review_accept_with_actionable_findings_still_requires_revision():
    text = json.dumps(
        {
            "patch_risk": "medium",
            "verdict": "accept",
            "findings": [
                {
                    "risk_type": "public_regression_risk",
                    "suggested_fix": "Add a missing-key guard before pop().",
                }
            ],
            "regression_risks": [],
            "coverage_gaps": [],
            "required_changes": [],
        }
    )

    assert _patch_review_requires_revision(text) is True


def test_patch_review_low_risk_high_fanout_path_does_not_require_revision():
    text = json.dumps(
        {
            "patch_risk": "low",
            "verdict": "accept",
            "findings": [],
            "impact_surface": {
                "changed_core_paths": ["lib/mpl_toolkits/mplot3d/art3d.py"],
            },
            "coverage_gaps": [],
            "required_changes": [],
        }
    )

    assert _patch_review_requires_revision(text) is False


def test_agent_tool_blocks_reading_subagent_output_dirs(tmp_path):
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = ToolRegistry()
    registry.register_many(build_agent_tools(runner))

    response = registry.execute(
        "agent",
        {
            "type": "research",
            "description": "读取输出",
            "prompt": "Read .repo_agent_runs/latest/subagents/123-research files.",
        },
    )

    assert response.status == "error"
    assert response.error["code"] == "SUBAGENT_OUTPUT_READ_BLOCKED"


def test_scoped_implement_tools_limit_context_and_tool_surface(tmp_path):
    (tmp_path / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    constraints = WorkerConstraints.from_tool_input(
        {"allowed_files": ["app.py"], "allowed_ranges": {"app.py": [[1, 2]]}}
    )
    registry = runner._tools_for(get_worker_spec("implement"), constraints)

    assert set(registry.names()) == {"read_file", "replace_in_file", "write_file"}
    assert registry.execute("search_repo", {"query": "one"}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("git_diff", {}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("run_command", {"command": "pytest"}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("read_file", {"path": "other.py", "start_line": 1, "end_line": 1}).error["code"] == "IMPLEMENT_SCOPE_DENIED"
    assert registry.execute("read_file", {"path": "app.py", "start_line": 3, "end_line": 3}).error["code"] == "IMPLEMENT_SCOPE_DENIED"
    assert registry.execute("read_file", {"path": "app.py", "start_line": 1, "end_line": 2}).status == "success"


def test_patch_review_worker_is_read_only_and_cannot_run_commands(tmp_path):
    (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=_base_tools(), cwd=tmp_path, output_dir=tmp_path / "out")
    registry = runner._tools_for(get_worker_spec("patch_review"))

    assert set(registry.names()) == {"read_file", "git_diff"}
    assert registry.execute("search_repo", {"query": "one"}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("run_command", {"command": "pytest"}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("write_file", {"path": "app.py", "content": "x"}).error["code"] == "TOOL_NOT_FOUND"
    assert registry.execute("read_file", {"path": "app.py", "start_line": 1, "end_line": 1}).status == "success"
    assert registry.execute("read_file", {"path": "app.py", "start_line": 2, "end_line": 2}).status == "success"
    assert registry.execute("read_file", {"path": "app.py", "start_line": 1, "end_line": 2}).status == "success"
    blocked = registry.execute("read_file", {"path": "app.py", "start_line": 2, "end_line": 2})
    assert blocked.status == "error"
    assert blocked.error["code"] == "PATCH_REVIEW_READ_BUDGET_EXHAUSTED"


def test_swebench_verify_worker_has_one_command_hard_budget(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
        swebench_profile=True,
    )
    registry = runner._tools_for(get_worker_spec("verify"))

    unsafe = registry.execute("run_command", {"command": "python -m pytest tests | head -20"})
    first = registry.execute("run_command", {"command": "python3 -m py_compile app.py"})
    second = registry.execute("run_command", {"command": "which python3"})

    assert registry.names() == ["run_command"]
    assert unsafe.status == "error"
    assert unsafe.error["code"] == "UNSAFE_VERIFICATION_COMMAND"
    assert first.status == "success"
    assert second.status == "error"
    assert second.error["code"] == "VERIFY_COMMAND_BUDGET_EXHAUSTED"


def test_swebench_verify_worker_enforces_semantic_evidence_command(tmp_path):
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        base_tools=_base_tools(),
        cwd=tmp_path,
        output_dir=tmp_path / "out",
        swebench_profile=True,
    )
    execution = {}
    plan = {"command": "python3 -m py_compile app.py", "covers": ["syntax"]}
    registry = runner._tools_for(get_worker_spec("verify"), verification_plan=plan, verification_execution=execution)

    denied = registry.execute("run_command", {"command": "which python3"})
    allowed = registry.execute("run_command", {"command": plan["command"]})

    assert denied.error["code"] == "VERIFY_COMMAND_NOT_FROM_SEMANTIC_EVIDENCE"
    assert allowed.status == "success"
    assert execution["attempted"] is True
    assert execution["requested_command"] == plan["command"]
    assert execution["tool_status"] == "success"


def test_scoped_replace_in_file_applies_batch_inside_allowed_ranges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "app.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=base_tools, cwd=repo, output_dir=tmp_path / "out")
    constraints = WorkerConstraints.from_tool_input(
        {"allowed_files": ["app.py"], "allowed_ranges": {"app.py": [[2, 3]]}}
    )
    registry = runner._tools_for(get_worker_spec("implement"), constraints)

    response = registry.execute(
        "replace_in_file",
        {
            "path": "app.py",
            "replacements": [
                {"old": "two", "new": "TWO"},
                {"old": "three", "new": "THREE"},
            ],
        },
    )

    assert response.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour\n"


def test_scoped_replace_in_file_applies_line_replacements_and_preserves_guard(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "app.py").write_text(
        "if view_func and hasattr(view_func, \"__name__\"):\n"
        "    assert (\n"
        "        \".\" not in view_func.__name__\n"
        "    ), \"Blueprint view function name should not contain dots\"\n"
        "self.record(rule)\n",
        encoding="utf-8",
    )

    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=base_tools, cwd=repo, output_dir=tmp_path / "out")
    constraints = WorkerConstraints.from_tool_input(
        {"allowed_files": ["app.py"], "allowed_ranges": {"app.py": [[2, 4]]}}
    )
    registry = runner._tools_for(get_worker_spec("implement"), constraints)

    response = registry.execute(
        "replace_in_file",
        {
            "path": "app.py",
            "line_replacements": [
                {
                    "start_line": 2,
                    "end_line": 4,
                    "new": "    if \".\" in view_func.__name__:\n"
                    "        raise ValueError(\"Blueprint view function name should not contain dots\")",
                }
            ],
        },
    )

    assert response.status == "success"
    assert (repo / "app.py").read_text(encoding="utf-8") == (
        "if view_func and hasattr(view_func, \"__name__\"):\n"
        "    if \".\" in view_func.__name__:\n"
        "        raise ValueError(\"Blueprint view function name should not contain dots\")\n"
        "self.record(rule)\n"
    )


def test_scoped_replace_in_file_rejects_out_of_range_old_text(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    base_tools = ToolRegistry()
    register_repo_tools(base_tools, RepoWorkspace(repo, tmp_path / "out"))
    runner = SubAgentRunner(llm=FakeLLM([]), base_tools=base_tools, cwd=repo, output_dir=tmp_path / "out")
    constraints = WorkerConstraints.from_tool_input(
        {"allowed_files": ["app.py"], "allowed_ranges": {"app.py": [[2, 2]]}}
    )
    registry = runner._tools_for(get_worker_spec("implement"), constraints)

    response = registry.execute("replace_in_file", {"path": "app.py", "old": "three", "new": "THREE"})

    assert response.status == "error"
    assert response.error["code"] == "IMPLEMENT_SCOPE_DENIED"
    assert (repo / "app.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
