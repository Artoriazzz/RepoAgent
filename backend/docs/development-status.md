# RepoAgent 平台开发进度

本文档记录 RepoAgent 后端平台当前已经完成的能力、尚未实现的边界，以及下一阶段的开发顺序。当前主路线是先使用 SWE-bench 作为虚拟代码托管平台，验证多仓库、多 Issue、异步修复和评测流程；GitHub 的分支推送与 Draft PR 集成后置。

更新日期：2026-09-14

## 项目目标

平台面向多个代码仓库和开发者提交的 Issue。仓库所有者可以为某个 Issue 创建修复任务，后端异步调用 RepoAgent，在隔离的 Git worktree 中生成补丁并完成验证。

当前使用 SWE-bench 模拟真实业务：

| 真实业务对象 | SWE-bench 对应对象 |
| --- | --- |
| 代码托管平台 | SWE-bench 数据集与本地 repository mirror |
| GitHub 仓库 | 数据集中的 `repo` |
| GitHub Issue | `instance_id` 与 `problem_statement` |
| 修复基线 | `base_commit` |
| Issue Webhook | Benchmark Simulator 发起的业务请求 |
| 修复分支 | 每个任务独立的 branch 与 worktree |
| Pull Request | `model.patch` 与 RepairResult |
| CI | SWE-bench harness 的 F2P/P2P 评测 |

## 当前架构

```text
SWE-bench Dataset / GitHub Webhook
                |
                v
        Spring Boot Backend
                |
                v
             RabbitMQ
                |
                v
       Python RepoAgent Worker
                |
                v
       branch + worktree + Agent
                |
                v
 model.patch + validation + task status
```

后端负责用户、仓库、Issue、权限、任务状态和异步调度。RepoAgent Worker 负责准备代码工作区、执行 Agent、保存补丁并回传结果。SWE-bench harness 负责独立评测，不与 Agent 的生成上下文混用。

## 已完成

### 1. Spring Boot 基础后端

- 使用 Java 17、Spring Boot 3.5 和 Maven 建立项目。
- 使用 MySQL 8.4 保存业务数据，使用 Flyway 管理表结构。
- 提供 Docker Compose，用于启动 MySQL、RabbitMQ 和 Redis 等依赖。
- 提供 Spring Boot Actuator 健康检查。
- 已完成 Maven 测试，当前测试覆盖 Webhook 签名、Webhook 同步、任务状态机和 Worker Token 校验。

### 2. 用户、仓库和权限

- 已实现用户创建接口。
- 已实现代码仓库创建和绑定接口。
- 已实现仓库 Owner、Developer 成员关系。
- 已实现仓库访问权限校验。
- 开发阶段通过 `X-User-Id` 模拟登录身份。

### 3. Issue 管理

- 已实现仓库 Issue 的创建与保存。
- 已实现 Issue 开闭状态模型。
- GitHub `issues` Webhook 可以同步标题、正文和状态。
- Webhook 已实现 HMAC-SHA256 签名校验和 delivery ID 去重。

### 4. 异步修复任务

- 已实现 RepairTask 创建、查询和取消接口。
- 已实现 `Idempotency-Key`，避免重复创建同一修复任务。
- 已实现 RabbitMQ 消息发布和消费协议。
- 已实现任务状态机：

```text
CREATED -> QUEUED -> ANALYZING -> FIXING -> VALIDATING -> PATCH_READY -> PR_READY
```

- 已实现失败、取消和非法状态跳转处理。
- 已实现 patch 摘要、验证摘要和 PR URL 等结果字段。
- 已提供模拟 Worker，可在不调用 Agent 时验证完整异步链路。

### 5. RepoAgent Worker

- Python Worker 可以消费 RabbitMQ 的 RepairTask 消息。
- Worker 可以通过受 Token 保护的内部接口获取任务上下文并上报状态。
- 每个任务使用唯一 branch 和 worktree，能够隔离并行 Issue 修复。
- Worker 可以调用现有 RepoAgent Coordinator 流程并保存 `model.patch`。
- 本地补丁生成完成后进入 `PATCH_READY`，不会错误标记为已经创建 PR。

