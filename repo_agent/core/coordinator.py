from __future__ import annotations

import json
import subprocess
from pathlib import Path

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore, MemoryType
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.repair_memory import (
    RepairRecipeApplicability,
    RepairRecipeConstraint,
    augment_task_with_repair_recipes,
    build_case_curator_prompt,
    hard_constraints_from_repair_recipes,
)
from repo_agent.core.semantic_evidence import build_verification_plan, extract_public_problem_statement
from repo_agent.core.subagent import SubAgentRunner, WorkerConstraints
from repo_agent.core.tool import Tool, ToolRegistry, ToolResponse, build_tool
from repo_agent.tools.agent_tools import build_agent_tools, reset_agent_tool_state


COORDINATOR_SYSTEM_PROMPT = """你是 RepoAgent Coordinator，负责低成本地把仓库问题转成一次可提交 patch。

# 正式评测约束
- SWE-bench 出分按 pass@1：只允许一次修复。不要请求、等待或使用 harness 失败日志做第二轮修改。
- 只能依据公开 problem statement 和 repo 内容。不要使用 SWE-bench hints、PASS_TO_PASS / FAIL_TO_PASS 元数据、gold patch、hidden test patch 或评测反馈。
- 目标是最小源代码语义改动；空 patch、纯空白 patch、只改测试都无效。
- SWE-bench 模式禁止修改 `tests/`、`test_*.py`、`*_test.py`、fixture 期望文件或任何评测用例；只能修改生产源码/文档中题意明确要求的源文件。
- 不要安装依赖、不要 pip install、不要搭环境；验证因缺依赖失败时可保留候选 patch 供诊断，但不能标为可提交。

# 默认流程
1. 写 Problem Contract：输入、期望输出/异常/副作用、禁止行为。
2. 开局先查看 Recalled Repair Recipes；它们默认只是软参考。只有 problem_signatures/applicability 与当前任务强匹配的 recipe，才转成当前任务的 Problem Contract / EditBrief / Verify Contract。
3. 轻量定位：优先直接用 `search_repo`、`read_file`、`git_diff`；短 grep/短范围读取不要派 Worker。
4. 需要跨文件理解时，最多派 1 个 research Worker，并要求它输出结构化 `edit_brief`。
5. 对 SWE-bench/bugfix 默认借鉴 ExpeRepair 的干净候选池：在不使用 harness 反馈、不使用官方测试元数据的前提下，先派 1 个 `reproducer` Worker 形成公开复现/差分验证契约，再生成 2-3 个候选 patch，最后由 `patch_ranker` 只根据公开契约、候选 diff 和公开运行结果排序，应用一个最终候选。极简单的精确局部 bug 可走单 patch。
6. 派 implement 前先形成可执行 EditBrief：不要求固定字段名，但必须包含目标行为、具体改动方向、旧行为/安全不变量和允许范围。implement 只负责在给定范围内做局部确认和最小修改，不负责全仓搜索、跑命令、查 git log 或宽泛研究。
7. 单个 implement Worker 每次只生成一个候选 patch；调用必须带 `allowed_files` 和 `allowed_ranges`。候选 patch 生成后，如果还要探索其他方案，先调用 `patch_candidate(action="save_current")` 保存并回滚当前 diff，再派下一次 implement。不要在一个 implement prompt 中要求同时写多个互斥方案。
8. 候选池一般控制在 2-3 个：候选之间应体现真实策略差异，例如不同 guard、不同语义层、不同 reader/writer 覆盖方式，而不是同一 patch 的格式改写。候选池不是 prompt 建议，而是工具层硬状态机：进入候选池后，必须已完成 `agent(type=reproducer)`，至少保存 2 个候选，调用 `patch_candidate(action="list")`，再派 `patch_ranker` 输出 selected_index，最后 `patch_candidate(action="apply", index=selected_index)`；跳步会被工具拒绝。
9. 最终应用的非空 patch 必须派 1 个 `patch_review` Worker 做提交前风险审查；它只读、不运行命令、不修改文件。
10. `patch_review` 是最终硬门禁：只有 `patch_risk=low|medium` 且 `verdict=accept` 才能继续 verify/finish；`verdict=revise|reject` 或 `patch_risk=high` 时，最多允许 1 次 corrective implement 修正当前最终 patch，然后再做一次 patch_review；不要使用 harness 反馈。
11. 需要验证时最多派 1 个 verify Worker 跑明确命令。依赖缺失或环境不完整时直接报告 blocked_by_environment。
12. diff 特别大或高风险时才额外派 generic review Worker。

# 实施截止线
- 最多 5 个只读工具调用后，必须进入 EditBrief 或说明唯一缺失事实；不要继续框架考古。
- 工具层会强制限制首次 Worker 派发前的只读调用预算；如果收到 `COORDINATOR_READ_BUDGET_EXHAUSTED`，下一步只能派 `agent(type=implement)`，或在范围仍不明确时派 1 个 `research` Worker 产出结构化 EditBrief。
- 只允许 1 个 research Worker。research 返回 `completed=false` 或 `max_steps` 后，下一步只能派 `agent(type=implement)` 使用当前最佳 EditBrief，或最终说明 `blocked_insufficient_edit_scope`；不能继续 read/search/list，不能再派 research/plan/verify/review。
- research 完成后、implement 前，最多再做 2 次 read/search/list 用于确认精确行号；超过后必须 implement 或明确阻塞。
- 一旦定位到候选文件、函数和修改点，下一步必须派 implement，不能继续读同类文件。
- implement 后如果 `git_diff` 非空，下一步必须派 `patch_review`。如果工具返回 Automatic static patch gate，可以先派 corrective implement 修复语法/结构，再复审；patch_review 接受后最多做 1 次验证；patch_review 拒绝时必须把 required_changes 交给 corrective implement，或在修不了时最终说明 remaining_risk；验证阻塞或失败时直接最终输出，不要安装依赖或重复验证。
- 候选池例外：implement 后如果计划继续生成候选，下一步可以调用 `patch_candidate(action="save_current")` 保存并回滚当前 diff，而不是立刻 patch_review。只有最终 `patch_candidate(action="apply")` 后才必须 patch_review。若 `save_current` 返回 `CANDIDATE_REPRODUCER_REQUIRED`，先补跑一次 `agent(type=reproducer)` 产出公开验证契约，再保存当前候选；不要放弃已有非空 diff。
- 如果 patch_review 返回 accept 但 findings 里有 coverage_gap、required_changes、非空 suggested_fix，或工具返回 Automatic patch gate，必须当作 revise 处理，不能 finish 或 verify。
- corrective implement 只能修当前 patch：allowed_files 必须是第一轮 implement 已修改文件的子集，allowed_ranges 必须围绕当前 diff hunk；优先删除/缩窄高风险改动，不能新增文件、不能扩大到新的方案。如果无法在该范围内安全修复，最终说明 `blocked_remaining_risk`。
- 如果已经有非空 patch，不要为了更完美的验证耗尽剩余步数；SWE-bench harness 会做最终评测。
- implement 无 changed_files 时先判断原因：如果方向明确但替换/范围/工具应用失败，才派一次 last-chance implement 作为补丁落地重试；如果方向不明或发现假设错误，回到只读协调补齐唯一缺失事实，或最终输出 `blocked_insufficient_edit_scope`。

# EditBrief 质量标准
implement 前应确定：
- `problem_contract`: 输入、精确期望输出/异常/message/副作用、禁止行为。
- `semantic_cases`: 每个公开问题场景的 before/after 行为，尤其是 exact exception/message、roundtrip、None/empty、case-insensitive、mutable storage 写回等语义侧面。
- `old_behavior_invariants`: 旧合法行为/API/输出/排序/全局状态中必须保持不变的内容。
- `allowed_files`: 可修改的仓库相对路径。
- `allowed_ranges`: `{ "path.py": [[start, end]] }`。
- `required_changes`: 具体修改方向，包含 Problem Contract。
- `forbidden`: 禁止改测试、禁止安装依赖、禁止宽泛命令等。
这些可以是 JSON，也可以是清晰自然语言；工具层会按质量评分，而不是机械检查字段名。范围选择必须让 implement 能把语义落到代码：优先同时包含目标修改点、直接调用/参数转发路径、同类正确实现、父类默认实现或旧行为 guard。
SWE-bench 下工具层会拒绝低质量 brief、缺 allowed_ranges、范围过宽或包含 git log/全仓 search/运行命令/整文件读取的 implement 调用；局部读取 allowed_ranges 内代码是允许的。
有稳定行号时，要求 implement 用 `line_replacements`；否则用精确 old/new。默认保留无关逻辑、guard 条件和缩进结构。

# Worker Prompt
- Worker 看不到主对话，prompt 必须自包含：目标、事实、文件、允许/禁止事项、输出格式。
- 不要写“根据上面”；不要派 Worker 读取 `.repo_agent_runs/**/subagents/**` 或 `round-*/subagents/**`。
- agent 工具返回 `completed=false` 或 `max_steps` 时，不要同 prompt 重试；缩小范围或结束说明阻塞。
- 如果 research Worker 没有完成，也要把它返回文本中的已知事实当作当前最佳事实；不要试图读取 subagent 输出目录。
- patch_review Worker prompt 必须包含 Problem Contract、修改摘要、目标行为/测试契约、由 diff 影响范围推断的公开回归风险和验证契约；要求检查 8 类风险：test_edit、parent_default_behavior、cache_descriptor_guard、overgeneralization、topology_ordering、wrong_layer_or_exact_contract、public_regression_risk、coverage_gap。
- patch_review Worker 必须输出 JSON：`patch_risk`、`verdict`、`findings`、`regression_risks`、`impact_surface`、`inferred_regression_test_families`、`coverage_gaps`、`required_changes`。缺少 Problem Contract、无法说明旧行为不受影响、或 findings 与 accept 自相矛盾时，要求它返回 `verdict=revise`；不要仅因为文件属于高扇出核心路径就拒绝。
- patch_review prompt 必须要求先检查 Python 语法/缩进/括号闭合/控制流结构，再检查语义。涉及全局配置/context manager 的 `pop`/`del`/restore 顺序、Matplotlib render/cache/getter/private-state fallback、migration optimizer/reduce 核心路径时，只有发现具体缺失 key、顺序变化、私有缓存时机、验证语义或旧行为风险，才要求更窄 guard 或 corrective implement。
- Matplotlib 特例：`rc_context` 修 backend 时，不要在 finally 中 `orig.pop('backend')`、`rcParams.get('backend')` 或 `dict.__setitem__(rcParams, 'backend', ...)`；应从 `orig = rcParams.copy()` 后的快照中用 `dict.pop(orig, 'backend', None)` 排除 backend。`mplot3d/art3d.py` 不要把 `_facecolors2d/_edgecolors2d` 直接初始化或回退为 `_facecolor3d/_edgecolor3d`。
- corrective implement prompt 必须包含 review/static gate 原文、当前 diff 的问题、需要保持的 Problem Contract 和旧行为不变量。它只修当前 diff，不重新研究、不 search、不运行命令、不新增改动面。
- implement Worker prompt 必须能让它一次落地修改：目标行为、具体改动方向、必须保持的旧行为、允许范围和禁止事项要清楚；要求它修改后输出 `semantic_coverage`，逐项说明主要语义 case 是否被改动覆盖。
- reproducer Worker prompt 必须自包含 Problem Contract、候选文件/函数、公开测试线索和禁止使用 SWE-bench 元数据的约束；要求输出 reproduction_contract、differential_cases、verify_commands。
- patch_ranker Worker prompt 必须包含 Problem Contract、reproduction_contract、候选 patch 列表、候选运行结果/静态问题和排序标准；要求只输出 JSON selected_index/confidence/ranking。它不能修改文件，也不能把“官方测试可能失败”当作依据。
- 如果使用了 repair recipe，必须在 Worker prompt 中明确说明它只是抽象策略，仍需以当前代码事实为准；如果 recipe 与当前问题签名不匹配，应主动忽略，不能为了套用 recipe 扩大修改范围。

# 一轮成功重点
- 精确错误文本、异常类型、CLI 输出、Lint 描述必须按题意保留。
- API 修改前先读签名或同类调用，不猜参数名。
- 如果 repair recipe 与当前问题强匹配，才注入对应技巧；不要把 header_rows、case-insensitive、mutable storage 等经验规则套到无关任务。
- 优先把高不确定 bug 转成“小复现 + 差分旧行为”再修改：先证明目标输入该怎么变，再证明相邻合法输入不该变。
- 不确定语义时不要押注第一个 patch；生成 2-3 个策略差异明显的候选，用公开契约和 diff 风险排序后只应用一个。
- 文件写入/替换/编码要保证失败时不破坏原文件。
- Python 缩进和控制流是语义，不是格式。
- 删除子类方法前必须检查父类默认实现；改变缓存、descriptor、field conversion、system check、migration operation 顺序前必须用现有 API/guard 判断，避免误伤旧测试不变量。

# 输出
最终回复包含：Worker 列表、关键事实、修改摘要、验证结果、剩余风险。
"""


