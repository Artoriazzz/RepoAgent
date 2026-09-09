# 第四周学习讲义：记忆、实验与评测

## 学习目标

完成本周后，你应该能回答四个问题：

1. RepoAgent 保存什么记忆，如何召回，为什么召回不等于采用。
2. 模式一如何把失败修复过程变成经验，模式二为何只能读取冻结记忆。
3. SWE-bench 的 F2P、P2P、resolved、harness error 分别代表什么。
4. 如何设计一个可信实验，判断瓶颈位于定位、实现、验证还是环境。

本周必须理解的代码：

1. `repo_agent/core/memory.py`：记忆文件、作用域、索引、BM25 召回。
2. `repo_agent/core/experience.py`：双记忆、模式策略、失败反馈和记忆提炼。
3. `scripts/swebench_batch.py`：批量运行、模式隔离、harness 和阶段归因。
4. `repo_agent/core/repair_memory.py`：检索结果如何经过 applicability check。

第二层阅读：

5. `repo_agent/tools/memory_tools.py`：模型如何读写记忆。
6. `repo_agent/core/verification.py`：公开验证与环境失败的分类。
7. `tests/test_memory.py`、`tests/test_experience.py`、`tests/test_swebench_batch.py`：不变量的可执行定义。

不要从头顺序读完 `swebench_batch.py`。先跟这条主链：

```text
main
  -> load_instances
  -> run_instance_once
  -> run_agent_pass
  -> validate_model_patch
  -> run_swebench_harness
  -> finalize_stage_attribution
  -> prediction_for
```

## 1. 记忆系统解决什么问题

LLM 的参数不会因为一次运行自动更新。所谓“持续进化”，在本项目中不是在线训练，而是：

```text
历史成功轨迹
  -> 独立验证
  -> 抽象与去重
  -> 持久化记忆
  -> 新任务词法召回
  -> 当前源码适用性检查
  -> 作为软指导影响决策
```

这是一种检索增强的经验复用。它改变的是下一次推理的上下文，不是模型权重。

记忆系统必须同时控制两类风险：

- **遗忘**：历史上已经证明错误的做法被反复尝试。
- **污染**：旧任务的路径、补丁或特殊规则被错误搬到新任务。

因此，记得更多不一定更好；可追溯、可筛选和可撤销更重要。

## 2. MemoryStore：存储层而不是真理层

`MemoryStore` 使用带 frontmatter 的 Markdown 文件，而不是向量数据库：

```markdown
---
name: normalize before predicate
description: nullable numeric arrays require normalization before type checks
type: semantic_insight
role: implement
provenance: dev-experiment-v3
---

正文……
```

这样设计的优点是易读、可审计、能进 Git、方便手动修订。缺点是并发写入、规模化检索和复杂语义匹配能力有限。

### 2.1 两种作用域

```python
MemoryStore.for_project(cwd)
MemoryStore.for_scope("repo:scikit-learn/scikit-learn")
```

- `for_project` 对真实路径做 hash，适合普通本地仓库，避免项目间污染。
- `for_scope` 使用稳定逻辑标识。SWE-bench 每道题的 checkout 路径不同，但同仓库经验仍需共享，所以不能只依赖路径。

面试时要指出：作用域不是检索过滤的小细节，而是长期记忆的安全边界。

### 2.2 索引与正文分离

索引只保留名称、描述、类型、角色、来源和修改时间；正文按需读取。这样 system prompt 不会无条件塞入全部记忆。

`MAX_RECALLED_MEMORIES`、单文件大小和总记忆大小限制同时控制 token 与磁盘增长。过期记忆会带 freshness warning，提醒 Agent 必须重新检查当前源码。

### 2.3 BM25 为什么够用

episode 和 semantic insight 通过 `_recall_bm25()` 排序，文档由以下内容组成：

```text
name + description + content
```

BM25 是词法相关性模型：稀有关键词权重大，并对长文档做归一化。它便宜、确定、可解释，适合当前小规模记忆库。

它不知道代码语义。两个文本同时出现 `dtype` 不代表修复契约相同，所以 BM25 只能负责召回候选，不能决定采用。

## 3. 三种修复记忆

### 3.1 Episodic Memory：一次经历的紧凑示范

一条 episode 描述：

```text
问题签名
  -> 初始证据
  -> 失败尝试
  -> 失败反馈
  -> 成功的结构化修改
  -> 独立验证证据
```

