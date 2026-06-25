from pathlib import Path

from repo_agent.core.llm import ChatLLM
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolRegistry


class RepoAgent:
    """对外兼容的 Agent facade。

    真正的双层架构在 QueryEngine + query_loop：
    - QueryEngine 管会话生命周期
    - query_loop 管单次查询的 LLM/工具循环
    """

    def __init__(
        self,
        llm: ChatLLM,
        tools: ToolRegistry,
        system_prompt: str | None = None,
        max_steps: int = 20,
        output_dir: Path | str = ".repo_agent_runs/latest",
        cwd: Path | str | None = None,
        semantic_memory_recall: bool = False,
    ):
        self.engine = QueryEngine(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            max_steps=max_steps,
            output_dir=Path(output_dir),
            cwd=cwd,
            semantic_memory_recall=semantic_memory_recall,
        )

    def run(self, task: str) -> str:
        return self.engine.chat(task)

    def abort(self) -> None:
        self.engine.abort()
