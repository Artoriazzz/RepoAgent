# RepoAgent Backend

RepoAgent Backend 是“智能代码缺陷修复平台”的 Java 业务后端。平台管理多个 GitHub 仓库与 Issue；仓库所有者选择 Issue 后，系统异步派发 RepoAgent 修复任务，并为完成的修复创建独立分支和 Draft Pull Request。

RepoAgent 本身负责分析、补丁和测试；本项目负责用户权限、仓库、Issue、修复任务、任务状态、GitHub 事件和后续 PR 发布。

当前开发进度、SWE-bench 虚拟仓库方案和后续实施顺序见 [开发进度文档](docs/development-status.md)。

## 技术栈

- Java 17、Spring Boot 3.5
- MySQL 8.4 + Flyway：业务数据和数据库迁移
- Redis：缓存和限流预留
- RabbitMQ：长耗时修复任务派发
- Docker Compose：本地/服务器依赖服务
- Vue 3、TypeScript、Vite：任务监控前端

## 启动

```bash
docker compose up -d --wait
mvn spring-boot:run
```

默认数据库为 `repoagent`，本地用户名和密码均为 `repoagent`。可通过
`SPRING_DATASOURCE_URL`、`SPRING_DATASOURCE_USERNAME` 和
`SPRING_DATASOURCE_PASSWORD` 覆盖连接配置。

服务默认运行在 `http://localhost:8080`，健康检查地址为 `GET /actuator/health`。

任务监控前端位于 [`frontend`](frontend)，开发模式启动：

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

浏览器访问 `http://localhost:5173`，填写一个已有用户 UUID。页面展示任务队列、Worker 当前任务、实时阶段事件、验证结果和运行产物。
页面中的“批量创建修复任务”会列出当前用户可访问、尚未解决且没有正在执行任务的 SWE-bench Issue。可跨虚拟仓库勾选最多 50 条 Issue，一次创建独立的 repair task；每条任务仍使用自己的 branch、worktree 和执行租约。

首次可按以下顺序调用接口。当前使用 `X-User-Id` 表示已认证用户，便于在接入 GitHub OAuth/App 前完成业务链路验证；生产环境会由登录会话或访问令牌替代这个请求头。

```bash
curl -X POST http://localhost:8080/api/users \
  -H 'Content-Type: application/json' \
  -d '{"githubUserId": 10001, "login": "octo", "displayName": "Octo Cat"}'

curl -X POST http://localhost:8080/api/repositories \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: {userId}' \
  -d '{"githubRepositoryId": 1, "ownerLogin": "octo", "name": "demo", "defaultBranch": "main"}'

curl -X POST http://localhost:8080/api/repositories/{repositoryId}/issues \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: {userId}' \
  -d '{"githubIssueNumber": 12, "title": "Parser error", "body": "Steps to reproduce"}'

curl -X POST http://localhost:8080/api/issues/{issueId}/repair-tasks \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: {userId}' \
  -H 'Idempotency-Key: issue-12-first-run' \
  -d '{"baseSha": "abc123"}'
```

仓库所有者可以通过 `POST /api/repositories/{repositoryId}/members` 添加开发者成员；开发者可读任务、创建 Issue，所有者可创建或取消修复任务。

## GitHub Issue Webhook

GitHub App 注册填写项、Webhook 签名、业务 API 和 Worker 内部接口见 [GitHub App 与后端接口文档](docs/github-app-integration.md)。

设置 GitHub App 或仓库 Webhook 的 `issues` 事件，投递地址为：

```text
POST /api/integrations/github/webhooks
```

服务会校验 `X-Hub-Signature-256`、以 `X-GitHub-Delivery` 去重，并把已绑定仓库的 Issue 标题、正文和开闭状态同步到本地。启动服务前配置同一个 Webhook secret：

```bash
export GITHUB_WEBHOOK_SECRET='replace-with-a-random-secret'
```

仓库绑定与用户资料目前由内部 API 建立；下一阶段接入 GitHub OAuth 与 GitHub App installation token，改为由授权回调自动完成。

创建任务后，后端会写入修复任务并向 RabbitMQ 发布 `RepairTaskMessage`。后续 Python RepoAgent Worker 消费该消息，创建 `branch + worktree + Docker` 运行环境，并通过内部接口回传 patch、验证结果和日志。

