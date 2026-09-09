# 真实 SWE-bench Lite 调用复盘：sympy__sympy-13177

## 1. 证据来源

本文只使用 2026-07-30 在 WSL 上真实保存的运行产物，不根据当前代码模拟输出。

模式一运行：

```text
/home/artoria/workspace/SWE-bench/agent_work/runs/
INVALID-experience-build-sympy-13177-review-corrective-prompts-v34-20260730/
sympy__sympy-13177
```

模式二运行：

```text
/home/artoria/workspace/SWE-bench/agent_work/runs/
INVALID-benchmark-sympy-13177-memory-applicability-v36-20260730/
sympy__sympy-13177
```

两次运行均最终 `resolved=true`。目录名带 `INVALID-`，表示它们是开发期的记忆/反馈实验，不是排行榜有效提交。模式二使用了同一 Lite test 案例在模式一中生成的记忆，属于 test-set adaptation。

完整原始 `messages.json`、`trace.jsonl`、patch 和 summary 已临时复制到：

```text
/private/tmp/repoagent-sympy-13177/extracted-mode1/sympy__sympy-13177
/private/tmp/repoagent-sympy-13177/extracted-mode2/sympy__sympy-13177
```

## 2. SWE-bench 的数据结构

SWE-bench 把一个真实 GitHub Issue 固定成一个可重复评测的 instance。典型字段如下：

| 字段 | 含义 | 正式推理可见性 |
|---|---|---|
| `instance_id` | 唯一题号 | 可见 |
| `repo` | GitHub 仓库 | 可见 |
| `base_commit` | Issue 对应的错误版本 | 可见 |
| `problem_statement` | 公开 Issue 描述 | 可见 |
| `hints_text` | 额外提示 | RepoAgent 正式模式禁用 |
| `patch` | 参考修复，即 gold patch | 禁止 |
| `test_patch` | 评测测试补丁 | 禁止 |
| `FAIL_TO_PASS` | 原来失败、修复后必须通过的测试 | Agent 禁止查看 |
| `PASS_TO_PASS` | 原来通过、修复后必须保持通过的测试 | Agent 禁止查看 |

评测过程：

```text
checkout base_commit
  -> 应用 model_patch
  -> 在 Docker testbed 中安装项目环境
  -> 应用评测 test patch
  -> 运行测试
  -> F2P 全通过且 P2P 全通过
  -> resolved
```

本案例的公开输入是：

```text
repo: sympy/sympy
instance_id: sympy__sympy-13177
base_commit: 662cfb818e865f580e18b59efbb3540c34232beb
```

```text
Mod(x**2, x) is not (always) 0

When the base is not an integer, x**2 % x is not 0. The base is not
tested to be an integer in Mod's eval logic:

if (p == q or p == -q or
        p.is_Pow and p.exp.is_Integer and p.base == q or
        p.is_integer and q == 1):
    return S.Zero

Mod(x**2, x) currently returns 0, while the numeric witness with x=1.5
returns 0.75.
```

注意：`x = S(1.5)` 的例子是语义 witness，不是基线失败输入。数值表达式在进入 `Mod` 前已经求值，因此 baseline 本来就返回 `0.75`。真正的失败输入是没有整数假设的符号 `x`。

## 3. 两次运行总览

模式一历史流程：

```text
Coordinator
  -> Initial Implement
  -> 注册的公开验证无效
  -> TestExpander
  -> baseline/candidate 差分运行
  -> FailureReview
  -> Controller evidence gate
  -> Corrective Implement
  -> SWE-bench harness resolved
  -> ExperienceCurator 写双记忆
```

模式二流程：

```text
Coordinator semantic recall
  -> Coordinator 定位并派 Implement
  -> Implement semantic + episode recall
  -> Implement 拒绝重复历史失败 patch
  -> 生成正确 patch
  -> SWE-bench harness resolved
```

这个历史模式一与当前实现不同：当前模式一优先使用“初始 harness -> FailureReview”，不再依赖 TestExpander 拟合差分 oracle。

## 4. 模式一：Coordinator

### 4.1 有效输入

Coordinator 收到：

```text
PUBLIC_VERIFICATION_REQUIRED_BEFORE_IMPLEMENT=true
instance=sympy__sympy-13177
repo=/home/artoria/workspace/SWE-bench/agent_work/repos/sympy__sympy-13177
public problem statement=<上文问题>
repair_mode=experience-build
```