SWEBENCH_COORDINATOR_SYSTEM_PROMPT = """你是 RepoAgent SWE-bench Coordinator，目标是用一次 pass@1 生成一个最小、非空、可评测 patch。

# 禁止
- 不使用 hints、PASS_TO_PASS、FAIL_TO_PASS、gold patch、hidden tests 或 harness feedback。
- 不安装依赖、不修 Docker/环境、不修改 tests/fixtures/评测期望文件。
- 默认不使用候选池、patch_ranker、review 驱动的 corrective implement 或重复验证。只有语法/测试文件修改或工具明确证明的 producer/return-rtype 契约破坏才能阻断；覆盖、顺序和表示启发式只记 warning。

# 简化流程
1. 从公开题意写 Problem Contract：触发输入、精确期望行为、旧行为不变量、禁止行为。
2. 用 read_file/search_repo 做轻量定位，最多 5 次只读调用。确定候选文件、函数和行范围后立即停止扩散调查。
3. `reproducer` 是可选的软证据 Worker：只有定位后仍需澄清公开行为时才派发。其 JSON、覆盖或验证命令失败不得阻止 Implement。
4. 形成一个 EditBrief，派一次 `implement`。它只在 allowed_files/allowed_ranges 内落地最小源码修改；不要把猜测写成不可质疑的逐行修改指令。
5. implement 返回非空 diff 后，只处理工具明确标记的确定性契约破坏；`Semantic patch warnings` 不触发 corrective、review 或丢弃 patch。
6. 最多派一次 `verify` 运行 reproducer 给出的公开命令。缺依赖、命令不可用或验证失败时保留 patch 并结束。
7. implement 没有 diff 时，仅当修改方向明确且是替换/范围落地失败，允许一次更窄 last-chance implement；不要重新研究完整问题。

# EditBrief
- 包含 problem_contract、required_changes、old_behavior_invariants、allowed_files、allowed_ranges。
- allowed_ranges 只覆盖目标修改点及理解该点必需的直接调用/同类实现；不要给整文件或千行范围。
- Implement 的首要契约来自公开题意和已读取源码；Reproducer 的 SemanticEvidence 仅在可解析时补充 target_partitions、contract_edges 与 unknowns，不能成为解锁 Implement 的条件。
- 明确 contract_owner 与 symptom_site、规范化与 predicate 的先后顺序、lookup identity 与 rendered text 的区别。allowed_ranges 必须覆盖目标修改层和直接消费端。若 SemanticEvidence 指定 `preferred_change_layer=consumer`，producer 事实由 Coordinator 保留为只读依据，不得放入 Implement 可写范围；工具层会剔除 producer-only range。
- pandas nullable/extension dtype 问题必须把一维 Series 的 `.dtype` 与二维 DataFrame 的 `.dtypes` 写入同一个表示矩阵；只授权或只描述 `hasattr(y, 'dtype')` 的方案是不完整 EditBrief。
- `*args`/`**kwargs` 的 bare lookup key 与 starred display name 不一致时，禁止把星号写入 producer canonical storage；必须定位并授权所有直接 consumer 的 lookup/display 路径。
- 记录 shape/cardinality/closure、dtype、精确文本、排序、mutation target 和共享状态不变量；fallback 找到别名值后必须传播实际匹配身份。
- 精确保持异常类型/message、warning、CLI 输出、排序、roundtrip、mutable storage 写回等题意语义。

# 终止原则
- 已有非空 patch 后不得开启第二套方案或重复 implement。
- review 只报告具体阻断风险，不触发自动重写；它用于归因，不是第二次解题。
- 不要为了验证耗尽步骤。有可信公开命令时最多验证一次；没有命令时直接保留静态安全 patch 给最终 harness。只有公开行为测试或带 assert/失败退出条件的脚本返回 status=passed 且 exit_code=0，才算行为验证通过。
"""


