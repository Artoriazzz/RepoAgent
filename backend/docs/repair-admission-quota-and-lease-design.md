# RepairTask 配额、入口背压与租约防重开发设计

更新日期：2026-09-18

## 实现状态

本设计已落地到后端与 Worker 协议：

- `V16` 创建工作流状态、配额、全局容量和阶段执行审计结构；`V17` 为历史任务回填提交者与活动计数。
- `POST /api/issues/{issueId}/repair-tasks`、批量创建与显式 retry 统一经过 `RepairAdmissionService`；默认每用户最多 3 条运行中工作流、每日 20 条。
- `GET /api/repair-capacity` 返回个人并发槽位、日额度、全局容量、阶段等待量和 ETA，监控页面同步展示个人额度。
- RabbitMQ 消息携带 `stageSequence`；领取和所有 Worker 写操作使用 `leaseToken + stageSequence + leaseEpoch`，阶段历史保存在 `repair_stage_execution`。
- Worker 领取采用数据库悲观锁，终态释放由 `capacity_released_at` 保障最多执行一次。

## 1. 文档目标

本文档定义 RepoAgent 平台在大量用户提交 RepairTask 时的容量治理方案，覆盖以下功能：

- 每个用户最多同时运行 3 条完整修复工作流，超过上限直接拒绝，不建立用户等待队列。
- 每个用户具有每日任务额度，重复幂等请求不重复扣减。
- 设置全局运行任务上限、Agent 阶段等待上限和 Outbox 积压上限。
- 在 HTTP 入口实施速率限制和业务背压，避免流量直接冲击 MySQL、RabbitMQ 和 Worker。
- 返回当前容量、剩余额度和预计开始等待时间。
- 重新划分工作流状态、Agent 阶段和阶段执行状态。
- 使用全局唯一任务 ID、阶段序号、租约令牌和 fencing epoch 防止重复执行及过期 Worker 覆盖结果。

本文档不包含 GitHub App、分支推送和 Draft PR 创建。上述能力可以在本方案稳定后接入，不影响任务调度模型。

## 2. 核心决策

### 2.1 不建立用户等待队列

用户提交任务时，系统只存在两种业务结果：

1. 满足用户额度和系统容量，任务被接受并立即成为运行中的完整工作流。
2. 不满足条件，任务直接被拒绝，不创建 RepairTask，不占用 RabbitMQ 队列。

“运行中的完整工作流”不表示某一时刻一定有 Worker 正在执行。任务在 Agent 之间交接时可能等待下一角色领取，但在最终成功、失败或取消前始终占用用户并发名额。

### 2.2 区分三种状态

现有 `RepairTaskStatus` 同时表达业务结果和 Agent 阶段，容易把等待 Implement 的任务统计为正在执行。新模型拆为：

| 维度 | 字段 | 作用 |
| --- | --- | --- |
| 工作流状态 | `workflow_status` | 表示完整修复任务是否仍在运行以及最终结果 |
| 当前阶段 | `current_stage` | 表示应该由哪类 Agent 处理 |
| 阶段状态 | `stage_status` | 表示当前阶段正在等待、执行、完成或失败 |

### 2.3 MySQL 是配额事实来源

- 用户并发、每日额度和全局容量以 MySQL 为最终事实来源。
- Redis 用于 HTTP 突发限流、容量查询缓存和 ETA 指标，不承担不可丢失的额度扣减。
- RepairTask、配额计数和 Outbox 必须在同一数据库事务内提交。

### 2.4 已接受任务不得在内部交接时被背压拒绝

入口容量检查只决定是否接受一个新工作流。任务一旦被接受，Coordinator 到 Implement、Validation 等内部阶段的交接必须继续完成，不能因为队列达到阈值而停在半途中。

因此系统同时设置：

- `global_max_inflight`：全局非终态工作流硬上限，用于保证最坏情况下的总任务规模。
- `global_max_waiting_stages`：当前等待 Agent 的阶段数量高水位，用于新任务入口背压。
- `outbox_backlog_limit`：未成功发布的 Outbox 事件高水位，用于消息系统异常时停止接收新任务。

## 3. 术语与不变量

### 3.1 术语

