# 公开测试验证 Agent

`public_validation` 在 Coordinator 启动前运行，只接收公开 Issue 和环境说明，自行读取修改前源码、文档及已有测试。顺序为：独立设计测试 → 基线执行并冻结 → Coordinator → Implement → 固定测试验证 → 失败归因与纠正 → 最终评分。测试结果不注入初始 Coordinator，失败时才交给纠正流程。串行准备保证测试设计期间源码尚未被修改。

默认 `execution_mode=native`：在相邻测试目录新增 `test_*.py` 模块，复用测试基类、装饰器及 fixture，通过仓库原有测试入口运行。生成文件的 `path` 和完整 `content` 在 `files` 数组中返回，由执行器落盘至独立的 `public-test-files/suite-*` 目录。Docker 验证将其复制到临时容器的 `/testbed` 对应路径；本地验证临时加入工作树，执行结束后移除。禁止覆盖已有测试、`conftest.py` 或包初始化文件；生成用例不进入生产 patch，官方最终评测也不加载这些文件。

基线和修复后执行相同原生用例，并对比测试文件哈希，防止中途更改断言。只有不依赖框架初始化的简单函数可显式使用 `execution_mode=inline`，同时给出 `inline_reason`。

冻结前保留一次方案修正与覆盖复核。格式错误、目标未复现或回归未通过时，先修正这些问题；初版基线符合计划时，同一会话仍核对 Issue 期望是否遗漏、unknowns 是否只是未查询的已有行为、断言是否误解对象结构。沿用完整历史和源码结果，补充原始 Issue、基线分类摘要及不符项日志；没有问题时返回原计划，有问题时返回修订后的完整计划并重新执行。原计划保存在 `public_validation_initial.json`，两次调用 token 合并统计，`worker.baseline_repair.reason` 区分 baseline_mismatch 与 coverage_review。只有环境阻塞、没有有效行为观察时不额外复核；明确返回 unavailable 的合法方案也不重试。最多一次，不增加修复纠正轮数或新 Agent。

测试应从公开合法样例做最小改动，并配对合法边界；不能用无关错误满足目标断言。复核后仍不符合预期的用例保持不可用状态，不再循环。覆盖复核是模型检查，不能保证自动发现所有缺口，仍需固定 patch 对照和最终 Harness 评分。

如果 Python 报 `No module named pytest`，测试未启动。基线阶段复用原有的一次方案复核，尝试切换到仓库已有入口，不安装依赖；这个错误不触发生产代码纠正。unittest 全部跳过也不计为通过。

测试设计优先验证可观察行为：从相关已有测试读取完整断言，将适用的异常、错误分类、日志、状态等契约应用到新增输入；检查源码已经支持的相关输入变体。每个新增测试在 basis 说明排除的合理错误行为，构造能区分不同实现的最小数据。内部调用是否发生需要独立的公开依据，不能直接将某个候选修法当成测试预期。新增内容仅包含 test_*.py 模块，不生成占位或说明文件。

预期应对应实际调用入口：内部异常可能被外层捕获，相似后端或 docstring 的说明需与函数体和该入口已有测试核对。恢复类问题同时检查后续正常操作；配置禁用某功能不能直接等同于禁止调用某个内部函数。可复现目标尚未确定时仍保留有效回归用例，使用 ready 返回。

测试计划优先包含至少一条相关已有测试类或模块的整体回归命令，覆盖原有非法输入、错误处理及相邻行为。最多六条指执行命令数量，一条类/模块命令可以执行其中的全部测试方法。重复的单方法回归应合并；基线已通过的整体回归在修复后失败，同样触发失败归因。

源码工具包含 `list_files`、`read_file`、`search_repo`，关闭记忆和动态项目上下文。设计会话另有受限的 `run_baseline_tests`：提供 `commands` 字符串数组和可选的 `files` 测试文件数组，在修改前运行临时测试，确认缺省输入、已有行为或 fixture。试跑不要求最终方案的 status、kind、basis 等字段。最多两次、每次最多两条命令、每条至多 120 秒；试跑与一次设计修正共享预算。生产源码仍不能通过文件工具修改，执行复用现有隔离运行时。

