# 27 道无效补丁题的诊断重跑

- 第一轮 `lite-invalid27-localization-recovery`：5/27 通过。
- 第二轮 `lite-invalid12-observation-followup`：仅重跑第一轮没有可评估补丁的 12 题。2 题通过（django-11797、sympy-14024），4 题有补丁但未通过，5 题无补丁，1 题达到输出上限。
- 两轮合计 7 道不同题通过（7/27，25.9%）。这是跨轮的诊断累计，不是单版本成绩，不并入 154/300。

## 第二轮失败归因

- sphinx-8474、sympy-12236、sympy-13031、sympy-15346：定位结束仍缺少决定改哪里、怎么改的源码证据，未交付补丁。新版源码已同步，但 launcher 没有显式设置 `REPO_AGENT_LOCALIZATION_RECOVERY=1`，所用 env 文件没有该配置，实际会话也没有基线观察工具的记录。不能把本轮当作新版观察机制的有效对照。
- django-15738：已派发 Implement，但它走到步数上限仍未明确完成，候选补丁长度为零；主流程记录 `implementation_delivery_incomplete`。
- django-16400：GLM 首次输出的 12,288 token 全为推理，未产生正文或工具调用；自动降为 low 重试后仍返回 `finish_reason=length`，没有正文或工具调用。这是单次模型输出预算触顶，不是连接超时。