它保存“输入到输出的关系”，但不保存完整 diff、instance id、gold patch、隐藏测试或一次性路径。

episode 主要注入 Implement。它最有价值的部分通常不是成功代码，而是被验证为错误的假设。例如：“在消费者处增加宽类型判断”失败，而“在公共生产者处复用已有标准化路径”成功。

### 3.2 Semantic Memory：跨案例的抽象规则

semantic insight 是从一个或多个成功案例抽象出的角色级规则：

- `coordinator`：如何识别契约层和修改边界。
- `implement`：如何落实语义关系并避免已知反模式。
- `review`：如何检查共享核心路径上的回归风险。

它不讲某道题怎么改，而讲遇到某类证据时应该如何推理。相似 insight 使用 ADD、EDIT、REMOVE 维护，不应重复堆积。

### 3.3 Repair Recipe：早期的策略记忆

recipe 保存 recognition、strategy、verification、anti-pattern 等抽象步骤。当前双记忆主线更强调 episode 与 role-specific semantic insight，recipe 可视为兼容保留的早期形式。

三者的差别可以这样记：

| 类型 | 回答的问题 | 主要粒度 |
|---|---|---|
| Episode | 上次类似失败怎样转成成功 | 单次轨迹 |
| Semantic insight | 多次经历说明了什么一般规则 | 角色规则 |
| Recipe | 这类问题通常按什么步骤处理 | 修复策略 |

## 4. 召回不等于采用

完整过程有三层：

```text
Retrieval       文本上可能相关吗？
Applicability   当前任务满足这条经验的前提吗？
Adoption        当前源码证据足以采用其中哪一部分？
```

`repair_memory.py` 中的 `evaluate_repair_recipe_applicability()` 会从任务和 recipe 提取签名词，检查识别条件是否命中。episode 注入 prompt 后也要求逐项比较：

1. 问题签名是否一致。
2. 当前 predicate/contract 是否一致。
3. Coordinator 要求的改法是否复现了历史失败模式。

记忆始终是软指导。当前 problem statement、当前源码和当前失败证据优先级更高。

为什么不能把召回结果直接变成硬约束？因为历史经验可能陈旧、来源不同、只在某种调用链成立。直接硬化会把检索误差放大成错误 patch。

## 5. 模式策略是一张能力矩阵

`repair_mode_policy()` 不只是配置便捷函数，它实现实验隔离：

| 能力 | standard | experience-build | benchmark |
|---|---:|---:|---:|
| Semantic recall | 是 | 否 | 是 |
| Episodic recall | 否 | 否 | 是 |
| 写记忆 | 否 | 是 | 否 |
| 失败反馈纠正 | 否 | 是 | 否 |
| 最大纠正次数 | 0 | 1 | 0 |

### 5.1 模式一：Experience Build

模式一期望形成真正的失败到成功轨迹：

```text
公开 issue
  -> Coordinator + Implement 生成初始 patch
  -> 独立 harness
  -> 若未解决且不是环境错误：FailureReview
  -> 最多一次 Corrective Implement
  -> 最终独立 harness
  -> 仅 resolved 时由 Curator 写双记忆
```

模式一关闭旧记忆召回，目的是避免“使用某条记忆修成功，再把同一结论写回去”的循环自证。它是离线经验构建，不是排行榜推理。

### 5.2 模式二：Benchmark

```text
公开 issue + 冻结记忆快照
  -> Coordinator + Implement
  -> 唯一最终 patch
  -> harness 只评一次
  -> 结果不反馈给 Agent
  -> 不写记忆
```

这是 pass@1 语义：Agent 在提交 patch 前不能看到该题隐藏测试结果，也不能根据结果重修。

### 5.3 Standard

普通仓库使用的保守模式：可以使用 semantic insight，但不跑隐藏反馈闭环，也不自动积累 benchmark 经验。

## 6. FailureReview 为什么使用结构化证据

`FailureFeedback` 不只保存一句“根因可能是类型判断错了”，还包含：

- `failure_stage`：失败属于实现语义、回归、环境等哪一层。
- `issue_contract`：公开问题要求维持的行为。
- `observed_failure`：实际失败现象。
- `root_cause`：基于证据的因果解释。
- `invalidated_assumption`：初始 patch 哪个假设被推翻。
- `required_correction`：下一轮应改变什么关系。
- `evidence_ledger`：证据来源、事实和支持类型。
- `correction_evidence_ids`：真正授权纠正的证据引用。

证据分为：