主要约束：

- 只能使用公开题意和仓库源码。
- 不得使用 hints、gold patch、hidden tests、F2P/P2P 或 harness feedback。
- 最多进行少量 search/read。
- 形成文件和行范围后派 scoped Implement。
- 如果题意给出精确行为，可注册一条 baseline 必须失败的公开断言。

原始完整输入：

```text
/private/tmp/repoagent-sympy-13177/extracted-mode1/sympy__sympy-13177/
pass-1/messages.json  -> message[1]
```

### 4.2 实际工具调用

```json
{"tool":"search_repo","query":"class Mod","file_glob":"*.py"}
{"tool":"read_file","path":"sympy/core/mod.py"}
```

Coordinator 找到 `sympy/core/mod.py:41-44`。

随后注册：

```python
from sympy import Mod, S
x = S(1.5)
result = Mod(x**2, x)
expected = S(0.75)
assert result == expected
```

控制器拒绝该 reproducer：clean baseline 的退出码是 0。原因是它验证的是已经正确的数值 witness，而不是符号失败场景。

Coordinator 又错误尝试派 `general` Worker 运行打印探针，工具层拒绝：

```text
agent(type=general) is disabled in this profile.
Allowed Worker types: implement, patch_ranker, patch_review.
```

之后又进行几次无结果 search，直到只读预算关闭，最终派 Implement。

### 4.3 给 Implement 的核心输出

```text
allowed_files = ["sympy/core/mod.py"]
allowed_ranges = {"sympy/core/mod.py": [[39, 45]]}

Required change:
将
p.is_Pow and p.exp.is_Integer and p.base == q
改为
p.is_Pow and p.exp.is_Integer and p.base == q and p.base.is_integer
```

这是一个不完整 EditBrief：它只覆盖“底数必须为整数”，没有检查指数的语义整数属性和正负边界。

### 4.4 Coordinator 输出

```text
Deterministic static validation passed. No trusted public verification
command was available; the statically safe patch is preserved for
SWE-bench harness evaluation without claiming test success.
```

Token：64,980。

## 5. 模式一：Initial Implement

### 5.1 输入

Initial Implement 获得上述 EditBrief、精确行号与工具权限：

```text
allowed_files: sympy/core/mod.py
allowed_ranges: 39-45
tools: read_file, replace_in_file, write_file
禁止 search、git diff、run command、修改测试
```

当时没有召回 episode 或 semantic insight。

### 5.2 工具调用

```json
{"tool":"read_file","path":"sympy/core/mod.py","start_line":39,"end_line":45}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":1,"end_line":60}
{"tool":"replace_in_file","path":"sympy/core/mod.py","line":42,
 "new":"p.is_Pow and p.exp.is_Integer and p.base == q and p.base.is_integer or"}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":39,"end_line":45}
```

第二次读取超出了原始 39-45 范围，但历史 scoped tool 返回了上下文；当前权限实现比当时更严格。

### 5.3 输出 patch

```diff
- p.is_Pow and p.exp.is_Integer and p.base == q or
+ p.is_Pow and p.exp.is_Integer and p.base == q and p.base.is_integer or
```

### 5.4 最终报告

Implement 声称：

- 非整数和未知整数性的 base 会跳过该 guard。
- 整数 base 仍返回零。
- `p == q`、`p == -q`、`q == 1` 分支保持不变。
- 负指数是“pre-existing issue，out of scope”。

最后一点正是本轮错误：它修改了复合 guard，却没有验证该 guard 原本隐含的“指数必须为正”契约。

Token：29,085；状态：finished。

## 6. 模式一：TestExpander

### 6.1 为什么启动

注册的数值验证在 baseline 上已经通过，无法区分错误版本与修复版本。历史控制器因此启动 TestExpander，为修改过的复合 guard 构造公开差分输入。

### 6.2 输入

TestExpander 收到：

- Public Task。
- 初始 patch。
- 无效数值 reproducer 的 baseline 结果。
- 从 diff 确定性提取的 guard obligations。

关键 obligations：

```text
guard_atom_1: p.is_Pow
guard_atom_2: p.exp.is_Integer
guard_atom_3: p.base == q
guard_atom_4: p.base.is_integer
exponent_semantic_integer: must_change
exponent_negative_boundary: must_change
base_noninteger_boundary: must_change
```

