from __future__ import annotations

import json

from repo_agent.core.memory import MAX_SEMANTIC_INSIGHTS_PER_ROLE, MemoryStore, MemoryType
from repo_agent.core.tool import Tool, ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool


_SEMANTIC_ROLES = {"coordinator", "implement", "review"}
_FORBIDDEN_REPAIR_MEMORY_MARKERS = (
    "diff --git",
    "fail_to_pass",
    "pass_to_pass",
    "gold patch",
    "hidden test",
    "instance_id",
)


def _repair_memory_policy_error(
    memory_type: MemoryType,
    *,
    content: str,
    role: str,
    provenance: str,
) -> str:
    if memory_type not in {
        MemoryType.REPAIR_EPISODE,
        MemoryType.SEMANTIC_INSIGHT,
        MemoryType.GLOBAL_EXPERIENCE,
        MemoryType.REPOSITORY_EXPERIENCE,
    }:
        return ""
    if not provenance.strip():
        return "repair experience requires truthful provenance"
    if memory_type == MemoryType.REPAIR_EPISODE and role != "implement":
        return "repair_episode role must be implement"
    if memory_type == MemoryType.SEMANTIC_INSIGHT and role not in _SEMANTIC_ROLES:
        return "semantic_insight role must be coordinator, implement, or review"
    max_chars = {
        MemoryType.REPAIR_EPISODE: 6_000,
        MemoryType.SEMANTIC_INSIGHT: 2_500,
        MemoryType.GLOBAL_EXPERIENCE: 3_500,
        MemoryType.REPOSITORY_EXPERIENCE: 4_500,
    }[memory_type]
    if len(content) > max_chars:
        return f"{memory_type.value} must be compact (at most {max_chars} characters)"
    lowered = content.lower()
    marker = next(
        (item for item in _FORBIDDEN_REPAIR_MEMORY_MARKERS if item in lowered),
        "",
    )
    if marker:
        return f"{memory_type.value} must not contain benchmark answers or full patches: {marker}"
    return ""


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    def list_memories(inp: dict) -> ToolResponse:
        memories = [
            {
                "filename": memory.filename,
                "name": memory.name,
                "description": memory.description,
                "type": memory.type.value,
                "role": memory.role,
                "provenance": memory.provenance,
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
                "role": memory.role,
                "provenance": memory.provenance,
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
        parsed_type = MemoryType(memory_type)
        role = str(inp.get("role") or "")
        provenance = str(inp.get("provenance") or "")
        policy_error = _repair_memory_policy_error(
            parsed_type,
            content=str(inp["content"]),
            role=role,
            provenance=provenance,
        )
        if policy_error:
            return ToolResponse.failure("REPAIR_MEMORY_POLICY_VIOLATION", policy_error)
        if memory_type in {
            MemoryType.REPAIR_RECIPE.value,
            MemoryType.REPAIR_EPISODE.value,
            MemoryType.SEMANTIC_INSIGHT.value,
            MemoryType.GLOBAL_EXPERIENCE.value,
            MemoryType.REPOSITORY_EXPERIENCE.value,
        }:
            similar = store.similar_memories(
                inp["name"],
                inp["description"],
                memory_type=parsed_type,
                role=role,
            )
            if similar:
                candidates = [
                    {
                        "filename": memory.filename,
                        "name": memory.name,
                        "description": memory.description,
                        "type": memory.type.value,
                        "role": memory.role,
                    }
                    for memory in similar
                ]
                error_code = (
                    "SIMILAR_REPAIR_RECIPE_EXISTS"
                    if memory_type == MemoryType.REPAIR_RECIPE.value
                    else "SIMILAR_REPAIR_MEMORY_EXISTS"
                )
                return ToolResponse.failure(
                    error_code,
                    (
                        f"A similar {memory_type} already exists. Read the closest memory and either call "
                        "update_memory to improve it, or do nothing if the new case adds no reusable knowledge.\n\n"
                        f"Similar memories:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}"
                    ),
                )
        if (
            memory_type == MemoryType.SEMANTIC_INSIGHT.value
            and store.count(MemoryType.SEMANTIC_INSIGHT, role=role) >= MAX_SEMANTIC_INSIGHTS_PER_ROLE
        ):
            return ToolResponse.failure(
                "SEMANTIC_MEMORY_FULL",
                (
                    f"Semantic memory role={role or 'unspecified'} already has "
                    f"{MAX_SEMANTIC_INSIGHTS_PER_ROLE} insights. REMOVE a contradictory, redundant, outdated, "
                    "or lower-value insight with delete_memory before ADD."
                ),
            )
        filename = store.save_memory(
            name=inp["name"],
            description=inp["description"],
            type=memory_type,
            content=inp["content"],
            role=role,
            provenance=provenance,
        )
        return ToolResponse.success(f"Saved memory: {filename}", {"filename": filename})

    def update_memory(inp: dict) -> ToolResponse:
        header = next(
            (memory for memory in store.list_memories() if memory.filename == inp["filename"]),
            None,
        )
        if header is None:
            return ToolResponse.failure("MEMORY_NOT_FOUND", f"Memory not found: {inp['filename']}")
        if inp.get("content") is not None:
            policy_error = _repair_memory_policy_error(
                header.type,
                content=str(inp["content"]),
                role=str(inp.get("role") if inp.get("role") is not None else header.role),
                provenance=str(
                    inp.get("provenance")
                    if inp.get("provenance") is not None
                    else header.provenance
                ),
            )
            if policy_error:
                return ToolResponse.failure("REPAIR_MEMORY_POLICY_VIOLATION", policy_error)
        try:
            filename = store.update_memory(
                inp["filename"],
                name=inp.get("name"),
                description=inp.get("description"),
                content=inp.get("content"),
                role=inp.get("role"),
                provenance=inp.get("provenance"),
            )
        except FileNotFoundError:
            return ToolResponse.failure("MEMORY_NOT_FOUND", f"Memory not found: {inp['filename']}")
        return ToolResponse.success(f"Updated memory: {filename}", {"filename": filename})

    def delete_memory(inp: dict) -> ToolResponse:
        try:
            filename = store.delete_memory(inp["filename"])
        except FileNotFoundError:
            return ToolResponse.failure("MEMORY_NOT_FOUND", f"Memory not found: {inp['filename']}")
        return ToolResponse.success(f"Deleted memory: {filename}", {"filename": filename})

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
                ToolParameter("name", "string", "Short memory title; for repair experiences use a descriptive English name."),
                ToolParameter("description", "string", "One-line index description; for repair experiences summarize the observed problem, not the fix."),
                ToolParameter("type", "string", f"One of: {', '.join(item.value for item in MemoryType)}"),
                ToolParameter(
                    "content",
                    "string",
                    "Memory body. Repair experiences include a title, problem_description, usage_scenario and repair lessons. global_experience is cross-repository; repository_experience may retain stable repository APIs and contracts.",
                ),
                ToolParameter(
                    "role",
                    "string",
                    "Optional repair role: coordinator, implement, or review.",
                    required=False,
                ),
                ToolParameter(
                    "provenance",
                    "string",
                    "Optional data provenance/split. Never hide benchmark test provenance.",
                    required=False,
                ),
            ],
            handler=save_memory,
            prompt=(
                "只有当用户明确要求记住信息，或给出可复用偏好/反馈/项目决策时才调用 save_memory；"
                "修复经验只使用 global_experience 或 repository_experience；"
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
                ToolParameter("role", "string", "Updated role", required=False),
                ToolParameter("provenance", "string", "Updated provenance", required=False),
            ],
            handler=update_memory,
            prompt=(
                "如果成功修复案例与已有同类经验相似，优先 read_memory 后用 update_memory 合并新抽象；"
                "如果没有新增可复用策略，不要更新也不要新增。"
            ),
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="delete_memory",
            description="Delete a contradictory, outdated, redundant, or invalid long-term memory.",
            parameters=[ToolParameter("filename", "string", "Existing memory filename to delete")],
            handler=delete_memory,
            prompt=(
                "只在 Semantic Memory 的 REMOVE 操作，或记忆已被成功证据明确否定时使用；"
                "不得为了腾空间随意删除无关但仍有效的经验。"
            ),
            source=ToolSource.BUILTIN,
        ),
    ]


def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    registry.register_many(build_memory_tools(store))