- `direct`：公开任务、失败日志、当前 patch、源码或已校验 semantic evidence 直接给出的事实。
- `inference`：由直接事实推导的解释。
- `unknown`：尚未证明。

路由门禁要求至少同时存在：

1. 直接失败证据，例如 failure log 或 validation summary。
2. 直接任务/代码证据，例如 public task、failed patch 或 semantic evidence。

inference 可以连接因果，但不能独自授权改代码。否则 Reviewer 猜出的故事会直接变成新的 patch。

## 7. 什么情况下可以写记忆

核心不变量是：

```text
independent_validation_succeeded(final_result) == True
```

仅有以下信息都不够：

- Agent 自己说“已经完成”。
- patch 能应用。
- 公开轻量命令退出码为 0。
- FailureReview 认为根因可信。
- F2P 有一部分通过。

只有最终独立评测明确 resolved，Curator 才能生成记忆操作。Curator 还需要：

- 搜索相似记忆。
- 有新证据则 EDIT，而不是重复 ADD。
- 没有可迁移知识则 NOOP。
- 删除被新成功证据推翻的旧 insight。
- 写入 provenance，保留来源审计能力。

这解释了为什么 MemoryStore 本身不承担“真理判断”：存储层不会知道一次运行是否真的通过。

## 8. SWE-bench 一道题包含什么

Agent 可见的核心公开输入是：

- `instance_id`
- GitHub 仓库与 `base_commit`
- `problem_statement`

评测器还持有用于验证的测试信息。正式 benchmark 中，这些结果只能在最终 patch 生成后由 harness 使用，不能回流到 Agent。

`scripts/swebench_batch.py` 对每道题执行：

1. checkout 固定 base commit。
2. 构造公开任务。
3. 运行 RepoAgent。
4. 提取并确定性检查 patch。
5. 输出 prediction。
6. 可选运行 SWE-bench harness。
7. 合并 Agent 阶段与 harness 结果做归因。

提交文件的核心字段是：

```json
{
  "instance_id": "project__repo-1234",
  "model_patch": "diff --git ...",
  "model_name_or_path": "repoagent-deepseek-v4-flash"
}
```

## 9. F2P、P2P 与 resolved

### F2P：FAIL_TO_PASS

目标测试在原始仓库上失败，应用 patch 后应通过。F2P 失败说明任务要求没有被完整修复。

### P2P：PASS_TO_PASS

原始仓库上通过的回归测试，应用 patch 后仍应通过。P2P 失败说明修复破坏了既有行为。

### Resolved

通常要求该 instance 的目标测试和回归测试都满足预期：

```text
F2P 全通过 AND P2P 全通过
```

因此“修好了两个目标测试但坏了三个回归测试”仍是 unresolved。

### Harness error

harness error 表示环境、镜像、clone、依赖下载、Docker 或评测基础设施失败。它不能算 resolved，也不能直接算 patch 的语义失败，应修复环境后对同一 prediction 重评。

### Empty/invalid patch

- empty patch：Agent 没交付可评测改动，通常归因于实现交付或流程提前终止。
- invalid patch：diff 语法、范围、安全规则或确定性校验拒绝了候选。

## 10. 阶段归因比总分更有诊断价值

`finalize_stage_attribution()` 把结果映射为互斥首要原因：

- `agent_runtime`：Agent/API/工具运行失败。
- `harness_environment`：评测环境失败。
- `implementation_delivery`：空 patch。
- `deterministic_validation`：patch 在进入 harness 前被拒绝。
- `semantic_fix_incomplete`：F2P 仍失败。
- `public_regression`：P2P 失败。
- `semantic_fix_and_regression`：目标未完全修好且引入回归。
- `success`：resolved。

阶段归因回答的是“最早在哪里失去成功可能”，而不是简单复述最后一条错误。调优时应先看类别分布，再决定改 Coordinator、Implement、Review、验证器还是基础设施。

## 11. 一个可信对照实验怎么做

实验变量必须冻结：

- RepoAgent commit。
- system prompt 和工具定义。
- 模型、reasoning 模式、temperature 等参数。
- max steps 与超时。
- 数据切片和顺序。
- memory snapshot 与 provenance。
- Docker 镜像、代理和 harness 版本。

对照组和实验组只改变一个主要变量，并使用相同 instance 集合。至少记录：

- resolved 数和比例。
- F2P/P2P 失败数。
- empty/invalid patch 数。
- harness error 数。
- 输入、输出、reasoning token。
- 每题耗时和超时数。
- 阶段归因分布。

