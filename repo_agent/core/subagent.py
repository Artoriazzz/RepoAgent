from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolRegistry


SubAgentType = Literal["research", "plan", "implement", "verify", "review", "memory_curator", "general"]


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    description: str
    allowed_tools: set[str] | None
    system_prompt: str
    max_steps: int = 12
    can_write: bool = False


@dataclass(frozen=True)
class WorkerResult:
    type: str
    description: str
    text: str
    output_dir: Path
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


RESEARCH_TOOLS = {
    "list_files",
    "read_file",
    "search_repo",
    "git_diff",
    "get_issue",
    "get_repo_metadata",
    "download_workflow_logs",
    "list_memories",
    "read_memory",
    "search_memories",
}

PLAN_TOOLS = {
    "list_files",
    "read_file",
    "search_repo",
    "git_diff",
    "list_memories",
    "read_memory",
    "search_memories",
}

IMPLEMENT_TOOLS = {
    "read_file",
    "search_repo",
    "write_file",
    "apply_patch",
    "git_diff",
    "run_command",
}

VERIFY_TOOLS = {
    "read_file",
    "search_repo",
    "run_command",
    "git_diff",
}

MEMORY_TOOLS = {
    "list_memories",
    "read_memory",
    "search_memories",
    "save_memory",
}


RESEARCH_PROMPT = """你是 RepoAgent 的 Research Worker。

你是只读 Worker，负责快速收集事实，不做修改。
规则：
- 只能读取、搜索、查看 diff / issue / CI logs / memory。
- 不要尝试修改文件、运行写入命令或创建长期记忆。
- 输出必须包含具体文件路径、关键发现、证据摘要。
- 如果信息不足，明确说明还缺什么。
"""


PLAN_PROMPT = """你是 RepoAgent 的 Plan Worker。

你是只读 Worker，负责把已知事实转成可执行计划。
输出格式：
1. 当前事实
2. 关键文件
3. 实施步骤
4. 验证命令
5. 风险与回滚点

不要写“根据上面的发现”；你的输出必须自包含。
"""


IMPLEMENT_PROMPT = """你是 RepoAgent 的 Implement Worker。

你负责按 Coordinator 给出的明确范围实施修改。
规则：
- 只修改 prompt 明确允许的文件或文件集。
- 修改已有文件前必须 read_file。
- 优先 apply_patch 做局部修改，只有新文件或小文件完整重写才用 write_file。
- 修改后返回改动摘要和 git_diff 摘要。
- 不要创建 PR、push、comment issue 或保存长期记忆。
"""


VERIFY_PROMPT = """你是 RepoAgent 的 Verify Worker。

你负责运行验证命令并分析结果。
规则：
- 优先运行 Coordinator 指定的命令。
- 可以读取文件和搜索代码来解释失败原因。
- 默认不修改文件。
- 输出必须包含命令、退出码、成功/失败摘要、下一步建议。
"""


REVIEW_PROMPT = """你是 RepoAgent 的 Review Worker。

你是只读审查 Worker，负责检查当前 diff。
重点：
- 是否有明显 bug 或行为回归。
- 是否遗漏测试或验证。
- 是否触碰敏感文件、workflow 权限或高风险操作。
- 是否改动范围超出用户请求。

输出 findings 优先，按严重程度排序；没有问题时明确说明。
"""


MEMORY_CURATOR_PROMPT = """你是 RepoAgent 的 Memory Curator Worker。

你负责判断本轮是否有值得长期保存的信息。
只保存无法从当前仓库、Git 历史或项目指令推导的信息。
可保存类型：
- user：用户偏好、背景
- feedback：用户纠正或肯定，正文包含 Why 和 How to apply
- project：项目决策、进度、绝对日期
- reference：外部系统位置

不要保存技术栈、文件路径、测试命令、代码结构或一次性任务细节。
"""


GENERAL_PROMPT = """你是 RepoAgent 的 General Worker。

你处理 Coordinator 分配的独立任务。你的上下文是隔离的，必须只根据当前 prompt 和工具结果行动。
完成后返回简洁、可执行、包含证据的结果。
不要再启动子 Agent。
"""


WORKER_SPECS: dict[str, WorkerSpec] = {
    "research": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT),
    "explore": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT),
    "plan": WorkerSpec("plan", "只读结构化计划", PLAN_TOOLS, PLAN_PROMPT),
    "implement": WorkerSpec("implement", "串行代码修改", IMPLEMENT_TOOLS, IMPLEMENT_PROMPT, max_steps=16, can_write=True),
    "verify": WorkerSpec("verify", "验证与失败分析", VERIFY_TOOLS, VERIFY_PROMPT, max_steps=12),
    "review": WorkerSpec("review", "提交前只读审查", VERIFY_TOOLS, REVIEW_PROMPT, max_steps=10),
    "memory_curator": WorkerSpec("memory_curator", "长期记忆维护", MEMORY_TOOLS, MEMORY_CURATOR_PROMPT, max_steps=8),
    "general": WorkerSpec("general", "通用独立任务", None, GENERAL_PROMPT, max_steps=14, can_write=True),
}


class SubAgentRunner:
    def __init__(
        self,
        *,
        llm: ChatLLM,
        base_tools: ToolRegistry,
        cwd: Path | str,
        output_dir: Path | str,
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
    ):
        self.llm = llm
        self.base_tools = base_tools
        self.cwd = Path(cwd).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.semantic_memory_recall = semantic_memory_recall

    def run(self, agent_type: str, prompt: str, description: str = "sub-agent task") -> WorkerResult:
        spec = get_worker_spec(agent_type)
        worker_tools = self._tools_for(spec)
        worker_output_dir = self.output_dir / "subagents" / f"{int(time.time() * 1000)}-{_safe_name(spec.name)}"
        engine = QueryEngine(
            llm=self.llm,
            tools=worker_tools,
            system_prompt=spec.system_prompt,
            max_steps=spec.max_steps,
            output_dir=worker_output_dir,
            cwd=self.cwd,
            memory_store=self.memory_store,
            semantic_memory_recall=self.semantic_memory_recall,
        )
        text = engine.chat(_worker_prompt(description, prompt))
        return WorkerResult(
            type=spec.name,
            description=description,
            text=text or "(Worker produced no final text.)",
            output_dir=worker_output_dir,
            prompt_tokens=engine.total_prompt_tokens,
            completion_tokens=engine.total_completion_tokens,
            total_tokens=engine.total_tokens,
        )

    def _tools_for(self, spec: WorkerSpec) -> ToolRegistry:
        if spec.allowed_tools is None:
            return self.base_tools.excluding({"agent"})
        return self.base_tools.subset(spec.allowed_tools)


def get_worker_spec(agent_type: str | None) -> WorkerSpec:
    return WORKER_SPECS.get((agent_type or "general").strip(), WORKER_SPECS["general"])


def _worker_prompt(description: str, prompt: str) -> str:
    return (
        f"任务简介：{description}\n\n"
        "下面是 Coordinator 分配给你的完整任务。你看不到主 Agent 的其他上下文，"
        "因此必须只基于这里的信息和工具结果完成工作。\n\n"
        f"{prompt}"
    )


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80] or "worker"
