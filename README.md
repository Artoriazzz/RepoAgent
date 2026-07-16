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
  - repair_recipe：成功修复案例抽象出的可复用修复策略
```

对应文件：

- `repo_agent/core/session_memory.py`：当前 run 内的具体短期记忆。
- `repo_agent/core/memory.py`：文件式长期记忆、索引、召回和 freshness warning。
- `repo_agent/core/context.py`：上下文管理和压缩管道。
- `repo_agent/tools/memory_tools.py`：长期记忆工具。

长期记忆默认存储在 `~/.repoagent/projects/{repo_hash}/memory/`，可以通过环境变量 `REPOAGENT_HOME` 改变根目录。每个记忆文件使用 frontmatter，`MEMORY.md` 只作为紧凑索引注入 system prompt。

如果一个仓库会被 checkout 到多个路径，可以用 `--memory-scope` 指定稳定共享边界，例如：

```bash
repo-agent run \
  --repo /path/to/repo \
  --coordinator \
  --memory-scope repo:owner/name \
  --task "修复这个问题"
```

### 持续进化记忆

RepoAgent 支持把成功修复抽象为 `repair_recipe`，用于下次修复前给 Coordinator 提供思路。它不是保存完整补丁，而是保存：

- problem_signatures：如何识别类似问题。
- applicability：适用条件。
- repair_strategy：抽象修复步骤。
- edit_brief_rules：Coordinator 应覆盖哪些文件角色、范围和禁止项。
- verification_contract：必须验证哪些行为。
- anti_patterns：容易漏掉的错误修法。

成功案例通过 `case_curator` 子 Agent 判断是否值得记录。手动保存一次成功修复：

`case_curator` 不会盲目新增记忆。它会先搜索相似 `repair_recipe`：

- 如果没有相似策略，才保存新 recipe。
- 如果已有相似策略，并且新案例提供了新的抽象步骤、验证契约或反模式，则用 `update_memory` 改进旧 recipe。
- 如果没有新增可迁移知识，则什么也不保存。

工具层也会兜底：`save_memory(type="repair_recipe")` 发现相似 recipe 时会拒绝新增，要求读取旧记忆后更新或 no-op。

Coordinator 派发 `implement` 时也会执行 recipe 硬约束校验。比如某条 recipe 的 `edit_brief_rules` 要求覆盖 reader/writer/roundtrip，`agent` 工具会拒绝只覆盖 writer 小范围的 `allowed_ranges`，要求 Coordinator 重新扩大 EditBrief。

```bash
repo-agent curate-repair \
  --repo /path/to/repo \
  --model deepseek-v4-flash \
  --memory-scope repo:owner/name \
  --task-file .repo_agent_runs/latest/task.md \
  --patch-file .repo_agent_runs/latest/model.patch \
  --result "pytest passed"
```

SWE-bench batch 支持在 resolved 后自动沉淀 recipe，但默认关闭：

```bash
python scripts/swebench_batch.py ... \
  --memory-scope 'swebench-dev:{repo}' \
  --evolve-memory
```

正式 SWE-bench 提交前应冻结代码、prompt 和 memory。正式跑分不要使用 `--evolve-memory`，并确保 `repair_recipe` 没有来自同一 test split 的反馈。

上下文管理包含四层：

- 大工具结果超过 30KB 时保存到 `tool-results/`，消息里只保留预览和路径。
- 上下文压力较高时，对历史 tool result 做预算裁剪。
- 利用率更高时 snip 过时的旧读取/搜索/命令结果。
- 输入 token 超过窗口阈值时，用短期记忆生成轻量摘要压缩历史。

## Coordinator 多 Agent

Coordinator 模式把主 Agent 变成纯编排者：

```text
Coordinator
  - 可直接用只读工具做轻量 search/read/diff
  - 写入和复杂验证通过 agent 工具派发 Worker
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
| `implement` | 受限写入，串行 | 按 Coordinator 的 EditBrief 修改指定文件 |
| `patch_review` | 只读门禁 | 提交前检查 diff 的回归风险、契约覆盖和精确输出 |
| `verify` | 验证 | 运行测试、分析失败 |
| `review` | 只读 | 检查 diff、风险和遗漏验证 |
| `memory_curator` | 记忆工具 | 判断并保存值得长期保留的信息 |
| `case_curator` | 记忆工具 | 将成功修复抽象成 `repair_recipe` |
| `general` | 通用 | 处理独立任务，但不能再创建子 Agent |

Coordinator 的关键约束：