试跑使用独立的 overlay 和状态，不改变正式计划，也不自动将用例加入最终计划；真实观察随工具结果进入原会话。最终方案仍重新执行并冻结。试跑产物位于 `baseline-probes/`，正式产物中的 `baseline_probes` 记录路径与错误。此能力不会额外启动 LLM，也不向 Coordinator 或其他角色开放。测试角色仍最多十步；预算结束后可沿用完整历史输出 JSON，格式错误有一次纯输出修正机会。

测试设计先确认行为要求及已有输入契约，无需确定具体修法。对源码和已有测试已经支持的输入结构，检查同一要求是否适用，并引用读取依据；未知的新错误编号、措辞等单独记录，仍验证已有公开依据的触发条件与边界。

测试设计按三部分收敛：目标输入的完整结果、相关配置或扩展输入、需要保留的可观察行为。缺陷源码缺少某个动作不构成排除已有契约的依据；基线已通过的相关输入应作为回归保护。内部调用方式可变，日志、状态以及公开执行上下文等可观察结果需要独立检查。mock 用于隔离资源，不在缺少公开依据时限定修复必须调用或跳过某个内部函数。

设计调优可先单独运行 `design-public-tests`，用历史自产 patch 做离线对照，区分生成测试的变化与修复模型的随机差异；对照产物不输入测试或修复 Agent。读取输入定义的强化提示曾补回个别测试，但完整批次未保持成绩，当前采用 v6 提示词，保留测试初始化分类和批处理证据路由修复。

Coordinator 提示词恢复自 GitHub `origin/full-stack-integration`（fc4b043），不追加测试交接规则，工具不要求 test_context。测试 Agent 不读取候选方案、编辑范围或经验。已有公开契约默认保留；只有 Issue 明确要求改变时才说明冲突依据。新增编号无法推导时记录未知，不重新分配旧编号。目标测试与回归测试分开执行。

用例分为 `target` 和 `regression`。执行器在修改前逐条记录基线结果，在修改后重跑相同命令：目标用例需要在基线触发行为失败，回归用例需要在基线通过。基线通过、修复后失败的回归用例也能单独触发 FailureReview。依赖、导入、语法和测试收集错误不会成为纠正依据。标准 traceback 末帧位于测试模块加载、类定义或初始化函数时标记 invalid_test，进入基线设计修正；测试体或生产函数内的 NameError 仍可作为真实行为失败，不能仅按异常名称排除。

Docker 镜像缺失等启动故障标记为 environment_blocked，不触发测试重写或代码纠正；Django SimpleTestCase 禁止访问数据库的断言标记为 invalid_test，反馈给测试设计者修正初始化。真实数据库缺表异常仍可作为行为失败，两者不混淆。

若 traceback 终止于 unittest.mock 的参数注入层，且错误明确为测试方法形参数量不符，标记 invalid_test，在冻结前交给原有的一次设计修正。进入生产函数后发生的 TypeError 仍属于行为失败，不因测试使用了 mock 就排除。等价对照须独立执行并覆盖每组目标数据，不能用一组对照证明另一组数据的结果；有配置字段时区分其合法空值、None 和属性缺失。

unittest 的加载失败若指向命令中不存在的模块或测试节点，标记 invalid_test，进入原有的一次设计修正；加载时缺少其他依赖仍标记 environment_blocked。测试命令应由实际读取的文件模块路径组成，不能假设测试类在包顶层导出。

基线抛出异常不能证明异常之后的预期断言正确。兼容已有行为的目标输入，应同时用已有可用的等价形式、完全相同的数据与边界生成独立回归对照，先在基线确认预期。目标暂未复现时保留有依据的回归用例；没有公开依据时不生成猜测预期。

支持 Django 的 `python tests/runtests.py ...` 和 `python manage.py test ...` 入口。命令逐条校验，无效或重复用例写入 `rejected_cases`，有效用例继续执行并可触发反馈；存在被拒绝用例时保留验证不足状态。验证角色的 token 纳入 `stage_attribution.json` 的 `worker_events` 和总消耗。