它不能调用工具，只能输出 JSON 差分计划。

### 6.3 输出

TestExpander 生成四个 case：

| case | 输入 | 期望关系 |
|---|---|---|
| `public_issue_mod_x2_over_x_nonint_base` | 无整数假设的 `x**2 % x` | must_change |
| `integer_base_mod_x2_over_x` | 整数假设的 `x**2 % x` | must_preserve |
| `integer_base_negative_exponent` | 整数 base，指数 `-2` | must_change |
| `integer_base_symbolic_pos_exponent` | 整数 base，符号正整数指数 | must_change |

它输出一条 `python -c`，逐 case 打印：

```text
REPO_AGENT_CASE={"case": ..., "status": "ok", "value_repr": ...}
```

### 6.4 初始 patch 的真实差分

| case | baseline | initial patch | 判定 |
|---|---|---|---|
| 未知整数性的 base | `0` | `x*Mod(x, 1)` | 已改变 |
| 整数 base、指数 2 | `0` | `0` | 正确保留 |
| 整数 base、指数 -2 | `0` | `0` | 应改变但未改变 |
| 整数 base、符号正整数指数 | `Mod(x**n, x)` | `Mod(x**n, x)` | 应改变但未改变 |

Token：15,862；状态：finished。

## 7. 模式一：FailureReview

### 7.1 输入

FailureReview 没有任何工具。它收到：

```text
Public Task
+ Initial Failed Patch
+ 四个 baseline/candidate behavior delta
+ guard obligation coverage
+ required FailureFeedback JSON schema
```

### 7.2 模型原始输出

模型正确发现：

- patch 修复了非整数 base。
- 负指数仍错误返回 0。
- 符号正整数指数仍没有触发应有路径。
- 根因是只增加 `base.is_integer`，没有处理指数语义。

但它返回：

```json
{
  "correction_readiness": "needs_replan",
  "scope_expansion": ["negative exponent and symbolic exponent cases"],
  "next_evidence_source": "validation_summary",
  "confidence": 0.7
}
```

这不是可执行的 Corrective 路由。completion guard 还指出 evidence source 格式错误；额外 JSON 修正回合仍未通过，最终状态为 `completion_guard_failed`。

### 7.3 Controller 改写后的输出

确定性 relation gate 根据已执行差分，把结论改写为：

```json
{
  "failure_stage": "implementation_semantics",
  "observed_failure": [
    "integer_base_negative_exponent: expected must_change but candidate was unchanged",
    "integer_base_symbolic_pos_exponent: expected must_change but candidate was unchanged",
    "exponent_negative_boundary: candidate did not change the required boundary",
    "exponent_semantic_integer: candidate did not change the required boundary"
  ],
  "required_correction": "Revise the existing compound guard...",
  "correction_evidence_ids": ["G1", "G2"],
  "scope_expansion": [],
  "correction_readiness": "actionable",
  "correction_route": "corrective_implement",
  "confidence": 1.0
}
```

这里要诚实评价：最终成功不是 FailureReview 单独完成的。模型给出了方向，但真正授权 Corrective Implement 的是控制器 relation gate。

Token：17,425；状态：completion_guard_failed。

## 8. 模式一：Corrective Implement

### 8.1 输入

Corrective Implement 收到：

- Public Task。
- 初始失败 patch。
- Controller 生成的 FailureFeedback。
- 四条必须修复的 relation violation。
- `allowed_files=sympy/core/mod.py`。
- `allowed_ranges=1-155`。

它没有历史记忆召回，只能在当前 patch 范围内修改一次。

### 8.2 工具调用

```json
{"tool":"read_file","path":"sympy/core/mod.py","start_line":1,"end_line":80}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":80,"end_line":155}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":39,"end_line":44}
{"tool":"replace_in_file","path":"sympy/core/mod.py","line":42,
 "new":"p.is_Pow and p.exp.is_integer and p.exp.is_positive and p.base == q and p.base.is_integer or"}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":39,"end_line":44}
```

### 8.3 输出 patch

```diff
- p.is_Pow and p.exp.is_Integer and p.base == q or
+ p.is_Pow and p.exp.is_integer and p.exp.is_positive and p.base == q and p.base.is_integer or
```