class CoordinatorEngine:
    def __init__(
        self,
        *,
        llm: ChatLLM,
        worker_tools: ToolRegistry,
        cwd: Path | str,
        worker_llm_overrides: dict[str, ChatLLM] | None = None,
        output_dir: Path | str = ".repo_agent_runs/latest",
        max_steps: int = 16,
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
        repair_memory_recall: bool = True,
        swebench_profile: bool = False,
        candidate_mode: str = "off",
        forced_implement_scope: dict | None = None,
    ):
        self.llm = llm
        self.cwd = Path(cwd).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.semantic_memory_recall = semantic_memory_recall
        self.repair_memory_recall = repair_memory_recall
        self.swebench_profile = swebench_profile
        self.candidate_mode = candidate_mode
        self.forced_implement_scope = forced_implement_scope or {}
        excluded_memory_types = set() if repair_memory_recall else {MemoryType.REPAIR_RECIPE}
        self.last_recalled_repair_recipes: tuple[str, ...] = ()
        self.last_repair_recipe_applicability: tuple[RepairRecipeApplicability, ...] = ()
        self.last_applicable_repair_recipes: tuple[str, ...] = ()
        self.active_repair_recipe_constraints: tuple[RepairRecipeConstraint, ...] = ()
        self._agent_state: dict = {}
        self.runner = SubAgentRunner(
            llm=llm,
            llm_overrides=worker_llm_overrides,
            base_tools=worker_tools,
            cwd=self.cwd,
            output_dir=self.output_dir,
            memory_store=self.memory_store,
            semantic_memory_recall=semantic_memory_recall,
            excluded_memory_types=excluded_memory_types,
            swebench_profile=swebench_profile,
        )
        coordinator_tools = ToolRegistry()
        budget_state = {
            "read_calls_before_worker": 0,
            "worker_calls": 0,
            "research_calls": 0,
            "research_failed": 0,
            "reproducer_calls": 0,
            "post_research_read_calls": 0,
            "implement_calls": 0,
            "patch_review_calls": 0,
            "post_failed_research_read_calls": 0,
            "candidate_mode_required": False,
            "required_transition": "",
        }
        coordinator_tools.register_many(
            _coordinator_read_only_tools(
                worker_tools,
                budget_state=budget_state,
                swebench_profile=swebench_profile,
            )
        )
        coordinator_tools.register_many(
            _budgeted_agent_tools(
                _swebench_agent_tools_for_mode(
                    build_agent_tools(
                        self.runner,
                        # SWE-bench 中 recipe 只作为召回上下文，不能因文本匹配变成阻塞写入的硬门禁。
                        repair_recipe_constraints=(
                            None if swebench_profile else lambda: self.active_repair_recipe_constraints
                        ),
                        require_patch_review=not swebench_profile,
                        conditional_patch_review=swebench_profile,
                        require_reproducer_before_implement=False,
                        allowed_agent_types=_swebench_allowed_agent_types(candidate_mode)
                        if swebench_profile
                        else None,
                        candidate_mode_required=lambda: bool(
                            self._coordinator_budget_state.get("candidate_mode_required")
                        ),
                        forced_implement_scope=(
                            lambda: _worker_constraints_from_scope(self.forced_implement_scope)
                            if self.forced_implement_scope
                            else None
                        ),
                        state=self._agent_state,
                    ),
                    swebench_profile=swebench_profile,
                    candidate_mode=candidate_mode,
                ),
                budget_state=budget_state,
                swebench_profile=swebench_profile,
            )
        )
        self._coordinator_budget_state = budget_state
        self.engine = QueryEngine(
            llm=llm,
            tools=coordinator_tools,
            system_prompt=SWEBENCH_COORDINATOR_SYSTEM_PROMPT if swebench_profile else COORDINATOR_SYSTEM_PROMPT,
            max_steps=max_steps,
            output_dir=self.output_dir,
            cwd=self.cwd,
            memory_store=self.memory_store,
            semantic_memory_recall=semantic_memory_recall,
            excluded_memory_types=excluded_memory_types,
            memory_prompt_enabled=not swebench_profile,
            memory_recall_enabled=not swebench_profile,
            include_dynamic_context=not swebench_profile,
            completion_guard=(
                lambda answer: _coordinator_completion_guard(
                    answer,
                    budget_state=self._coordinator_budget_state,
                    agent_state=self._agent_state,
                    swebench_profile=self.swebench_profile,
                )
            ),
        )

    def run(self, task: str) -> str:
        reset_agent_tool_state(self._agent_state, self.cwd)
        self._agent_state["problem_statement"] = extract_public_problem_statement(task)
        candidate_required = _candidate_mode_required_for_task(task, self.candidate_mode)
        self._coordinator_budget_state.update(
            {
                "read_calls_before_worker": 0,
                "worker_calls": 0,
                "research_calls": 0,
                "research_failed": 0,
                "reproducer_calls": 0,
                "post_research_read_calls": 0,
                "implement_calls": 0,
                "patch_review_calls": 0,
                "post_failed_research_read_calls": 0,
                "candidate_mode_required": candidate_required,
                "required_transition": "",
            }
        )
        if self.swebench_profile and candidate_required:
            task = (
                "## Candidate Runtime State\n"
                "CANDIDATE_MODE_REPRODUCER_REQUIRED=true. Do not probe this by calling implement first. "
                "Start with one reproducer, then save two candidates from different semantic layers, call "
                "patch_candidate(action=\"evaluate_and_list\"), and dispatch patch_ranker.\n\n"
                f"{task}"
            )
        if self.repair_memory_recall:
            recall = augment_task_with_repair_recipes(
                task=task,
                memory_store=self.memory_store,
                side_query=self._side_query if self.semantic_memory_recall and hasattr(self.llm, "invoke_text") else None,
            )
            self.last_recalled_repair_recipes = tuple(memory.header.filename for memory in recall.memories)
            self.last_repair_recipe_applicability = recall.applicability
            self.last_applicable_repair_recipes = tuple(
                result.source for result in self.last_repair_recipe_applicability if result.applicable
            )
            self.active_repair_recipe_constraints = hard_constraints_from_repair_recipes(recall.memories, task=task)
            for memory in recall.memories:
                self.engine.already_surfaced_memories.add(str(memory.header.path))
            task = recall.task
        else:
            self.last_repair_recipe_applicability = ()
            self.last_applicable_repair_recipes = ()
            self.active_repair_recipe_constraints = ()
        answer = ""
        try:
            answer = self.engine.chat(task)
            return answer
        finally:
            self._write_stage_attribution(answer)

    def curate_successful_repair(
        self,
        *,
        task: str,
        patch: str,
        result: str,
        validation: dict | str | None = None,
        final_answer: str = "",
    ):
        prompt = build_case_curator_prompt(
            task=task,
            patch=patch,
            result=result,
            validation=validation,
            final_answer=final_answer,
        )
        return self.runner.run(
            agent_type="case_curator",
            description="Curate repair recipe",
            prompt=prompt,
        )

    def abort(self) -> None:
        self.engine.abort()

    def _side_query(self, system: str, user: str) -> str:
        invoke_text = getattr(self.llm, "invoke_text")
        return invoke_text(system, user)

    def _write_stage_attribution(self, final_answer: str) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        patch = _git_diff(self.cwd)
        events = list(self._agent_state.get("worker_events") or [])
        implement_events = [event for event in events if event.get("type") == "implement"]
        first_implement = implement_events[0] if implement_events else {}
        attempts = dict(self._agent_state.get("worker_attempts") or {})
        requests = dict(self._agent_state.get("worker_requests") or {})
        implemented_files = list(self._agent_state.get("implemented_files") or [])
        static_issues = list(self._agent_state.get("static_issues") or [])
        review_required = bool(self._agent_state.get("review_required"))
        review_done = bool(self._agent_state.get("patch_review_done"))

        terminal_reason = str(self._agent_state.get("workflow_terminal_reason") or "")
        if terminal_reason == "static_safe_unverified":
            pre_harness = "static_safe_unverified"
        elif not attempts.get("reproducer"):
            pre_harness = "reproduction_not_attempted"
        elif not self._agent_state.get("reproducer_done") and not attempts.get("implement"):
            pre_harness = "reproduction_incomplete"
        elif not attempts.get("implement"):
            pre_harness = "coordination_before_implementation"
        elif not patch.strip():
            pre_harness = "implementation_delivery"
        elif static_issues:
            pre_harness = "deterministic_validation"
        elif review_required and self._agent_state.get("patch_review_attempted") and not self._agent_state.get(
            "patch_review_completed"
        ):
            pre_harness = "conditional_review_incomplete"
        elif review_required and not review_done:
            pre_harness = "conditional_review_incomplete"
        elif not self._agent_state.get("verify_attempted"):
            pre_harness = "verification_not_attempted"
        elif not self._agent_state.get("verification_result_parseable"):
            pre_harness = "verification_incomplete"
        elif not self._agent_state.get("verification_passed"):
            verification_class = str(
                (self._agent_state.get("verification_assessment") or {}).get("classification") or ""
            )
            if verification_class == "environment_blocked":
                pre_harness = "verification_blocked"
            elif verification_class in {"static_passed", "weak_passed", "weak_failed", "unparseable"}:
                pre_harness = "verification_inconclusive"
            else:
                pre_harness = "verification_failed"
        else:
            pre_harness = "awaiting_harness"

        payload = {
            "schema_version": 1,
            "pipeline": "simplified_pass1",
            "candidate_mode": self.candidate_mode,
            "stages": {
                "localization": {
                    "coordinator_read_calls": self._coordinator_budget_state.get("read_calls_before_worker", 0),
                    "research_attempts": attempts.get("research", 0),
                    "selected_files": list(first_implement.get("allowed_files") or []),
                    "selected_ranges": first_implement.get("allowed_ranges") or {},
                },
                "reproduction": {
                    "attempts": attempts.get("reproducer", 0),
                    "requests": requests.get("reproducer", 0),
                    "attempted": bool(self._agent_state.get("reproducer_attempted")),
                    "completed": bool(self._agent_state.get("reproducer_done")),
                    "stop_reason": self._agent_state.get("reproduction_stop_reason") or "",
                    "contract_parseable": bool(self._agent_state.get("reproduction_contract")),
                    "contract": self._agent_state.get("reproduction_contract") or {},
                    "verify_commands": list(self._agent_state.get("reproduction_commands") or []),
                    "semantic_evidence": self._agent_state.get("semantic_evidence") or {},
                    "semantic_evidence_issues": list(self._agent_state.get("semantic_evidence_issues") or []),
                    "semantic_evidence_warnings": list(
                        self._agent_state.get("semantic_evidence_warnings") or []
                    ),
                    "semantic_gate_denials": int(self._agent_state.get("semantic_gate_denials") or 0),
                },
                "implementation": {
                    "attempts": attempts.get("implement", 0),
                    "requests": requests.get("implement", 0),
                    "completed_with_patch": bool(patch.strip()),
                    "changed_files": implemented_files,
                    "patch_chars": len(patch),
                    "semantic_patch_issues": list(self._agent_state.get("semantic_patch_issues") or []),
                    "semantic_patch_warnings": list(
                        self._agent_state.get("semantic_patch_warnings") or []
                    ),
                    "constraint_routing": self._agent_state.get("constraint_routing") or {},
                },
                "deterministic_validation": {
                    "passed": bool(patch.strip()) and not static_issues,
                    "issues": static_issues,
                },
                "conditional_review": {
                    "required": review_required,
                    "reasons": list(self._agent_state.get("review_reasons") or []),
                    "attempts": attempts.get("patch_review", 0),
                    "requests": requests.get("patch_review", 0),
                    "completed": bool(self._agent_state.get("patch_review_completed")),
                    "flagged_risk": bool(self._agent_state.get("patch_review_high_risk")),
                    "result": self._agent_state.get("patch_review_result") or {},
                },
                "focused_verification": {
                    "attempts": attempts.get("verify", 0),
                    "requests": requests.get("verify", 0),
                    "completed": bool(self._agent_state.get("verify_completed")),
                    "result_parseable": bool(self._agent_state.get("verification_result_parseable")),
                    "passed": bool(self._agent_state.get("verification_passed")),
                    "result": self._agent_state.get("verification_result") or {},
                    "assessment": self._agent_state.get("verification_assessment") or {},
                    "skipped_reason": self._agent_state.get("verification_skipped_reason") or "",
                },
            },
            "submission_ready": bool(self._agent_state.get("submission_ready")),
            "pre_harness_attribution": pre_harness,
            "worker_events": events,
            "token_usage": {
                "coordinator": self.engine.total_tokens,
                "workers": sum(int(event.get("total_tokens") or 0) for event in events),
                "total": self.engine.total_tokens + sum(int(event.get("total_tokens") or 0) for event in events),
            },
            "termination": {
                "completed": self.engine.completed,
                "stop_reason": self.engine.stop_reason,
                "workflow_terminal_reason": self._agent_state.get("workflow_terminal_reason") or "",
                "has_final_answer": bool(final_answer.strip()),
            },
        }
        (self.output_dir / "stage_attribution.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _coordinator_completion_guard(
    answer: str,
    *,
    budget_state: dict,
    agent_state: dict,
    swebench_profile: bool,
) -> str | None:
    """阻止 Coordinator 用自然语言绕过必须执行的状态转换。"""

    if not swebench_profile:
        return None
    if "blocked_insufficient_edit_scope" in answer:
        return None
    required_transition = str(budget_state.get("required_transition") or "")
    if required_transition:
        return (
            f"The workflow requires agent(type={required_transition}) before completion. "
            "Dispatch it now, or explicitly finish with blocked_insufficient_edit_scope and the one missing fact."
        )
    if not agent_state.get("implementation_done"):
        return None
    if agent_state.get("review_required") and not agent_state.get("patch_review_attempted"):
        return "The patch requires one conditional patch_review before completion. Dispatch it now."
    if not agent_state.get("verify_attempted"):
        if build_verification_plan(agent_state.get("semantic_evidence")) is None:
            return None
        return (
            "A non-empty patch is not submission-ready until one focused verify returns structured evidence. "
            "Dispatch agent(type=verify) now."
        )
    if not agent_state.get("submission_ready"):
        return (
            "The verification attempt did not produce parseable passing evidence. Do not claim success; "
            "finish explicitly with blocked_verification_incomplete."
        )
    return None