没有可靠预期时记录 `unknowns`；用例不可用时仍允许首次代码修复，结果标记为验证不足。所有可用用例通过且至少一个目标用例完成失败到成功的变化，才记为公开验证成功。这个结果仅代表生成用例的覆盖范围。

## 单独设计与基线验证

在 WSL 已配置的项目环境中执行：

```bash
repo-agent design-public-tests \
  --repo /path/to/clean-baseline \
  --task-file /path/to/public-issue.md \
  --output-dir /path/to/run/test-design
```

`--context-file` 保留兼容入口，其内容不注入独立测试会话，产物 test_context 为 {}。用例、断言依据、基线与修复后结果保存在 `public_validation.json`；独立模型轨迹保存在 `subagents/`。

## 接入修复

```bash
repo-agent run --repo /path/to/repo --coordinator --public-validation \
  --task "公开问题描述" --output-dir /path/to/run

repo-agent experience-build --repo /path/to/repo \
  --task "公开问题描述" --provenance public-issue \
  --output-dir /path/to/learning-run
```

`run` 设计测试并验证初始补丁；`experience-build` 未指定反馈命令时自动启用该角色，并复用现有一次反馈纠正。可用 `--public-validation-timeout` 指定每条测试的超时，默认 300 秒。`--final-eval-command` 可单独执行最终评分；公开测试路径的经验生成仅依据公开测试结果。

## SWE-bench 批处理

学习批处理增加 `--feedback-source public-tests`，现在这是默认值。保留原有数据集、模型、镜像和输出目录参数，并使用 `--repair-mode experience-build --evaluate --memory-provenance <真实来源>`。

公开测试支持最多两次纠正，并复用已有范围检查和 Coordinator replan 路由。官方 Harness 只对最终补丁评分，结果不参与该路径的纠正或经验生成。测试准备失败时不会回退到官方反馈。`--feedback-source harness` 显式选择历史诊断流程，其反馈与经验仍来自官方评测。

批处理纠正路由与 CLI 使用相同的证据来源规则：直接失败事实加上已读生产源码可授权定向纠正，无需再重复引用任务或 patch。推断可解释因果，不能替代直接事实；未知证据仍不能授权。

纠正过程中记录已通过的有效公开用例。新补丁引入回归时仍可使用剩余纠正机会；预算结束后若这些用例未恢复，则回退到保留它们的已验证补丁，并在 public_regression_rollback 保存放弃候选的验证结果。该保护依据公开用例，不能保证未覆盖的官方测试不退化。

`run_public_tests_first10_wsl.py` 使用 SWE-bench 虚拟环境执行镜像预检。并行批次采用 cache_level=instance，防止一个 Harness 的全局清理删除另一题新建的镜像；实例镜像保留至后续显式清理。

Docker 运行复用 `REPO_AGENT_VERIFY_DOCKER_IMAGE` 和已准备的 `/testbed`，不应用官方测试补丁。此角色不自动配置依赖或安装测试运行环境；环境缺失会保留执行日志。历史经验与调优数据仍需单独审计，启用该角色本身不证明榜单合规。

角色 Prompt：`repo_agent/core/public_validation.py` 中的 `PUBLIC_VALIDATION_PROMPT`。本次接入 CLI 与 SWE-bench 批处理，后端阶段队列尚未接入此角色。

## 全局 Top-1 门控预跑

在批处理参数中使用 `--repair-mode benchmark --feedback-source public-tests --require-public-pass --evaluate --memory-root <冻结经验目录>`。经验目录只放 `global/memory/` 下的全局经验；启动时拒绝空库、混入仓库经验或其他 Markdown 记忆的目录。沿用 Coordinator 的全局 Top-1 检索，无匹配时不注入。公开测试可以驱动定向纠正，运行过程中不更新经验库。

只有最终公开验证为 `passed` 的有效补丁才运行一次 Harness。失败或证据不足的补丁仍可供离线分析，Harness 状态记录为 `public_tests_not_passed`，不视作官方判分。该入口标记为诊断预跑，不能把前 50 题或门控后子集的结果当成 Lite 全集成绩。要制作正式材料，应冻结并审计经验来源，披露开发使用过的评测数据、内部尝试策略及提交选择规则，再按官方流程评估和打包。
