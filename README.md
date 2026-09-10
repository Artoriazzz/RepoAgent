# RepoAgent

一个面向 GitHub 仓库维护与程序修复的多 Agent 系统。项目直接基于 OpenAI-compatible Chat Completions 的 `tools/tool_calls` 协议实现 Agent Loop，不依赖 LangGraph 或高层 Agent SDK。

RepoAgent 能读取公开 Issue 和仓库源码，由 Coordinator 定位问题并生成受限编辑契约，再交给 Implement Worker 完成最小代码修改。系统同时提供上下文压缩、工具权限隔离、短期记忆、双层长期记忆、失败归因和 SWE-bench 批量评测能力。

> 当前项目以工程研究和本地实验为目标。README 中的 SWE-bench 数字是本地开发结果，不是官方排行榜成绩。

## 核心亮点

- **自研 Agent Runtime**：自行管理消息历史、工具调用、step budget、token 统计、trace 与终止条件。
- **Coordinator 多 Agent**：将问题定位、代码实现、补丁审查和验证拆成独立角色，Worker 使用独立上下文与工具白名单。
- **受限代码修改**：Implement 只能修改 Coordinator 指定的文件与行范围，工具层校验越界、测试文件修改和危险命令。
- **双层记忆**：Episodic Memory 保存紧凑修复示例，Semantic Memory 保存跨任务可迁移的修复原则；本地 BM25 召回，无需向量数据库。
- **学习与评测隔离**：`experience-build` 允许离线失败反馈和经验沉淀；`benchmark` 冻结记忆并严格执行单次修复。
- **可观测与可归因**：每次运行输出完整 trace、patch、验证结果、短期记忆和阶段归因。

## 架构

```mermaid
flowchart TD
    U["Issue / CI 日志 / 用户任务"] --> C["Coordinator"]
    M["冻结的 Episodic + Semantic Memory"] --> C
    C -->|"公开题意 + 仓库证据"| R["RepairBrief"]
    R --> I["Scoped Implement Worker"]
    I -->|"受限文件与行范围"| T["Tool Registry"]
    T --> W["Repository Workspace"]
    W --> G["静态检查与 Patch Gate"]
    G --> O["model.patch / trace / validation"]

    C -. "离线学习模式" .-> F["FailureReview"]
    F -.-> CI["Corrective Implement"]
    CI -.-> E["Experience Curator"]
    E -.-> M
```

运行时分为两层：

- `QueryEngine` 管理一次会话的 messages、token、trace、上下文和短期记忆。
- `query_loop` 管理单轮 LLM 调用、function calling、工具结果回填和结束判定。

详细设计见 [架构说明](docs/architecture.md)。

## 快速开始

要求 Python 3.11+。

```bash
git clone https://github.com/Artoriazzz/RepoAgent.git
cd RepoAgent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env` 后，将配置加载到当前 shell：

```bash
set -a
source .env
set +a
```

配置任意 OpenAI-compatible 模型接口：

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-v4-flash"
```

分析或修改本地仓库：

```bash
repo-agent run \
  --repo /path/to/repository \
  --coordinator \
  --task "定位测试失败原因，完成最小修复并运行相关测试"
```

根据 GitHub Issue 修复代码：

```bash
export GITHUB_TOKEN="your-github-token"
repo-agent fix-issue \
  --repo /path/to/repository \
  --issue-url https://github.com/owner/repo/issues/123 \
  --coordinator
```

诊断 GitHub Actions：

```bash
repo-agent repair-ci \
  --repo /path/to/repository \
  --github-repo owner/repo \
  --run-id 123456 \
  --coordinator
```

## 后端 Worker

`repo-agent-worker` 将 Java 后端的 RabbitMQ 任务接入现有 Coordinator，不替代 Agent loop。它读取后端任务上下文，在预同步的本地仓库镜像中创建独立 worktree，运行 `repo-agent run --coordinator`，保存 `model.patch` 并将状态回传为 `PATCH_READY`。

安装 Worker 依赖：

```bash
python -m pip install -e ".[dev,worker]"
```

配置并启动：

```bash
export REPO_AGENT_RABBITMQ_URL='amqp://repoagent:repoagent@localhost:5672/%2F'
export REPO_AGENT_BACKEND_URL='http://localhost:8080'
export REPO_AGENT_WORKER_TOKEN='same-as-REPAIR_WORKER_TOKEN'
export REPO_AGENT_WORK_ROOT='/var/lib/repo-agent'
export REPO_AGENT_MODEL='deepseek-v4-flash'
repo-agent-worker
```

后端会按 `<repository-id>` 查找 `${REPO_AGENT_WORK_ROOT}/mirrors/<repository-id>` 作为 Git mirror 或普通 clone。Worker 创建 `${REPO_AGENT_WORK_ROOT}/worktrees/<task-id>`，并将运行产物写到 `${REPO_AGENT_WORK_ROOT}/runs/<task-id>`。当前没有 GitHub App installation token 时，Worker 不 clone 私有仓库、不推送分支、不创建 PR；产出非空 patch 后停在 `PATCH_READY`，等待后续发布阶段。

## 两种修复模式

### Benchmark：冻结推理

用于正式评测或可复现对比。记忆只读，每道题只生成一个最终 patch，不把 harness 失败反馈送回 Agent。

```bash
repo-agent benchmark \
  --repo /path/to/repository \
  --memory-root /path/to/frozen-memory \
  --memory-scope repo:owner/name \
  --task "公开 Issue 内容"
