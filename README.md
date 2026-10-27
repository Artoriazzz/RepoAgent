<div align="center">

# RepoAgent

### 把一个 Issue，交给一支各司其职的修复小队。

**定位有依据 · 修改有边界 · 失败有反馈 · 成功有经验**

[快速开始](#快速开始) · [认识修复小队](#认识修复小队) · [运行流程](#运行流程) · [经验与检索](#经验与检索) · [深入代码](#深入代码)

</div>

---

RepoAgent 是面向软件仓库的多 Agent 程序修复系统。它把公开 Issue、源码和工具组合起来，由协调者决定修复方向，由实现者完成受限修改，再用独立验证判断结果。

每个角色拥有独立上下文，只接收完成本阶段所需的信息：**定位的人决定改哪里，写代码的人落实方案，分析失败的人依据证据纠偏，整理经验的人记录验证成功的知识。**

项目直接使用 OpenAI-compatible Chat Completions 的 `tools/tool_calls` 协议，自行实现 Agent Loop、工具权限、上下文管理和角色调度，**不依赖 LangGraph 或高层 Agent SDK**。这里使用的 `openai` 包仅是模型接口客户端。

> 当前仓库包含 CLI、Python 后端 Worker 和 SWE-bench 研究工具。Java/Spring Boot 平台位于独立的 [backend 仓库](https://github.com/Artoriazzz/backend)。

## 为什么是多 Agent

| 常见困境 | RepoAgent 的做法 |
| --- | --- |
| 找了一大圈代码，仍不知道真正应该修改哪里 | Coordinator 围绕关键未知读取源码，形成明确的行为契约和 EditBrief |
| 改掉异常，却破坏了另一条合法路径 | 交接时显式列出需要保持的旧行为；独立评测检查目标行为与回归 |
| 修复失败后，把全部日志原样塞回模型 | FailureReview 提取具体失败事实，给出结构化修正依据和范围 |
| 每个任务都从零开始 | 成功后抽象全局经验与仓库经验，供下一次任务检索参考 |
| 多任务之间串上下文、串工作目录 | 无状态 Runtime 复用客户端，一次性 Session 隔离历史、工具状态与工作区 |

## 认识修复小队

### 主线角色

| 角色 | 它负责什么 | 交付物 |
| --- | --- | --- |
| **Coordinator：定位与决策** | 理解公开问题，确认错误操作与契约所属层，决定修改方案和授权范围；证据不足时如实停止 | Problem Contract、源码证据、EditBrief、允许文件与行范围 |
| **Initial Implement：首次实现** | 在授权范围内落实方案；发现反证时报告，不擅自选择另一套修法 | 初始 patch、完成状态、已知风险 |
| **FailureReview：失败归因** | 读取提供的失败日志与允许读取的源码，区分语义错误、遗漏路径和环境故障 | 证据清单、失效假设、修正建议、是否需要扩大范围 |
| **Corrective Implement：纠正实现** | 沿用实现角色，根据失败归因和新授权，对已有 patch 做定向修改 | 纠正 patch、修改说明和剩余风险 |
| **ExperienceCurator：经验整理** | 仅在独立验证成功后提炼经验，检索已有项并选择新增、合并或不保存 | 全局/仓库经验及维护操作摘要 |

**Corrective Implement 是实现角色在纠正阶段的独立调用，受本次授权范围约束。Harness 是独立的执行与评分程序。**

### 按需调用的辅助角色

- **MemoryQueryAgent**：仅从公开 Issue 提取 `problem_description`、`usage_scenario`，供检索使用；不读取源码，不共享 Coordinator 对话。
- **Coordinator Replan**：原授权不足时，由协调者重新确认修正方案与范围，再交给实现者执行。
- **Research / Reproducer / TestExpander**：提供只读定位、公开复现或补充验证设计。
- **PatchReview / Verify / PatchRanker**：用于需要审查、运行验证或比较候选的可选路径。

默认 SWE-bench 简化主线按运行入口、模式和预算选择辅助角色，控制额外分析的调用次数与上下文用量。

## 运行流程

### 模式一：从失败中学习

```mermaid
flowchart TD
    Issue["公开 Issue + 基线仓库"] --> C["Coordinator<br/>定位根因 / 决定方案 / 授权范围"]
    C --> I["Initial Implement<br/>落实最小修改"]
    I --> H1["Harness<br/>独立评测初始 patch"]
    H1 -->|"通过"| E["ExperienceCurator<br/>抽象 / 去重 / 保存经验"]
    H1 -->|"失败且可反馈"| F["FailureReview<br/>从失败事实形成纠正依据"]
    F -->|"原范围足够"| CI["Corrective Implement<br/>定向修正"]
    F -->|"需要新范围且允许重规划"| R["Coordinator Replan<br/>重新确认方案与权限"]
    R --> CI
    CI --> H2["Harness<br/>独立评测纠正 patch"]
    H2 -->|"通过"| E
    H2 -->|"未通过"| S["保存结果与失败归因<br/>停止，或按入口预算继续纠正"]
    F -->|"无可执行修正依据"| S
```

这条路径用于离线经验构建。`experience-build` 关闭历史经验召回，依据本次任务的独立验证结果决定是否保存经验；仅验证成功的修复进入经验整理阶段。

**不同入口的纠正预算不同**：CLI/后端阶段流水线支持一次反馈纠正；`scripts/swebench_batch.py` 的学习路径可执行最多两次纠正，并在需要时尝试 replan。具体行为以运行入口和预算配置为准。

图中的 Harness 流程需要配置评测器。普通 `repo-agent run` 不会自动安装 SWE-bench、准备镜像或运行完整官方评测。CLI 的 `experience-build` 还支持显式传入公开验证与最终评测命令。

### 模式二：带着经验，一次出手

```mermaid
flowchart LR
    Q["公开 Issue"] --> MQ["独立查询提取"]
    MQ --> Search["经验检索"]
    G[("全局经验")] --> Search
    M[("当前仓库经验")] --> Search
    Search -->|"各类 Top-1"| C["Coordinator<br/>结合当前源码复核"]
    Q --> C
    C --> I["Initial Implement"]
    I --> P["唯一初始 patch"]
    P --> H["独立 Harness 评测"]
```

`benchmark` 模式只读召回，不做失败反馈纠正，不写入记忆。评测结果只用于统计，不回流给修复 Agent。

**实验合规需要审计完整流程**：记忆来源、历史调优、是否使用同题评测反馈，以及补丁选择规则都需如实披露。

## 经验与检索

### 两类经验，各司其职

| 类型 | 帮助解决什么 | 保存什么 |
| --- | --- | --- |
| **全局经验** `global_experience` | 在其他仓库识别同类机制 | 问题机制、适用条件、通用修复步骤、验证思路、已证伪做法 |
| **仓库经验** `repository_experience` | 在当前仓库更快找到正确实现层 | 稳定模块/API、调用关系、辅助函数、仓库契约、公开验证方法 |

新经验以“标题 → 问题描述 → 使用场景 → 机制与适用条件 → 修复步骤 → 验证思路”组织，从已验证的修复中提炼可复用做法。已有同类知识时优先合并；没有新知识可以不保存。

### 先检索问题，再参考解法

历史侧与查询侧都使用 `problem_description` 和 `usage_scenario` 表达问题。检索描述回答“遇到了什么、在什么条件下发生”，经验正文回答“如何处理、哪些做法有风险”。

```text
当前公开 Issue
    ↓ 独立查询提取
问题描述 + 使用场景
    ↓ embedding + 相似度排序
全局 Top-1 + 当前仓库 Top-1
    ↓ 仅在 Coordinator 开始时注入
结合当前题意和源码复核，再形成 EditBrief
```

**当前接入范围需要区分：**

- **普通 CLI**：本地经验仍通过 `MemoryStore` 的词法检索召回。`--memory-query-model` 启用查询提取，**不会单独启用向量检索**。
- **向量实验入口**：`scripts/vector_memory_agent.py` 在召回阶段提取查询，用已有 embedding 环境编码，再按向量相似度对冻结经验库排名。现有实验采用 BAAI/bge-small-zh-v1.5、L2 归一化、分块余弦比较，各类 Top-1；题号仅用于事后命中审计。
- **后端 Worker**：通过业务后端的经验检索 API 召回固定 revision；与 CLI 本地文件记忆是不同的存储路径。

查询提取与 Coordinator 的上下文隔离。查询 JSON 无效时允许一次纯格式修正，保留完整原始 Issue；格式修正过程不注入 Coordinator。实验向量入口在提取仍失败时明确报错，不退回按题号匹配。

## 自研 Agent Loop：会话层与执行层

```mermaid
sequenceDiagram
    participant E as QueryEngine（会话层）
    participant L as query_loop（执行层）
    participant M as LLM
    participant T as ToolRegistry
    E->>L: 消息 + 工具 + 预算
    loop 直到完成、阻塞或预算耗尽
        L->>M: 请求流式响应
        M-->>L: 文本 / tool_calls
        L->>T: 参数校验与权限检查
        T-->>L: 结构化工具结果
        L-->>E: 事件、用量、阶段进度
    end
    E->>E: 保存历史、短期记忆与运行产物
```

- **会话层**：管理消息历史、短期状态、上下文裁剪、工具大结果落盘与 trace。
- **执行层**：处理流式分片、工具调用拼接、观察结果回填、停止条件与结构化输出修正。
- **工具层**：声明式 schema、工具白名单、文件/行范围授权、读取一致性和危险操作检查。
- **并发层**：按角色复用无状态 Runtime；任务使用独立 Session，池耗尽时等待，不复用另一题的历史。

这些限制作用于应用层。**操作系统级隔离需要额外配置容器等沙箱环境**。本地命令可能影响宿主机，请使用独立 worktree 管理改动，并通过容器隔离不可信执行；SWE-bench Harness 环境使用 Docker。

## 快速开始

需要 Python 3.11+、Git，建议安装 ripgrep。Docker 仅在使用 Docker 评测环境时需要。

```bash
git clone https://github.com/Artoriazzz/RepoAgent.git
cd RepoAgent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env` 中的模型配置，然后加载到 shell。CLI 不自动读取 `.env`：

```bash
set -a
source .env
set +a
```

最少配置三个变量，下面是接口示例；也可换成其他兼容服务：

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
export OPENAI_MODEL="glm-5.3-flash"
```

### 修复一个本地问题

```bash
repo-agent run \
  --repo /path/to/repository \
  --coordinator \
  --task "定位这个异常的根因，保留相关合法输入的行为，完成最小修复" \
  --output-dir .repo_agent_runs/my-fix
```

### 从 Issue 或 CI 进入

```bash
# 私有仓库或 GitHub API 请求可配置 GITHUB_TOKEN。
repo-agent fix-issue \
  --repo /path/to/repository \
  --issue-url https://github.com/owner/repo/issues/123 \
  --coordinator

repo-agent repair-ci \
  --repo /path/to/repository \
  --github-repo owner/repo --run-id 123456 \
  --coordinator
```

### 调整角色模型与思考预算

```bash
repo-agent run \
  --repo /path/to/repository --coordinator \
  --task "修复公开问题描述中的行为" \
  --model glm-5.3-flash \
  --coordinator-reasoning-effort high \
  --implement-reasoning-effort low \
  --reasoning-effort low \
  --max-steps 16
```

可用 `--implement-model` 单独切换实现模型。思考参数如何生效取决于模型供应商；代码对 GLM、DeepSeek 等协议差异做适配，不假定每个服务都接受同一套参数。

### 单独检查检索查询

```bash
repo-agent extract-memory-query \
  --issue-file /path/to/public-issue.md \
  --model glm-5.3-flash \
  --output-dir .repo_agent_runs/query
```

只执行查询提取，不启动 Coordinator、实现或评测。修复时可用 `--memory-query-model` 启用，接口与密钥还可通过 `REPO_AGENT_MEMORY_QUERY_BASE_URL`、`REPO_AGENT_MEMORY_QUERY_API_KEY` 单独配置。

## 作为服务组件运行

RepoAgent 提供 CLI 和后端 Worker 两种运行入口。

```text
Spring Boot 业务后端
    ↓ RabbitMQ 阶段消息
Python Worker → 独立 Git worktree → 多 Agent 修复 → Harness
    ↓ 状态 / 心跳 / 补丁 / 评测 / 经验提案
后端任务记录与监控页面
```

Worker 支持任务 claim 与续租、心跳、取消、连接重试、阶段状态落盘，以及回放已有结果用于无模型额度联调。高层阶段可以使用独立队列；每角色并发配置限制为 1–3，用于提高多任务吞吐量。单题中有依赖关系的阶段按顺序执行。

```bash
python -m pip install -e ".[worker,benchmark]"

export REPO_AGENT_RABBITMQ_URL='amqp://repoagent:repoagent@localhost:5672/%2F'
export REPO_AGENT_BACKEND_URL='http://localhost:8080'
export REPO_AGENT_WORKER_TOKEN='replace-with-your-shared-worker-token'
export REPO_AGENT_WORK_ROOT='/var/lib/repo-agent'
export REPO_AGENT_MODEL='glm-5.3-flash'
export REPO_AGENT_MEMORY_MODE='recall'
repo-agent-worker
```

启动前需要运行配套后端与 RabbitMQ，并按后端仓库说明同步仓库镜像。后端 Worker 通过 `REPO_AGENT_MEMORY_MODE` 配置 `off`、`recall`、`learn`、`evolve`；CLI 则通过 `--repair-mode` 选择修复策略，两者分别控制各自入口。

详细部署与业务设计见 [后端仓库](https://github.com/Artoriazzz/backend) 和 [平台设计](docs/defect-repair-platform-design.md)。

## 每次修复，都留下可检查的现场

按入口和阶段不同，运行目录会包含：

| 产物 | 可以检查什么 |
| --- | --- |
| `model.patch` | 实际准备提交的代码改动 |
| `trace.jsonl`、`messages.json` | 原始调用、工具观察和会话历史 |
| `stage_attribution.json` | 定位、实现、验证等阶段状态与 Token 用量 |
| `memory-query/` | 独立检索查询的输入、输出与格式修正记录 |
| `memory_retrieval.json`、`vector_coordinator_input.md` | 向量实验的完整排名与实际注入内容 |
| `initial-evaluation/`、`experience-correction/` | 配置 Harness 的学习批处理中的评测与纠正记录 |

各入口按实际执行阶段生成对应文件。发布日志前应检查密钥、业务数据和源码权限；`.env`、运行产物、备份和本地诊断档案保留在本地。

## SWE-bench 实验与边界

项目提供初始修复、失败反馈纠正、经验构建、直接注入消融、真实向量召回以及环境异常补跑工具。

最近一次同题历史经验诊断：从历史“首次失败、纠正成功”的 71 题中，使用冻结经验库在线向量召回，关闭反馈纠正与记忆写入，**55/71 在新的首轮修复中通过**。环境异常补跑后两类经验的注入审计全部通过。

这个数字衡量的是**已学同题经验的利用能力**，适用范围为上述 71 题的同题历史经验诊断。历史批次存在模型、Prompt 和反馈策略变化，合并统计属于研发记录。完整 Lite、未见题泛化和官方排行榜 pass@1 需要独立的评测设计与合规审计，正式提交时应披露记忆来源、运行配置和完整轨迹。

`scripts/run_vector35_wsl.py` 等脚本保留了本地实验路径，需要先配置数据集、仓库 mirror、embedding 环境和 Harness，**不能直接当通用安装命令运行**。`scripts/vector_memory_agent.py` 展示了在线查询、真实排名及注入审计的接入方式。

## 深入代码

```text
repo_agent/
├── cli.py                   # CLI 与角色模型配置
├── core/
│   ├── coordinator.py       # 决策、交接与模式边界
│   ├── subagent.py          # 角色定义、Prompt、工具权限
│   ├── query_engine.py      # 会话生命周期、上下文与轨迹
│   ├── query_loop.py        # LLM → 工具 → 观察结果循环
│   ├── llm.py               # 流式接收、协议适配与请求诊断
│   ├── experience.py        # 学习/推理策略、失败反馈与经验格式化
│   ├── memory_query.py      # 独立检索查询提取
│   ├── memory.py            # 本地长期记忆
│   ├── session_memory.py    # 短期任务状态
│   └── runtime_pool.py      # 无状态角色 Runtime 池
├── tools/                   # 声明式工具与仓库操作
└── worker/                  # 消息队列、后端 API、评测与记忆提案
scripts/                     # 批处理、向量实验、环境修复与统计
tests/                       # 单元与离线集成测试
```

- [架构与运行时](docs/architecture.md)
- [Coordinator Prompt](docs/prompts/Coordinator.md)
- [Implement Prompt](docs/prompts/Implement.md)
- [FailureReview Prompt](docs/prompts/FailureReview.md)
- [ExperienceCurator Prompt](docs/prompts/ExperienceCurator.md)
- [并发流水线实验](docs/pipeline-concurrency-benchmark.md)

```bash
python -m pytest -q
repo-agent --help
```

## License

[MIT](LICENSE)