模型具有随机性。小样本从 2/5 变成 3/5 不能直接证明机制有效；应在固定开发集重复运行或扩大样本，并报告方差和置信区间。

## 12. 四组诊断实验如何定位瓶颈

对固定开发题做以下实验，不作为排行榜结果：

| 输入条件 | 想回答的问题 |
|---|---|
| 原始 issue | 真实端到端能力如何 |
| issue + gold 文件范围 | 定位是否是瓶颈 |
| issue + gold 位置，不给 patch | 语义理解或实现是否是瓶颈 |
| 已生成 patch + 明确失败日志 | Review/Corrective 是否有能力纠偏 |

解释方式：

- 给文件范围后明显提升：Coordinator 定位弱。
- 给位置后仍失败：Implement 的契约理解或代码实现弱。
- 给准确失败日志仍修不对：FailureReview/Corrective 能力弱。
- patch 正确但本地校验拒绝：验证器可信度或门禁设计有问题。

这比不断修改大 prompt 更有效，因为它把“系统失败”拆成可证伪假设。

## 13. 本项目长期实验分别证明了什么

### 13.1 空 patch 阶段

早期大量 empty patch 说明问题首先是交付协议和 loop 终止，不是模型不会写代码。增加结构化 EditBrief、明确 Implement 职责和 patch 提取后，才有资格讨论语义正确率。

### 13.2 多 Agent 与 Review 阶段

Coordinator 提升了定位和范围控制，但过多 Research、Review、Corrective 会增加 token 和上下文转换损失。实验说明：角色拆分只有在权限边界和信息产物明确时才有价值。

### 13.3 门禁阶段

过宽门禁放过回归，过严门禁拒绝潜在正确 patch。最终认识是：确定性门禁适合检查结构、安全与证据完整性，不适合编码大量项目特定语义。

### 13.4 反馈闭环阶段

轻量验证命令若不能稳定复现基线失败，就无法提供可信纠正信号。模式一改用独立 harness 后，FailureReview 才获得真实失败证据。但该反馈闭环只用于离线学习，不能用于正式 pass@1。

### 13.5 双记忆阶段

episode 保存失败到成功的局部关系，semantic insight 保存跨案例规则。实验重点不是“记住题目”，而是检验抽象经验能否迁移，同时控制相似题污染和 test-set adaptation。

### 13.6 简化阶段

多轮实验表明，机制越多不必然修复率越高。最终流程保留少量职责清晰的环节，并用阶段归因决定下一处改动。这是项目最重要的工程结论之一。

## 14. Test-set Adaptation 与数据泄漏

“只记抽象方法”比保存答案安全，但不自动意味着评测合规。判断关键在于来源和使用方式：

- 如果记忆由同一官方 test split 的隐藏评测反馈产生，再用于该 split，仍属于 test-set adaptation。
- 不保存完整 patch，并不能消除从测试结果反复调系统带来的适应。
- 排行榜模式必须使用事先冻结、来源可说明的 memory snapshot。
- benchmark 运行中不得写记忆、不得把 harness 反馈给 Agent、不得重试后挑最好结果。
- hints、gold patch、F2P/P2P 列表或隐藏测试内容不能注入正式推理。

最稳妥的做法是：在独立训练/开发任务上构建记忆，冻结后一次性评估未见测试集，并公开模型、commit、memory provenance 和运行配置。

## 15. 本地成绩为什么不一定是排行榜成绩

本地 resolved 数只有在以下条件一致时才可比较：

- 同一数据版本和完整切片。
- 同一 RepoAgent commit 与模型配置。
- 同一冻结记忆。
- 无 diagnostic gold context。
- 无隐藏反馈重试。
- 每题只有一个最终 prediction。
- harness error 已单独重评，且未借机重新生成 patch。

项目历史中不同区间曾使用不同版本，因此把分段 resolved 直接相加可用于工程回顾，却不能当成严格冻结系统的官方 300 题分数。

## 16. Token 成本应该怎样分析

只看总 token 会误导。应按角色和阶段拆分：

```text
Coordinator 定位与编排
Implement 首次修复
Patch Review
FailureReview
Corrective Implement
Memory Curator
```

模式一天然比模式二贵，因为可能包含两次 patch 生成、两次 harness 和记忆提炼。正式 benchmark 的成本应只统计冻结推理链。

优化顺序建议：

