# RepoAgent 记忆 RAG 设计

本文整理当前已实现的记忆 RAG 方案。MySQL 保存记忆、版本和审计记录；Qdrant 保存可重建的向量副本，Redis 保留为未启用 Qdrant 时的热点索引加速层。

## 1. 整体流程

记忆分为全局通用记忆和仓库专属记忆。每次修复同时搜索全局和当前仓库的经验：

```text
问题文本 → Embedding → 问题向量
                         ↓
            读取全局和当前仓库的记忆向量
                         ↓
              Java 计算余弦相似度，取 Top-K
                         ↓
            按 memoryId + revision 获取记忆 JSON
                         ↓
                    提供给 Agent 参考
```

MySQL 永久保存记忆和版本；启用 Qdrant 后，问题向量直接在 Qdrant 内完成相似度检索，命中结果只回源校验少量 memoryId + revision。未启用 Qdrant 时，Redis 缓存经常读取的索引、记忆向量和正文，Java 再计算余弦相似度。问题向量不作为 Redis Key，也不做跨任务 Embedding 结果缓存。

历史 SWE-bench 成功案例单独进入 `HISTORICAL_SWE_BENCH` 作用域。真实仓库任务会额外搜索该作用域；
`SWE_BENCH` 虚拟仓库任务只搜索正常全局和本仓库作用域，避免把已解决评测实例的经验带回同一评测集。

## 2. 问题用什么字段生成 Embedding

围绕“什么条件下，什么行为违反了什么预期”组织查询：

| 字段 | 内容 |
| --- | --- |
| object | 涉及的组件、输入类型或数据结构 |
| operation | 正在执行的操作 |
| symptom | 实际错误或异常表现 |
| expectedBehavior | 应当保持的行为 |
| triggerConditions | 触发问题的输入、配置或边界条件 |

例如最终输入：

```text
对象：pandas nullable 数值数组
操作：目标类型判断
现象：合法数值输入被识别为 unknown
预期：与普通数值数组保持一致的分类语义
条件：扩展 dtype，可能包含缺失值
```

这些字段共同生成一个问题向量。未知字段省略，不猜测根因；不加入任务 ID、完整日志或尚未验证的修复方案。

复用 Coordinator 已有分析，不新增专门提炼查询的子 Agent。首次召回没有结构化特征时，使用 Issue 标题和正文；Coordinator 运行后会从已落盘的公开语义证据提取这些字段，并在发生一次纠正修复前以精炼查询重新召回。没有足够的公开证据时不会猜测根因，仍沿用首次检索结果。

## 3. 记忆用什么字段生成 Embedding

新增 `retrievalText`，由现有 Memory Curator 总结成功案例时生成，描述这条经验适用于什么问题。

| 字段 | 是否参与 Embedding |
| --- | --- |
| title：经验标题 | 是 |
| retrievalText：对象、操作、故障特征、预期契约 | 是 |
| applicability：适用条件 | 是 |
| summary：摘要 | 旧记忆没有 retrievalText 时作为替代 |
| repairStrategy：修复步骤 | 否，命中后给 Agent 阅读 |
| verificationStrategy：验证方法 | 否，命中后给 Agent 阅读 |
| antiPatterns：应避免的做法 | 否，命中后给 Agent 阅读 |
| ID、revision、scope、质量分 | 否，用于定位、过滤和排序 |

拼接 `title + retrievalText + applicability`，生成一个记忆向量。例如：

```text
标题：扩展数值容器的类型识别兼容
问题特征：扩展 dtype 容器在通用分类逻辑中被错误识别，
应保持与普通数值容器相同的分类语义。
适用条件：扩展 dtype，尤其包含缺失值或跨越容器转换边界时。
```

全局记忆侧重可迁移的问题形态，弱化项目专名；仓库记忆可以保留相关公开 API 名和仓库约定。两者都不保存题目答案、完整 patch 或隐藏测试名。

## 4. Embedding 设置

使用后端已有的 OpenAI 兼容 Embedding 接口，单独配置向量模型：

```dotenv
MEMORY_EMBEDDING_ENABLED=true
MEMORY_EMBEDDING_BASE_URL=<Embedding服务地址>
MEMORY_EMBEDDING_API_KEY=<密钥>
MEMORY_EMBEDDING_MODEL=<Embedding模型ID>
MEMORY_EMBEDDING_TIMEOUT_MILLIS=15000
```

问题和记忆必须使用同一套模型与向量维度。更换模型或记忆编码方式后，重新生成记忆向量并使用新的索引版本标识，避免混用。下文的 `memory-retrieval-v1` 表示已确定的模型及编码版本；旧规则生成的向量标记为 `legacy-v0`，索引调度器会渐进补齐重建任务。

记忆向量生成后保存在 MySQL，内容版本未改变时可重复读取。未启用 Embedding 或服务失败时，保留现有关键词检索作为降级。

## 5. Redis 回退缓存及示例

业务上分为两层：第一层查找相关记忆，第二层获取完整正文。第一层内部拆成“作用域成员列表”和“独立向量”，方便单条更新与批量读取。

### 第一层：检索缓存

假设任务属于仓库 `repo-A`，全局有记忆 g1、g2，当前仓库有记忆 r1。示例使用简短 ID，实际使用 UUID。

作用域索引：

```text
Key: memory:index:global:memory-retrieval-v1
Value: [{"memoryId":"g1","revision":3}, {"memoryId":"g2","revision":1}]

Key: memory:index:repo:repo-A:memory-retrieval-v1
Value: [{"memoryId":"r1","revision":2}]
```

根据成员列表构造向量 Key，使用 MGET 批量读取：

