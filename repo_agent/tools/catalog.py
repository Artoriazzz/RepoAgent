from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from repo_agent.core.memory import MemoryStore
from repo_agent.core.tool import Tool, ToolRegistry, ToolSource
from repo_agent.tools.repo_tools import RepoWorkspace, build_repo_tools


@dataclass
class ToolImportContext:
    """工具导入上下文。

    这里对应工具系统的运行时过滤层：同一套代码可以按 CLI 模式、权限或集成开关
    组装出不同工具池。
    """

    workspace: RepoWorkspace
    github_token: str | None = None
    enable_github: bool = True
    enable_memory: bool = True
    memory_root: Path | None = None
    memory_store: MemoryStore | None = None
    simple_mode: bool = False
    denied_tools: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolBundle:
    name: str
    source: ToolSource
    build: Callable[[ToolImportContext], list[Tool]]


def get_all_base_tool_bundles(context: ToolImportContext) -> list[ToolBundle]:
    """Layer 1：核心工具直接 import，集成工具按 feature/context 条件导入。"""

    bundles = [
        ToolBundle(
            name="repo",
            source=ToolSource.BUILTIN,
            build=lambda ctx: build_repo_tools(ctx.workspace),
        )
    ]
    if context.enable_memory:
        from repo_agent.tools.memory_tools import build_memory_tools

        bundles.append(
            ToolBundle(
                name="memory",
                source=ToolSource.BUILTIN,
                build=lambda ctx: build_memory_tools(
                    ctx.memory_store or MemoryStore.for_project(ctx.workspace.repo_path, root=ctx.memory_root)
                ),
            )
        )
    if context.enable_github:
        from repo_agent.tools.github_tools import build_github_tools

        bundles.append(
            ToolBundle(
                name="github",
                source=ToolSource.INTEGRATION,
                build=lambda ctx: build_github_tools(token=ctx.github_token),
            )
        )
    return bundles


def get_tools(context: ToolImportContext) -> list[Tool]:
    """Layer 2：根据运行时上下文过滤工具。"""

    tools: list[Tool] = []
    for bundle in get_all_base_tool_bundles(context):
        for tool in bundle.build(context):
            if context.simple_mode and tool.source != ToolSource.BUILTIN:
                continue
            if tool.name in context.denied_tools:
                continue
            tools.append(tool)
    return tools


def assemble_tool_pool(
    context: ToolImportContext,
    extra_tools: Iterable[Tool] = (),
) -> ToolRegistry:
    """Layer 3：内置工具和扩展工具分区排序、去重，组装最终工具池。"""

    seen: set[str] = set()
    ordered_tools: list[Tool] = []
    for tool in _partition_sort([*get_tools(context), *extra_tools]):
        if tool.name in seen:
            continue
        seen.add(tool.name)
        ordered_tools.append(tool)

    registry = ToolRegistry()
    registry.register_many(ordered_tools)
    return registry


def _partition_sort(tools: list[Tool]) -> list[Tool]:
    partition_order = {
        ToolSource.BUILTIN: 0,
        ToolSource.INTEGRATION: 1,
        ToolSource.EXTERNAL: 2,
    }
    return sorted(
        tools,
        key=lambda tool: (partition_order.get(tool.source, 99), tool.sort_key or tool.name, tool.name),
    )