- 活跃工作流：`workflow_status = RUNNING` 的任务。
- 阶段等待：活跃工作流中 `stage_status = WAITING` 的任务。
- 阶段执行：活跃工作流中 `stage_status = RUNNING` 且租约有效的任务。
- 终态任务：`SUCCEEDED`、`FAILED` 或 `CANCELLED`。
- 阶段序号：同一任务中每次 Agent 交接递增的 `stage_sequence`。
- 租约代数：同一阶段每次重新领取时递增的 `lease_epoch`。

### 3.2 必须保持的不变量

1. 一个用户的活跃工作流数量不超过配置的 `max_active_workflows`，默认值为 3。
2. 一个 RepairTask 任意时刻只有一个当前阶段。
3. 一个阶段任意时刻最多只有一个有效 Worker 租约。
4. Worker 的任何写操作都必须匹配 `task_id + stage_sequence + lease_token + lease_epoch`。
5. 过期租约的 Worker 无法更新任务、保存阶段结果或触发下一阶段。
6. 同一 `submitted_by + idempotency_key` 只创建一个任务。
7. 同一 `task_id + stage_sequence` 只创建一个阶段 Outbox 事件。
8. 活跃计数只在非终态到终态的第一次转换中释放一次。
9. 内部纠正修复不增加每日任务额度，也不额外占用用户工作流名额。

现有 `UUID.randomUUID()` 生成的 RepairTask ID 可以继续作为全局任务 ID。任务 ID 解决的是身份唯一性，阶段序号和租约代数解决的是重复消息与过期 Worker 并发写入，两类问题不能互相替代。

## 4. 新状态模型

### 4.1 工作流状态

```java
public enum RepairWorkflowStatus {
    RUNNING,
    SUCCEEDED,
    FAILED,
    CANCELLED
}
```

新任务创建成功后直接进入 `RUNNING`，不再存在面向用户的 `QUEUED` 工作流状态。

### 4.2 Agent 阶段

继续使用现有 `RepairPipelineStage`：

```text
COORDINATOR
IMPLEMENT
VALIDATION
FAILURE_REVIEW
CORRECTIVE_IMPLEMENT
EXPERIENCE_MEMORY
```

允许的流转保持为：

```text
COORDINATOR -> IMPLEMENT -> VALIDATION
VALIDATION -> EXPERIENCE_MEMORY
VALIDATION -> FAILURE_REVIEW -> CORRECTIVE_IMPLEMENT -> VALIDATION
FAILURE_REVIEW -> EXPERIENCE_MEMORY
EXPERIENCE_MEMORY -> 工作流终态
```

Corrective Implement 最多执行一次，由现有 `correction_attempt/max_correction_attempts` 约束。

### 4.3 阶段状态

```java
public enum RepairStageStatus {
    WAITING,
    RUNNING,
    SUCCEEDED,
    FAILED
}
```

状态变化：

```text
创建任务：RUNNING + COORDINATOR + WAITING
Worker 领取：RUNNING + COORDINATOR + RUNNING
阶段交接：RUNNING + IMPLEMENT + WAITING
不可恢复错误：FAILED + 当前阶段 + FAILED
正常结束：SUCCEEDED + EXPERIENCE_MEMORY + SUCCEEDED
用户取消：CANCELLED + 当前阶段 + 当前状态
```

`PATCH_READY` 不再作为工作流状态。补丁是否存在由 `patch_artifact_path` 和阶段事件表达。

## 5. 数据库设计

### 5.1 repair_task 调整

新增字段：

```sql
ALTER TABLE repair_task
    ADD COLUMN submitted_by_user_id CHAR(36) NULL,
    ADD COLUMN workflow_status VARCHAR(32) NULL,
    ADD COLUMN current_pipeline_stage VARCHAR(64) NULL,
    ADD COLUMN stage_status VARCHAR(32) NULL,
    ADD COLUMN stage_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN lease_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN capacity_released_at TIMESTAMP(6) NULL,
    ADD COLUMN admitted_at TIMESTAMP(6) NULL;
```

保留现有字段：

- `worker_id`
- `lease_token`
- `lease_expires_at`
- `last_heartbeat_at`
- `cancel_requested_at`
- `pipeline_stage`，迁移完成后由 `current_pipeline_stage` 替代
- `version`，继续用于 JPA 乐观锁

索引与唯一约束：

