# RepoAgent Plain

这是一个不依赖 OpenAI Agents SDK、LangGraph 等高封装框架的仓库 Agent 示例项目。

它参考 `HelloAgents-main` 的工程思路，但只保留最小核心：

- `ToolResponse`：统一工具返回协议。
- `ToolRegistry`：工具池、schema 生成、参数校验和安全分发。
- `ToolBundle` / `ToolImportContext`：按上下文组装工具，不在 CLI 中硬编码具体工具模块。
- `ChatLLM`：直接调用 OpenAI-compatible Chat Completions API。
- `RepoAgent`：自己维护对话历史，自己实现 agent loop。
- `repo_tools`：仓库读写、搜索、运行命令、生成 diff。
- `github_tools`：读取 issue、读取仓库信息、下载 GitHub Actions 日志。
- `memory_tools`：保存、读取和搜索长期记忆。

## 安装

```bash
cd repo_agent_plain
python -m pip install -e .[dev]
```

## 环境变量

```bash
export OPENAI_API_KEY="你的 API Key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4.1"
export GITHUB_TOKEN="可选，访问 GitHub API 时使用"
```

`OPENAI_BASE_URL` 可以换成任意 OpenAI 兼容接口。

## 使用

分析并修改本地仓库：

```bash
repo-agent run --repo /path/to/repo --task "分析项目并补充 README"
```

修复 GitHub issue：

```bash
repo-agent fix-issue --repo /path/to/repo --issue-url https://github.com/owner/repo/issues/1
```

诊断 CI：

```bash
repo-agent repair-ci --repo /path/to/repo --github-repo owner/repo --run-id 123456
```

默认长期记忆使用本地关键词召回，不额外消耗 API。需要语义召回时可以加：

```bash
repo-agent run --repo /path/to/repo --task "修复测试" --semantic-memory
```

复杂任务可以启用 Coordinator 模式：

```bash
repo-agent fix-issue --repo /path/to/repo --issue-url https://github.com/owner/repo/issues/1 --coordinator
```

## 双层 Agent Loop

核心循环拆成两层：

```text
QueryEngine（会话级）
  - 接收用户输入
  - 维护 messages
  - 累计 token 统计
  - 写 trace / 自动保存
  - 提供 abort()

query_loop（单轮级）
  - 调用 LLM function calling
  - 检查 tool_calls
  - 执行 ToolRegistry
  - 将 tool observation 写回 messages
  - 产出 LoopEvent
  - 没有工具调用或调用 finish 时结束
```

对应文件：

- `repo_agent/core/query_engine.py`
- `repo_agent/core/query_loop.py`
- `repo_agent/core/agent.py`

这里没有使用 Agents SDK。模型工具调用只使用 OpenAI Chat Completions 原生 `tools` / `tool_calls` 协议。

## 系统提示词

系统提示词采用“稳定模板 + 动态上下文”的 builder 方式生成：

```text
SYSTEM_PROMPT_TEMPLATE
  - Identity / System
  - Doing Tasks
  - Executing Actions With Care
  - Using Tools
  - Tone And Style
  - Output Efficiency

build_system_prompt()
  - 注入工作目录、日期、平台、Shell
  - 注入 Git 分支、最近提交、工作区状态
  - 加载 REPOAGENT.md / AGENTS.md / CLAUDE.md
  - 加载 .repoagent/rules/*.md / .agents/rules/*.md / .claude/rules/*.md
  - 注入工具自身 prompt 规则
```

对应文件：

- `repo_agent/core/prompts.py`
- `repo_agent/core/query_engine.py`

项目指令文件支持 `@./path`、`@~/path`、`@/absolute/path` include，带循环引用和最大深度保护。敏感文件名如 `.env`、私钥文件会被跳过。

## 记忆与上下文

项目包含两类记忆：

```text
ShortTermMemory（短期）
  - 当前用户目标
  - 已读文件、mtime/摘要
  - 已修改文件和修改原因
  - 搜索结果、issue/CI 诊断摘要
  - 验证命令、退出码、结果摘要
  - 风险动作、阻塞点、下一步

MemoryStore（长期）
  - user：用户偏好和背景
  - feedback：用户对 Agent 行为的纠正或肯定
  - project：无法从代码推导的项目决策、进度、绝对日期
  - reference：外部系统或资料位置
```

对应文件：

- `repo_agent/core/session_memory.py`：当前 run 内的具体短期记忆。
- `repo_agent/core/memory.py`：文件式长期记忆、索引、召回和 freshness warning。
- `repo_agent/core/context.py`：上下文管理和压缩管道。
- `repo_agent/tools/memory_tools.py`：长期记忆工具。