为了先验证异步链路，可在本地设置 `APP_REPAIR_SIMULATOR_ENABLED=true`。模拟 Worker 会消费消息并走过 `QUEUED -> ANALYZING -> FIXING -> VALIDATING -> PATCH_READY`，在任务详情中留下明确标注为 simulation 的结果；它不会修改仓库或创建 PR。

## SWE-bench 虚拟仓库

`POST /api/benchmark/repair-tasks` 接收一个公开 SWE-bench 实例快照，自动创建或复用虚拟仓库与 Issue，再异步创建修复任务。请求只包含 `instanceId`、`repo`、`base_commit`、`problem_statement` 和 split，不接收 gold patch 或隐藏测试信息。

```bash
repo-agent-benchmark-submit \
  --dataset /data/SWE-bench/test-00000-of-00001.parquet \
  --backend-url http://localhost:8080 \
  --user-id '<owner-user-uuid>' \
  --instance-id pallets__flask-4992 \
  --split lite
```

使用 `--start`、`--limit` 和 `--concurrency` 可以批量模拟多仓库 Issue；需要有意重跑同一实例时使用 `--run-key <name>`。后端以 repository provider 区分 GitHub 和 SWE-bench，Worker 会从 `${REPO_AGENT_SWEBENCH_MIRROR_ROOT}/owner__repo.git` 创建独立 worktree。

只导入虚拟仓库和 Issue、留待监控页面批量选择时，增加 `--issues-only`。任务列表将执行阶段与最终结果分开：执行阶段来自 Worker 的 Coordinator、Implement、FailureReview、Corrective Implement 和最终 harness 事件；最终结果只依据 `RESOLVED`、`UNRESOLVED` 或执行/环境错误。模式三最多允许初始修复后再创建一次错误分析重试。

## RepoAgent Worker 回调

真实 Python Worker 从 RabbitMQ 收到 `RepairTaskMessage` 后，先通过内部接口读取仓库与 Issue 快照，再使用 `baseSha` 和 `branchName` 创建隔离运行环境。RabbitMQ 消息保持轻量，不包含 Issue 正文：

```text
GET  /internal/repair-tasks/{taskId}/context
POST /internal/repair-tasks/{taskId}/claim
POST /internal/repair-tasks/{taskId}/lease/renew
GET  /internal/repair-tasks/{taskId}/control
POST /internal/repair-tasks/{taskId}/events
POST /internal/repair-tasks/{taskId}/updates
POST /internal/repair-tasks/{taskId}/evaluation
POST /internal/workers/{workerId}/heartbeat
X-RepoAgent-Worker-Token: <REPAIR_WORKER_TOKEN>
```

设置 `REPAIR_WORKER_TOKEN` 后，Worker 可依次上报 `ANALYZING`、`FIXING`、`VALIDATING`，在本地 patch 产出后进入 `PATCH_READY`；SWE-bench harness 独立记录 `RESOLVED`、`UNRESOLVED` 或 `HARNESS_ERROR` 以及 F2P/P2P。完成 GitHub 分支推送与 Draft PR 创建后才进入 `PR_READY`。后端拒绝跳过状态、取消后的更新和未授权调用，并保存 patch artifact、验证摘要与可选 PR 地址。

## P0 查询与运维接口

业务端可以分页查询仓库、Issue 和任务，通过任务进度与事件接口轮询执行状态，并下载受白名单保护的运行产物：

```text
GET  /api/repositories
GET  /api/repositories/{repositoryId}/issues
GET  /api/issues?state=OPEN&provider=SWE_BENCH&repairable=true
GET  /api/repair-tasks?repositoryId=&status=&runKey=
GET  /api/repair-tasks/{taskId}/progress
GET  /api/repair-tasks/{taskId}/events
GET  /api/repair-tasks/{taskId}/events/stream?after=<ISO-8601-time>
GET  /api/repair-tasks/{taskId}/artifacts
GET  /api/repair-tasks/{taskId}/artifacts/{type}
GET  /api/repair-tasks/statistics?repositoryId=&runKey=
POST /api/repair-tasks/{taskId}/cancel
POST /api/repair-tasks/{taskId}/retry
POST /api/repair-tasks/batch
```

批量接口请求体为 `{"issueIds":["<issue-uuid>"],"runKey":"optional-run"}`，最多接收 50 个 Issue。响应逐项返回 accepted 或 rejected，因此一个无效条目不会回滚同批次中已经成功创建的任务。服务会锁定 Issue 并拒绝并发创建重复的运行中任务，也不会再次提交已经得到 `RESOLVED` 结果的 Issue。

监控前端使用以下聚合接口，避免浏览器直接读取 RabbitMQ：