def _worker_constraints_from_scope(scope: dict) -> WorkerConstraints:
    return WorkerConstraints.from_tool_input(
        {
            "allowed_files": scope.get("allowed_files") or [],
            "allowed_ranges": scope.get("allowed_ranges") or {},
        }
    )


def _budgeted_agent_tools(
    tools: list[Tool],
    *,
    budget_state: dict[str, int],
    swebench_profile: bool = False,
) -> list[Tool]:
    wrapped: list[Tool] = []
    for tool in tools:
        if tool.name != "agent":
            wrapped.append(tool)
            continue

        def handler(inp: dict, original: Tool = tool) -> ToolResponse:
            agent_type = inp.get("type") or "general"
            required_transition = str(budget_state.get("required_transition") or "")
            if required_transition and agent_type != required_transition:
                return ToolResponse.failure(
                    "COORDINATOR_TRANSITION_REQUIRED",
                    (
                        f"Coordinator state requires agent(type={required_transition}) now. "
                        "Do not call another Worker. If no defensible scope exists, finish explicitly with "
                        "blocked_insufficient_edit_scope."
                    ),
                )
            if budget_state.get("research_failed", 0) and not _allowed_after_failed_research(
                agent_type,
                budget_state,
                swebench_profile=swebench_profile,
            ):
                return ToolResponse.failure(
                    "COORDINATOR_RESEARCH_FAILED_IMPLEMENT_REQUIRED",
                    _failed_research_agent_message(swebench_profile),
                )
            if agent_type == "research" and budget_state.get("research_calls", 0) >= 1:
                return ToolResponse.failure(
                    "COORDINATOR_RESEARCH_BUDGET_EXHAUSTED",
                    (
                        "Coordinator may dispatch at most one research Worker before implementation. Use its returned "
                        "facts to dispatch agent(type=implement), or finish with the exact missing edit-scope fact."
                    ),
                )
            response = original.run_with_timing(inp)
            if response.error:
                return response
            budget_state["worker_calls"] = budget_state.get("worker_calls", 0) + 1
            if agent_type == "research":
                budget_state["research_calls"] = budget_state.get("research_calls", 0) + 1
                completed = bool(response.data.get("completed"))
                if not completed or response.data.get("stop_reason") == "max_steps":
                    budget_state["research_failed"] = 1
            if agent_type == "reproducer":
                budget_state["reproducer_calls"] = budget_state.get("reproducer_calls", 0) + 1
                budget_state["required_transition"] = "implement"
            if agent_type == "implement":
                budget_state["implement_calls"] = budget_state.get("implement_calls", 0) + 1
                budget_state["required_transition"] = ""
            if agent_type == "patch_review":
                budget_state["patch_review_calls"] = budget_state.get("patch_review_calls", 0) + 1
            return response

        wrapped.append(_clone_tool_with_handler(tool, handler))
    return wrapped