```sql
CREATE UNIQUE INDEX uk_repair_task_user_idempotency
    ON repair_task(submitted_by_user_id, idempotency_key);

CREATE INDEX ix_repair_task_user_workflow
    ON repair_task(submitted_by_user_id, workflow_status);

CREATE INDEX ix_repair_task_stage_waiting
    ON repair_task(workflow_status, current_pipeline_stage, stage_status, created_at);

CREATE INDEX ix_repair_task_expired_lease
    ON repair_task(workflow_status, stage_status, lease_expires_at);
```

### 5.2 用户配额表

```sql
CREATE TABLE repair_user_quota (
    user_id CHAR(36) PRIMARY KEY,
    max_active_workflows INT NOT NULL DEFAULT 3,
    daily_task_limit INT NOT NULL DEFAULT 20,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_quota_user FOREIGN KEY (user_id) REFERENCES app_user(id)
);
```

没有用户专属记录时使用系统默认配置。管理端后续可为特定用户调整额度。

### 5.3 用户运行计数

```sql
CREATE TABLE repair_user_runtime (
    user_id CHAR(36) PRIMARY KEY,
    active_workflows INT NOT NULL DEFAULT 0,
    row_version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repair_runtime_user FOREIGN KEY (user_id) REFERENCES app_user(id)
);
```

`active_workflows` 从任务创建成功开始占用，到工作流第一次进入终态时释放。

### 5.4 每日额度表

```sql
CREATE TABLE repair_daily_usage (
    id CHAR(36) PRIMARY KEY,
    user_id CHAR(36) NOT NULL,
    usage_date DATE NOT NULL,
    accepted_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    UNIQUE KEY uk_repair_daily_user_date(user_id, usage_date),
    CONSTRAINT fk_repair_daily_user FOREIGN KEY (user_id) REFERENCES app_user(id)
);
```

日期统一按 UTC 计算，接口返回下一次重置时间。内部 Failure Review 和 Corrective Implement 不增加 `accepted_count`。

任务一旦被接受，当日额度不因用户取消、修复失败或环境错误而退回，避免反复提交和取消绕过额度。用户主动创建显式 retry 属于新的完整工作流，需要重新占用并发名额和每日额度；系统内部的一次 Corrective Implement 不计为新任务。

### 5.5 全局容量表

```sql
CREATE TABLE repair_system_capacity (
    capacity_key VARCHAR(64) PRIMARY KEY,
    active_workflows INT NOT NULL DEFAULT 0,
    row_version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP(6) NOT NULL
);
```

初始插入唯一记录 `capacity_key = 'GLOBAL'`。全局活跃工作流计数与任务创建、终态转换在同一事务中更新。

### 5.6 阶段执行记录

新增不可变历史表，用于审计、ETA 统计和阶段幂等：

```sql
CREATE TABLE repair_stage_execution (
    id CHAR(36) PRIMARY KEY,
    task_id CHAR(36) NOT NULL,
    stage_sequence BIGINT NOT NULL,
    pipeline_stage VARCHAR(64) NOT NULL,
    stage_status VARCHAR(32) NOT NULL,
    lease_epoch BIGINT NOT NULL DEFAULT 0,
    worker_id VARCHAR(255) NULL,
    started_at TIMESTAMP(6) NULL,
    finished_at TIMESTAMP(6) NULL,
    duration_ms BIGINT NULL,
    error_code VARCHAR(255) NULL,
    error_summary TEXT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    UNIQUE KEY uk_repair_stage_sequence(task_id, stage_sequence),
    CONSTRAINT fk_repair_stage_task FOREIGN KEY (task_id) REFERENCES repair_task(id)
);
```

### 5.7 Outbox 唯一键调整

现有 Outbox 以 `task + stage name` 查重，无法自然区分纠正修复前后的两次 Validation。调整为：

```text
event_key = taskId + ":" + stageSequence
```

为 `repair_outbox_event.event_key` 建立唯一索引。消息重发只更新同一事件的发布状态，不创建第二条业务事件。

## 6. 配置项

```yaml
app:
  repair:
    quota:
      default-max-active-workflows: ${REPAIR_USER_MAX_ACTIVE_WORKFLOWS:3}
      default-daily-task-limit: ${REPAIR_USER_DAILY_TASK_LIMIT:20}
    capacity:
      global-max-inflight: ${REPAIR_GLOBAL_MAX_INFLIGHT:100}
      global-max-waiting-stages: ${REPAIR_GLOBAL_MAX_WAITING_STAGES:80}
      outbox-backlog-limit: ${REPAIR_OUTBOX_BACKLOG_LIMIT:500}
    admission:
      requests-per-minute: ${REPAIR_CREATE_REQUESTS_PER_MINUTE:30}
      burst-capacity: ${REPAIR_CREATE_BURST_CAPACITY:10}
    eta:
      sample-window-hours: ${REPAIR_ETA_SAMPLE_WINDOW_HOURS:24}
      fallback-stage-seconds: ${REPAIR_ETA_FALLBACK_STAGE_SECONDS:60}
```

