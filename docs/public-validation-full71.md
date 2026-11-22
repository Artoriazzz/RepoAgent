# 测试 Agent 完整 71 题评估

## 题单

使用历史 71 道首轮未通过题，不是 Lite 数据集前 71 道。前 35 题与剩余 36 题的既有 selection 合并后，已按 dataset index 排序，核对无重复且与本地数据集逐项一致。

WSL 准备目录：

```text
/home/artoria/workspace/SWE-bench/agent_work/runs/public-tests-full71-preparation-20261101/
  selection.json
  inventory.json
  orchestration.py
```

共 71 题、11 个仓库。题单 SHA256 为 `0e17e290f555d0e325777fd693bf57948d3e877695b5ef5209355755f93c5b0d`。数据集哈希、来源和缓存缺口保存在 inventory.json。准备完成不代表实验已经启动。

## 选版与冻结

先等 `public-tests-first10-plus3-contract-v26-20261101` 完成，再与同组 v24 的 5/13 比较。只有 v26 有明确修复收益、原成功保留及 P2P 表现支持采用时才选它；否则使用冻结 v24。API/环境中断单列，不作为确定的语义失败；有必要时仅补跑外部错误，不反复挑选正常失败题的最好结果。

停止继续追加提示规则。选定后完整重新生成 71 题，不复用不同版本各题最好的 patch；执行中不修改冻结源码、提示词、配置或题单。各旧版本与诊断结果全部保留。

设置沿用当前实验：glm-5.3-flash/high、Coordinator max_steps=24、两题并行、环境构建串行，关闭记忆召回，公开测试用于纠正，官方 Harness 只在最终评分，不把官方反馈注入 Agent。采样行为保持所选版本原样。新运行目录中的 experiment.json 保存实际代码哈希、来源、题单与编排脚本哈希。

## 环境

准备脚本已覆盖 11 个仓库。35 题已有实例镜像，另外 36 题需要创建；每题先通过环境检查才调用模型。基础构建使用 host 网络、国内 pip 源与本地仓库 mirror。复用既有 pytest 版本标签修复、Sphinx 3.1 测试依赖修复以及 Matplotlib 依赖约束，不修改问题修复逻辑。

基线检查要求干净 HEAD、原 base_commit 为其祖先且生产文件匹配。仅 Sphinx 3.1 可包含官方安装产生的 setup.py/tox.ini 提交差异；其他生产代码变动仍拒绝，检查结果逐题落盘。相关编排与最终 patch 读取测试共 14 项在 WSL 通过；其他仓库的实际构建仍须逐题验证，不能仅凭单测宣布环境全部就绪。

2026-11-01 实测 WSL 根分区约 689GiB 可用，承载 VHDX 的 Windows D 盘约 213GiB 可用；容量判断以宿主磁盘余量为约束，不能只看虚拟根分区。

## 进度与恢复

启动后以实际运行目录的 run.log、progress.json 和逐题 summary.json 为准。finished.txt 仅表示所有任务已得到终态，不代表无 API、环境或 Harness 错误。

发现供应商明确余额不足（RateLimitError/code 1113）时，停止派发剩余模型任务，保留已完成产物，写 credit-exhausted.json 与 waiting-for-api-credit.txt；普通 API 超时和并发限流不据此判定余额不足。恢复后已有外部错误应另建补跑目录，沿用相同冻结 Agent，保留来源，不覆盖旧记录。

统计最终 patch 时必须读取 summary.model_patch，并与同题根 predictions.json 核对。pass-1/model.patch 是首轮产物，不能冒充最终纠正 patch。需要首轮官方成绩时，可在全部生成结束后单独进行离线评估，反馈不回流模型。

最终汇总 resolved、逐题成功保留、F2P/P2P、空 patch、环境/API/Harness 错误、token 与缓存输入。测试通过只能证明已覆盖行为，不代表官方全部通过。本轮是已有开发题上的工程评估，不宣称无测试集适应的正式排行榜成绩。