```text
Key: memory:vector:g1:3:memory-retrieval-v1
Value: [0.8, 0.6]

Key: memory:vector:g2:1:memory-retrieval-v1
Value: [0.0, 1.0]

Key: memory:vector:r1:2:memory-retrieval-v1
Value: [0.96, 0.28]
```

以上二维向量仅用于演示。假设问题向量为 `[1.0, 0.0]`，Java 计算得到：

| 记忆 | 余弦相似度 |
| --- | --- |
| r1@2：仓库记忆 | 0.96 |
| g1@3：全局记忆 | 0.80 |
| g2@1：全局记忆 | 0.00 |

取 Top-2 时选择 r1@2、g1@3。问题向量用于比较读取到的记忆向量，不用于匹配 Redis Key。

### 第二层：正文缓存

全局和仓库记忆共用 `memoryId + revision` 的 Key 格式：

```text
Key: memory:content:g1:3
Value:
{
  "title": "扩展数值容器的类型识别兼容",
  "retrievalText": "扩展 dtype 容器进入通用分类逻辑时发生错误识别。",
  "applicability": "扩展 dtype、缺失值或容器转换边界。",
  "repairStrategy": "检查已有输入规范化路径，限定转换范围。",
  "verificationStrategy": "对照普通输入、扩展输入和缺失值输入。",
  "antiPatterns": "避免只枚举几个精确 dtype 字符串。"
}

Key: memory:content:r1:2
Value: 对应仓库记忆的完整 JSON，字段结构相同。
```

批量读取这两条正文并提供给 Agent，未命中的 g2 无需读取正文。

其他任务即使问题不同，也会反复读取相同的全局索引、仓库索引和热门记忆，因此这些缓存可以复用。repo-B 可以复用全局缓存，但不能召回 repo-A 的专属记忆。

### 缓存更新

- 作用域成员索引建议缓存 20 秒，记忆新增、更新或失效后使对应索引失效。
- 独立向量和版本正文建议缓存一天；revision 或模型版本改变后使用新 Key。
- 缓存未命中时从 MySQL 获取并回填；向量缓存缺失不意味着再次调用 Embedding API。
- 正文按指定 revision 读取，避免旧向量匹配新正文。新版本向量未就绪时可以走关键词降级。
- Redis 故障时回源 MySQL；权限、仓库范围、有效状态和同题来源排除仍需校验。

实际 Key 统一加项目配置前缀，例如 `repoagent:v1:`。普通 Redis 负责数据读取，Java 负责余弦计算；缓存加速重复读取，不会省去候选向量的比较。

### Redis 回退路径的并发保护

- 作用域索引使用随机短租约锁：只有一个请求回源 MySQL 并回填；其他请求短暂等待，仍未命中时本次跳过该作用域，避免缓存击穿把热点放大为 MySQL 候选池扫描。
- 索引 TTL 加少量随机抖动，避免大批 Key 在同一秒失效；正文和向量使用 `memoryId + revision` Key，新版本天然不会读取旧值。
- Redis 故障只影响加速层。生产上启用 Qdrant 时不再把故障回退为全候选向量扫描；未启用 Qdrant 时仍可走受 `max-candidates` 限制的 MySQL/关键词降级。

## 6. Qdrant 向量副本

Qdrant 的 point ID 由 `memoryId + contentRevision + embeddingProfile` 确定，payload 保存 scope、状态、版本、质量分和完整语义记忆 JSON。查询时先按全局/当前仓库 scope、ACTIVE 状态、embedding model/profile 过滤，再返回 Top-K；后端只校验这些命中记录在 MySQL 中是否仍为当前 revision。

| 情况 | 行为 |
| --- | --- |
| 正常但没有相似记忆 | 返回 `QDRANT_EMPTY`，不扫描 MySQL 全候选池 |
| Qdrant 超时、限流或熔断 | 返回 `MEMORY_UNAVAILABLE`，Agent 本轮不使用记忆继续修复 |
| Qdrant 命中旧版本 | MySQL revision 校验剔除该命中 |
| 新版本写入 | 先写新版本 point，再按 `memory_id + profile` 删除旧 point |

查询端有并发信号量和熔断器。连续失败后在冷却时间内快速返回空记忆结果，只允许一个恢复探针访问 Qdrant，防止下游故障拖慢 Repair Worker。索引任务会实时写入当前版本；低频对账任务会从 MySQL 重放当前 embedding，修复短暂写入失败或历史旧 point。

## 7. 记忆写入与并发合并

成功修复不会立即改写共享记忆。Worker 先创建不可变 observation，再由 Curator 写入 `CREATE`、`UPDATE` 或 `NO_CHANGE` proposal：

```text
成功修复 → observation → proposal(PENDING, baseVersion)
         → Memory Merge Worker 领取固定批次
         → 版本校验 → MySQL 原子发布新 revision → 异步索引 Qdrant
```

- proposal 先经历可配置的静置窗口（默认 60 秒），让同一版本的多个成功修复一起进入一个批次；窗口内不会被 Merge Worker 提前领取。
- UPDATE 必须携带召回时的 `baseVersion`。领取或提交时发现版本不同，proposal 标为 `STALE`，绝不自动 rebase 到新内容。
- 同一 `memoryId + baseVersion` 同时最多一个运行批次；批次带 workerId、lease token 和过期时间，崩溃后会释放提案供后续 Worker 重领。
- 合并后在批次开始期间迟到、仍基于旧版本的提案全部标记 `STALE`。新任务必须先召回新版本后再提出修改。
- CREATE 按作用域串行领取，最终仍以 `(scope_key, fingerprint)` 唯一约束去重。

这使 Qdrant、Redis 和 MySQL 的职责明确：MySQL 决定事实与版本，Qdrant 负责向量检索，Redis 只做可丢失的热点加速。