用户并发默认值固定为 3，但保留用户表配置能力，方便管理员降低或冻结账号额度。

## 7. 创建任务入口

### 7.1 单任务创建接口

保持现有接口：

```http
POST /api/issues/{issueId}/repair-tasks
X-User-Id: <user-id>
Idempotency-Key: <client-generated-key>
```

成功返回 `202 Accepted`：

```json
{
  "id": "0199...",
  "workflowStatus": "RUNNING",
  "currentStage": "COORDINATOR",
  "stageStatus": "WAITING",
  "stageSequence": 0,
  "estimatedStartWaitSeconds": 42,
  "quota": {
    "active": 2,
    "maxActive": 3,
    "dailyUsed": 7,
    "dailyLimit": 20,
    "resetAt": "2026-09-19T00:00:00Z"
  }
}
```

### 7.2 创建事务

`RepairTaskAdmissionService.create()` 按固定顺序执行：

1. 校验用户、仓库权限和 Issue 基线。
2. 按 `submitted_by_user_id + idempotency_key` 查询已有任务；存在则直接返回，不检查和扣减额度。
3. 获取 Redis 突发限流结果；超限直接返回 429。
4. 锁定 `repair_system_capacity` 的 `GLOBAL` 行。
5. 锁定或创建 `repair_user_runtime` 行。
6. 锁定或创建当天 `repair_daily_usage` 行。
7. 读取用户配额，依次检查账号启用、用户并发、每日额度和全局背压。
8. 创建 RepairTask，状态为 `RUNNING + COORDINATOR + WAITING`。
9. 创建 `stage_sequence = 0` 的阶段执行记录。
10. 用户和全局 `active_workflows + 1`，每日 `accepted_count + 1`。
11. 创建 Coordinator Outbox 事件。
12. 提交事务后计算并返回 ETA。

固定加锁顺序为：

```text
system capacity -> user runtime -> daily usage -> issue -> repair task
```

所有创建、批量创建、重试和后台操作必须遵守相同顺序，避免数据库死锁。

首次创建用户运行计数或每日额度行时，应先执行 `INSERT ... ON DUPLICATE KEY UPDATE` 保证记录存在，再执行 `SELECT ... FOR UPDATE`。不能采用“查询不存在后直接插入”的方式，否则同一用户的并发首请求会发生唯一键竞争。

### 7.3 并发幂等

两个相同幂等键请求可能同时通过首次查询，因此必须依赖数据库唯一约束做最终防线。

推荐实现：

- 外层非事务方法负责捕获 `DataIntegrityViolationException`。
- 内层 `REQUIRES_NEW` 事务执行创建。
- 唯一约束冲突后，在新事务中查询并返回已存在任务。
- 重复请求不得再次增加用户、每日和全局计数。

## 8. 入口背压

入口背压分为三层。

### 8.1 HTTP 突发限流

Redis 使用 Lua 脚本实现用户级 Token Bucket：

```text
key: repair:admission:rate:{userId}
rate: 30 requests/minute
burst: 10
```

该层用于抵挡短时间重复点击和脚本突发，不替代每日业务额度。Redis 不可用时降级为继续执行 MySQL 业务检查，同时记录告警。

### 8.2 用户业务限制

以下任一条件成立则拒绝：

- 用户已禁用 RepairTask。
- `active_workflows >= max_active_workflows`。
- `accepted_count >= daily_task_limit`。

用户并发达到 3 时不创建任务，因此不存在“第 4 条排队任务”。

### 8.3 全局容量限制

以下任一条件成立则停止接受新任务：

- `global.active_workflows >= global_max_inflight`。
- 当前 `RUNNING + WAITING` 的任务数达到 `global_max_waiting_stages`。
- Outbox 的 `PENDING + FAILED` 数量达到 `outbox_backlog_limit`。
- RabbitMQ 或 MySQL 健康检查失败。
- 当前在线 Coordinator Worker 为 0。

