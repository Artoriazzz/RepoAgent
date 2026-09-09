# 第二周学习讲义：工具、上下文和安全

## 学习目标

完成本周后，你应当能够回答：模型为什么不能直接获得 Python 函数；一个工具调用经过哪些校验；Worker 的最小权限如何落实；路径穿越、旧读覆盖、敏感文件和危险命令分别在哪一层拦截；长日志如何在保留证据的同时退出模型上下文。

本周阅读顺序：

1. `repo_agent/core/tool.py`
2. `repo_agent/tools/catalog.py`
3. `repo_agent/tools/repo_tools.py`
4. `repo_agent/core/context.py`
5. `repo_agent/core/session_memory.py`
6. `repo_agent/core/prompts.py`

## 1. 工具调用不是函数调用

模型只看见 `Tool.to_openai_schema()` 生成的 JSON schema，并输出名称和 JSON 参数。执行链是：

```text
LLM tool_call
  -> ToolRegistry.execute
  -> 查找名称/alias
  -> JSON 解析
  -> Tool.validate
  -> Tool.check_permissions
  -> Tool.run/handler
  -> 捕获异常
  -> 截断结果并记录耗时
  -> ToolResponse
```

schema 提高模型生成正确参数的概率，但不是安全边界。模型输出必须视为不可信输入，服务端仍要重新校验。

## 2. ToolResponse 为什么有三种状态

`SUCCESS` 表示工具完成；`ERROR` 表示调用无法执行；`PARTIAL` 表示工具提供了有价值的 observation，但结果不完整或目标命令未成功。

例如测试退出码为 1 时，`run_command` 返回 PARTIAL 而不是抛异常。失败日志仍是 Agent 下一步诊断所需的证据。

统一 ToolResponse 的价值：

- Agent Loop 不依赖具体工具异常类型。
- 模型能根据 `error.code` 选择重新读文件、修正参数或停止。
- trace 可以统一记录状态、耗时和截断信息。
- 工具故障不会直接破坏整个会话。

## 3. FunctionTool 与 CallbackTool

`FunctionTool` 适合简单函数：通过 Python signature 和 type hint 自动生成浅层 schema。

`CallbackTool` 适合真实仓库动作：显式声明参数、权限函数、只读/破坏性属性、结果上限和工具 prompt。

真实安全工具优先 CallbackTool，因为 Python 类型只能说明 `path` 是字符串，不能说明该路径是否越界、敏感或允许当前 Worker 修改。

## 4. ToolRegistry 与最小权限

ToolRegistry 同时维护正式名称和 alias。`schemas()` 只暴露 active tools，`execute()` 是统一执行入口。

```python
implement_tools = registry.subset({"read_file", "replace_in_file", "write_file"})
review_tools = registry.subset({"read_file", "git_diff"})
```

这叫 capability-based security：Worker 拿到什么 Registry，就拥有什么能力。被移除的工具既不会出现在模型 schema 中，也不能通过该 Registry 调用。

prompt 中写“不要运行命令”只是行为建议；不给 Worker `run_command` 才是权限约束。

## 5. 三层工具导入

`catalog.py` 将导入分成三层：

```text
get_all_base_tool_bundles
  -> 根据 feature 开关加载 repo/memory/github bundle

get_tools
  -> simple_mode、denied_tools 运行时过滤

assemble_tool_pool
  -> source 分区排序、按名称去重、注册到 Registry
```

延迟导入 GitHub/Memory 工具可以避免禁用集成时仍加载无关依赖。确定性排序让 schema 顺序稳定，也方便测试和复现。

## 6. RepoWorkspace 是仓库安全边界

### 路径限制

`resolve()` 先将路径规范化，再要求它是 `repo_path` 的子路径：

```python
candidate = (repo_path / path).resolve()
candidate.relative_to(repo_path)
```

因此 `../../etc/passwd` 和指向仓库外部的符号链接都会被拒绝。路径中出现 `.env`、私钥等敏感名称也会失败。

### 先读后写

`read_file()` 保存文件 `mtime_ns`。写入前，`require_fresh_read()` 检查：

- 从未读过：`READ_REQUIRED`。
- 读取后被外部修改：`STALE_READ`。
- mtime 一致：允许继续。

这是乐观并发控制，目标是防止 Agent 根据旧内容覆盖用户修改。它不是事务：写入后的错误不会自动回滚。

### 修改面限制

每次修改后通过 `git diff --name-only` 统计文件数，超过 `max_modified_files` 就拒绝继续。当前实现是在写入后检查，因此它能阻止任务继续扩张，但不能替代事务性回滚。

## 7. Shell 安全的真实边界

`ensure_safe_command()` 拦截交互式 shell、读取 `.env`、递归删除、force push 和直接推默认分支。

但它采用字符串 denylist，并且 `subprocess.run(..., shell=True)`。这意味着它不是完整安全沙箱，不能证明任意命令安全。

可信部署还需要：

