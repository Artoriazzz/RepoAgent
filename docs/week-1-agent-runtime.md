# 第一周学习讲义：单 Agent Runtime

## 学习目标

完成本周后，你应该能够不看代码画出 RepoAgent 的单 Agent 状态机，解释一条工具调用消息如何往返，并独立写出一个最小 function-calling Agent。

本周只学习以下五个文件：

1. `repo_agent/cli.py`：依赖装配和执行入口。
2. `repo_agent/core/llm.py`：模型协议适配。
3. `repo_agent/core/query_loop.py`：单轮状态机。
4. `repo_agent/core/query_engine.py`：会话生命周期。
5. `repo_agent/core/agent.py`：对外 facade。

工具的具体安全实现、Coordinator 和记忆算法留到后续学习。本周只需要知道 `ToolRegistry.schemas()` 向模型暴露工具定义，`ToolRegistry.execute()` 负责真实执行。

## 一次请求的完整路径

```mermaid
sequenceDiagram
    participant CLI
    participant Agent as RepoAgent
    participant Engine as QueryEngine
    participant Loop as query_loop
    participant LLM as ChatLLM
    participant Registry as ToolRegistry

    CLI->>Agent: run(task)
    Agent->>Engine: chat(task)
    Engine->>Engine: append user message
    Engine->>Loop: messages, tools, max_steps
    Loop->>LLM: messages + tool schemas
    LLM-->>Loop: assistant + tool_calls
    Loop->>Registry: execute(name, JSON arguments)
    Registry-->>Loop: ToolResponse
    Loop->>Loop: append tool observation
    Loop->>LLM: updated messages
    LLM-->>Loop: final content
    Loop-->>Engine: FINISH event
    Engine->>Engine: save trace/messages/session
    Engine-->>CLI: final answer
```

关键认知：LLM 不会直接运行 Python 函数。它只输出“我想调用什么工具、参数是什么”。真正的执行权始终在 RepoAgent 进程中的 ToolRegistry。

## 必须看懂的代码

### 1. CLI 的依赖装配

从 `cli.py::main()` 看四件事：

- 参数如何决定模型和 max steps。
- `RepoWorkspace` 如何绑定目标仓库。
- `assemble_tool_pool()` 如何生成 ToolRegistry。
- 非 Coordinator 路径如何构造 `RepoAgent` 并调用 `run()`。

CLI 是 composition root：它负责创建对象，但不应该包含 Agent Loop。

### 2. LLM 协议边界

`ChatLLM.invoke_with_tools()` 将 `messages` 和工具 schema 发送给兼容接口，再转换为 `LLMToolResponse`。

必须区分：

- `content`：模型给人的普通文本。
- `tool_calls`：模型提出的动作意图。
- `raw_message`：供应商原始扩展字段。
- `usage`：本次请求的 token 统计。

`ToolCall.arguments` 暂时保持 JSON 字符串。参数解析、类型校验和权限判断由 ToolRegistry 完成，这是清晰的职责分离。

### 3. query_loop 状态机

主循环可以压缩成下面的伪代码：

```python
for step in range(max_steps):
    response = llm(messages, tool_schemas)
    emit(LLM_RESPONSE)

    if not response.tool_calls:
        validate_final_answer()
        emit(FINISH)
        return

    messages.append(assistant_tool_call_message)
    for call in response.tool_calls:
        emit(TOOL_CALL)
        result = registry.execute(call)
        messages.append(tool_result_message)
        emit(TOOL_RESULT)

    emit(CONTINUE)
```

一个 step 是一次模型调用加该响应中的全部工具调用。模型一次返回三个工具调用，仍只消耗一个普通 step。

### 4. QueryEngine 会话层

`QueryEngine.achat()` 在调用循环前后承担会话职责：

- 添加 user message。
- 召回长期记忆并做上下文压缩。
- 消费 LoopEvent。
- 累计 token 并更新 ShortTermMemory。
- 区分 finished、aborted、max_steps 和 incomplete。
- 在 `finally` 中保存消息与 trace。

这使 query_loop 保持可测试：FakeLLM 不需要真实网络，也不需要知道输出目录。

### 5. RepoAgent facade

`RepoAgent` 不实现新逻辑，只隐藏 QueryEngine 的构造细节。Facade 的价值是给 CLI 和未来 API 一个稳定入口，避免外部代码依赖会话运行时的全部参数。

## 消息协议

典型工具调用历史如下：

```json
[
  {"role": "system", "content": "系统规则"},
  {"role": "user", "content": "读取 app.py"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"path\":\"app.py\"}"}
      }
    ]
  },
  {
    "role": "tool",
    "tool_call_id": "call_1",
    "content": "{\"status\":\"success\",\"text\":\"...\"}"
  },
  {"role": "assistant", "content": "读取完成。"}
]
```

`tool_call_id` 将 observation 与原始调用对应起来。assistant/tool 任意一侧缺失都会破坏协议历史。

