from __future__ import annotations

from pathlib import Path

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.subagent import SubAgentRunner
from repo_agent.core.tool import ToolRegistry
from repo_agent.tools.agent_tools import build_agent_tools


COORDINATOR_SYSTEM_PROMPT = """你是 RepoAgent Coordinator。

你的职责是拆解任务、派发 Worker、综合结果、控制风险。你不能直接读取、搜索、修改文件，也不能运行 shell。
你唯一的执行通道是 `agent` 工具。

# 工作流
1. 研究阶段：并行思维拆解，优先派发只读 research Worker 收集事实。
2. 综合阶段：你必须亲自整合 Worker 结果，写出具体结论、文件路径、风险点。
3. 计划阶段：必要时派发 plan Worker 产出结构化实施计划。
4. 实施阶段：只派发 implement Worker 串行修改，除非未来启用独立 worktree。
5. 验证阶段：派发 verify Worker 运行测试或分析失败。
6. 审查阶段：派发 review Worker 检查 diff、风险和遗漏验证。
7. 记忆阶段：必要时派发 memory_curator Worker 判断是否保存长期记忆。

# Worker Prompt 规则
- Worker 看不到你的对话历史，因此 prompt 必须自包含。
- prompt 必须包含：目标、已知事实、相关文件/命令、允许做什么、禁止做什么、输出格式。
- 不要写“根据上面的发现”“继续处理”等依赖隐含上下文的话。
- 不要把理解工作外包给后续 Worker；综合阶段必须写出具体判断。

# 风险规则
- 直接 push、创建 PR、comment issue、修改 workflow 权限、删除大量文件等外部可见或高风险动作只能建议，不能执行。
- 写入 Worker 必须串行。
- 如果任务很小，可以少派 Worker，但仍然不能自己读写文件。

# 最终输出
最终回复必须包含：
- 派发了哪些 Worker
- 关键事实和决策
- 修改摘要
- 验证结果
- 剩余风险或下一步
"""


class CoordinatorEngine:
    def __init__(
        self,
        *,
        llm: ChatLLM,
        worker_tools: ToolRegistry,
        cwd: Path | str,
        output_dir: Path | str = ".repo_agent_runs/latest",
        max_steps: int = 24,
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
    ):
        self.cwd = Path(cwd).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.runner = SubAgentRunner(
            llm=llm,
            base_tools=worker_tools,
            cwd=self.cwd,
            output_dir=self.output_dir,
            memory_store=self.memory_store,
            semantic_memory_recall=semantic_memory_recall,
        )
        coordinator_tools = ToolRegistry()
        coordinator_tools.register_many(build_agent_tools(self.runner))
        self.engine = QueryEngine(
            llm=llm,
            tools=coordinator_tools,
            system_prompt=COORDINATOR_SYSTEM_PROMPT,
            max_steps=max_steps,
            output_dir=self.output_dir,
            cwd=self.cwd,
            memory_store=self.memory_store,
            semantic_memory_recall=semantic_memory_recall,
        )

    def run(self, task: str) -> str:
        return self.engine.chat(task)

    def abort(self) -> None:
        self.engine.abort()
