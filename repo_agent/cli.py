from __future__ import annotations

import argparse
from pathlib import Path

from repo_agent.core.agent import RepoAgent
from repo_agent.core.coordinator import CoordinatorEngine
from repo_agent.core.llm import ChatLLM
from repo_agent.tools import RepoWorkspace, ToolImportContext, assemble_tool_pool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument("--model", default=None)
        subparser.add_argument("--output-dir", type=Path, default=Path(".repo_agent_runs/latest"))
        subparser.add_argument("--max-steps", type=int, default=20)
        subparser.add_argument("--semantic-memory", action="store_true")
        subparser.add_argument("--coordinator", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    task = _build_task(args)
    workspace = RepoWorkspace(repo_path=args.repo, output_dir=args.output_dir)
    registry = assemble_tool_pool(ToolImportContext(workspace=workspace))
    llm = ChatLLM(model=args.model)
    if args.coordinator:
        coordinator = CoordinatorEngine(
            llm=llm,
            worker_tools=registry,
            max_steps=args.max_steps,
            output_dir=args.output_dir,
            cwd=args.repo,
            semantic_memory_recall=args.semantic_memory,
        )
        print(coordinator.run(task))
        return
    agent = RepoAgent(
        llm=llm,
        tools=registry,
        max_steps=args.max_steps,
        output_dir=args.output_dir,
        cwd=args.repo,
        semantic_memory_recall=args.semantic_memory,
    )
    print(agent.run(task))


def _build_task(args: argparse.Namespace) -> str:
    if args.command == "run":
        return args.task
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