阶段等待数可以从 MySQL 聚合并短时缓存到 Redis。该值是入口高水位，不阻止已接受任务继续向下一阶段交接。

### 8.4 错误响应

统一返回 Problem Details：

| 场景 | HTTP | errorCode | 说明 |
| --- | --- | --- | --- |
| 用户并发已满 | 429 | `USER_ACTIVE_LIMIT_REACHED` | 当前已有 3 条运行工作流 |
| 每日额度耗尽 | 429 | `USER_DAILY_LIMIT_REACHED` | 返回 `resetAt` |
| 请求过快 | 429 | `ADMISSION_RATE_LIMITED` | 返回 `retryAfterSeconds` |
| 全局运行上限 | 503 | `SYSTEM_INFLIGHT_LIMIT_REACHED` | 返回建议重试时间 |
| 阶段队列过载 | 503 | `SYSTEM_STAGE_BACKLOG_HIGH` | 暂停接收新任务 |
| Outbox 积压 | 503 | `OUTBOX_BACKLOG_HIGH` | 消息链路异常 |
| 重复幂等请求 | 200/202 | 无错误 | 返回原任务 |

## 9. 批量创建语义

现有批量接口继续支持部分成功：

```http
POST /api/repair-tasks/batch
```

规则：

- 保留请求中 Issue 的顺序。
- 每个 Issue 使用 `batchKey + issueId` 作为子幂等键。
- 每接受一个任务立即占用一个用户并发名额和一个每日额度。
- 用户只剩 2 个并发名额时，批次最多接受前 2 个合法任务，其余返回 `USER_ACTIVE_LIMIT_REACHED`。
- 同一批次中的幂等重复项返回已有任务，不占用新额度。
- 单项失败不回滚已成功项，响应继续使用现有 `accepted/rejected/items` 结构。

批量循环不能持有一个超长事务。每个子任务调用独立的 Admission 事务，以降低锁持有时间。

## 10. 预计等待时间

### 10.1 指标来源

为每个 Agent 阶段维护最近 24 小时指标：

- 已完成阶段数量。
- 阶段 P50 和 P90 耗时。
- EWMA 平均耗时。
- 在线 Worker 数。
- 当前 WAITING 数量。
- 当前有效租约数量。

指标由 `repair_stage_execution` 聚合，结果缓存于 Redis：

```text
repair:eta:{stage}:ewma_ms
repair:eta:{stage}:p90_ms
repair:eta:{stage}:online_workers
repair:eta:{stage}:waiting_count
```

### 10.2 新任务预计开始时间

新任务首先进入 Coordinator 队列：

```text
effectiveWorkers = max(1, min(在线 Coordinator 数, 配置容量))
wavesAhead = floor(Coordinator WAITING 数 / effectiveWorkers)
estimatedStartWait = wavesAhead * Coordinator EWMA 耗时
```

返回值按 10 秒向上取整，并同时计算 P50/P90 范围。前端显示“预计 30 秒至 1 分钟开始”，避免给出不可信的精确时间。

### 10.3 运行中阶段 ETA

任务详情可以返回：

- `queuePositionInStage`
- `estimatedStageStartAt`
- `estimatedStageFinishAt`

完整工作流包含失败分支，暂不承诺精确完成时间。可用历史失败概率对 Failure Review 和 Corrective Implement 做加权估算，但界面应标注为“预计”。

## 11. RabbitMQ 消息协议

消息升级为：

```json
{
  "messageId": "outbox-event-id",
  "taskId": "0199...",
  "issueId": "...",
  "stage": "IMPLEMENT",
  "stageSequence": 1,
  "baseSha": "...",
  "branchName": "repoagent/..."
}
```

Worker 不根据消息内容直接判定任务可执行，必须先调用后端 claim 接口。消息只是唤醒信号，MySQL 中的任务状态才是执行权限来源。

RabbitMQ 保持：

- 每个 Agent 角色独立队列。
- 每个消费者 `prefetch = 1`。
- 消息持久化。
- 发布端使用 publisher confirm。
- 传输错误使用延迟重试，不进行立即反复 requeue。

## 12. 租约与重复执行防护

### 12.1 原子领取

Worker 调用 claim 时提交：

```json
{
  "workerId": "implement-2",
  "stage": "IMPLEMENT",
  "stageSequence": 1,
  "leaseSeconds": 90
}
```

