from __future__ import annotations

import json

from repo_agent.core.memory import MemoryStore, MemoryType
from repo_agent.core.tool import Tool, ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    def list_memories(inp: dict) -> ToolResponse:
        memories = [
            {
                "filename": memory.filename,
                "name": memory.name,
                "description": memory.description,
                "type": memory.type.value,
            }
            for memory in store.list_memories()
        ]
        return ToolResponse.success(json.dumps(memories, ensure_ascii=False, indent=2), {"memories": memories})

    def read_memory(inp: dict) -> ToolResponse:
        filename = inp["filename"]
        return ToolResponse.success(store.read_memory(filename), {"filename": filename})

    def search_memories(inp: dict) -> ToolResponse:
        query = inp["query"]
        limit = int(inp.get("limit") or 5)
        memories = [
            {
                "filename": memory.filename,
                "name": memory.name,
                "description": memory.description,
                "type": memory.type.value,
            }
            for memory in store.search(query, limit=limit)
        ]
        return ToolResponse.success(json.dumps(memories, ensure_ascii=False, indent=2), {"memories": memories})

    def save_memory(inp: dict) -> ToolResponse:
        memory_type = inp["type"]
        if memory_type not in {item.value for item in MemoryType}:
            return ToolResponse.failure("BAD_MEMORY_TYPE", f"Expected one of: {', '.join(item.value for item in MemoryType)}")
        filename = store.save_memory(
            name=inp["name"],
            description=inp["description"],
            type=memory_type,
            content=inp["content"],
        )
        return ToolResponse.success(f"Saved memory: {filename}", {"filename": filename})

    return [
        build_tool(
            name="list_memories",
            description="List long-term memories for this project.",
            parameters=[],
            handler=list_memories,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="read_memory",
            description="Read a long-term memory file by filename.",
            parameters=[ToolParameter("filename", "string", "Memory filename, for example feedback_no_summary.md")],
            handler=read_memory,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="search_memories",
            description="Search long-term memories by query.",
            parameters=[
                ToolParameter("query", "string", "Search query"),
                ToolParameter("limit", "integer", "Maximum results", required=False),
            ],
            handler=search_memories,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="save_memory",
            description="Save a long-term memory only when the user explicitly asks to remember something.",
            parameters=[
                ToolParameter("name", "string", "Short memory name"),
                ToolParameter("description", "string", "One-line memory index description"),
                ToolParameter("type", "string", "One of: user, feedback, project, reference"),
                ToolParameter("content", "string", "Memory body. feedback should include Why and How to apply."),
            ],
            handler=save_memory,
            prompt=(
                "只有当用户明确要求记住信息，或给出可复用偏好/反馈/项目决策时才调用 save_memory；"
                "不要保存能从当前仓库、Git 历史或项目指令重新推导的信息。"
            ),
            source=ToolSource.BUILTIN,
        ),
    ]


def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    registry.register_many(build_memory_tools(store))