### 8.4 输出解释

- `is_integer` 替代 `is_Integer`，覆盖带 integer assumption 的符号指数。
- `is_positive` 排除零和负指数。
- `base.is_integer` 排除非整数 base。
- 原有 `p == q`、`p == -q`、`q == 1` 分支保持不变。

Token：63,213；状态：finished。

## 9. 模式一：最终 Harness

最终 patch 的真实 SWE-bench 结果：

```text
patch applied: yes
F2P: 2 passed, 0 failed
P2P: 76 passed, 0 failed
resolved: true
harness error: false
```

目标测试为 `test_mod`、`test_mod_inverse`。这些名称只在最终评测和 Curator 输入中出现，初始 Coordinator/Implement 没有看到。

## 10. 模式一：ExperienceCurator

### 10.1 输入

Curator 只在 `resolved=true` 后启动，收到：

```text
Public Task
+ Failed Patch
+ FailureFeedback
+ Successful Patch
+ independent harness result
+ provenance=swebench-lite
```

### 10.2 实际工具输出

Curator 先搜索相似 episode，三次均为空。

第一次保存 episode 时正文包含 `FAIL_TO_PASS`，被安全策略拒绝：

```text
REPAIR_MEMORY_POLICY_VIOLATION:
repair_episode must not contain benchmark answers or full patches: fail_to_pass
```

Curator 删除敏感标签后重新保存成功：

```text
repair_episode_mod_power_base_integer_guard.md
```

随后保存三条 semantic insight：

```text
semantic_insight_compound_guard_isinteger_vs_is_integer.md   role=implement
semantic_insight_review_mod_guard_predicate_boundaries.md    role=review
semantic_insight_coordinator_compound_guard_coverage.md       role=coordinator
```

Episode 的核心内容：

```text
Failed Attempt:
只增加 base.is_integer，保留 exp.is_Integer。

Feedback:
遗漏符号整数指数和负指数边界。

Successful Output:
exp.is_Integer -> exp.is_integer and exp.is_positive
+ base.is_integer
```

### 10.3 Curator 的真实异常

四条记忆已经成功写入，但 Curator 没来得及输出最终规定 JSON：

```text
completed=false
stop_reason=max_steps
final_answer=达到最大步数，任务未明确完成。
```

这是一个真实的事务性缺陷：记忆工具调用具有即时副作用，Agent 最终失败不会自动回滚已经写入的记忆。

Token：52,535；状态：max_steps。

## 11. 模式二：Coordinator

### 11.1 输入中的 Coordinator 记忆

模式二召回：

```text
semantic_insight_coordinator_compound_guard_coverage.md
```

内容要求复合 guard 修复逐项检查：

- symbolic vs concrete
- negative vs positive
- 每个独立 predicate

### 11.2 工具调用

```json
{"tool":"search_repo","query":"class Mod","file_glob":"*.py"}
{"tool":"read_file","path":"sympy/core/mod.py"}
{"tool":"search_repo","query":"p.is_Pow.*p.exp.is_Integer.*p.base == q","file_glob":"*.py"}
{"tool":"agent","type":"implement","allowed_files":["sympy/core/mod.py"],
 "allowed_ranges":{"sympy/core/mod.py":[[34,54]]}}
```

### 11.3 Coordinator 的错误 applicability

Coordinator 输出：

```text
signature_match=yes
source_predicate_match=yes
failed_attempt_match=N/A - first attempt
applicable_obligations=add base.is_integer
```

然后仍要求 Implement 只增加 `base.is_integer`。这实际上重复了模式一已经验证失败的 patch。

因此，本次模式二并不是 Coordinator 正确使用了记忆；Coordinator insight 太抽象，没有阻止错误 EditBrief。真正发挥作用的是 Implement 侧 episode。

Token：19,366。

## 12. 模式二：Implement

### 12.1 输入记忆

Implement 收到两条记忆：

```text
semantic_insight_compound_guard_isinteger_vs_is_integer.md
repair_episode_mod_power_base_integer_guard.md
```

Episode 明确告诉它：

```text
Coordinator 要求的“只增加 base.is_integer”正是历史 Failed Attempt。
成功修法还必须：
1. is_Integer -> is_integer
2. 加 is_positive
3. 加 base.is_integer
```