### 6. P0 任务运营能力

- 已实现仓库、Issue 和 RepairTask 的分页列表与详情查询。
- 已实现任务进度时间线，Worker 可上报阶段、消息和结构化元数据。
- 已实现任务统计，可按仓库或 `runKey` 汇总状态、评测结果、耗时和 Token。
- 已实现 patch、trajectory、validation、Agent 日志和 harness 日志的白名单下载。
- 已实现显式重试，保存父任务、尝试次数和独立幂等键。
- 已实现协作式取消：排队任务立即取消，运行中任务由 Worker 轮询后终止进程组。
- 已实现 Worker 心跳、任务 claim、执行租约续期和 lease token fencing，避免过期 Worker 覆盖新结果。
- 已实现失败分类、错误摘要和 `retryable` 标记，区分空补丁、超时、模型错误、环境错误和评测错误。
- 已记录模型、执行耗时和 Token 使用量；任务事件可用于前端轮询进度。

### 7. GitHub App 环境验证

- 已创建 GitHub App `repoagent-artoria`。
- App JWT 签发验证通过。
- Installation Token 签发验证通过。
- 已验证对 `Artoriazzz/backend` 的仓库和 Issues API 访问。
- 当前权限包含 Contents、Issues、Pull Requests 写权限，以及 Actions、Checks、Statuses 读权限。
- Smee Webhook 通道从 WSL 访问正常。

以上是环境与凭据验证结果。后端代码尚未实现自动签发 Installation Token、推送修复分支和创建 Draft PR。

### 8. P1 可靠运行能力

- 使用 MySQL Transactional Outbox 保存任务派发事件，任务与待投递消息在同一事务提交。
- Outbox 发布器使用 RabbitMQ publisher confirm；投递失败按指数退避重试，超过上限进入 `DEAD`。
- 提供内部 Outbox 列表与人工重试接口，故障不会要求重新创建业务任务。
- 租约恢复扫描器可以接管 claim 后或执行中的失联任务，并重新产生派发消息。
- harness 运行期间租约过期时保留已有 patch，将评测标记为 `HARNESS_ERROR`，不会重跑 Agent 覆盖产物。
- 提供 SSE 任务事件流；连接时补发持久事件，连接后实时推送新事件。
- Actuator 暴露 Outbox 发布、失败、积压、死信和租约恢复指标。
- Python Worker 在空闲与忙碌状态持续心跳，RabbitMQ 断线后自动指数退避重连。

### 9. 任务监控前端

- 已实现 Vue 3 + TypeScript 监控控制台，第一屏直接展示任务运行状态。
- 已实现排队、执行中、今日完成、在线 Worker 和平均等待时间指标。
- 已实现 Worker 当前任务、心跳、任务阶段和仓库/Issue 展示。
- 已实现任务分页、状态筛选、详情时间线、F2P/P2P、错误和产物下载。
- 已接入 SSE 实时任务事件，并支持按时间游标断线补发。
- 已实现前端取消和显式重试操作。
- 后端提供 `/api/monitoring/overview` 与 `/api/monitoring/workers` 聚合接口。

### 10. RAG 经验记忆

- MySQL 永久保存不可变 observation、语义记忆、revision、proposal、召回反馈和索引任务。
- 支持 global 与 repository 两级作用域，召回时组合向量、关键词、技术元数据和历史质量。
- 并发 UPDATE 使用 `baseVersion` 乐观语义校验与短事务行锁；冲突返回当前 revision，要求 Curator 重新合并。
- 相同 fingerprint、幂等键和召回 revision 均有数据库唯一约束，避免重复学习和重复计数。
- embedding 使用 OpenAI 兼容接口并异步重建；外部请求不持有数据库事务，失败时降级到关键词召回。
- 正式 Worker 使用只读 `recall`，离线 `learn` 不召回且只保存独立评测成功案例，防止 SWE-bench 隐藏反馈进入修复 loop。
- 持续进化 `evolve` 模式在修复前召回、独立评测成功后学习；Curator 更新前重新查询最新记忆版本。
- 同一 Issue 来源记忆在服务端排除；使用效果只有 Agent 显式输出记忆 ID 时才计入质量分。