长期记忆默认存储在 `~/.repoagent/projects/{repo_hash}/memory/`，可以通过环境变量 `REPOAGENT_HOME` 改变根目录。每个记忆文件使用 frontmatter，`MEMORY.md` 只作为紧凑索引注入 system prompt。

上下文管理包含四层：

- 大工具结果超过 30KB 时保存到 `tool-results/`，消息里只保留预览和路径。
- 上下文压力较高时，对历史 tool result 做预算裁剪。
- 利用率更高时 snip 过时的旧读取/搜索/命令结果。
- 输入 token 超过窗口阈值时，用短期记忆生成轻量摘要压缩历史。

## Coordinator 多 Agent

Coordinator 模式把主 Agent 变成纯编排者：

```text
Coordinator
  - 只能调用 agent 工具
  - 不能直接 read/write/search/run shell
  - 负责拆任务、派 Worker、综合结果、控制风险

Worker
  - 独立 QueryEngine
  - 独立 messages / trace / short memory
  - 按类型获得不同工具白名单
  - 完成后把结果文本返回 Coordinator
```

对应文件：

- `repo_agent/core/coordinator.py`：CoordinatorEngine 和协调器系统提示词。
- `repo_agent/core/subagent.py`：WorkerSpec、SubAgentRunner、Worker 类型和工具白名单。
- `repo_agent/tools/agent_tools.py`：`agent` 工具。

内置 Worker 类型：

| 类型 | 权限 | 用途 |
|------|------|------|
| `research` / `explore` | 只读 | 读取 issue/CI、搜索代码、收集事实 |
| `plan` | 只读 | 输出结构化实施计划 |
| `implement` | 写入，串行 | 按 Coordinator 明确范围修改代码 |
| `verify` | 验证 | 运行测试、分析失败 |
| `review` | 只读 | 检查 diff、风险和遗漏验证 |
| `memory_curator` | 记忆工具 | 判断并保存值得长期保留的信息 |
| `general` | 通用 | 处理独立任务，但不能再创建子 Agent |

Coordinator 的关键约束：

- Worker prompt 必须自包含，包含目标、已知事实、相关文件/命令、允许和禁止事项、输出格式。
- 不要写“根据上面的发现”这类依赖隐含上下文的话。
- 写入 Worker 默认串行，避免多个 Agent 同时改同一 worktree。
- 高风险动作只能建议，不能执行。

## 工具系统

工具导入采用三层流水线：

```text
get_all_base_tool_bundles()
  - 核心仓库工具直接导入
  - GitHub 集成按上下文条件导入

get_tools()
  - 按 simple_mode、denied_tools 等运行时规则过滤

assemble_tool_pool()
  - 内置工具和集成工具分区排序
  - 按名称去重
  - 生成最终 ToolRegistry
```

对应文件：

- `repo_agent/core/tool.py`：工具契约、fail-closed 默认值、参数校验、结果截断。
- `repo_agent/tools/catalog.py`：工具导入和组装流水线。
- `repo_agent/tools/repo_tools.py`：仓库工具 bundle。
- `repo_agent/tools/github_tools.py`：GitHub 工具 bundle。
- `repo_agent/tools/memory_tools.py`：记忆工具 bundle。
- `repo_agent/tools/agent_tools.py`：Coordinator 派发 Worker 的 agent 工具。

几个重要规则：

- 工具错误返回 `ToolResponse(status="error")`，不会向外抛给 agent loop；模型可以据此自我纠正。
- 工具结果超过 50K 字符会保留头尾并截断，避免挤爆上下文。
- Agent loop 内部会先把超过 30KB 的工具结果持久化到输出目录，再做截断。
- 修改已有文件前必须先调用 `read_file`，工具会记录 mtime；如果文件被外部修改，会要求重新读取。
- 每个工具可以自带 prompt 片段，`QueryEngine` 会自动注入 system prompt。

## 输出

运行结束后输出目录会包含：

- `messages.json`：完整对话历史。
- `trace.jsonl`：每一步 LLM 和工具调用记录。
- `model.patch`：当前仓库 diff。
- `validation.json`：最终回答、任务信息和修改文件。
- `short_memory.json` / `short_memory.md`：当前 run 的短期记忆快照。
- `tool-results/`：被上下文管理器持久化的大工具结果。
- `subagents/`：Coordinator 模式下各 Worker 的独立输出。

默认输出目录是 `.repo_agent_runs/latest`，可用 `--output-dir` 指定。