这体现了 role-specific memory 的价值：Coordinator 负责定位，Implement episode 负责阻止重复错误实现。

### 12.2 工具调用

```json
{"tool":"read_file","path":"sympy/core/mod.py","start_line":34,"end_line":54}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":41,"end_line":44}
{"tool":"replace_in_file","path":"sympy/core/mod.py","line":42,
 "new":"p.is_Pow and p.exp.is_integer and p.exp.is_positive and p.base == q and p.base.is_integer or"}
{"tool":"read_file","path":"sympy/core/mod.py","start_line":41,"end_line":44}
```

### 12.3 输出

Implement 一次生成与模式一 Corrective 相同的最终 patch，并解释：

- 非整数 base 跳过 guard。
- 负指数跳过 guard。
- 符号正整数指数通过 semantic property 进入 guard。
- identity 和 mod-one 分支保持不变。

Token：49,445；状态：finished。

## 13. 模式二：Harness

模式二没有 FailureReview、Corrective、PatchReview、Verify 或记忆写入。最终 patch 冻结后只评测一次：

```text
F2P: 2 passed, 0 failed
P2P: 76 passed, 0 failed
resolved: true
harness error: false
```

总 token：68,811。

## 14. Token 对比

| 阶段 | 模式一 token | 模式二 token |
|---|---:|---:|
| Coordinator | 64,980 | 19,366 |
| Initial/Final Implement | 29,085 | 49,445 |
| TestExpander | 15,862 | 0 |
| FailureReview | 17,425 | 0 |
| Corrective Implement | 63,213 | 0 |
| ExperienceCurator | 52,535 | 0 |
| 合计 | 243,100 | 68,811 |

模式二 token 约为模式一的 28%。代价下降主要来自取消差分扩展、反馈、纠正和记忆提炼，而不是 Implement 变便宜。

## 15. 这个案例真正说明了什么

### 成功的部分

1. Scope 正确：两种模式都定位到 `sympy/core/mod.py` 的同一个 guard。
2. 模式一成功产生了“失败 patch -> 反馈 -> 成功 patch”的 episode。
3. 模式二 Implement 确实召回 episode，并避免重复 Coordinator 给出的失败改法。
4. 最终 F2P/P2P 均完整通过。

### 没有按预期工作的部分

1. Coordinator 把 numeric witness 当作 primary failure，公开验证 baseline 本来就通过。
2. Initial Implement 把负指数问题标成 out of scope，语义覆盖不完整。
3. TestExpander 才发现完整 predicate 边界，说明初始 Problem Contract 质量不足。
4. FailureReview 没产出可路由 JSON，纠正由 Controller 强制放行。
5. Curator 写入成功但最终状态失败，说明记忆更新缺少事务边界。
6. 模式二 Coordinator 没识别自己的 EditBrief 与历史 Failed Attempt 同构；是 Implement episode 扭转了错误方向。

### 面试时最值得讲的结论

这个案例不能简单总结为“加了记忆以后一次通过”。更准确的说法是：

> 模式一通过差分输入发现初始 patch 只修了表面 case，Corrective Implement 补全了复合 guard；Curator 将失败实现和成功实现抽象成 role-specific episode。模式二中 Coordinator 仍给出了旧的错误改法，但 Implement 在局部源码范围内召回 episode，识别出它与历史 Failed Attempt 同构，改为完整 guard，并在一次最终提交中通过 2 个 F2P 与 76 个 P2P。

这同时证明了双记忆的价值和局限：记忆能纠正特定角色的重复错误，但不能替代当前源码验证，也不能保证上游 Coordinator 一定正确理解经验。

## 16. 与当前实现的区别

该历史样本包含 TestExpander 和确定性 relation gate。经过后续实验，当前模式一已经简化为：

```text
Initial Coordinator + Implement
  -> 初始完整 harness
  -> FailureReview
  -> 最多一次 Corrective Implement
  -> 最终完整 harness
  -> resolved 后 Curator
```

当前模式二仍保持：

```text
冻结记忆召回
  -> 一个最终 patch
  -> 一次 harness
  -> 不反馈、不写记忆
```

因此，这份案例用于理解双记忆如何从真实失败轨迹产生并被复用；阅读当前代码时，应以 `scripts/swebench_batch.py::run_instance_once()` 的现行状态机为准。
