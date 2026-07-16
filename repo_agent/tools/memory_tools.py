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
        memory_type = None
        if inp.get("type"):
            try:
                memory_type = MemoryType(inp["type"])
            except ValueError:
                return ToolResponse.failure("BAD_MEMORY_TYPE", f"Expected one of: {', '.join(item.value for item in MemoryType)}")
        memories = [
            {
                "filename": memory.filename,
                "name": memory.name,
                "description": memory.description,
                "type": memory.type.value,
            }
            for memory in store.search(
                query,
                limit=limit,
                memory_type=memory_type,
            )
        ]
        return ToolResponse.success(json.dumps(memories, ensure_ascii=False, indent=2), {"memories": memories})

    def save_memory(inp: dict) -> ToolResponse:
        memory_type = inp["type"]
        if memory_type not in {item.value for item in MemoryType}:
            return ToolResponse.failure("BAD_MEMORY_TYPE", f"Expected one of: {', '.join(item.value for item in MemoryType)}")
        if memory_type == MemoryType.REPAIR_RECIPE.value:
            similar = store.similar_repair_recipes(inp["name"], inp["description"])
            if similar:
                candidates = [
                    {
                        "filename": memory.filename,
                        "name": memory.name,
                        "description": memory.description,
                        "type": memory.type.value,
                    }
                    for memory in similar
                ]
                return ToolResponse.failure(
                    "SIMILAR_REPAIR_RECIPE_EXISTS",
                    (
                        "A similar repair_recipe already exists. Read the closest memory and either call update_memory "
                        "to improve it, or do nothing if the new case adds no reusable strategy.\n\n"
                        f"Similar memories:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}"
                    ),
                )
        filename = store.save_memory(
            name=inp["name"],
            description=inp["description"],
            type=memory_type,
            content=inp["content"],
        )
        return ToolResponse.success(f"Saved memory: {filename}", {"filename": filename})

    def update_memory(inp: dict) -> ToolResponse:
        try:
            filename = store.update_memory(
                inp["filename"],
                name=inp.get("name"),
                description=inp.get("description"),
                content=inp.get("content"),
            )
        except FileNotFoundError:
            return ToolResponse.failure("MEMORY_NOT_FOUND", f"Memory not found: {inp['filename']}")
        return ToolResponse.success(f"Updated memory: {filename}", {"filename": filename})

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
                ToolParameter("type", "string", "Optional memory type filter", required=False),
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
                ToolParameter("type", "string", f"One of: {', '.join(item.value for item in MemoryType)}"),
                ToolParameter("content", "string", "Memory body. feedback should include Why and How to apply; repair_recipe should be an abstract strategy."),
            ],
            handler=save_memory,
            prompt=(
                "只有当用户明确要求记住信息，或给出可复用偏好/反馈/项目决策时才调用 save_memory；"
                "成功修复后的 repair_recipe 只能保存抽象修复策略、验证契约和反模式；"
                "不要保存完整 diff、具体 instance、gold patch、hidden test 或一次性文件路径。"
            ),
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="update_memory",
            description="Update an existing long-term memory file instead of creating a duplicate.",
            parameters=[
                ToolParameter("filename", "string", "Existing memory filename to update"),
                ToolParameter("name", "string", "Updated memory name", required=False),
                ToolParameter("description", "string", "Updated one-line description", required=False),
                ToolParameter("content", "string", "Updated full memory body", required=False),
            ],
            handler=update_memory,
            prompt=(
                "如果成功修复案例与已有 repair_recipe 相似，优先 read_memory 后用 update_memory 合并新抽象；"
                "如果没有新增可复用策略，不要更新也不要新增。"
            ),
            source=ToolSource.BUILTIN,
        ),
    ]


def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    registry.register_many(build_memory_tools(store))