DeepSeek 思考模式还要求把上一轮的 `reasoning_content` 原样带回。RepoAgent 保存在 `raw_message`，并由 `_assistant_tool_call_message()` 写回下一轮消息。

## 六种结束路径

| 路径 | 触发条件 | Engine 状态 |
|---|---|---|
| 自然结束 | 模型没有返回 tool calls | `finished` |
| finish 工具 | 模型调用显式 finish | `finished` |
| 工具终止 | ToolResponse 携带 `terminate_loop` | 完成或 incomplete |
| 用户中断 | AbortSignal 被设置 | `aborted` |
| 步数耗尽 | 达到 max_steps | `max_steps` |
| 终稿校验失败 | completion guard 修正后仍非法 | `completion_guard_failed` |

completion guard 不是验证补丁正确性的 Reviewer。它只阻止格式错误、缺字段或违反状态机要求的终稿被误认为完成。

## 同步与异步

OpenAI 客户端和 ToolRegistry 当前提供同步方法，但 query_loop 是异步生成器。`asyncio.to_thread()` 将阻塞调用移到线程中，因此事件循环仍能处理中断、消费事件或并行运行彼此独立的 Worker。

这不是让同一个 worktree 中的写工具并行执行。并发调度与代码写冲突是另一层问题。

## 为什么使用事件

query_loop 不直接写 trace 或累计 token，而是产生 `LLM_RESPONSE`、`TOOL_CALL`、`TOOL_RESULT`、`CONTINUE` 和 `FINISH`。

好处是同一循环可以被 CLI、测试、日志系统或未来 UI 复用。QueryEngine 将事件投影成会话状态，但循环本身不依赖存储方式。这和事件驱动设计相似，但项目不是完整的 event sourcing，因为状态仍保存在内存对象和快照文件中。

## 每日安排

### Day 1：跑通与观察

运行 `tests/test_query_loop.py`，查看 FakeLLM 如何模拟“调用工具后结束”。打开测试生成的 `messages.json`、`trace.jsonl` 和 `session.json`。

### Day 2：读 LLM 与消息协议

手写一组带 tool call 的 messages，解释每个 role 和 `tool_call_id`。回答为什么不能让 LLM 层直接执行工具。

### Day 3：手画 query_loop

不看代码画出所有状态和六种结束路径，再与 `query_loop.py` 对照。重点说明一个 step 的定义。

### Day 4：理解 QueryEngine

跟踪 `_handle_event()` 和 `_auto_save()`，说明为什么保存逻辑在 `finally` 中。模拟 max steps 和 abort。

### Day 5：最小复刻

新建练习脚本，只实现 FakeLLM、两个工具、消息历史和最多五步循环。不要复制项目代码。

### Day 6：测试驱动理解

给练习脚本补四个测试：自然结束、工具调用、未知工具、max steps。再阅读 RepoAgent 对应测试。

### Day 7：口述与白板

完成一次 10 分钟无代码讲解：入口、消息协议、循环、事件、终止、持久化。录音后检查是否把“模型调用工具”和“程序执行工具”混为一谈。

## 自测问题与参考答案

**为什么不用 while True？**

有界 `max_steps` 能限制失控循环、成本和延迟，并为未完成任务提供明确归因。

**为什么 messages 由 QueryEngine 创建，却由 query_loop 修改？**

Engine 拥有会话，loop 只负责推进该会话。列表按引用传入，让工具 observation 立即成为同一会话历史，同时避免 loop 管理存储生命周期。

**没有 tool_calls 是否一定代表任务正确完成？**

不一定，只代表模型想停止。completion guard 可以验证结构和工作流条件，代码正确性仍需测试、静态检查或外部评测。

**为什么保留 raw_message？**

内部稳定字段无法覆盖所有供应商扩展。保留原始消息可以兼容 reasoning_content，又不让整个系统依赖某个 SDK 类型。

**为什么工具结果要重新作为 message 发给模型？**

模型不会共享 Python 进程状态。只有把 observation 写入对话，它下一轮才知道工具执行结果。

**为什么 QueryEngine 不直接写一个 while 循环？**

拆分后，循环策略可以独立测试和复用；会话层则专注 token、trace、记忆、持久化和停止状态。

**completion guard 为什么有额外且不计 max_steps 的回合？**

普通步骤可能全部用于收集证据，最后只差把已有结论整理成合法 JSON。额外回合不暴露工具，只允许格式修正，避免为了格式问题重跑整个 Agent。

**`asyncio.to_thread()` 是否让工具天然线程安全？**

不是。它只避免阻塞事件循环。文件并发写入、共享状态和 Worker 隔离仍需单独控制。

## 本周验收

在不打开项目代码的情况下完成以下内容：

1. 画出 CLI 到 ToolRegistry 再返回最终答案的序列图。
2. 写出一段合法 assistant/tool 消息对。
3. 列出六种结束路径和对应 stop reason。
4. 解释 QueryEngine 与 query_loop 的职责边界。
5. 独立实现并测试一个最小 function-calling loop。