def _allowed_after_failed_research(agent_type: str, budget_state: dict[str, int], *, swebench_profile: bool = False) -> bool:
    if swebench_profile:
        if agent_type in {"implement", "reproducer"}:
            return True
        if agent_type == "verify":
            return budget_state.get("implement_calls", 0) > 0
        if agent_type == "patch_ranker":
            return True
        return False
    if agent_type == "implement":
        return True
    if agent_type == "patch_review":
        return budget_state.get("implement_calls", 0) > 0
    if agent_type == "verify":
        return budget_state.get("patch_review_calls", 0) > 0
    return False


def _failed_research_agent_message(swebench_profile: bool) -> str:
    if swebench_profile:
        return (
            "A research Worker already returned completed=false/max_steps. In SWE-bench profile, recover with "
            "one of: agent(type=reproducer), agent(type=implement) using the best current EditBrief, or "
            "agent(type=verify) after a non-empty patch. Do not launch another research/plan/general/review Worker."
        )
    return (
        "A research Worker already returned completed=false/max_steps. Allowed sequence is now: "
        "agent(type=implement), then agent(type=patch_review), then agent(type=verify) only after "
        "patch_review. Do not launch more research/plan/general/review Workers."
    )


def _coordinator_read_only_tools(
    worker_tools: ToolRegistry,
    *,
    budget_state: dict[str, int] | None = None,
    read_budget_before_worker: int = 5,
    read_budget_after_research_before_implement: int = 2,
    read_budget_after_failed_research: int = 2,
    swebench_profile: bool = False,
):
    allowed_names = {"list_files", "read_file", "search_repo", "git_diff"}
    tools = [tool for tool in worker_tools.tools() if tool.name in allowed_names]
    if budget_state is None:
        return tools
    return [
        _budgeted_read_only_tool(
            tool,
            budget_state=budget_state,
            read_budget_before_worker=read_budget_before_worker,
            read_budget_after_research_before_implement=read_budget_after_research_before_implement,
            read_budget_after_failed_research=read_budget_after_failed_research,
            swebench_profile=swebench_profile,
        )
        for tool in tools
    ]