```text
GET /api/monitoring/overview
GET /api/monitoring/workers
```

重试会创建新的任务 attempt，并保留 `parentTaskId` 与 `attemptNumber`。运行中取消采用协作式协议：后端记录取消请求，Worker 轮询 control 接口并终止 Agent 或 harness 进程组。claim、lease token 和续期机制用于拒绝过期 Worker 的迟到回调。

产物根目录由 `REPAIR_ARTIFACT_ROOT` 配置，并应与 Worker 的 `${REPO_AGENT_WORK_ROOT}/runs` 指向同一持久化目录。可下载类型为 `PATCH`、`TRAJECTORY`、`VALIDATION`、`AGENT_LOG` 和 `HARNESS_LOG`。

## P1 可靠派发

任务创建和 Outbox 记录在同一个 MySQL 事务中完成。定时发布器通过 RabbitMQ publisher confirm 确认投递，失败时指数退避，避免数据库提交成功但消息丢失。失联 Worker 的执行租约过期后，后端将运行中任务恢复为 `QUEUED` 并重新派发；若 patch 已生成而 harness 中断，则保留 patch 并记录 `HARNESS_ERROR`。

运维接口和指标：

```text
GET  /internal/repair-outbox-events?status=FAILED
POST /internal/repair-outbox-events/{eventId}/retry
GET  /actuator/metrics/repoagent.outbox.published
GET  /actuator/metrics/repoagent.outbox.failed
GET  /actuator/metrics/repoagent.outbox.backlog
GET  /actuator/metrics/repoagent.outbox.dead
GET  /actuator/metrics/repoagent.task.leases.recovered
```

内部 Outbox 接口要求 `X-RepoAgent-Worker-Token`。SSE 接口仍要求业务端 `X-User-Id`，`after` 参数可用于断线后按时间补发事件。

## 当前范围

已实现：用户、仓库成员权限、仓库、Issue、修复任务 API；Flyway 表结构；任务状态机、幂等键和结果字段；RabbitMQ 消息协议与可选模拟 Worker；SWE-bench 虚拟仓库、批量请求模拟、mirror/worktree 路由和独立 harness 结果；任务列表、进度事件与 SSE、统计、产物下载、重试、实际取消、Worker 心跳与执行租约；Transactional Outbox、确认投递、退避重试、失联恢复与 Actuator 指标；GitHub Issue Webhook 的签名校验、投递去重和同步；本地依赖环境。

待接入：GitHub OAuth/App installation 授权、真实 GitHub 分支推送和 Draft PR 创建。任务监控前端已经可用，当前采用开发阶段的 `X-User-Id` 身份头，后续登录接入后再替换为正式会话认证。

数据库已从 PostgreSQL 切换到 MySQL 8.4。当前项目仍处于开发阶段，Flyway
迁移脚本按全新 MySQL 数据库设计；已有 PostgreSQL 数据不会自动迁移，如需保留
真实数据，应先完成单独的数据导出与转换。

每个修复任务都应创建唯一分支和本地 worktree，例如：

```text
repoagent/issue-128/run-<task-id>
/data/worktrees/<repository-id>/<task-id>
```

不同 Issue 使用不同 worktree，因此可并行修复；最终 PR 冲突由仓库所有者在 GitHub 常规流程中处理。

## RAG 经验记忆

Embedding 文本设计、分层缓存与并发一致性方案见 [RAG 开发文档](docs/memory-rag-design.md)。

后端以 MySQL 作为经验事实源，保存不可变修复观察、语义记忆、完整 revision、并发 proposal、召回记录和异步向量索引任务。向量仅是可重建索引，语义内容与版本不会依赖向量服务。

Worker 内部接口均要求 `X-RepoAgent-Worker-Token`：

```text
POST /internal/memories/search
POST /internal/memories/observations
POST /internal/memories/proposals
POST /internal/memories/{memoryId}/feedback
POST /internal/memory-merge-batches/claim
POST /internal/memory-merge-batches/{batchId}/complete
POST /internal/memory-merge-batches/{batchId}/abandon
```

启用学习模式时，修复 Worker 负责生成 proposal；合并进程独立启动，避免把批量语义合并阻塞在修复流水线中：

```bash
export REPO_AGENT_MEMORY_MODE=evolve  # 或 learn
repo-agent-memory-merge-worker
```

该进程只会在 `learn` 或 `evolve` 模式启动；`off`、`recall` 和 SWE-bench 回放环境不会领取或写入记忆提案。