1. 删除没有改变决策的 Worker。
2. 限制召回数量和注入字符。
3. 把日志压缩成保留失败关系的结构化证据。
4. 让简单、可信的静态 patch 尽快结束 Coordinator。
5. 最后再压缩关键 Implement 的推理预算。

## 17. 建议的七天学习顺序

### Day 1：MemoryStore

跟踪 `save_memory -> list_memories -> recall -> _recall_bm25`，手工保存两条 insight，观察 frontmatter、索引和排序。

### Day 2：双记忆

阅读 `augment_task_with_semantic_insights()` 和 `augment_implement_with_episodes()`，各写一个“应召回”和“不应采用”的例子。

### Day 3：模式状态机

把 `repair_mode_policy()` 画成能力矩阵，再从 `run_instance_once()` 找出模式一和模式二的分叉位置。

### Day 4：失败反馈

阅读 `FailureFeedback.correction_route` 和证据门禁。构造 direct、inference、unknown 三种 ledger，预测路由结果后运行测试验证。

### Day 5：SWE-bench Harness

选择一条历史 summary，手工计算 F2P/P2P，确认为什么 resolved 或 unresolved，并区分 harness error。

### Day 6：实验复盘

选两次历史改动，写清 hypothesis、control、treatment、metrics、result、conclusion，判断当时是否同时改变了过多变量。

### Day 7：面试演练

不看代码画出模式一、模式二时序图，并用三分钟解释：为什么经验构建使用反馈，而正式 benchmark 禁止反馈。

## 18. 面试自测题

1. 为什么 MemoryStore 不使用向量数据库？何时应该迁移？
2. BM25 召回分高为什么不能直接采用记忆？
3. episode、semantic insight 和 recipe 的边界是什么？
4. 为什么模式一关闭召回但允许写入？
5. 为什么模式二允许召回但禁止写入与反馈？
6. inference 为什么不能独立授权 Corrective Implement？
7. F2P 全通过但 P2P 失败，应该如何归因？
8. harness error 能否计为 unresolved？应怎样处理 prediction？
9. 使用 Lite test 成功案例提炼抽象方法，为什么仍可能构成 test-set adaptation？
10. 一项机制让 resolved +1、token 翻倍、harness error +3，能否说它有效？
11. 如何证明瓶颈是定位而不是实现？
12. 为什么反复根据同十道测试改 prompt 会使结果失真？

## 19. 参考答案要点

**问题 1**：当前记忆量小，Markdown + BM25 可解释、低成本、易审计。规模扩大、同义表达召回不足或需要跨语言检索时，可加入 embedding；仍应保留 provenance 和 applicability 层。

**问题 2**：BM25 只证明词汇相似，不证明当前调用链、契约和边界条件一致。

**问题 4**：避免已有记忆影响经验生成后再被写回，形成循环自证；模式一要观察无记忆条件下真实失败到成功轨迹。

**问题 5**：模式二模拟冻结系统的单次提交。可使用评测前已经冻结的知识，但不能从当前测试结果继续学习。

**问题 6**：推断可能是 Reviewer 的猜测。代码修改必须同时锚定真实失败和当前任务/代码事实。

**问题 7**：属于回归风险，`public_regression`；修复不能只追求目标测试通过。

**问题 8**：它是无有效判决，不应归咎 patch；保留原 prediction，修复环境后重评，不能重新生成 patch。

**问题 9**：合规性取决于数据来源和评测协议，不只取决于记忆文本是否抽象。反复利用 test split 结果调系统就是适应测试集。

**问题 11**：在固定开发题上提供 gold 文件范围。如果成功率显著提升，定位是主要瓶颈；再提供 gold 位置可继续分离语义与实现问题。

## 20. 当前设计的局限

- BM25 对同义语义和跨项目命名迁移能力有限。
- Markdown 文件仓库不适合多进程并发写入。
- applicability 仍依赖规则和模型复核，无法保证零误召回。
- Curator 本身是 LLM，可能抽象过度或遗漏条件，独立成功只保证案例有效，不保证总结完美。
- 模式一使用 harness 反馈适合离线研究，但必须和正式 benchmark 的记忆来源严格隔离。
- `swebench_batch.py` 承担较多职责，后续可拆成 runner、evaluator、reporter，但在行为稳定前不应为美观而重构。

真正应该掌握的不是每个参数，而是四条不变量：**证据先于记忆、召回先过适用性、学习与评测隔离、总分必须能够被阶段归因解释。**