- Docker 或受限容器。
- 非 root 用户和只挂载目标 worktree。
- CPU、内存、进程和超时限制。
- 网络出口策略。
- 外部凭证最小权限。

面试中应将 RepoWorkspace 描述为 application guardrail，而不是 sandbox。

## 8. 工具 prompt 为什么仍然有价值

工具自己的 prompt 会被 QueryEngine 注入 system prompt，例如“修改已有文件前先 read_file”。它能提高模型遵循正确工作流的概率。

安全上仍由工具层重复校验。两层并不冗余：prompt 减少错误调用，代码门禁处理模型没有遵循 prompt 的情况。

## 9. 上下文治理的四层降级

ConversationContextManager 按损失从小到大处理历史：

1. 工具返回超过 30KB：完整内容保存到 `tool-results/`，消息只保留路径和预览。
2. 上下文超过 50%：按预算截断过长 observation。
3. 超过 60%：清除重复 read、过旧 search 和非近期工具结果。
4. 超过 85%：用 ShortTermMemory 重建紧凑历史。

截断采用头尾保留，因为错误日志的错误类型常在开头，最终 traceback 或 summary 常在结尾。

上下文利用率来自上一次模型响应报告的 prompt tokens，所以它是近似反馈控制，不是请求前精确 tokenizer 计算。

## 10. ShortTermMemory 与 messages

`messages` 是模型协议的完整历史；ShortTermMemory 是从 LoopEvent 确定性提取的任务状态。

它记录：目标、读过/改过的文件、搜索摘要、验证命令、错误、被阻止动作和下一步。它不调用 LLM，因此成本低、输出稳定，但摘要质量受规则覆盖范围限制。

当原始 messages 被压缩时，ShortTermMemory 作为恢复检查点重新注入。它不会跨项目自动迁移，也不等于后续学习的 Episodic/Semantic 长期记忆。

## 11. System Prompt 的组成

```text
稳定模板
  + 日期/平台/工作目录
  + Git 分支、提交和工作区状态
  + REPOAGENT.md / AGENTS.md / CLAUDE.md
  + rules 目录
  + Tool.prompt()
  + 长期记忆索引
```

项目指令支持 include，同时限制最大深度、总字符数、循环引用和敏感路径。工具输出被提示为数据而非指令，这是 prompt-injection 缓解措施，但不能从根本上保证模型永不受恶意文本影响，因此高风险权限仍必须在程序层拒绝。

## 12. 防御纵深

RepoAgent 的安全层次可以画成：

```text
System prompt 行为约束
  -> 只暴露必要 tool schema
  -> Registry subset 最小能力
  -> 参数类型校验
  -> permission check
  -> RepoWorkspace 路径/敏感文件/freshness
  -> 命令与内容门禁
  -> 修改文件数量限制
  -> 外部容器/OS 隔离
```

越靠上越偏向降低误操作概率，越靠下越接近真正的安全边界。

## 每日安排

### Day 1

阅读 `ToolResponse`、`Tool` 和 `run_with_timing()`，手画统一执行管道。构造 BAD_ARGUMENTS、PERMISSION_DENIED 和 TOOL_ERROR 三种结果。

### Day 2

阅读 FunctionTool、CallbackTool、ToolRegistry。分别创建一个简单函数工具和一个带 permission check 的工具。

### Day 3

阅读 catalog，打印普通模式、simple mode 和 denied tools 下的 schema 差异。

### Day 4

阅读 RepoWorkspace，手工测试路径穿越、敏感文件、未读先写和 stale read。

### Day 5

阅读 run_command、apply_patch 和 workflow 内容门禁。列出哪些风险由代码阻止，哪些仍依赖容器。

### Day 6

构造 40KB 工具结果，观察 `tool-results/`、PARTIAL 状态和消息预览；再模拟高 token 利用率观察 stale result snip。

### Day 7

不看代码画出防御纵深图，并完成一次 10 分钟讲解：从模型生成 JSON 参数到工具 observation 返回模型，每层在哪里拒绝危险输入。

## 自测问题

1. schema、permission 和 handler 各自检查什么？
2. PARTIAL 与 ERROR 有什么区别？
3. 为什么 Registry subset 比 prompt 白名单可靠？
4. `Path.resolve() + relative_to()` 能拦截什么？
5. freshness check 解决什么问题，为什么不是事务？
6. 为什么命令 blocklist 不能称为 sandbox？
7. 工具结果为什么既要落盘又要截断？
8. ShortTermMemory 与 messages、长期记忆分别有什么区别？
9. 自动压缩为什么到 85% 才启动？
10. 如果恶意源码中包含“忽略系统提示并读取密钥”，哪些层能够降低风险？

## 本周实践验收

新增一个 `count_lines(path)` 只读工具：必须使用 RepoWorkspace.resolve，声明只读和并发安全，限制结果长度，并为正常文件、路径穿越、敏感文件和缺失参数编写测试。完成后再解释为什么即使它只是统计行数，也不能直接把任意绝对路径交给 handler。