后端使用条件更新或悲观锁保证只有一个 Worker 成功：

```sql
UPDATE repair_task
SET stage_status = 'RUNNING',
    worker_id = :workerId,
    lease_token = :leaseToken,
    lease_epoch = lease_epoch + 1,
    lease_expires_at = :expiresAt,
    last_heartbeat_at = :now
WHERE id = :taskId
  AND workflow_status = 'RUNNING'
  AND current_pipeline_stage = :stage
  AND stage_sequence = :stageSequence
  AND stage_status = 'WAITING';
```

更新行数为 1 表示领取成功；更新行数为 0 时重新读取任务并分类处理：

- 工作流已终态：确认并丢弃重复消息。
- `stage_sequence` 落后：确认并丢弃旧消息。
- 当前阶段不同：确认并丢弃旧消息。
- 当前阶段已有有效租约：使用延迟重试，不立即循环。

### 12.2 Fencing epoch

全局唯一 `task_id` 只能说明消息属于哪个任务，不能阻止旧 Worker 在租约过期后回写。因此所有 Worker 写接口必须携带：

```text
taskId
stageSequence
leaseToken
leaseEpoch
```

后端更新条件必须同时匹配四项。任务被重新领取时 `lease_epoch + 1`，旧 Worker 即使完成耗时命令，也无法写入结果。

### 12.3 心跳续租

- Worker 每 20 秒续租一次，默认租约 90 秒。
- 续租只允许匹配当前 lease token 和 epoch 的 Worker。
- LLM 请求、Docker 验证和文件操作期间均由独立心跳线程续租。
- 连续续租失败时，Worker 应终止本地子进程并停止写回。

### 12.4 阶段交接事务

阶段完成接口在一个事务中执行：

1. 校验工作流仍为 RUNNING。
2. 校验 stage、stage sequence、lease token 和 lease epoch。
3. 将当前 `repair_stage_execution` 标记为 SUCCEEDED 或 FAILED。
4. 保存本阶段摘要、Patch 或验证信息。
5. 计算下一阶段。
6. `stage_sequence + 1`。
7. 更新 `current_pipeline_stage` 和 `stage_status = WAITING`。
8. 清除当前租约字段。
9. 创建下一阶段执行记录。
10. 创建唯一 Outbox 事件。

事务提交后 Worker 才确认 RabbitMQ 消息。

### 12.5 租约过期恢复

Recovery Service 扫描：

```text
workflow_status = RUNNING
stage_status = RUNNING
lease_expires_at < now
```

恢复事务：

- 将阶段重新设为 WAITING。
- 清除旧 lease token、worker 和过期时间。
- 保留并记录当前 `lease_epoch`，下一次 claim 再递增。
- 将当前 stage sequence 的 Outbox 事件重新设为 PENDING。
- 写入 `LEASE_EXPIRED` 事件和恢复指标。

若 Validation 已生成 Patch，但 harness 过程中租约过期，继续沿用现有策略：保留 Patch，将评测标为环境错误或重新派发 Validation，不得重新运行 Implement 覆盖已有补丁。

## 13. 工作流终止与额度释放

统一由 `RepairWorkflowCompletionService` 完成终态转换：

```text
RUNNING -> SUCCEEDED
RUNNING -> FAILED
RUNNING -> CANCELLED
```

同一事务内：

1. 条件更新任务，要求 `workflow_status = RUNNING` 且 `capacity_released_at IS NULL`。
2. 写入终态、完成时间和 `capacity_released_at`。
3. 用户 `active_workflows - 1`。
4. 全局 `active_workflows - 1`。
5. 清理租约。
6. 写入最终事件。

`capacity_released_at` 是防止重复释放额度的最终防线。计数不得小于 0，出现异常时记录严重告警。

用户取消运行任务时先设置 `cancel_requested_at`。Worker 停止后进入 CANCELLED 并释放容量；若 Worker 失联，由 Recovery Service 在租约过期后完成取消。

## 14. 计数纠偏

即使所有写操作都在事务内，人工数据修改、旧版本服务和迁移仍可能造成计数漂移。新增定时 Reconciliation Job：

- 每 5 分钟统计真实 `workflow_status = RUNNING` 数量。
- 按用户统计真实活跃工作流数量。
- 与 `repair_user_runtime` 和 `repair_system_capacity` 比较。
- 不一致时修正计数并记录 `repair.capacity.reconciled` 指标。
- 差异超过阈值时触发告警。