## 已完成：SWE-bench 虚拟仓库链路

- repository provider 已支持 `GITHUB` 与 `SWE_BENCH`。
- Benchmark Simulator 可读取 Parquet、JSON 或 JSONL，并支持选择实例、范围、并发与重跑 key。
- 后端可幂等创建虚拟仓库、虚拟 Issue 和 RepairTask。
- Worker context 已携带 provider、instance ID、split 和 mirror key。
- Worker 可从本地 `owner__repo.git` mirror 创建独立 branch/worktree。
- patch artifact 路径会保存到任务详情。
- 可选的官方 SWE-bench harness 会在 patch 生成后独立执行。
- 后端可区分 `RESOLVED`、`UNRESOLVED` 与 `HARNESS_ERROR`，并保存 F2P/P2P 计数。
- harness 代理会透传给 Git、Python requests 和 HTTPS 下载链路。

运行时冒烟测试已使用 `pallets__flask-4992` 验证完整链路。确定性假补丁得到 `UNRESOLVED`，F2P 通过 0、失败 1，P2P 通过 18、失败 0，说明任务与评测结果能够正确回传。

## 尚未完成

- GitHub App JWT 与 Installation Token 的后端自动管理。
- GitHub 分支推送和 Draft PR 创建。
- Redis 缓存和接口限流。
- 每仓库与每用户并发配额。
- 正式登录认证和前端页面。

## SWE-bench 虚拟仓库实现

### 第一步：统一仓库来源

为仓库增加来源类型，使核心业务不直接依赖 GitHub：

```text
provider: GITHUB | SWE_BENCH
externalId: pallets/flask
mirrorKey: pallets__flask
```

后端使用 `RepositoryProvider` 枚举保存来源，Python Worker 根据 provider 选择源代码准备方式：

- `SWE_BENCH`：从 Worker 本地 mirror root、mirror key 和 `base_commit` 创建 worktree。
- `GITHUB`：继续按平台 repository UUID 查找预同步 mirror。

现有 GitHub 仓库记录保持兼容，SWE-bench 仓库不伪造 GitHub repository ID。

### 第二步：导入 Benchmark 实例

虚拟 Issue 和 RepairTask 保存或携带：

```text
instanceId
repo
baseCommit
problemStatement
datasetSplit
```

新增接口：

```http
POST /api/benchmark/repair-tasks
```

该接口完成以下操作：

1. Python Simulator 从本地数据集读取实例并剔除非公开字段。
2. 创建或复用虚拟仓库。
3. 创建或复用虚拟 Issue。
4. 创建 RepairTask 并投递 RabbitMQ。

同一 instance 和 idempotency key 返回原任务；`--run-key` 用于有意创建新的修复尝试。

### 第三步：接通真实 Worker

Worker 上下文包含 `provider`、`instanceId`、`datasetSplit`、`mirrorKey` 和 `baseCommit`。Worker 根据 provider 选择代码准备方式，不再假设所有任务都来自 GitHub。

验收标准：一个 SWE-bench 实例可以从本地 mirror 创建独立 worktree，调用 RepoAgent，并把非空 patch 回传为 `PATCH_READY`。

### 第四步：接入独立评测

RepoAgent 完成后，Worker 启动独立 harness 子进程。它只消费最终 patch，不会把结果送回 Coordinator。评测结果保存：

```text
resolved
failToPassPassed
passToPassPassed
harnessStatus
harnessLogPath
```

评测错误必须和 Agent 未修复分开记录，例如 `HARNESS_ERROR` 不能直接归类为 `UNRESOLVED`。