- Worker prompt 必须自包含，包含目标、已知事实、相关文件/命令、允许和禁止事项、输出格式。
- 不要写“根据上面的发现”这类依赖隐含上下文的话。
- 写入 Worker 默认串行，避免多个 Agent 同时改同一 worktree。
- 派发 `implement` 必须传结构化 `allowed_files` 和 `allowed_ranges`，否则 `agent` 工具会拒绝。
- `implement` 只拥有受限 `read_file`、`replace_in_file`、`write_file`，不能搜索仓库、看完整 diff、手写 patch 或运行命令。
- 小范围替换必须使用 `replace_in_file`；有稳定行号时优先传 `line_replacements`，否则传精确 old/new，由工具校验替换是否位于 `allowed_ranges` 内。
- Coordinator 给 `implement` 的 old/new 应尽量最小化，默认保留周边 guard 条件、缩进结构和无关逻辑。
- 普通维护任务仍可使用完整 research/review 流程；SWE-bench profile 使用单独的简化流程，避免多个同源 Worker 重复推理。
- SWE-bench 默认流程为：最多 5 次轻量定位、1 个 reproducer、1 个 implement、确定性静态校验、按风险触发的只读 patch_review、最多 1 个 verify。
- SWE-bench 默认 `--candidate-mode off`。`auto` 和 `required` 只保留为消融实验选项，不再自动进入候选池。
- SWE-bench 的 implement 前必须完成一次 reproducer；reproducer 只使用公开题意和仓库内容，给出最小复现、相邻旧行为和公开验证命令。
- `patch_review` 仅在多文件、大语义 diff、公共签名、共享状态/dispatch 或共享核心路径等确定性风险命中时运行。它只做归因，不触发 corrective implement。
- 已经得到非空 patch 后禁止开启第二套方案；验证被环境阻塞时保留当前 patch 并结束。
- SWE-bench 正式评测按 pass@1 执行：每个问题只允许一次 Agent 修复；harness 失败日志只用于离线分析，不会回灌给模型做第二轮修改。
- 高风险动作只能建议，不能执行。

`implement` 的典型 EditBrief：

```json
{
  "problem_contract": {
    "inputs": ["Blueprint endpoint/name 包含点号"],
    "expected_behavior": ["抛出 ValueError，message 保持题意要求"],
    "forbidden_behavior": ["合法 endpoint 和已有自动 endpoint 生成逻辑不变"]
  },
  "semantic_cases": [
    {
      "case": "用户传入含点号 endpoint",
      "before": "可能通过或触发 AssertionError",
      "after": "抛出 ValueError"
    }
  ],
  "old_behavior_invariants": [
    "不含点号的 endpoint 继续注册成功",
    "view_func.__name__ 缺失时保持原有 guard 行为"
  ],
  "allowed_files": ["src/flask/blueprints.py"],
  "allowed_ranges": {
    "src/flask/blueprints.py": [[185, 195], [360, 375]]
  },
  "required_changes": [
    "Blueprint 名称包含点号时抛出 ValueError",
    "endpoint 和 view_func.__name__ 包含点号时抛出 ValueError"
  ],
  "forbidden": ["不要修改测试", "不要安装依赖", "不要运行宽泛命令"]
}
```

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
- 工具结果会保留头尾并截断；仓库读取、搜索、命令输出和 diff 默认使用更低上限，避免批量 SWE-bench 时挤爆上下文。
- Agent loop 内部会先把超过 30KB 的工具结果持久化到输出目录，再做截断。
- 修改已有文件前必须先调用 `read_file`，工具会记录 mtime；如果文件被外部修改，会要求重新读取。
- 每个工具可以自带 prompt 片段，`QueryEngine` 会自动注入 system prompt。

## 输出

运行结束后输出目录会包含：

- `messages.json`：完整对话历史。
- `trace.jsonl`：每一步 LLM 和工具调用记录。
- `model.patch`：当前仓库 diff。
- `validation.json`：最终回答、任务信息和修改文件。
- `stage_attribution.json`：定位、复现、实现、静态校验、条件 review、公开验证及 token 使用情况。

SWE-bench batch 会把 Agent 阶段记录与 harness 合并，并在 `batch_summary.json` 中统计主要归因：

- `implementation_delivery`：没有留下可评测 patch。
- `deterministic_validation`：patch 被语法、测试文件修改或静态策略拦截。
- `semantic_fix_incomplete`：目标 FAIL_TO_PASS 仍失败。
- `public_regression`：目标行为通过但 PASS_TO_PASS 出现回归。
- `semantic_fix_and_regression`：目标行为和旧行为同时失败。
- `harness_environment`：Docker、网络或 harness 执行错误。
- `short_memory.json` / `short_memory.md`：当前 run 的短期记忆快照。
- `tool-results/`：被上下文管理器持久化的大工具结果。
- `subagents/`：Coordinator 模式下各 Worker 的独立输出。

默认输出目录是 `.repo_agent_runs/latest`，可用 `--output-dir` 指定。
