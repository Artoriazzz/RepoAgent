from pathlib import Path

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore, MemoryType
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
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
        repair_memory_recall: bool = True,
        swebench_profile: bool = False,
    ):
        # Facade 只把面向用户的参数翻译为 QueryEngine 配置。它没有自己的 loop，
        # 因而 CLI、测试或未来 HTTP API 都可以复用同一个会话运行时。
        self.engine = QueryEngine(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            max_steps=max_steps,
            output_dir=Path(output_dir),
            cwd=cwd,
            memory_store=memory_store,
            semantic_memory_recall=semantic_memory_recall,
            excluded_memory_types=set() if repair_memory_recall else {MemoryType.REPAIR_RECIPE},
            memory_prompt_enabled=not swebench_profile,
            memory_recall_enabled=not swebench_profile,
            include_dynamic_context=not swebench_profile,
        )

    def run(self, task: str) -> str:
        """同步执行一个仓库任务。"""

        return self.engine.chat(task)

    def abort(self) -> None:
        self.engine.abort()