该任务只负责纠偏，不参与正常请求的额度判断。

## 15. 查询接口

新增用户容量接口：

```http
GET /api/repair-capacity
X-User-Id: <user-id>
```

响应：

```json
{
  "acceptingTasks": true,
  "activeWorkflows": 2,
  "maxActiveWorkflows": 3,
  "dailyUsed": 7,
  "dailyLimit": 20,
  "dailyRemaining": 13,
  "resetAt": "2026-09-19T00:00:00Z",
  "globalInflight": 31,
  "globalInflightLimit": 100,
  "waitingStages": {
    "COORDINATOR": 4,
    "IMPLEMENT": 8,
    "VALIDATION": 2,
    "FAILURE_REVIEW": 0,
    "CORRECTIVE_IMPLEMENT": 0,
    "EXPERIENCE_MEMORY": 1
  },
  "estimatedCoordinatorWaitSeconds": 40,
  "rejectionReason": null
}
```

前端创建按钮应在容量不足时禁用并展示明确原因，但后端仍必须执行完整校验，不能依赖前端限制。

## 16. 监控与指标

新增 Micrometer 指标：

```text
repair.admission.accepted
repair.admission.rejected{reason}
repair.workflow.active
repair.workflow.active{userId} 仅在用户量可控时启用
repair.stage.waiting{stage}
repair.stage.running{stage}
repair.stage.duration{stage,result}
repair.lease.claim_conflict{stage}
repair.lease.expired{stage}
repair.lease.stale_write_rejected{stage}
repair.capacity.reconciled
repair.eta.error_seconds{stage}
```

不要将高基数的 `taskId` 放入指标标签。具体任务信息保存在事件表和日志中。

监控前端调整：

- 顶部增加“我的运行任务 2/3”和“今日额度 7/20”。
- 创建任务前展示预计等待时间。
- “排队中”继续按六类 Agent 展示阶段等待数。
- “修复中”只展示持有有效租约的阶段执行数量。
- 任务详情同时展示工作流状态、当前阶段、阶段状态、stage sequence 和 lease epoch。

## 17. Redis 使用边界

Redis 保存：

- HTTP Token Bucket。
- 容量查询的 1 至 3 秒短缓存。
- 各阶段 EWMA、P50、P90 和 ETA 数据。
- 可选的 SSE 节点间事件通知。

Redis 不保存：

- 唯一的用户每日额度事实。
- 唯一的用户活跃工作流计数。
- 唯一的任务状态或租约所有权。

Redis 清空或短时不可用不得造成额度永久丢失或允许超过 3 条工作流。

## 18. 代码模块规划

建议新增或调整：

```text
repair/admission/
  RepairAdmissionService
  RepairAdmissionPolicy
  RepairCapacityService
  RepairCapacityController
  RepairQuotaProperties
  RepairBackpressureReason
  UserRepairQuota
  UserRepairRuntime
  RepairDailyUsage
  RepairSystemCapacity

repair/workflow/
  RepairWorkflowStatus
  RepairStageStatus
  RepairStageExecution
  RepairWorkflowCompletionService
  RepairStageLeaseService
  RepairCapacityReconciliationJob

repair/eta/
  RepairEtaService
  RepairStageMetricsAggregator
```

现有 `RepairTaskService` 逐步缩小为查询和用户操作入口，创建事务交给 Admission Service，Worker claim、handoff 和 complete 交给 Stage Lease Service 与 Workflow Completion Service。

## 19. 数据迁移方案

建议新增 Flyway `V16__add_repair_admission_and_workflow_state.sql`。

迁移步骤：

1. 先以可空字段增加新列和新表。
2. 为现有任务回填 `submitted_by_user_id`。当前演示数据可使用仓库 Owner；生产数据必须根据任务创建审计记录回填。
3. 根据旧状态回填工作流状态：
   - `CANCELLED -> CANCELLED`
   - `FAILED -> FAILED`
   - 已评测 `RESOLVED -> SUCCEEDED`
   - 已评测 `UNRESOLVED/HARNESS_ERROR -> FAILED`
   - 其余非终态 -> RUNNING