```

### Experience Build：离线学习

用于开发集、公开 CI 或人工任务。初始修复失败后，允许一次 FailureReview 和一次受限纠正；只有独立验证成功的案例才能写入双记忆。

```bash
repo-agent experience-build \
  --repo /path/to/repository \
  --memory-scope repo:owner/name \
  --provenance public-issue \
  --final-eval-command "python -m pytest -q" \
  --task "公开 Issue 内容"
```

这两个模式在控制器层隔离，避免开发阶段的反馈机制污染 pass@1 评测。

## 工具与安全边界

工具通过 `ToolRegistry` 统一注册、生成 schema、校验参数并分发：

| 工具组 | 能力 |
|---|---|
| Repository | 文件树、范围读取、搜索、受限替换、命令、Git diff |
| GitHub | Issue、仓库元数据、Actions 日志 |
| Memory | 保存、更新、搜索和召回长期记忆 |
| Agent | 派发 Research、Implement、Review、Verify 等 Worker |

关键约束：

- 修改前必须读取文件，并校验文件是否被外部更改。
- Implement Worker 必须携带 `allowed_files` 和 `allowed_ranges`。
- SWE-bench profile 禁止修改测试、fixture 和评测期望文件。
- 敏感文件、主分支 push、危险 shell 命令和越权写入默认拒绝。
- 工具错误转换为结构化 `ToolResponse`，交由 Agent 决定是否恢复。

## 记忆与上下文

短期记忆记录当前 run 的目标、已读/已改文件、搜索结论、验证结果、风险和下一步，并参与长对话压缩。

长期记忆分为：

- **Episodic Memory**：问题特征、关键证据、失败尝试、反馈、成功修改结构和验证结果。
- **Semantic Memory**：分别面向 Coordinator、Implement、Review 的定位原则、语义修复经验和回归风险规则。

记忆默认保存到 `~/.repoagent/projects/<repo_hash>/memory/`。每次运行只注入少量高相关条目，避免把整个记忆库塞进模型上下文。

## 运行产物

默认输出目录为 `.repo_agent_runs/latest/`：

```text
messages.json             完整消息历史
trace.jsonl               LLM 与工具调用轨迹
model.patch               最终 Git diff
validation.json           验证命令与结果
stage_attribution.json    阶段归因与 token 统计
short_memory.json         当前运行的短期记忆
subagents/                Worker 独立运行记录
```

这些产物可以用于复盘“定位错误、实现错误、回归风险或环境失败”发生在哪个阶段。

## SWE-bench Lite 本地实验

本地开发实验累计生成了 300 条 prediction 和 trajectory，聚合结果为 **118/300 resolved（39.33%）**。其中三个区间并非由同一个冻结版本生成，因此这个数字只用于工程分析，不能作为官方排行榜结果。

同任务区间的迭代实验中，Lite 10-99 的空 patch 从 25 降至 2，harness error 从 5 降至 0，resolved 从 39 提升到 46。实验表明当前主要瓶颈已经从 patch 交付转向精确语义修复与旧行为保持。

完整口径、仓库分布、成本和局限见 [SWE-bench Lite 技术报告](docs/swebench-lite-technical-report.md)。

## 项目结构

```text
repo_agent/
├── cli.py                       CLI 与模式控制器
├── core/
│   ├── query_engine.py          会话级运行时
│   ├── query_loop.py            单轮工具调用循环
│   ├── coordinator.py           Coordinator 编排
│   ├── subagent.py              Worker 隔离与权限
│   ├── semantic_evidence.py     RepairBrief 与语义证据
│   ├── experience.py            学习/评测模式与失败反馈
│   └── memory.py                长期记忆与本地检索
└── tools/
    ├── catalog.py               工具导入与组装
    ├── repo_tools.py            仓库工具
    ├── github_tools.py          GitHub 工具
    └── agent_tools.py           Worker 调度与安全门禁
```

## 工程质量

- 309 个单元与集成测试，覆盖 Agent Loop、工具权限、Coordinator、记忆、语义证据、SWE-bench 批处理和后端 Worker 编排。
- CI 在 Python 3.11 和 3.12 上运行完整测试。
- 不保存 API Key；`.env`、运行轨迹和 benchmark 工作目录默认被忽略。
- 批处理会记录代码版本、模式、模型、评测资格与实例级阶段归因。

## 设计取舍

RepoAgent 没有追求无限反思或不断增加 Reviewer。当前默认路径强调一次定位、一次受限实现和确定性安全检查；复杂的失败反馈只存在于离线学习模式。这样做的目标是让每个阶段可解释、可测试、可消融，也让 token 成本和评测边界更容易控制。

## License

[MIT](LICENSE)