可用 `REPO_AGENT_MEMORY_MERGE_CONCURRENCY` 配置多个无状态合并运行时，每次调用仍携带独立 batch lease 和版本号。

典型流程：正式修复先按 `taskId` 召回 global 与当前仓库经验，并固定使用返回的 `revision`；只有独立验证成功的离线学习任务才能创建 observation。Memory Curator 将成功案例抽象成 CREATE、UPDATE 或 NO_CHANGE proposal。proposal 会先进入待合并池；独立的 Memory Merge Worker 按同一 `memoryId + baseVersion` 领取小批量提案并原子发布。版本落后的 UPDATE 标为 `STALE`，不会由旧任务自动 rebase 覆盖新记忆。

Worker 的 `evolve` 模式会组合上述两段流程：修复前召回，成功后学习。当前任务召回到的 revision 会记录使用效果；成功案例只形成版本固定的 proposal，后续由独立合并 Worker 处理，因此不会以修复前的旧 revision 覆盖并发改进。

默认关闭 embedding，系统仍可用关键词、元数据和质量分召回。启用 OpenAI 兼容 embedding 接口：

```bash
export MEMORY_EMBEDDING_ENABLED=true
export MEMORY_EMBEDDING_BASE_URL=https://api.openai.com/v1
export MEMORY_EMBEDDING_API_KEY=...
export MEMORY_EMBEDDING_MODEL=text-embedding-3-small
# 模型或编码文本规则变更时同步升级该版本标识，触发旧记忆渐进重建。
export MEMORY_EMBEDDING_PROFILE=memory-retrieval-v1
```

### 导入历史 SWE-bench 经验

旧版 RepoAgent 的 `repair_recipe_*.md`、`repair_episode_*.md` 与
`semantic_insight_*.md` 可以一次性迁移为全局语义记忆。导入器只读取已抽象的经验，
不会导入完整 patch、隐藏测试名或运行轨迹；它用 fingerprint 去重，重复启动不会重复写入。
由于这些记录来源于已解决的 SWE-bench，系统将其放入隔离的历史作用域：真实仓库任务可以
参考，SWE-bench 虚拟仓库任务不会召回它们，避免评测集信息回流。

```bash
export MEMORY_LEGACY_IMPORT_ENABLED=true
export MEMORY_LEGACY_IMPORT_ROOT=/home/artoria/.repoagent
```

与 embedding 配置同时启用时，导入会创建异步索引任务；启动日志出现
`Legacy SWE-bench memory import finished` 后即可关闭 `MEMORY_LEGACY_IMPORT_ENABLED`。
历史 revision 没有当前系统中的 RepairTask，因此 `source_observation_id` 为空，导入来源记录在
`change_reason`，与正常修复运行产生的 observation 明确区分。

Redis 会缓存监控页聚合结果和用户容量/预计等待时间，默认 TTL 均为 2 秒，以减少前端轮询产生的数据库聚合查询。实际任务准入、每日额度扣减和全局容量占用仍在 MySQL 事务中校验，缓存只用于展示，不参与正确性判断。相关配置：

```bash
export APP_CACHE_PREFIX=repoagent:v1
export MONITORING_CACHE_TTL_MILLIS=2000
export ADMISSION_STATUS_CACHE_TTL_MILLIS=2000
# RAG 作用域索引、记忆向量和正文的缓存配置。
export MEMORY_RAG_CACHE_ENABLED=true
export MEMORY_RAG_CACHE_INDEX_TTL_SECONDS=20
export MEMORY_RAG_CACHE_VECTOR_TTL_SECONDS=86400
export MEMORY_RAG_CACHE_CONTENT_TTL_SECONDS=86400
```

两类缓存均采用 cache-aside；Redis 不可用、读取失败或反序列化失败时自动回源，不阻断修复任务主流程。RAG 将全局/仓库作用域索引、按 revision 固定的记忆向量和记忆正文放入 Redis，候选向量通过 MGET 批量读取后由 Java 计算余弦相似度。动态问题向量仅在当前修复流程内使用，不作为 Redis Key 或跨任务缓存，避免维护低命中的查询缓存。

并发召回是无锁读取；召回使用反馈按 `(taskId, memoryId, revision)` 幂等。语义更新使用短事务行锁、内容版本检查和唯一 fingerprint，embedding 网络请求在数据库事务外执行。SWE-bench 同一 Issue 产生的记忆会被服务端排除，防止题目自适应泄漏。