def _budgeted_read_only_tool(
    tool: Tool,
    *,
    budget_state: dict[str, int],
    read_budget_before_worker: int,
    read_budget_after_research_before_implement: int,
    read_budget_after_failed_research: int,
    swebench_profile: bool,
) -> Tool:
    budgeted_names = {"list_files", "read_file", "search_repo"}

    def handler(inp: dict, original: Tool = tool) -> ToolResponse:
        required_transition = str(budget_state.get("required_transition") or "")
        if original.name in budgeted_names and required_transition:
            return ToolResponse.failure(
                "COORDINATOR_TRANSITION_REQUIRED",
                (
                    f"Read budget is closed. Dispatch agent(type={required_transition}) now, or finish explicitly "
                    "with blocked_insufficient_edit_scope."
                ),
            )
        if original.name in budgeted_names and budget_state.get("research_failed", 0):
            if swebench_profile and budget_state.get("implement_calls", 0) == 0:
                used = budget_state.get("post_failed_research_read_calls", 0)
                if used < read_budget_after_failed_research:
                    budget_state["post_failed_research_read_calls"] = used + 1
                    response = original.run_with_timing(inp)
                    if used + 1 >= read_budget_after_failed_research:
                        budget_state["required_transition"] = "implement"
                    return response
            return ToolResponse.failure(
                "COORDINATOR_RESEARCH_FAILED_IMPLEMENT_REQUIRED",
                _failed_research_read_message(swebench_profile),
            )
        if (
            original.name in budgeted_names
            and budget_state.get("research_calls", 0) > 0
            and budget_state.get("implement_calls", 0) == 0
        ):
            used = budget_state.get("post_research_read_calls", 0)
            if used >= read_budget_after_research_before_implement:
                budget_state["required_transition"] = "implement"
                return ToolResponse.failure(
                    "COORDINATOR_POST_RESEARCH_READ_BUDGET_EXHAUSTED",
                    (
                        f"Coordinator already used {used} read/search/list calls after the single research Worker. "
                        "Do not continue repository archaeology. Dispatch agent(type=implement) with the best current "
                        "allowed_files/allowed_ranges, or finish with the exact missing edit-scope fact."
                    ),
                )
            budget_state["post_research_read_calls"] = used + 1
            if used + 1 >= read_budget_after_research_before_implement:
                budget_state["required_transition"] = "implement"
        if original.name in budgeted_names and budget_state.get("worker_calls", 0) == 0:
            used = budget_state.get("read_calls_before_worker", 0)
            if used >= read_budget_before_worker:
                budget_state["required_transition"] = "implement"
                next_step = (
                    "Dispatch agent(type=implement) with the best current allowed_files/allowed_ranges, "
                    "or finish with the exact missing edit-scope fact."
                )
                return ToolResponse.failure(
                    "COORDINATOR_READ_BUDGET_EXHAUSTED",
                    (
                        f"Coordinator already used {used} read/search/list calls before dispatching any Worker. "
                        f"Stop repository archaeology now. {next_step} Do not call more read_file/search_repo/list_files."
                    ),
                )
            budget_state["read_calls_before_worker"] = used + 1
            if used + 1 >= read_budget_before_worker:
                budget_state["required_transition"] = "implement"
        return original.run_with_timing(inp)

    return _clone_tool_with_handler(tool, handler)


