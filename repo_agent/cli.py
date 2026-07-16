from __future__ import annotations

import argparse
import json
from pathlib import Path

from repo_agent.core.agent import RepoAgent
from repo_agent.core.coordinator import CoordinatorEngine
from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore
from repo_agent.core.subagent import SubAgentRunner, WorkerConstraints
from repo_agent.tools import RepoWorkspace, ToolImportContext, assemble_tool_pool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument("--model", default=None)
        subparser.add_argument(
            "--implement-model",
            default=None,
            help="Optional model override for Implement and corrective Implement Workers.",
        )
        subparser.add_argument("--output-dir", type=Path, default=Path(".repo_agent_runs/latest"))
        subparser.add_argument("--max-steps", type=int, default=16)
        subparser.add_argument("--semantic-memory", action="store_true")
        subparser.add_argument("--memory-root", type=Path)
        subparser.add_argument("--memory-scope", help="Stable shared memory scope, for example repo:owner/name.")
        subparser.add_argument("--no-repair-memory", action="store_true", help="Disable repair_recipe recall for this run.")
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
        subparser.add_argument(
            "--forced-scope-file",
            type=Path,
            help="Diagnostic-only JSON scope oracle for Implement Worker. Never use for leaderboard runs.",
        )

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    task = _build_task(args)
    workspace = RepoWorkspace(repo_path=args.repo, output_dir=args.output_dir)
    memory_store = _build_memory_store(args)
    registry = assemble_tool_pool(
        ToolImportContext(
            workspace=workspace,
            memory_root=args.memory_root,
            memory_store=memory_store,
            enable_github=not args.swebench_profile,
            enable_memory=not args.swebench_profile,
        )
    )
    llm = ChatLLM(model=args.model)
    implement_llm = ChatLLM(model=args.implement_model) if args.implement_model else llm
    worker_llm_overrides = {"implement": implement_llm}
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
        forced_scope = _read_json_or_text(args.forced_scope_file) or {}
        if forced_scope and not isinstance(forced_scope, dict):
            parser.error("--forced-scope-file must contain a JSON object")
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
        )
        print(coordinator.run(task))
        return
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


def _build_task(args: argparse.Namespace) -> str:
    if args.command == "run":
        return args.task
    if args.command == "curate-repair":
        if args.task_file:
            return args.task_file.read_text(encoding="utf-8", errors="replace")
        return args.task
    if args.command == "corrective-implement":
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
