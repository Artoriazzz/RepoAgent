# RepoAgent 异步流水线并发基准

该基准用于隔离并比较“增加完整 RepoAgent 数量”和“按角色拆分异步流水线”带来的性能收益。它使用真实线程、阻塞队列和 Worker 消费过程，但以固定阶段耗时代替 LLM、Docker 和网络调用，保证四组实验面对完全相同的工作量。

## 实验组

| 实验组 | 执行方式 | 对比目的 |
|---|---|---|
| `serial-1` | 一个完整 RepoAgent 依次执行整条工作流 | 流水线改造前的基线 |
| `serial-3` | 三个完整 RepoAgent 并行，各自串行执行整条工作流 | 排除单纯增加实例数量带来的收益 |
| `pipeline-1` | 每类 Agent 一个 Worker，阶段间通过队列流转 | 验证流水线解耦本身的收益 |
| `pipeline-3` | 每类 Agent 使用 Worker 池 | 验证当前并发流水线的吞吐能力 |

成功任务执行 Coordinator、Implement、Validation 和 Experience Memory。需要纠正的任务额外执行 Failure Review、Corrective Implement，并再次进入 Validation。所有实验使用同一批预先生成的任务路径，避免不同失败比例影响结果。

## 运行

正式基准默认根据子 Agent 的处理复杂度使用以下固定耗时：

| Agent 阶段 | 固定耗时 |
|---|---:|
| Coordinator | 12 秒 |
| Implement | 20 秒 |
| Validation | 15 秒 |
| Failure Review | 14 秒 |
| Corrective Implement | 18 秒 |
| Experience Memory | 10 秒 |

正式运行 20 个任务，其中一半经过纠正修复：

```bash
repo-agent-pipeline-benchmark \
  --tasks 20 \
  --retry-ratio 0.5 \
  --repetitions 3 \
  --warmups 1
```

这组正式实验耗时较长。开发阶段可统一缩短阶段耗时，只检查调度和统计逻辑：

```bash
repo-agent-pipeline-benchmark \
  --tasks 20 \
  --retry-ratio 0.5 \
  --stage-delay 0.1 \
  --repetitions 1 \
  --warmups 0 \
  --output-dir benchmark-results/pipeline-smoke
```

不同阶段耗时不同时可以单独覆盖：

```bash
repo-agent-pipeline-benchmark \
  --tasks 50 \
  --stage-duration implement=8 \
  --stage-duration validation=12 \
  --stage-duration coordinator=2
```

命令会生成：

- `pipeline-benchmark.json`：每次运行和汇总指标；
- `pipeline-benchmark.csv`：便于导入 Excel 绘图；
- `pipeline-stage-benchmark.csv`：每个 Agent 阶段的排队与执行耗时；
- `pipeline-benchmark.md`：可直接阅读的对比表。

统计指标包括总完成时间、每分钟吞吐量、任务 P50/P95/P99 延迟、阶段平均/P95 排队时间、Worker 利用率、最大队列长度，以及相对 `serial-1` 的吞吐提升和总耗时降低比例。多次运行的汇总使用中位数，降低线程调度抖动的影响。

## 如何解释结果

`serial-1` 与 `serial-3` 的差异代表横向增加完整 RepoAgent 的收益；`serial-3` 与 `pipeline-3` 的差异才代表在并发资源增加后，按角色拆分和阶段重叠执行带来的额外收益。流水线整体吞吐受最慢阶段限制，因此报告中的阶段排队时间也用于定位需要扩容的 Agent 角色。

该基准不替代端到端压测。最终还应在 Replay Worker + RabbitMQ + MySQL 环境中验证消息幂等、租约恢复、Outbox 投递和资源使用；真实 LLM 修复率应单独评估，不能与调度吞吐混为一个指标。
