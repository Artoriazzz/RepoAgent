from __future__ import annotations

from repo_agent.core.subagent import SubAgentRunner
from repo_agent.core.tool import Tool, ToolParameter, ToolResponse, ToolSource, build_tool


def build_agent_tools(runner: SubAgentRunner) -> list[Tool]:
    def launch_agent(inp: dict) -> ToolResponse:
        agent_type = inp.get("type") or "general"
        description = inp.get("description") or "sub-agent task"
        prompt = inp["prompt"]
        result = runner.run(agent_type=agent_type, prompt=prompt, description=description)
        text = (
            f"Sub-agent [{result.type}] completed: {result.description}\n\n"
            f"{result.text}\n\n"
            f"Output dir: {result.output_dir}\n"
            f"Tokens: prompt={result.prompt_tokens}, completion={result.completion_tokens}, total={result.total_tokens}"
        )
        return ToolResponse.success(
            text,
            {
                "type": result.type,
                "description": result.description,
                "output_dir": str(result.output_dir),
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            },
        )

    return [
        build_tool(
            name="agent",
            description=(
                "Launch a sub-agent Worker and return its result. "
                "Types: research/explore, plan, implement, verify, review, memory_curator, general."
            ),
            parameters=[
                ToolParameter("description", "string", "Short 3-8 word description of the worker task"),
                ToolParameter("prompt", "string", "Self-contained detailed instructions for the worker"),
                ToolParameter("type", "string", "Worker type. Default: general", required=False),
            ],
            handler=launch_agent,
            prompt=(
                "Coordinator 只能通过 agent 工具派发 Worker。Worker prompt 必须自包含，"
                "包含目标、已知事实、文件路径、输出格式和约束；不要写“根据上面的发现”。"
            ),
            source=ToolSource.BUILTIN,
        )
    ]