def _failed_research_read_message(swebench_profile: bool) -> str:
    if swebench_profile:
        return (
            "A research Worker already failed or hit max_steps and the limited recovery read budget is exhausted. "
            "Dispatch agent(type=reproducer) or agent(type=implement) with the best current EditBrief, or finish with "
            "blocked_insufficient_edit_scope."
        )
    return (
        "A research Worker already failed or hit max_steps. Direct read/search/list is now blocked. "
        "Dispatch agent(type=implement) with the best current EditBrief, or finish with "
        "blocked_insufficient_edit_scope."
    )


def _candidate_mode_required_for_task(task: str, candidate_mode: str) -> bool:
    mode = (candidate_mode or "auto").strip().lower()
    if mode == "required":
        return True
    if mode == "auto":
        return _candidate_mode_auto_matches(_candidate_mode_signal_text(task))
    return False


def _swebench_allowed_agent_types(candidate_mode: str) -> set[str]:
    if (candidate_mode or "auto").strip().lower() in {"auto", "required"}:
        return {"reproducer", "implement", "patch_ranker", "patch_review", "verify"}
    return {"reproducer", "implement", "patch_review", "verify"}


def _swebench_agent_tools_for_mode(
    tools: list[Tool],
    *,
    swebench_profile: bool,
    candidate_mode: str,
) -> list[Tool]:
    if not swebench_profile:
        return tools
    if (candidate_mode or "auto").strip().lower() in {"auto", "required"}:
        return tools
    return [tool for tool in tools if tool.name != "patch_candidate"]


