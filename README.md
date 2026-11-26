# RepoAgent 固定代码快照 · 2026-11-23

GitHub `main` 于 2026-11-26 更新为本冻结版。源码、提示词与测试保持原样；发布副本只增加运行产物忽略规则和来源校验记录。实验结果、轨迹、数据集及密钥均未上传。

这份目录用于下一轮 SWE-bench Lite 300 题实验准备。它复制自当日 `repo_agent_plain` 的实际工作区，包含未提交的公开证据提示词约束。所有复制的源码均与源目录逐字节一致，没有调整修复逻辑、模型、预算或测试规则。

## 目录

- `repo_agent/`：当前 Agent、工具、提示词、验证、记忆以及 Python 服务组件代码。
- `scripts/swebench_batch.py`：当前逐题批测入口。
- `scripts/prebuild_swebench_image.py`：镜像准备入口。
- `scripts/repair_swebench_test_environment.py`：现有测试环境辅助工具。
- `scripts/resume_lite_harness_errors_wsl.py`：现有基础设施错误重评工具。
- `scripts/prepare_swebench_submission.py`：提交产物整理工具。
- `scripts/audit_public_tests_wsl.py`：公开验证产物检查工具。
- `scripts/export_repair_prompts.py`：原有提示词导出工具。
- `tests/`：当前代码相关测试；排除了依赖历史专用实验启动器的六个测试文件，清单见来源记录。
- `snapshot/provenance.json`：源提交、源分支、工作区状态、复制文件及 SHA-256。
- `snapshot/working-tree.patch`：相对源提交的已跟踪代码改动；新增源码以实际文件和哈希为准。
- `snapshot/CHECKSUMS.sha256`：此快照文件校验清单。

没有复制旧版本备份、历史实验启动器、实验成绩、轨迹、数据集、仓库镜像、Docker 镜像、下载缓存、经验记忆、Java 后端、Git 历史、虚拟环境或真实 API 密钥。`.env.example` 仅保留配置示例。

## 使用约定

所有依赖安装、测试和实验均在 WSL 执行，本机仅保存源码副本。WSL 对应目录为：

```text
/home/artoria/workspace/RepoAgent/repoagent-frozen-20261123
```

300 题实验已启动。依赖版本、官方 Harness 源码哈希、数据集哈希、模型参数与生成协议保存在 `runs/lite300/protocol.json`；各分批续跑设置保存在同目录的 `protocol-index*.json`。实验记录以这些配置为准。

确认实验设置后应通过独立虚拟环境安装此目录，避免 `repo-agent` 仍指向旧代码。结果、缓存和工作仓库统一放在本目录的 `runs/lite300/` 下，与源码子目录隔离。API 密钥通过外部环境配置提供，不写入快照。

## 本轮输出位置

使用 `bash snapshot/run_lite300.sh <实验参数>` 启动时，脚本固定以下位置，传入的同名路径参数会被末尾的固定参数覆盖：

```text
runs/lite300/
├── results/                   # predictions.json、batch_summary.json、逐题目录
│   └── <instance_id>/         # model.patch、summary.json、pass-1/ 及子 Agent 轨迹
├── logs/run.log               # 批测标准输出和错误
├── work/                      # 工作仓库和 repo_cache
└── harness/                   # Harness 的报告、logs/run_evaluation 和构建日志
```

启动脚本使用此快照的 `.venv/bin/python` 和 `.venv/bin/repo-agent`，要求先在 WSL 安装好本目录的独立环境和相关依赖。它不提供模型、API、预算及反馈策略的默认实验协议，仍需启动时明确传入。不要用其他历史启动器运行本轮。Docker 镜像层仍由 Docker 管理，不会搬入本目录。

在 WSL 核对代码：

```bash
cd /home/artoria/workspace/RepoAgent/repoagent-frozen-20261123
sha256sum -c snapshot/CHECKSUMS.sha256
```

后续如需改代码或提示词，另建快照；保留本目录供这一轮实验使用。

## 本轮 pass@1 统计口径与环境恢复

本轮实验将有明确环境故障证据的恢复操作纳入 pass@1 统计，并保留完整执行记录。具体处理如下：

- API 未返回有效响应时重试，保存错误类型与重试记录；已经生成的候选补丁予以保留。
- 已有候选、仅最终评测环境故障时，修复依赖、网络或 Harness 后重评原补丁，不调用模型改写候选。
- 镜像构建失败且尚未调用模型时，修复镜像后开始该题的首次生成。
- TestDesigner 的公开基线因缺少依赖、镜像不可用等环境问题未能执行时，可在修复并验证环境后重新执行完整 Agent 流程。此类整题重跑会产生额外模型调用，也可能产生不同候选；原候选、原轨迹、故障证据和重跑次数均保留。
- 最终候选采用预定恢复流程的输出，不在多个候选中根据官方成绩择优。空补丁、真实功能失败和正常预算耗尽按失败记录。

本文的 pass@1 是包含上述恢复策略的实验统计口径；完整模型调用与整题重跑情况应结合原始记录读取，不能将这些情况解释为每题仅发生过一次生成。提交实验材料时一并提供此说明与完整轨迹。

运行环境修复不改变冻结的 Agent 代码、提示词、模型和推理预算。流程内使用公开测试反馈，官方 Harness 仅用于最终评分，其测试内容、参考补丁及评测反馈不传给修复 Agent。经验召回与生成均关闭。

原始结果位于 `runs/lite300/results/`；环境修复、原补丁重评和完整重跑分开保存在 `runs/lite300/audit/`，通过题目 ID、补丁 SHA-256 和来源路径关联。

## 已知测试状态

公开证据约束相关回归测试在 WSL 通过 91 项。`test_json_correction_keeps_full_failure_evidence_and_repairs_reference` 是现有失败项，禁用新增约束后仍失败；快照保留原状，没有为了获得绿色测试而改动实现或测试。