4. 使用现有 `pipeline_stage` 回填 `current_pipeline_stage`。
5. 有有效租约的任务回填 `stage_status = RUNNING`，其余活跃任务回填 `WAITING`。
6. 为活跃任务建立当前阶段执行记录。
7. 按真实任务数据初始化用户与全局活跃计数。
8. 新代码双写旧字段和新字段，观察一个版本周期。
9. 前端和查询切换到新字段。
10. 稳定后删除旧 `task_status` 及兼容逻辑。

迁移期间通过功能开关控制：

```text
REPAIR_WORKFLOW_STATE_V2_ENABLED
REPAIR_ADMISSION_ENABLED
REPAIR_FENCING_EPOCH_REQUIRED
```

## 20. 测试计划

### 20.1 配额单元测试

- 用户第 1 至 3 条任务成功，第 4 条返回 `USER_ACTIVE_LIMIT_REACHED`。
- 任一任务进入终态后，用户可以再次提交。
- 达到每日额度后拒绝提交。
- 跨 UTC 日期后使用新的每日计数。
- 幂等重复请求不增加 active 或 daily 计数。
- 内部 Corrective Implement 不增加额度。

### 20.2 并发集成测试

- 同一用户并发发送 20 个创建请求，最终最多 3 个不同任务成功。
- 20 个相同幂等键请求只产生一个任务和一个 Outbox 事件。
- 多用户并发创建时不超过全局上限。
- 批量请求只接受剩余并发名额内的项目。
- 事务异常时任务、额度和 Outbox 全部回滚。

### 20.3 租约竞争测试

- 3 个 Worker 同时领取同一阶段，只有一个成功。
- 租约过期后新 Worker 的 epoch 大于旧 Worker。
- 旧 Worker 使用旧 epoch 上报结果时返回 409。
- 重复阶段消息、旧 stage sequence 和终态任务消息被安全确认。
- 阶段交接重复调用只产生一个下一阶段 Outbox 事件。

### 20.4 恢复测试

- Worker 在 claim 后崩溃，租约过期后任务重新进入 WAITING。
- Worker 在阶段交接事务提交前崩溃，RabbitMQ 消息重新投递后可继续执行。
- Worker 在事务提交后、RabbitMQ ack 前崩溃，重复消息被识别为旧 sequence。
- Outbox 发布失败后重试，超过次数进入 DEAD。
- 计数被人工修改后 Reconciliation Job 可以恢复。

### 20.5 背压与 ETA 测试

- 阶段等待数达到阈值时新任务返回 503，已接受工作流继续流转。
- Coordinator Worker 全部离线时拒绝新任务。
- Redis 不可用时业务额度检查仍正确。
- ETA 随 Worker 数量和队列长度变化。
- 对已完成任务比较预测和实际等待时间，记录误差指标。

## 21. 验收标准

功能完成后必须满足：

1. 同一用户无论单请求、批量请求还是并发请求，最多存在 3 条 RUNNING 工作流。
2. 第 4 条任务不落库、不写 Outbox、不进入 RabbitMQ，并返回明确错误码。
3. 每日额度在并发和幂等场景下不会超发或重复扣减。
4. 达到全局容量或消息积压阈值时，新任务被入口背压拒绝。
5. 创建成功响应包含用户剩余额度和预计开始等待时间。
6. 任意阶段最多只有一个 Worker 能取得有效租约。
7. 租约过期后，旧 Worker 的所有写回均被 fencing epoch 拒绝。
8. 重复 RabbitMQ 消息不会重复执行已完成阶段。
9. 工作流进入终态后，用户和全局活跃计数只释放一次。
10. 监控页能分别显示工作流状态、六类 Agent 阶段队列和实际运行中的 Worker。

## 22. 推荐实施顺序

第一阶段：状态与防重基础。

- 增加 workflow、stage status、stage sequence 和 lease epoch。
- 升级 RabbitMQ 消息与 Worker 写接口。
- 完成 claim、handoff、complete 的 fencing 校验。

第二阶段：用户与全局准入。

- 增加 quota、runtime、daily usage 和 system capacity 表。
- 实现 Admission Service 和统一终态容量释放。
- 接入单任务、批量任务和显式重试入口。

第三阶段：背压和 ETA。

- 增加 Redis Token Bucket。
- 增加阶段积压与 Outbox 积压检查。
- 增加 ETA 聚合、容量接口和前端展示。

第四阶段：恢复与上线。

- 增加计数纠偏、租约恢复和告警指标。
- 执行并发、故障和压力测试。
- 使用功能开关灰度切换到新状态模型。