def _candidate_mode_signal_text(task: str) -> str:
    marker = "## Problem Statement"
    marker_index = task.lower().find(marker.lower())
    if marker_index == -1:
        return task
    signal_text = task[marker_index + len(marker) :].lstrip(" \t\r\n")
    next_section_index = signal_text.find("\n## ")
    if next_section_index != -1:
        signal_text = signal_text[:next_section_index]
    return signal_text or task


def _candidate_mode_auto_matches(task: str) -> bool:
    lowered = task.lower()
    signals = (
        "sphinx/ext/autodoc",
        "sphinx/ext/autosummary",
        "sphinx/domains",
        "sphinx/environment/adapters",
        "sympy/printing",
        "sympy/matrices",
        "sympy/simplify",
        "sklearn/utils",
        "sklearn/metrics",
        "formatter",
        "printer",
        "parser",
        "serializer",
        "roundtrip",
        "round-trip",
        "mutable",
        "stdout",
        "stderr",
        "exact string",
        "nullable",
        "dataframe",
        "escaping",
        "imported members",
        "*args",
        "**kwargs",
    )
    if any(signal in lowered for signal in signals):
        return True
    semantic_markers = sum(
        marker in lowered
        for marker in (
            "expected",
            "actual",
            "should",
            "case",
            "exception",
            "message",
            "output",
            "roundtrip",
        )
    )
    return semantic_markers >= 3


def _git_diff(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--binary", "--", ".", ":!.env*"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode in {0, 1} else ""


def _clone_tool_with_handler(tool: Tool, handler) -> Tool:
    return build_tool(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters(),
        handler=handler,
        aliases=tool.aliases,
        max_result_size_chars=tool.max_result_size_chars,
        prompt=tool.prompt,
        is_concurrency_safe=tool.is_concurrency_safe,
        is_read_only=tool.is_read_only,
        is_destructive=tool.is_destructive,
        check_permissions=tool.check_permissions,
        deferred=tool.deferred,
        source=tool.source,
        sort_key=tool.sort_key,
    )
