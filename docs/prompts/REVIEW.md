# 模式一提示词检查

初始 Coordinator、批处理文案及以下三个角色已完成精简替换。
下方各角色列表记录替换前发现的问题，供追溯；同目录角色文件是替换后导出的正文。
同目录导出文件包含系统正文和带占位符的用户任务模板；不是某次实验的实际输入。
Implement 中的示例范围 1-20 仅展示格式，实际范围由调用方动态传入。

## Implement

- 基础系统提示要求把协调者的具体改法视为待确认假设，与已确认的“协调者决定修法、实现者落实方案”职责不一致。应只核对可实施性和直接反证，不重新选择方案。
- 存在未解释的 clean source、Active task constraint、profile、predicate、producer/consumer 等术语。
- 初始阶段追加规则与基础规则重复要求读取契约、保持输出、不修改测试。
- 纠正阶段要求只能执行 direct 证据授权的修改，但未清楚解释“直接事实支持修复义务”和“因果推断辅助确定改法”的区别，容易再次把必要推断一概排除。
- 最终 JSON 的 known_risks 只是报告字段；提示词不能代替语法校验，也不能解决已有 patch 阻止纠正派发的问题。

## FailureReview

- 系统正文和用户模板 Review Protocol、Output Rules、修正范围与路由重复定义证据、停止条件和路由。
- Output Rules 说“需要更多证据时使用 insufficient_evidence”，后文又规定缺少关键源码事实时使用 needs_replan，应统一为：有具体可补查位置时重新规划；没有补证方向或确实超出题意时停止。
- actionable、needs_replan、scope_expansion、regression_guards、direct/inference/unknown 等字段必须保留给程序读取，但应集中定义一次，不在多个区块交叉解释。
- geometry/cardinality 等特定题型限制挤在通用归因规则中；没有命中对应问题时不应无条件注入。
- 公共包装文字仍使用 Coordinator、主 Agent；实际职责已由当前任务自包含，不需要依赖这些名称。

## ExperienceCurator

- 当前实际维护两类记忆：global_experience（跨仓库机制）和 repository_experience（仓库约定）；不是保存完整修复过程。
- 系统和用户模板重复定义两类记忆、ADD/EDIT/REMOVE/NOOP、禁止内容及来源标记，可以合并为一份固定规则。
- insight、provenance、trajectory_kind 等名称有部分解释，但应就地用中文说明，避免和其他历史记忆类型混用。
- 去重、同机制优先合并、没有新知识则不写，以及不把测试来源伪装为训练来源的规则应保留。
- “本次验证成功”不等于“机制可跨仓库推广”；保存全局经验时应明确适用条件，不将偶然成功泛化为普遍规则。

## 本次未改变的程序行为

没有增减实现次数、失败纠正次数或查询预算；没有修改记忆检索开关。
条件审查的现有状态机仍保留：只有工具明确要求审查才按要求执行，不再通过提示词主动建议额外验证。
没有启动新的修复实验。

## 备份与恢复

本地备份：`backups/prompts-before-20261015-role-cleanup.tar.gz`（相对项目根目录）。
包含替换前的两个源码文件、相关测试、导出脚本和提示词文档。

WSL 备份：`/home/artoria/workspace/SWE-bench/agent_work/prompts-before-20261015-role-cleanup.tar.gz`。
包含 WSL 原有的两个源码文件，未用本地副本冒充远程备份。

恢复前先备份恢复时的现状，避免覆盖后续协作改动。只恢复运行提示词可在项目根目录执行：

```bash
tar -xzf backups/prompts-before-20261015-role-cleanup.tar.gz repo_agent/core/subagent.py repo_agent/core/experience.py
```

WSL 在 `/home/artoria/workspace/RepoAgent/RepoAgent` 执行：

```bash
tar -xzf /home/artoria/workspace/SWE-bench/agent_work/prompts-before-20261015-role-cleanup.tar.gz repo_agent/core/subagent.py repo_agent/core/experience.py
```
