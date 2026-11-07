from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from repo_agent.core.agent import RepoAgent
from repo_agent.core.coordinator import CoordinatorEngine
from repo_agent.core.differential_validation import (
    build_test_expander_prompt,
    extract_differential_obligations,
    parse_differential_test_plan,
    patch_source_context,
    trusted_oracle_evidence_from_prompt,
)
from repo_agent.core.experience import (
    RepairRunMode,
    assess_lightweight_validation,
    build_failure_review_prompt,
    constraints_from_patch,
    independent_validation_succeeded,
    parse_failure_feedback,
    repair_mode_policy,
)
from repo_agent.core.llm import DEFAULT_MODEL, ChatLLM
from repo_agent.core.memory import MemoryStore
from repo_agent.core.memory_query import MemoryQueryAgent
from repo_agent.core.public_validation import PublicValidationAgent
from repo_agent.core.subagent import SubAgentRunner, WorkerConstraints
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.replan import REPLAN_PROMPT, validate_replan
from repo_agent.core.semantic_evidence import extract_json_object, extract_public_problem_statement
from repo_agent.tools import RepoWorkspace, ToolImportContext, assemble_tool_pool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument(
            "--model",
            default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
            help="Model name. Defaults to OPENAI_MODEL, then the built-in default.",
        )
        subparser.add_argument(
            "--implement-model",
            default=None,
            help="Optional model override for Implement and corrective Implement Workers.",
        )
        subparser.add_argument(
            "--reasoning-effort",
            choices=("low", "medium", "high", "max"),
            default="high",
            help="Reasoning effort for reasoning-enabled review Workers.",
        )
        subparser.add_argument(
            "--coordinator-reasoning-effort",
            choices=("low", "medium", "high", "max"),
            default="max",
            help="Reasoning effort for the Coordinator itself.",
        )
        subparser.add_argument(
            "--implement-reasoning-effort",
            choices=("low", "medium", "high", "max"),
            default="max",
            help="Reasoning effort for Implement and corrective Implement.",
        )
        subparser.add_argument(
            "--coordinator-max-output-tokens",
            type=_positive_int,
            default=os.getenv("REPO_AGENT_COORDINATOR_MAX_OUTPUT_TOKENS", "12288"),
            help="Per-request Coordinator output cap, including reasoning and tool arguments (default: 12288).",
        )
        subparser.add_argument("--output-dir", type=Path, default=Path(".repo_agent_runs/latest"))
        subparser.add_argument(
            "--review-max-output-tokens",
            type=_positive_int,
            default=os.getenv("REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS"),
            help="Optional per-request review output cap, including reasoning.",
        )
        subparser.add_argument("--max-steps", type=int, default=16)
        subparser.add_argument("--semantic-memory", action="store_true")
        subparser.add_argument("--memory-root", type=Path)
        subparser.add_argument("--memory-scope", help="Stable shared memory scope, for example repo:owner/name.")
        subparser.add_argument("--no-repair-memory", action="store_true", help="Disable repair_recipe recall for this run.")
        subparser.add_argument(
            "--memory-query-model",
            default=os.getenv("REPO_AGENT_MEMORY_QUERY_MODEL"),
            help="Enable independent public-Issue query extraction with this model before Coordinator memory recall.",
        )
        subparser.add_argument(
            "--memory-query-base-url",
            default=os.getenv("REPO_AGENT_MEMORY_QUERY_BASE_URL"),
            help="Optional separate OpenAI-compatible endpoint for query extraction.",
        )
        subparser.add_argument(
            "--memory-query-max-output-tokens",
            type=_positive_int,
            default=os.getenv("REPO_AGENT_MEMORY_QUERY_MAX_OUTPUT_TOKENS", "4096"),
            help="Query extraction output cap including reasoning (default: 4096).",
        )
        subparser.add_argument(
            "--swebench-profile",
            action="store_true",
            help="Use the slim SWE-bench pass@1 profile: no GitHub tools, no memory tools, no mandatory patch_review.",
        )
        subparser.add_argument(
            "--candidate-mode",
            choices=("auto", "required", "off"),
            default="off",
            help="Candidate-pool ablation policy. The default off path uses one patch; auto/required are retained for experiments.",
        )
        subparser.add_argument("--coordinator", action="store_true")
        subparser.add_argument("--public-validation", action="store_true",
                               help="Design public tests before initial implementation and run the fixed suite after repair.")
        subparser.add_argument("--public-validation-timeout", type=_positive_int, default=300)
        subparser.add_argument(
            "--forced-scope-file",
            type=Path,
            help="Diagnostic-only JSON scope oracle for Implement Worker. Never use for leaderboard runs.",
        )
        subparser.add_argument(
            "--repair-mode",
            choices=tuple(mode.value for mode in RepairRunMode),
            default=RepairRunMode.STANDARD.value,
            help="Repair workflow policy. benchmark is frozen/read-only; experience-build allows one correction and memory write.",
        )

    extract_query = subparsers.add_parser(
        "extract-memory-query", help="Extract an isolated retrieval query from a public Issue, without running repair.",
    )
    extract_query.add_argument("--issue-file", required=True, type=Path)
    extract_query.add_argument("--issue-title", default="")
    extract_query.add_argument(
        "--model", default=os.getenv("REPO_AGENT_MEMORY_QUERY_MODEL") or os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
    )
    extract_query.add_argument("--base-url", default=os.getenv("REPO_AGENT_MEMORY_QUERY_BASE_URL"))
    extract_query.add_argument(
        "--max-output-tokens", type=_positive_int,
        default=os.getenv("REPO_AGENT_MEMORY_QUERY_MAX_OUTPUT_TOKENS", "4096"),
    )
    extract_query.add_argument("--output-dir", type=Path, default=Path(".repo_agent_runs/memory-query"))

    run = subparsers.add_parser("run", help="Run an arbitrary repository task.")
    add_common(run)
    run.add_argument("--task", required=True)

    fix_issue = subparsers.add_parser("fix-issue", help="Fix a GitHub issue in a local repository.")
    add_common(fix_issue)
    fix_issue.add_argument("--issue-url", required=True)

    repair_ci = subparsers.add_parser("repair-ci", help="Repair a failed GitHub Actions run.")
    add_common(repair_ci)
    repair_ci.add_argument("--github-repo", required=True)
    repair_ci.add_argument("--run-id", required=True, type=int)

    curate = subparsers.add_parser("curate-repair", help="Curate a successful repair into an abstract repair_recipe memory.")
    add_common(curate)
    curate.add_argument("--task", default="", help="Original task text. Use --task-file for long tasks.")
    curate.add_argument("--task-file", type=Path)
    curate.add_argument("--patch-file", required=True, type=Path)
    curate.add_argument("--result", default="success", help="Success evidence, e.g. harness resolved.")
    curate.add_argument("--validation-file", type=Path)
    curate.add_argument("--final-answer-file", type=Path)

    corrective = subparsers.add_parser(
        "corrective-implement",
        help="Run one diagnostic-only corrective Implement Worker against an existing patch.",
    )
    add_common(corrective)
    corrective.add_argument("--task-file", required=True, type=Path)
    corrective.add_argument("--constraints-file", required=True, type=Path)

    replan = subparsers.add_parser("coordinator-replan", help="Confirm a new corrective scope from failure evidence without editing.")
    add_common(replan)
    replan.add_argument("--task-file", required=True, type=Path)

    benchmark = subparsers.add_parser(
        "benchmark",
        help="Run one frozen dual-memory, single-patch benchmark inference.",
    )
    add_common(benchmark)
    benchmark.add_argument("--task", required=True)
    benchmark.set_defaults(
        coordinator=True,
        swebench_profile=True,
        repair_mode=RepairRunMode.BENCHMARK.value,
    )

    experience_build = subparsers.add_parser(
        "experience-build",
        help="Build dual memory from an independently validated repair, with at most one feedback correction.",
    )
    add_common(experience_build)
    experience_build.add_argument("--task", required=True)
    experience_build.add_argument(
        "--feedback-command",
        help="Lightweight public behavior command used for baseline/patch feedback.",
    )
    experience_build.add_argument(
        "--final-eval-command",
        help="Independent final evaluation command, run exactly once after correction.",
    )
    experience_build.add_argument(
        "--verify-command",
        help="Deprecated alias for --final-eval-command.",
    )
    experience_build.add_argument(
        "--verification-timeout",
        type=int,
        default=300,
        help="Timeout for each lightweight feedback command.",
    )
    experience_build.add_argument(
        "--final-eval-timeout",
        type=int,
        default=1800,
        help="Timeout for the single independent final evaluation.",
    )
    experience_build.add_argument(
        "--provenance",
        required=True,
        help="Truthful source, for example swebench-dev, public-issue, or private-ci. Do not hide test provenance.",
    )
    experience_build.set_defaults(
        coordinator=True,
        swebench_profile=True,
        repair_mode=RepairRunMode.EXPERIENCE_BUILD.value,
    )

    design_validation = subparsers.add_parser(
        "design-public-tests", help="Design and baseline-check public tests without starting repair.",
    )
    add_common(design_validation)
    design_validation.add_argument("--task-file", required=True, type=Path)
    design_validation.add_argument("--context-file", type=Path,
                                   help="Optional localization facts, hypotheses, unknowns and source_files JSON.")

    review_failure = subparsers.add_parser(
        "review-failure",
        help="Analyze a failed patch and produce structured correction evidence.",
    )
    add_common(review_failure)
    review_failure.add_argument("--task-file", required=True, type=Path)
    review_failure.add_argument("--patch-file", required=True, type=Path)
    review_failure.add_argument("--failure-log-file", required=True, type=Path)
    review_failure.add_argument("--validation-file", type=Path)
    review_failure.add_argument("--implementation-report-file", type=Path)
    review_failure.set_defaults(repair_mode=RepairRunMode.EXPERIENCE_BUILD.value)

    design_differential = subparsers.add_parser(
        "design-differential-tests",
        help="Design diagnostic differential tests from public repair evidence.",
    )
    add_common(design_differential)
    design_differential.add_argument("--task-file", required=True, type=Path)
    design_differential.add_argument("--patch-file", required=True, type=Path)
    design_differential.add_argument("--validation-file", required=True, type=Path)
    design_differential.set_defaults(repair_mode=RepairRunMode.EXPERIENCE_BUILD.value)

    curate_experience = subparsers.add_parser(
        "curate-experience",
        help="Curate an independently validated success into episodic and semantic memory.",
    )
    add_common(curate_experience)
    curate_experience.add_argument("--task-file", required=True, type=Path)
    curate_experience.add_argument("--successful-patch-file", required=True, type=Path)
    curate_experience.add_argument("--failed-patch-file", type=Path)
    curate_experience.add_argument("--feedback-file", type=Path)
    curate_experience.add_argument("--validation-file", required=True, type=Path)
    curate_experience.add_argument("--provenance", required=True)
    curate_experience.set_defaults(repair_mode=RepairRunMode.EXPERIENCE_BUILD.value)

    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_role_llms(args: argparse.Namespace) -> tuple[ChatLLM, dict[str, ChatLLM]]:
    coordinator_llm = ChatLLM(
        model=args.model,
        reasoning_effort=args.coordinator_reasoning_effort,
        enable_thinking=True,
        max_output_tokens=getattr(args, "coordinator_max_output_tokens", 12288),
        output_limit_recovery=True,
    )
    lightweight_llm = ChatLLM(model=args.model, enable_thinking=False)
    implement_llm = ChatLLM(
        model=args.implement_model or args.model,
        reasoning_effort=args.implement_reasoning_effort,
        enable_thinking=True,
    )
    review_llm = ChatLLM(
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        enable_thinking=True,
        max_output_tokens=getattr(args, "review_max_output_tokens", None),
        output_limit_recovery=getattr(args, "review_max_output_tokens", None) is not None,
    )
    overrides = {
        "research": lightweight_llm,
        "plan": lightweight_llm,
        "test_expander": lightweight_llm,
        "public_validation": review_llm,
        "verify": lightweight_llm,
        "general": lightweight_llm,
        "implement": implement_llm,
        "reproducer": review_llm,
        "patch_ranker": review_llm,
        "patch_review": review_llm,
        "review": review_llm,
        "failure_review": review_llm,
        "memory_curator": lightweight_llm,
        "case_curator": lightweight_llm,
        "experience_curator": lightweight_llm,
    }
    return coordinator_llm, overrides