验收标准：任务详情能区分 patch 生成、harness error、F2P 失败、P2P 回归和 resolved。

### 第五步：模拟并发业务

Benchmark Simulator 已支持指定实例范围和并发数，模拟多个仓库的 Issue。当前已保证：

- 同一幂等键只产生一个任务。
- 每个任务拥有独立 branch 和 worktree。
- 取消任务后，Worker 会终止 Agent 或 harness 子进程，后端不再接受普通状态更新。
- Worker 通过 claim、心跳和租约续期声明任务所有权，过期回调会被拒绝。
- 失败任务可以显式创建新的 retry attempt，不复用原任务状态。

RabbitMQ prefetch 当前为 1，单 Worker 串行消费；可以启动多个 Worker 横向扩容。每仓库并发上限仍属于后续优化，不阻塞当前单机业务链。

## P0 接口摘要

面向业务端的接口：

```text
GET  /api/repositories
GET  /api/repositories/{repositoryId}
GET  /api/repositories/{repositoryId}/issues
GET  /api/issues/{issueId}
GET  /api/repair-tasks
GET  /api/repair-tasks/{taskId}
GET  /api/repair-tasks/{taskId}/progress
GET  /api/repair-tasks/{taskId}/events
GET  /api/repair-tasks/{taskId}/events/stream
GET  /api/repair-tasks/{taskId}/artifacts
GET  /api/repair-tasks/{taskId}/artifacts/{type}
GET  /api/repair-tasks/statistics
POST /api/repair-tasks/{taskId}/cancel
POST /api/repair-tasks/{taskId}/retry
```

面向 RepoAgent Worker 的内部接口：

```text
POST /internal/repair-tasks/{taskId}/claim
POST /internal/repair-tasks/{taskId}/lease/renew
GET  /internal/repair-tasks/{taskId}/control
POST /internal/repair-tasks/{taskId}/events
POST /internal/repair-tasks/{taskId}/updates
POST /internal/repair-tasks/{taskId}/evaluation
POST /internal/workers/{workerId}/heartbeat
GET  /internal/repair-outbox-events
POST /internal/repair-outbox-events/{eventId}/retry
```

## 后续 GitHub 集成

SWE-bench 业务链路稳定后，再完成 GitHub Provider：

1. 后端根据 App 私钥自动签发短期 JWT。
2. 根据仓库 Installation ID 获取并缓存短期 Installation Token。
3. Webhook 自动绑定或同步仓库与 Issue。
4. Worker 推送任务分支。
5. 后端或 Worker 创建 Draft PR，并将任务更新为 `PR_READY`。

GitHub Token 只能保存在内存或受保护的短期缓存中，不写入数据库、日志、任务消息或 Agent 上下文。

## 推荐后续顺序

1. 使用真实 RepoAgent 跑 1 个 Lite 实例，验证模型配置和产物权限。
2. 用 5 个实例压测幂等、取消、租约回收、harness error 分类和磁盘清理。
3. 根据 WSL 资源设置 Worker 并发和单仓库并发上限。
4. 接入正式登录认证，并为任务列表与进度时间线提供前端页面。
5. 完成 GitHub Provider 的 token、push 和 Draft PR 能力。

## 当前边界

- 数据库迁移脚本面向全新 MySQL 8.4 实例；旧 PostgreSQL 测试数据不会自动迁移。
- SWE-bench 模式用于模拟业务和评估 Agent，但不能向 Agent 暴露 gold patch、隐藏测试或最终 harness 反馈。
- 模式一的研究性失败反馈与正式打榜流程必须分开保存和标识。
- `PATCH_READY` 只表示已生成本地补丁，不代表补丁通过 harness，也不代表已经创建 PR。
- 不同任务必须使用独立 worktree；禁止多个 Worker 直接修改共享 mirror。
- GitHub App 私钥、Webhook Secret 和 Worker Token 不得提交到 Git 仓库。
