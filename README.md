<div align="center">

# RepoAgent

**简体中文** | [English](README.en.md)

![各司其职的机器人协作修复 Issue](docs/assets/repair-squad-v2.png)

### 把一个 Issue，交给一支各司其职的修复小队。

**经验召回 · 协同定位 · 定向修复 · 持续学习**

[认识修复小队](#认识修复小队) · [运行流程](#运行流程) · [经验与检索](#经验与检索) · [后端介绍](#后端介绍) · [实验结果](#swe-bench-lite-实验结果) · [快速开始](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/docs/quickstart.md)

</div>

---

RepoAgent 是面向软件仓库的多 Agent 程序修复平台，将公开 Issue、源码、修复经验与执行工具组织成一条协作流水线。

每个角色拥有独立上下文，只接收当前阶段需要的信息：**查询者寻找可复用经验，协调者决定修复方向，实现者落实代码修改，归因者分析失败，经验整理者沉淀验证成功的知识。**

平台包含 Python Agent Runtime、Spring Boot 业务后端和 Vue 监控前端。Agent Runtime 直接使用 OpenAI-compatible `tools/tool_calls` 协议，实现会话管理、工具执行、流式响应、预算控制与角色调度。

完整平台源码位于 [full-stack-integration 分支](https://github.com/Artoriazzz/RepoAgent/tree/full-stack-integration)；`main` 分支保留 Python Agent Runtime、Worker 与实验工具。

## 为什么使用多智能体

程序修复包含定位、实现、测试、归因与经验整理等阶段。RepoAgent 为各阶段分配专门角色，让上下文、模型成本和执行流程都能独立管理。

1. **上下文分离**：每个角色拥有独立会话，只接收当前阶段所需的源码、任务和前序产物，减少无关历史对判断的干扰，控制上下文长度。
2. **简单工作交给低成本模型**：查询提取、经验整理等相对简单的工作可以使用低成本模型，复杂定位和代码修复使用能力更强的模型。各角色的模型由运行配置决定。
3. **便于流水线与并行调度**：阶段之间通过明确产物交接，方便后端排队、限流和分配资源。无依赖的工作或不同任务可并行执行，有前后依赖的阶段按顺序推进；实际并行度受模型限额和执行环境资源约束。
4. **阶段特化，方便独立迭代**：Coordinator 负责定位与决策，Implement 修改代码，TestDesigner 设计测试，FailureReview 分析失败。每个阶段都可以独立调整提示词、工具、预算或验证机制，并检查对整条流程的影响。
5. **让修复流程可控**：单一模型承担全部工作时，搜索、修改、验证和结束的时机容易随其当前推理变化。显式阶段与路由让程序管理执行顺序、授权范围、纠正次数和停止条件，模型在各自职责内完成判断。

### 流程中的协作

| 常见困境 | RepoAgent 的做法 |
| --- | --- |
| 查阅大量代码后，仍难以决定修改位置 | Coordinator 围绕关键未知读取源码，形成行为契约与 EditBrief |
| patch修改后，如何得知是否修复成功 | TestDesigner根据issue描述，结合代码自动生成测试用例，并在原来代码上验证待修复和回归场景 |
| 异常消失了，相邻合法路径却出现回归 | 将需要保持的旧行为交接给实现者，同时由 TestDesigner 设计回归用例，在独立验证中检查修复是否影响已有合法行为 |
| 失败日志很长，纠正方向反复变化 | FailureReview 提取失败事实、失效假设和可执行的修正依据 |
| 相似问题每次都要重新摸索 | 检索历史全局经验与仓库经验，供 Coordinator 结合当前源码复核 |
| 多个修复任务争抢资源、混用上下文 | 阶段队列调度任务，无状态 Runtime 复用客户端，独立 Session 隔离任务状态 |

## 认识修复小队

### 主线角色

| 角色 | 职责 | 交付物 |
| --- | --- | --- |
| **MemoryQueryAgent：经验查询** | 从公开 Issue 提取问题描述与使用场景，形成独立的检索查询 | `problem_description`、`usage_scenario` |
| **Coordinator：定位与决策** | 结合召回经验、当前题意和源码，确认错误操作，决定修复方案与编辑范围 | Problem Contract、源码证据、EditBrief、授权文件与行范围 |
| **TestDesigner：测试用例** | 根据公开 Issue、源码和已有测试设计用例；能够在修复前的基线上试跑，确保用例覆盖修复和回归场景 | 提供内部测试用例，帮助agent检验修复结果 |
| **Implement：代码实现** | 按照 EditBrief 在授权范围内完成修改，报告直接反证与已知风险 | 初始 patch、完成状态、风险说明 |
| **FailureReview：失败归因** | 分析验证失败事实和允许读取的源码，识别语义错误、遗漏路径或环境故障 | 证据清单、失效假设、纠正建议、范围调整需求 |
| **ExperienceCurator：经验整理** | 在独立验证成功后抽象知识，查重并维护已有经验 | 全局经验、仓库经验及新增/合并/不保存决策 |

### 每个角色分工明确，也保留自主判断，发挥模型智能

每个 Agent 都有明确的任务，也有工具和空间去验证自己的判断。前一个阶段给出的结论，到了下一阶段仍然可以被检查、质疑和修正。

- **TestDesigner：写完测试，先检查测试本身。** 它可以在修改前的代码上运行自己创建的用例，确认能否复现 Issue 中的问题，检查测试数据、初始化方式和断言是否合理。原本就能通过、且有依据的用例也可以保留，用于检查修复有没有破坏已有行为。
- **Coordinator：提出修复思路，也能先试一试。** 定位过程中，它可以在原代码上运行小型复现，或临时尝试候选修改，检查观察到的现象是否支持自己的判断。确认原因后，再把修改位置、修复方向和需要保持的行为交给 Implement。
- **Implement：按范围落实修改，同时报告自己的发现。** 它在 Coordinator 授权的范围内决定具体实现方式。修改过程中发现方案遗漏、相邻分支受影响，或某个假设与源码不符时，可以在交付补丁时说明自己的见解、未完成事项和已知风险，为后续判断提供依据。
- **FailureReview：先分清哪里出了问题，再决定交给谁。** 验证失败后，它结合日志、源码和前序交付判断：测试设置或断言有问题，就把依据和修改建议交给 TestDesigner；修复需要扩大范围或重新定位，就回到 Coordinator；方向和范围都合理，但代码没有改到位，就交给 Implement 继续纠正。

这样，每个阶段都有机会用实际结果检验自己的想法；发现问题后，也有明确的路径把证据交给能够处理它的角色。

## 运行流程

**TestDesigner 先独立准备测试，再开始修改代码。** 它根据公开 Issue、修改前源码和仓库已有测试设计用例，在原始基线上试跑后冻结可用测试。测试准备与 Coordinator 解耦，初始测试结果不注入 Coordinator；可选的经验召回只进入 Coordinator，帮助定位与决策。

Coordinator 交付 EditBrief 后，由 Implement 完成首次修复，再执行冻结的 TestDesigner 生成用例。失败或验证不足时检查剩余预算：耗尽则结束，否则交给 FailureReview。归因后有三条路：授权范围内的代码问题交给 Implement 定向纠正；需要扩展范围或重新定位时回到 Coordinator；生成测试有问题时，把失败证据和建议交给 TestDesigner 修订并重新试跑基线，再验证当前补丁。纠正后的代码都回到同一个独立验证环节。

![RepoAgent 修复协作流程：TestDesigner 基线测试、失败归因与三路纠正](docs/assets/repair-workflow-testdesigner-v12.png)


整个过程中保留三个明确的交接点：

1. **经验 → 决策**：召回内容进入 Coordinator，由当前题意与源码确认适用性。
2. **决策 → 实现**：EditBrief 同时交付目标行为、修改依据、授权范围和需要保持的旧行为。
3. **验证 → 学习**：验证成功后提炼经验；已有同类知识时优先合并，有效知识没有增加时保持原样。

## 经验与检索

### 全局经验与仓库经验

| 类型 | 面向的问题 | 保存内容 |
| --- | --- | --- |
| **全局经验** `global_experience` | 在其他仓库识别同类机制 | 问题机制、适用条件、通用修复步骤、验证思路、已证伪做法 |
| **仓库经验** `repository_experience` | 在当前仓库快速找到正确实现层 | 稳定模块/API、调用关系、可复用辅助函数、仓库契约、公开验证方法 |

新经验按“**标题 → 问题描述 → 使用场景 → 机制与适用条件 → 修复步骤 → 验证思路**”组织，保留问题的识别特征与可复用解法。ExperienceCurator 在写入前检索相关项，选择新增、合并或保持原样。

### 基于余弦相似度检索过程

历史侧和查询侧均以 `problem_description`、`usage_scenario` 表达问题：前者概括异常与预期行为，后者描述 API、操作、输入结构和触发条件。


## 自研 Agent Loop

```mermaid
sequenceDiagram
    participant E as QueryEngine（会话层）
    participant L as query_loop（执行层）
    participant M as LLM
    participant T as ToolRegistry
    E->>L: 消息 + 工具 + 预算
    loop 直到完成、阻塞或预算耗尽
        L->>M: 流式模型请求
        M-->>L: 文本 / tool_calls
        L->>T: 参数与权限校验
        T-->>L: 结构化观察结果
        L-->>E: 事件、用量、阶段状态
    end
    E->>E: 保存会话与短期任务状态
```

- **会话层**：管理消息历史、短期状态、上下文压缩和工具大结果落盘。
- **执行层**：拼接流式分片与工具参数，回填观察结果，处理停止条件与 JSON 输出修正。
- **工具层**：使用声明式 schema、角色白名单、文件/行范围授权和读取一致性检查。
- **调度层**：角色 Runtime 复用模型连接，任务 Session 隔离工作区、上下文与工具状态。

应用层权限控制与容器隔离分别承担工具授权和执行环境保护。架构细节见 [运行时说明](docs/architecture.md)。

## 后端介绍

Spring Boot 管理用户、仓库、Issue 和任务状态；Python Worker 执行修复阶段；Vue 前端展示任务队列、阶段事件、Worker 状态和验证结果。

```mermaid
flowchart LR
    UI["Vue 监控前端"] --> API["Spring Boot API"]
    API --> DB[("MySQL<br/>任务 / Outbox / 经验版本")]
    DB --> O["Outbox Dispatcher"]
    O --> MQ["RabbitMQ 阶段队列"]
    MQ --> W["Python Worker 池"]
    W --> R["独立 worktree<br/>Agent / Harness"]
    W -->|"心跳 / 进度 / 结果"| API
    API --> Cache[("Redis 缓存")]
    API --> Vector[("Qdrant 向量索引")]
```

### 按阶段推进的异步流水线

Coordinator、Implement、验证、失败归因和经验整理通过独立阶段队列衔接。每个 Worker 完成当前阶段后释放角色资源，下一阶段由对应队列继续消费，让多个任务在不同阶段重叠推进。

| 设计 | 作用 |
| --- | --- |
| **阶段队列 + 角色池** | 各角色独立扩容，当前配置支持每角色 1–3 个实例 |
| **事务 Outbox** | 将任务状态与待投递事件一起持久化，通过发布确认和重试推进消息投递 |
| **claim、租约与心跳** | 确认执行归属，识别离线 Worker，拒绝过期租约的迟到回调 |
| **任务专属 worktree 与阶段快照** | 隔离代码修改，以任务状态文件衔接阶段与恢复执行 |
| **取消、幂等与准入控制** | 控制重复执行、并发数量、用户配额与系统积压 |
| **监控聚合与事件流** | 查看排队、运行阶段、Worker 活跃情况与验证结果 |

单个任务按依赖关系推进，多任务可以并行占用不同角色的资源。调度基准方法见 [流水线并发实验](docs/pipeline-concurrency-benchmark.md)。

### 面向版本与并发更新的经验 RAG

- **MySQL 保存正式经验与 revision**：召回和提案绑定具体版本，支持追溯经验依据。
- **Qdrant 承担向量候选检索**：按作用域过滤候选，结合向量相似度与经验质量进行排序；通过配置启用。
- **TTL 抖动与短租约重建锁**：分散缓存失效时间，收敛热点回源请求。
- **成功经验通过提案合并**：修复结果生成 observation 和经验提案，由经验维护流程执行新增、更新或不变决策。
- **版本检查与索引对账**：过期提案重新基于当前版本判断；向量索引通过后台任务补齐和对账。

完整设计见 [经验 RAG](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/backend/docs/memory-rag-design.md)、[任务准入与租约](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/backend/docs/repair-admission-quota-and-lease-design.md) 和 [后端接口](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/backend/README.md)。

## SWE-bench Lite 实验结果

在 300 道 SWE-bench Lite 题目上的本地研发记录：

| 修复设置 | 通过题数 | 通过率 |
| --- | ---: | ---: |
| **无经验首轮修复** | 196 / 300 | **65.33%%** |
| **基于经验首轮修复** | 211 / 300 | **75.67%** |



## 深入代码

全栈分支的目录结构：

```text
repo_agent/                 Python Agent Runtime 与 Worker
├── core/
│   ├── coordinator.py      决策、交接与调度约束
│   ├── subagent.py         角色定义、Prompt、工具权限
│   ├── query_engine.py     会话生命周期与上下文
│   ├── query_loop.py       模型调用与工具执行循环
│   ├── memory_query.py     独立查询提取
│   └── runtime_pool.py     无状态角色池
├── tools/                  声明式工具与仓库操作
└── worker/                 阶段消费、后端回调、评测与经验提案
backend/                    Spring Boot 业务后端
└── frontend/               Vue 监控前端
scripts/                    批处理、向量实验和环境工具
tests/                      Python 单元与离线集成测试
```

- [快速开始](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/docs/quickstart.md) · [WSL 部署与开发](https://github.com/Artoriazzz/RepoAgent/blob/full-stack-integration/docs/full-stack-development.md)
- [Coordinator Prompt](docs/prompts/Coordinator.md) · [Implement Prompt](docs/prompts/Implement.md)
- [FailureReview Prompt](docs/prompts/FailureReview.md) · [ExperienceCurator Prompt](docs/prompts/ExperienceCurator.md)

## License

[MIT](LICENSE)