def _build_memory_query_agent(
    *, model: str, base_url: str | None, max_output_tokens: int,
) -> MemoryQueryAgent:
    return MemoryQueryAgent(ChatLLM(
        model=model,
        api_key=os.getenv("REPO_AGENT_MEMORY_QUERY_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=base_url,
        reasoning_effort="low",
        enable_thinking=False,
        max_output_tokens=max_output_tokens,
        output_limit_recovery=False,
    ))


def main(argv: list[str] | None = None) -> None:
    """CLI composition root：解析配置、装配依赖，再把任务交给具体运行时。

    这里有很多运行模式分支，但没有模型/工具循环。理解单 Agent Runtime 时，先关注
    task、workspace、registry、llm 的构造，以及函数末尾的 RepoAgent 路径即可。
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract-memory-query":
        query_agent = _build_memory_query_agent(
            model=args.model, base_url=args.base_url, max_output_tokens=args.max_output_tokens,
        )
        result = query_agent.extract(
            issue_title=args.issue_title,
            issue_body=extract_public_problem_statement(args.issue_file.read_text(encoding="utf-8")),
            output_dir=args.output_dir,
        )
        print(json.dumps({
            "problem_description": result.problem_description,
            "usage_scenario": result.usage_scenario,
            "status": result.status,
            "retrieval_text": result.retrieval_text,
        }, ensure_ascii=False, indent=2))
        return
    fixed_modes = {
        "benchmark": RepairRunMode.BENCHMARK.value,
        "experience-build": RepairRunMode.EXPERIENCE_BUILD.value,
        "review-failure": RepairRunMode.EXPERIENCE_BUILD.value,
        "design-differential-tests": RepairRunMode.EXPERIENCE_BUILD.value,
        "curate-experience": RepairRunMode.EXPERIENCE_BUILD.value,
    }
    expected_mode = fixed_modes.get(args.command)
    if expected_mode and args.repair_mode != expected_mode:
        parser.error(f"{args.command} requires --repair-mode {expected_mode}")
    if args.command == "experience-build" and args.feedback_command and not (args.final_eval_command or args.verify_command):
        parser.error("experience-build requires --final-eval-command")
    if args.public_validation and args.command in {"run", "fix-issue", "repair-ci"} and not args.coordinator:
        parser.error("--public-validation requires --coordinator")
    if args.memory_query_model and not args.no_repair_memory and args.repair_mode != "experience-build":
        if args.command in {"run", "benchmark"} and not args.coordinator:
            parser.error("--memory-query-model requires --coordinator")
        if args.command in {"fix-issue", "repair-ci"} and args.coordinator:
            parser.error("Query extraction requires public Issue text; use run --task or extract-memory-query --issue-file")
    task = _build_task(args)
    workspace = RepoWorkspace(repo_path=args.repo, output_dir=args.output_dir)
    memory_store = _build_memory_store(args)
    repair_policy = repair_mode_policy(args.repair_mode)
    registry = assemble_tool_pool(
        ToolImportContext(
            workspace=workspace,
            memory_root=args.memory_root,
            memory_store=memory_store,
            enable_github=not args.swebench_profile,
            enable_memory=not args.swebench_profile or repair_policy.memory_write,
        )
    )
    llm, worker_llm_overrides = _build_role_llms(args)
    if args.command == "design-public-tests":
        if _git_diff(args.repo).strip():
            parser.error("design-public-tests requires a clean baseline worktree")
        context = _read_json_or_text(args.context_file) or {}
        if not isinstance(context, dict):
            parser.error("--context-file must contain a JSON object")
        runner = SubAgentRunner(llm=llm, llm_overrides=worker_llm_overrides,
                                base_tools=registry, cwd=args.repo, output_dir=args.output_dir,
                                swebench_profile=True)
        result = PublicValidationAgent(runner, args.output_dir, args.public_validation_timeout).design(task, context)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "coordinator-replan":
        if args.repair_mode != "experience-build":
            parser.error("coordinator-replan requires experience-build")
        runner = SubAgentRunner(llm=llm, base_tools=registry, cwd=args.repo,
                                output_dir=args.output_dir, swebench_profile=True)
        tools = runner._swebench_failure_review_tools()
        engine = None

        def guard(answer):
            read_files = set()
            calls = {}
            for message in engine.messages:
                for call in message.get("tool_calls", []):
                    function = call.get("function", {})
                    if function.get("name") == "read_file":
                        calls[call["id"]] = json.loads(function["arguments"]).get("path")
                if message.get("role") == "tool" and message.get("tool_call_id") in calls:
                    result = json.loads(message["content"])
                    if result.get("status") == "success":
                        read_files.add(calls[message["tool_call_id"]])
            return validate_replan(answer, args.repo, read_files)

        engine = QueryEngine(llm=llm, tools=tools, system_prompt=REPLAN_PROMPT,
                             max_steps=6, cwd=args.repo, output_dir=args.output_dir,
                             memory_prompt_enabled=False, memory_recall_enabled=False,
                             include_dynamic_context=False, completion_guard=guard,
                             completion_guard_extra_turns=1)
        answer = engine.chat(task)
        data = extract_json_object(answer) or {}
        if guard(answer) or not engine.completed:
            data = {"status": "blocked", "reason": "Invalid or incomplete replan output"}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "replan.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(json.dumps(data, ensure_ascii=False))
        return
    if args.command == "design-differential-tests":
        candidate_patch = args.patch_file.read_text(
            encoding="utf-8",
            errors="replace",
        )
        guard_obligations = extract_differential_obligations(candidate_patch)
        runner = SubAgentRunner(
            llm=llm,
            llm_overrides=worker_llm_overrides,
            base_tools=registry,
            cwd=args.repo,
            output_dir=args.output_dir,
            memory_store=memory_store,
            semantic_memory_recall=False,
            excluded_memory_types=set(),
            swebench_profile=True,
        )
        test_expander_prompt = build_test_expander_prompt(
            task=task,
            candidate_patch=candidate_patch,
            initial_validation=_read_json_or_text(args.validation_file) or {},
            source_context=patch_source_context(args.repo, candidate_patch),
        )
        result = runner.run(
            agent_type="test_expander",
            description="Design public differential behavior inputs",
            prompt=test_expander_prompt,
        )
        plan = parse_differential_test_plan(
            result.text,
            guard_obligations,
            trusted_oracle_evidence_from_prompt(test_expander_prompt),
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        payload = plan or {"parse_error": True, "raw": result.text}
        (args.output_dir / "differential_test_plan.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (args.output_dir / "worker_result.json").write_text(
            json.dumps(_worker_payload(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "review-failure":
        runner = SubAgentRunner(
            llm=llm,
            llm_overrides=worker_llm_overrides,
            base_tools=registry,
            cwd=args.repo,
            output_dir=args.output_dir,
            memory_store=memory_store,
            semantic_memory_recall=False,
            excluded_memory_types=set(),
            swebench_profile=True,
        )
        result = runner.run(
            agent_type="failure_review",
            description="Attribute failed repair",
            prompt=build_failure_review_prompt(
                task=task,
                failed_patch=args.patch_file.read_text(encoding="utf-8", errors="replace"),
                failure_log=args.failure_log_file.read_text(encoding="utf-8", errors="replace"),
                validation=_read_json_or_text(args.validation_file),
                implementation_report=_read_json_or_text(args.implementation_report_file),
            ),
        )
        feedback = parse_failure_feedback(result.text)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        payload = feedback.to_dict() if feedback else {"parse_error": True, "raw": result.text}
        (args.output_dir / "failure_feedback.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "curate-experience":
        validation = _read_json_or_text(args.validation_file)
        if not independent_validation_succeeded(validation):
            parser.error("curate-experience requires independently validated success")
        coordinator = CoordinatorEngine(
            llm=llm,
            worker_llm_overrides=worker_llm_overrides,
            worker_tools=registry,
            max_steps=args.max_steps,
            output_dir=args.output_dir,
            cwd=args.repo,
            memory_store=memory_store,
            semantic_memory_recall=False,
            repair_memory_recall=True,
            swebench_profile=True,
            candidate_mode="off",
            repair_mode=RepairRunMode.EXPERIENCE_BUILD.value,
        )
        feedback_payload = _read_json_or_text(args.feedback_file)
        feedback = (
            parse_failure_feedback(json.dumps(feedback_payload, ensure_ascii=False))
            if isinstance(feedback_payload, dict)
            else None
        )
        result = coordinator.curate_repair_experience(
            task=task,
            successful_patch=args.successful_patch_file.read_text(encoding="utf-8", errors="replace"),
            validation=validation,
            provenance=args.provenance,
            failed_patch=_read_text(args.failed_patch_file),
            feedback=feedback,
        )
        print(result.text)
        return
    if args.command == "experience-build":
        if _git_diff(args.repo).strip():
            parser.error("experience-build requires a clean tracked worktree so failed patches can be reversed safely")
        report = _run_experience_build(
            args=args,
            task=task,
            llm=llm,
            worker_llm_overrides=worker_llm_overrides,
            registry=registry,
            memory_store=memory_store,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.command == "corrective-implement":
        constraints_data = json.loads(args.constraints_file.read_text(encoding="utf-8"))
        constraints = WorkerConstraints.from_tool_input(constraints_data)
        if not constraints.has_scope:
            parser.error("corrective-implement requires non-empty allowed_files and allowed_ranges")
        runner = SubAgentRunner(
            llm=llm,
            llm_overrides=worker_llm_overrides,
            base_tools=registry,
            cwd=args.repo,
            output_dir=args.output_dir,
            memory_store=memory_store,
            semantic_memory_recall=False,
            excluded_memory_types=set(),
            swebench_profile=True,
            implement_phase="experience_corrective",
        )
        result = runner.run(
            agent_type="implement",
            description="Diagnostic corrective implement",
            prompt=task,
            constraints=constraints,
        )
        payload = {
            "type": result.type,
            "model": result.model,
            "description": result.description,
            "text": result.text,
            "output_dir": str(result.output_dir),
            "completed": result.completed,
            "stop_reason": result.stop_reason,
            "max_steps": result.max_steps,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "changed_files": list(result.changed_files),
            "recalled_memories": [],
        }
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "worker_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(result.text)
        return
    if args.command == "curate-repair":
        coordinator = CoordinatorEngine(
            llm=llm,
            worker_llm_overrides=worker_llm_overrides,
            worker_tools=registry,
            max_steps=args.max_steps,
            output_dir=args.output_dir,
            cwd=args.repo,
            memory_store=memory_store,
            semantic_memory_recall=args.semantic_memory,
            repair_memory_recall=False,
            swebench_profile=args.swebench_profile,
            candidate_mode=args.candidate_mode,
            repair_mode=args.repair_mode,
        )
        result = coordinator.curate_successful_repair(
            task=task,
            patch=args.patch_file.read_text(encoding="utf-8", errors="replace"),
            result=args.result,
            validation=_read_json_or_text(args.validation_file),
            final_answer=_read_text(args.final_answer_file),
        )
        print(result.text)
        return
    if args.coordinator:
        # Coordinator 使用同一个底层 QueryEngine，但额外拥有可派发的 Worker。
        # 第一周只需知道这是多 Agent 分支，不必展开其状态机。
        forced_scope = _read_json_or_text(args.forced_scope_file) or {}
        if forced_scope and not isinstance(forced_scope, dict):
            parser.error("--forced-scope-file must contain a JSON object")
        memory_query_agent = None
        if args.memory_query_model and not args.no_repair_memory and args.repair_mode != "experience-build":
            memory_query_agent = _build_memory_query_agent(
                model=args.memory_query_model, base_url=args.memory_query_base_url,
                max_output_tokens=args.memory_query_max_output_tokens,
            )
        coordinator = CoordinatorEngine(
            llm=llm,
            worker_llm_overrides=worker_llm_overrides,
            worker_tools=registry,
            max_steps=args.max_steps,
            output_dir=args.output_dir,
            cwd=args.repo,
            memory_store=memory_store,
            semantic_memory_recall=args.semantic_memory,
            repair_memory_recall=not args.no_repair_memory,
            swebench_profile=args.swebench_profile,
            candidate_mode=args.candidate_mode,
            forced_implement_scope=forced_scope,
            repair_mode=args.repair_mode,
            memory_query_agent=memory_query_agent,
            public_validation=args.public_validation,
            public_validation_timeout=args.public_validation_timeout,
        )
        print(coordinator.run(task))
        if coordinator.public_validator:
            coordinator.public_validator.evaluate()
        return
    # 不启用 Coordinator 时，CLI 进入最短主链：RepoAgent -> QueryEngine -> query_loop。
    agent = RepoAgent(
        llm=llm,
        tools=registry,
        max_steps=args.max_steps,
        output_dir=args.output_dir,
        cwd=args.repo,
        memory_store=memory_store,
        semantic_memory_recall=args.semantic_memory,
        repair_memory_recall=not args.no_repair_memory,
        swebench_profile=args.swebench_profile,
    )
    print(agent.run(task))


def _run_experience_build(
    *,
    args: argparse.Namespace,
    task: str,
    llm: ChatLLM,
    worker_llm_overrides: dict[str, ChatLLM],
    registry,
    memory_store: MemoryStore,
) -> dict:
    feedback_command = str(args.feedback_command or "").strip()
    final_eval_command = str(args.final_eval_command or args.verify_command or "").strip()
    use_public_tests = bool(args.public_validation or not feedback_command)
    baseline_feedback = (
        _run_validation(registry, feedback_command, timeout=args.verification_timeout)
        if feedback_command and not use_public_tests
        else {}
    )
    coordinator = CoordinatorEngine(
        llm=llm,
        worker_llm_overrides=worker_llm_overrides,
        worker_tools=registry,
        max_steps=args.max_steps,
        output_dir=args.output_dir / "initial",
        cwd=args.repo,
        memory_store=memory_store,
        # Experience-build learns only after final success. Historical memory
        # must not influence Coordinator or either Implement pass.
        semantic_memory_recall=False,
        repair_memory_recall=False,
        swebench_profile=True,
        candidate_mode="off",
        repair_mode=RepairRunMode.EXPERIENCE_BUILD.value,
        public_validation=use_public_tests,
        public_validation_timeout=args.public_validation_timeout,
    )
    initial_answer = coordinator.run(task)
    failed_patch = _git_diff(args.repo)
    initial_feedback = (
        _run_validation(registry, feedback_command, timeout=args.verification_timeout)
        if feedback_command and not use_public_tests and failed_patch.strip()
        else {}
    )
    validation = (
        coordinator.public_validator.evaluate() if use_public_tests
        else assess_lightweight_validation(baseline_feedback, initial_feedback).to_dict()
    )
    report: dict = {
        "mode": RepairRunMode.EXPERIENCE_BUILD.value,
        "provenance": args.provenance,
        "validation_policy": {
            "feedback_command": feedback_command,
            "final_eval_command": final_eval_command,
            "max_corrections": 1,
            "max_full_evaluations": 1,
            "feedback_source": "public_tests" if use_public_tests else "feedback_command",
        },
        "initial": {
            "answer": initial_answer,
            "patch_chars": len(failed_patch),
            "lightweight_validation": validation,
        },
        "correction": None,
        "final_validation": None,
        "memory": None,
    }

    feedback = None
    successful_patch = failed_patch
    if validation.get("feedback_eligible") and failed_patch.strip():
        feedback_result = coordinator.runner.run(
            agent_type="failure_review",
            description="Attribute failed repair",
            prompt=build_failure_review_prompt(
                task=task,
                failed_patch=failed_patch,
                failure_log=str(validation.get("failure_evidence") or initial_feedback.get("output") or ""),
                validation=validation,
                implementation_report=dict(
                    coordinator._agent_state.get("implementation_report") or {}
                ),
            ),
        )
        feedback = parse_failure_feedback(feedback_result.text)
        feedback_payload = feedback.to_dict() if feedback else None
        correction_report: dict = {
            "feedback": feedback_payload,
            "feedback_worker": _worker_payload(feedback_result),
            "route": "none",
            "attempted": False,
        }
        if feedback and feedback.correction_route == "corrective_implement":
            correction_report["route"] = "corrective_implement"
            constraints = constraints_from_patch(failed_patch, args.repo)
            correction_prompt = (
                f"## Public Task\n{task}\n\n"
                f"## FailureFeedback\n{json.dumps(feedback.to_dict(), ensure_ascii=False, indent=2)}\n\n"
                f"## Current Failed Patch\n```diff\n{failed_patch}\n```\n\n"
                "The worktree already contains Current Failed Patch. Correct it in place exactly once; do not "
                "reapply it from scratch. Every edit must be authorized by correction_evidence_ids and must address "
                "the first failed predicate, identity, conversion, state transition, or exact output contract named "
                "by FailureFeedback. Preserve regression_guards and every hunk not invalidated by direct evidence. "
                "Do not search, run commands, edit tests, or expand the enforced file/range scope."
            )
            # Experience-build 的 corrective pass 只消费本次公开失败证据；
            # 历史记忆留给冻结的 benchmark 模式使用。
            recalled_semantic = ()
            recalled_episodes = ()
            if constraints.has_scope:
                correction_report["attempted"] = True
                correction_result = coordinator.runner.run(
                    agent_type="implement",
                    description="Correct failed repair",
                    prompt=correction_prompt,
                    constraints=constraints,
                    implement_phase="experience_corrective",
                )
                correction_report["worker"] = _worker_payload(correction_result)
                correction_report["recalled_episodes"] = [
                    memory.header.filename for memory in recalled_episodes
                ]
                correction_report["recalled_semantic_insights"] = [
                    memory.header.filename for memory in recalled_semantic
                ]
            else:
                correction_report["error"] = "failed patch did not provide a safe corrective scope"
        corrected_patch = _git_diff(args.repo)
        corrected_feedback = (
            _run_validation(
                registry,
                feedback_command,
                timeout=args.verification_timeout,
            )
            if correction_report["attempted"] and feedback_command and not use_public_tests
            else initial_feedback
        )
        validation = (
            coordinator.public_validator.evaluate() if use_public_tests
            else assess_lightweight_validation(baseline_feedback, corrected_feedback).to_dict()
        )
        correction_report["patch_chars"] = len(corrected_patch)
        correction_report["lightweight_validation"] = validation
        report["correction"] = correction_report
        successful_patch = corrected_patch

    final_validation = (
        _run_validation(registry, final_eval_command, timeout=args.final_eval_timeout)
        if successful_patch.strip() and final_eval_command
        else {
            "status": "skipped",
            "command": final_eval_command,
            "exit_code": None,
            "output": "final evaluation skipped: no patch or no independent evaluation command",
        }
    )
    report["final_validation"] = final_validation
    learning_validation = validation if use_public_tests else final_validation
    learning_passed = validation.get("status") == "passed" if use_public_tests else _validation_passed(final_validation)
    if learning_passed and successful_patch.strip():
        correction_attempted = bool((report.get("correction") or {}).get("attempted"))
        curator_result = coordinator.curate_repair_experience(
            task=task,
            successful_patch=successful_patch,
            validation=learning_validation,
            provenance=args.provenance,
            failed_patch=failed_patch if correction_attempted else "",
            feedback=feedback if correction_attempted else None,
        )
        report["memory"] = _worker_payload(curator_result)
        report["status"] = "experience_saved_or_updated"
    else:
        report["status"] = "not_learned_without_success"

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "experience_build.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _run_validation(registry, command: str, *, timeout: int) -> dict:
    response = registry.execute("run_command", {"command": command, "timeout": timeout})
    return {
        "status": response.status.value,
        "command": command,
        "exit_code": response.data.get("exit_code"),
        "output": response.text[-10_000:],
    }


def _validation_passed(validation: dict) -> bool:
    return validation.get("exit_code") == 0 and validation.get("status") == "success"


def _worker_payload(result) -> dict:
    return {
        "type": result.type,
        "model": result.model,
        "completed": result.completed,
        "stop_reason": result.stop_reason,
        "changed_files": list(result.changed_files),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        # 缓存指标按 Agent 与 Prompt 版本分组，避免只看总 token 掩盖命中率差异。
        "prompt_version": getattr(result, "prompt_version", ""),
        "cached_tokens": getattr(result, "cached_tokens", 0),
        "cache_hit_rate": getattr(result, "cache_hit_rate", 0.0),
        "text": result.text,
    }


def _git_diff(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "--", ".", ":!.env*"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    return result.stdout if result.returncode in {0, 1} else ""


def _build_task(args: argparse.Namespace) -> str:
    if args.command in {"run", "benchmark", "experience-build"}:
        return args.task
    if args.command == "curate-repair":
        if args.task_file:
            return args.task_file.read_text(encoding="utf-8", errors="replace")
        return args.task
    if args.command in {"corrective-implement", "coordinator-replan"}:
        return args.task_file.read_text(encoding="utf-8", errors="replace")
    if args.command in {
        "review-failure",
        "design-differential-tests",
        "design-public-tests",
        "curate-experience",
    }:
        return args.task_file.read_text(encoding="utf-8", errors="replace")
    if args.command == "fix-issue":
        return (
            f"读取并修复 GitHub issue: {args.issue_url}\n"
            "请先调用 get_issue，再定位代码、修改、验证，并调用 finish。"
        )
    if args.command == "repair-ci":
        return (
            f"修复 GitHub Actions CI。仓库: {args.github_repo}, run_id: {args.run_id}\n"
            "请先调用 download_workflow_logs，再定位失败原因、修改、验证，并调用 finish。"
        )
    raise ValueError(f"Unknown command: {args.command}")


def _build_memory_store(args: argparse.Namespace) -> MemoryStore:
    if getattr(args, "memory_scope", None):
        return MemoryStore.for_scope(args.memory_scope, root=args.memory_root)
    return MemoryStore.for_project(args.repo, root=args.memory_root)


def _read_json_or_text(path: Path | None):
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
