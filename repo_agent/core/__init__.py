from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ChatLLM": ("repo_agent.core.llm", "ChatLLM"),
    "ContextConfig": ("repo_agent.core.context", "ContextConfig"),
    "ConstraintProfile": ("repo_agent.core.task_constraints", "ConstraintProfile"),
    "ConstraintRouting": ("repo_agent.core.task_constraints", "ConstraintRouting"),
    "ContinueReason": ("repo_agent.core.query_loop", "ContinueReason"),
    "CoordinatorEngine": ("repo_agent.core.coordinator", "CoordinatorEngine"),
    "ConversationContextManager": ("repo_agent.core.context", "ConversationContextManager"),
    "DEFAULT_SYSTEM_PROMPT": ("repo_agent.core.prompts", "DEFAULT_SYSTEM_PROMPT"),
    "FrontmatterResult": ("repo_agent.core.frontmatter", "FrontmatterResult"),
    "LoopEvent": ("repo_agent.core.query_loop", "LoopEvent"),
    "LoopEventType": ("repo_agent.core.query_loop", "LoopEventType"),
    "MemoryStore": ("repo_agent.core.memory", "MemoryStore"),
    "MemoryType": ("repo_agent.core.memory", "MemoryType"),
    "PermissionResult": ("repo_agent.core.tool", "PermissionResult"),
    "QueryEngine": ("repo_agent.core.query_engine", "QueryEngine"),
    "RecalledMemory": ("repo_agent.core.memory", "RecalledMemory"),
    "RepoAgent": ("repo_agent.core.agent", "RepoAgent"),
    "RepairModePolicy": ("repo_agent.core.experience", "RepairModePolicy"),
    "LightweightValidation": ("repo_agent.core.experience", "LightweightValidation"),
    "RepairBrief": ("repo_agent.core.semantic_evidence", "RepairBrief"),
    "RepairRunMode": ("repo_agent.core.experience", "RepairRunMode"),
    "RepairRecipeApplicability": ("repo_agent.core.repair_memory", "RepairRecipeApplicability"),
    "RepairRecipeRecall": ("repo_agent.core.repair_memory", "RepairRecipeRecall"),
    "ShortTermMemory": ("repo_agent.core.session_memory", "ShortTermMemory"),
    "SubAgentRunner": ("repo_agent.core.subagent", "SubAgentRunner"),
    "SystemPromptContext": ("repo_agent.core.prompts", "SystemPromptContext"),
    "Tool": ("repo_agent.core.tool", "Tool"),
    "ToolParameter": ("repo_agent.core.tool", "ToolParameter"),
    "ToolRegistry": ("repo_agent.core.tool", "ToolRegistry"),
    "ToolResponse": ("repo_agent.core.tool", "ToolResponse"),
    "ToolSource": ("repo_agent.core.tool", "ToolSource"),
    "ToolStatus": ("repo_agent.core.tool", "ToolStatus"),
    "WorkerConstraints": ("repo_agent.core.subagent", "WorkerConstraints"),
    "WorkerResult": ("repo_agent.core.subagent", "WorkerResult"),
    "WorkerSpec": ("repo_agent.core.subagent", "WorkerSpec"),
    "augment_task_with_repair_recipes": ("repo_agent.core.repair_memory", "augment_task_with_repair_recipes"),
    "assess_lightweight_validation": (
        "repo_agent.core.experience",
        "assess_lightweight_validation",
    ),
    "build_repair_brief": ("repo_agent.core.semantic_evidence", "build_repair_brief"),
    "build_case_curator_prompt": ("repo_agent.core.repair_memory", "build_case_curator_prompt"),
    "build_experience_curator_prompt": ("repo_agent.core.experience", "build_experience_curator_prompt"),
    "build_failure_review_prompt": ("repo_agent.core.experience", "build_failure_review_prompt"),
    "build_system_prompt": ("repo_agent.core.prompts", "build_system_prompt"),
    "build_tool": ("repo_agent.core.tool", "build_tool"),
    "evaluate_repair_recipe_applicability": (
        "repo_agent.core.repair_memory",
        "evaluate_repair_recipe_applicability",
    ),
    "format_frontmatter": ("repo_agent.core.frontmatter", "format_frontmatter"),
    "get_worker_spec": ("repo_agent.core.subagent", "get_worker_spec"),
    "independent_validation_succeeded": (
        "repo_agent.core.experience",
        "independent_validation_succeeded",
    ),
    "parse_frontmatter": ("repo_agent.core.frontmatter", "parse_frontmatter"),
    "query_loop": ("repo_agent.core.query_loop", "query_loop"),
    "route_task_constraints": ("repo_agent.core.task_constraints", "route_task_constraints"),
    "repair_recipe_query_from_task": ("repo_agent.core.repair_memory", "repair_recipe_query_from_task"),
    "repair_mode_policy": ("repo_agent.core.experience", "repair_mode_policy"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
