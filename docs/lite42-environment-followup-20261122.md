# 42 题环境补评续跑

## 最终台账核验

累计名单已逐题回溯原始证据：`lite-cumulative-resolved-194-20261122.json`。
194 个唯一 ID 均找到历史成功 summary 或 Harness resolved_ids/report，缺失证据为 0；不是按几个批次总数直接相加。
WSL 原始索引位于 `agent_work/runs/lite-cumulative-194-audit-20261122/resolved-index.json`。
另已保存持久化审计归档 `agent_work/runs/lite42-environment-archive-20261122/`：42 题报告与原始日志的 SHA-256、Harness 解析器改动及备份、汇总名单和本轮脚本。没有复制 API 密钥或 `.env`。

以用户最新指定的 182 为基线，新增成功 12，累计 **194/300**。
42 题分类为：23 resolved（11 已在基线内）、18 completed_unresolved、1 baseline_timing_sensitive_unresolved。
所有 resolved 报告均已核验 patch_successfully_applied 及完整 F2P/P2P 无失败；最新逐题报告无 harness_error。
不把 completed_unresolved 机械解释为所有原始日志都没有次要警告。

23913 原始基线、已有补丁各进行三次独立容器检查，6 次均失败；两次定位调用的耗时全部在 0.880–0.984 秒，默认定位标记均为 true。
源码仅在耗时 >1 秒时发警告，测试无条件期待两条警告。诊断没有篡改时钟、阈值或增加负载；因此不新增成功。
证据目录：WSL `agent_work/runs/lite42-matplotlib23913-timing-audit/`，含六份日志和 `results.json`。
最终沿用官方测试的原始判定，将该题保留为未通过并单列计时敏感性，不改阈值、不制造负载、不补加成功数。本轮环境修复与重评已收尾。
最终核验：42 份最新报告均无 harness_error，42 份报告与原始日志哈希匹配归档，194 个成功题号均有去重后的成功证据。

## 最新核验结果

全部 42 题已取得重评结果，当前没有评测进程存活。
按 182 基线去重后新增 12 题：Matplotlib 8 题、Requests 4 题，累计 **194/300**。
这 42 题中有 23 题取得成功（其中 11 题已在 182 基线内），19 题仍未 resolved。
逐题结果和报告路径见 `lite42-environment-ledger-20261122.json`。

Requests 2148、2317、2674 最终通过记录在 `lite42-requests-service-v3-2148`、
`lite42-requests-service-v2-2317`、`lite42-requests-service-v2-2674`。
采用真实本地 httpbin HTTP 服务及保留证书验证的 HTTPS CONNECT，Google 跳转目的地也通过透明转发访问真实服务。
镜像中旧 `requests/cacert.pem` 刷新为系统 CA，属于明确记录的证书环境适配，未修改模型补丁或测试断言。

pytest 5413 第二次环境修复使用标准 `SETUPTOOLS_SCM_PRETEND_VERSION=4.6.0`，完整 Harness 后 P2P 92/92、F2P 0/1，不再是收集故障。
Astropy 14182 通过 `PIP_CONSTRAINT` 将兼容 setuptools/scm 约束传入隔离构建，缺模块错误消失；完整测试 P2P 9/9、F2P 0/1。

仍保留一项已确认的基线异常：Matplotlib 23913 的 `test_warn_big_data_best_loc`。
源码只有在定位耗时大于 1 秒时才发警告，而测试无条件要求两次警告；未应用模型补丁的基线也会失败。
没有修改计时阈值、模拟警告、删测或补加该题通过数。此项仍待进一步处理，不能宣称所有 Harness 问题已经清零。

下文保留此前各阶段的诊断记录；阶段性分数与进程状态以本节及逐题台账为准。

统计口径：172 为包含 requests-863 本地 httpbin 重评成功后的跨轮累计。
不将环境异常直接计为成功，也不将官方测试反馈传回生成流程。

## 已确认结果

- pytest 17 题已重评：10 resolved、7 unresolved。恢复 Git 版本标签和构建插件后，minversion 初始化故障消失。
- Matplotlib 9 份可用实例镜像已重评：6 resolved、3 unresolved。移除与 setuptools-scm 7.1.0 冲突的 vcs-versioning。
- Matplotlib 23913 已从本地 mirror 重建并重评：F2P 2/2，P2P 100/101，仍 unresolved。
- Sphinx 六题均未 resolved。解析器已增加对 `test PASSED [进度]` 的识别。8273、8282 的 tox 强制覆盖详细输出配置，P2P 逐条结果仍不完整；不得用总通过数补算。
- Flask 4045、Astropy 14182 已修依赖并重评，但 F2P 仍未全通过。

后续 Matplotlib 25079、26011 原补丁通过，新增确认成功为 10+8=18 题。
按用户最新指定的 182 基线（已经包括 pytest 新增 10 题和 requests-863）继续增加 8 题，跨轮累计暂为 190/300；这不是单版本排行榜成绩。

## 残留问题追踪

- Sphinx 8273、8282：镜像 pytest 入口仅追加 `-v -rA`，已完成完整重评。P2P 分别 3/3、16/16，F2P 均 0/1。原先 P2P 全失败是输出/解析问题，不能算新增通过。
- Matplotlib 24334：补评后 P2P 全通过，F2P 仍因 TypeError / ValueError 不符失败。
- Matplotlib 23913：F2P 2/2，P2P 100/101。唯一 P2P `test_warn_big_data_best_loc` 在不应用补丁的基线也失败，属于耗时阈值警告的基线异常；不跳过该测试，不计成功。
- Requests 1963：系统 CA 修复 issuer 错误后，旧 urllib3 暴露代理地址被误作 TLS 主机名的问题。新建透明 CONNECT TCP 转发，仅容器 hosts 将 httpbin.org 指向 127.0.0.2，不解密 TLS、不关闭证书检查、不修改测试。HTTP/HTTPS 容器预检均为 200。
- 透明转发首题完整重评 P2P 全通过，但一条 F2P 上传请求被远端断开，因此安排同补丁复测，不能当作纯代码失败。
- 下一条队列：`lite42-requests-connect-20261122`，进程 750504，等待 CA 队列 740172 完成后串行重评四道 Requests。转发容器名 `repoagent-httpbin-connect-relay-20261122`，仅服务本轮实验。
- 汇总脚本 `scripts/summarize_lite42_environment.py` 生成 WSL `agent_work/runs/lite42-environment-ledger-20261122.json`，以 ID 去重记录所有报告和新增成功。结果尚未最终核验，目标未完成。

## 当前后台队列

WSL 目录：`/home/artoria/workspace/SWE-bench/agent_work/runs/lite42-environment-followup-20261122`。

依次运行：Matplotlib 24334、25079、26011；Requests 1963、2148、2317、2674。
前三题固定 pandas 1.5.3，纠正与现有 numpy 的不兼容。
旧 Requests 四题恢复外部 HTTP/HTTPS httpbin 经代理访问；只提供 HTTP 的本地服务不适用于其混合协议测试。
三份此前缺失的 Matplotlib 实例镜像均已重建成功。

查看 `progress.json`、逐题 `.log` 和 `run.log`。补评目录使用 `lite42-followup-<题号>`，不覆盖历史报告。
串行执行，D 盘低于 12 GiB 停止启动下一题。当前仅复评已有补丁，尚未重启 Agent 生成修复。
