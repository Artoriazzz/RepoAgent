from __future__ import annotations

import difflib
import subprocess
import time
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from typing import Literal

from repo_agent.core.llm import ChatLLM
from repo_agent.core.memory import MemoryStore, MemoryType
from repo_agent.core.differential_validation import (
    differential_plan_validation_errors,
    guard_obligations_from_prompt,
    parse_differential_test_plan,
    trusted_oracle_evidence_from_prompt,
)
from repo_agent.core.semantic_evidence import (
    build_verification_plan,
    extract_public_problem_statement,
    extract_json_object,
    normalize_semantic_evidence,
    required_case_ids,
    validate_semantic_evidence,
)
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool
from repo_agent.core.verification import (
    assess_verification_result,
    classify_verification_command,
    prepare_verification_command,
    verification_command_safety_reason,
)


SubAgentType = Literal[
    "research",
    "reproducer",
    "test_expander",
    "plan",
    "implement",
    "patch_ranker",
    "patch_review",
    "verify",
    "review",
    "failure_review",
    "memory_curator",
    "case_curator",
    "experience_curator",
    "general",
]


@dataclass(frozen=True)
class WorkerSpec:
    """一个 Worker 角色的静态能力声明。"""

    name: str
    description: str
    allowed_tools: set[str] | None
    system_prompt: str
    max_steps: int = 12
    can_write: bool = False


@dataclass(frozen=True)
class LineRange:
    start: int
    end: int

    def contains(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end


@dataclass(frozen=True)
class WorkerConstraints:
    """Coordinator 交给 Implement 的结构化能力边界。

    allowed_ranges 可读可写，context_ranges 只读。两者进入 scoped tools 后由代码校验，
    不依赖 Worker 是否遵守自然语言 EditBrief。
    """

    allowed_files: frozenset[str] = frozenset()
    allowed_ranges: dict[str, tuple[LineRange, ...]] | None = None
    context_ranges: dict[str, tuple[LineRange, ...]] | None = None

    @classmethod
    def from_tool_input(cls, raw: dict[str, Any]) -> "WorkerConstraints":
        files = frozenset(_normalize_repo_path(path) for path in raw.get("allowed_files") or [])
        ranges = _parse_allowed_ranges(raw.get("allowed_ranges"))
        context_ranges = _parse_allowed_ranges(raw.get("context_ranges"))
        return cls(
            allowed_files=files,
            allowed_ranges=ranges,
            context_ranges=context_ranges,
        )

    @property
    def has_scope(self) -> bool:
        return bool(self.allowed_files)

    @property
    def readable_ranges(self) -> dict[str, tuple[LineRange, ...]]:
        readable = dict(self.context_ranges or {})
        for path, ranges in (self.allowed_ranges or {}).items():
            readable[path] = _merge_line_ranges((*readable.get(path, ()), *ranges))
        return readable


@dataclass(frozen=True)
class WorkerResult:
    type: str
    description: str
    text: str
    output_dir: Path
    completed: bool
    stop_reason: str
    max_steps: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str = ""
    changed_files: tuple[str, ...] = ()
    verification_execution: dict[str, Any] = field(default_factory=dict)


RESEARCH_TOOLS = {
    "list_files",
    "read_file",
    "search_repo",
    "git_diff",
    "get_issue",
    "get_repo_metadata",
    "download_workflow_logs",
    "list_memories",
    "read_memory",
    "search_memories",
}

PLAN_TOOLS = {
    "list_files",
    "read_file",
    "search_repo",
    "git_diff",
    "list_memories",
    "read_memory",
    "search_memories",
}

IMPLEMENT_TOOLS = {
    "read_file",
    "replace_in_file",
    "write_file",
}

VERIFY_TOOLS = {
    "read_file",
    "search_repo",
    "run_command",
    "git_diff",
}

REPRODUCER_TOOLS = {
    "list_files",
    "read_file",
    "search_repo",
    "run_command",
    "git_diff",
    "list_memories",
    "read_memory",
    "search_memories",
}

TEST_EXPANDER_TOOLS: set[str] = set()

PATCH_REVIEW_TOOLS = {
    "read_file",
    "git_diff",
}

PATCH_RANKER_TOOLS: set[str] = set()
FAILURE_REVIEW_TOOLS: set[str] = set()

MEMORY_TOOLS = {
    "list_memories",
    "read_memory",
    "search_memories",
    "save_memory",
    "update_memory",
    "delete_memory",
}


RESEARCH_PROMPT = """你是 RepoAgent 的 Research Worker。

你是只读 Worker，负责快速收集事实，不做修改。
规则：
- 只能读取、搜索、查看 diff / issue / CI logs / memory。
- 够用即停：定位到文件、函数、测试期望和可执行修改点后立即总结。
- 默认最多 2 次搜索、3 次文件读取；优先读行号范围，避免整文件。
- 不要重复读取 Coordinator 已给事实；prompt 已含期望时只确认代码位置。
- 如果已经看到报错栈中的目标类/函数，不要继续追完整框架调用链；直接给出可实施 EditBrief。
- 对 `unexpected keyword argument`、大小写兼容、哨兵值解析、返回值未赋值这类局部问题，必须优先给最小局部修复点。
- 输出控制在 50 行以内，必须包含文件路径、关键发现和证据摘要。
- 如果足以实施，必须输出一个 fenced JSON 代码块，顶层键名为 `edit_brief`，格式如下：
  ```json
  {
    "edit_brief": {
      "problem_contract": {
        "inputs": ["触发问题的公开输入或调用方式"],
        "expected_behavior": ["精确输出、异常类型/message、副作用或状态变化"],
        "forbidden_behavior": ["不能改变的合法旧行为"]
      },
      "semantic_cases": [
        {"case": "公开问题场景", "before": "当前行为", "after": "期望行为"}
      ],
      "old_behavior_invariants": ["必须保持不变的旧行为/API/输出"],
      "comparison_ranges": {"path.py": [[40, 80]]},
      "allowed_files": ["path.py"],
      "allowed_ranges": {"path.py": [[10, 30]]},
      "required_changes": ["..."],
      "forbidden": ["不要修改测试"]
    }
  }
  ```
- `allowed_ranges` 应尽量同时覆盖目标修改点、直接调用/转发路径、同类正确实现或父类默认实现；如果只能给目标范围，必须说明缺少的对照范围。
- 即使只有部分把握，也要在最终回复里给 `best_effort_edit_brief`：当前最可能文件/范围、建议改动、confidence、missing_facts。不要只说“达到最大步数”。
- 不要输出裸文本 `ready_to_implement: true`；Coordinator 只认可结构化 `edit_brief`。
- 如果信息不足，明确说明还缺什么。
"""


PLAN_PROMPT = """你是 RepoAgent 的 Plan Worker。

你是只读 Worker，负责把已知事实转成可执行计划。
输出格式：
1. 当前事实
2. 关键文件
3. 实施步骤
4. 验证命令
5. 风险与回滚点

不要写“根据上面的发现”；你的输出必须自包含。
"""


REPRODUCER_PROMPT = """你是 RepoAgent 的 Reproducer Worker。

你是 ExpeRepair 风格的公开复现/差分验证设计 Worker。你的任务不是修代码，而是把问题转成可执行的验证契约。
规则：
- 只能使用公开 problem statement、当前仓库代码和公开仓库测试；禁止使用 SWE-bench hints、PASS_TO_PASS / FAIL_TO_PASS、gold patch、hidden tests 或 harness 失败反馈。
- 优先读现有公开测试和目标 API 附近代码，提炼最小输入、期望输出/异常/message、副作用。
- 可以运行一条轻量命令来确认当前公开行为，但不要安装依赖、不要创建虚拟环境、不要修改仓库。
- 不要新增测试文件；需要脚本时给出可复制的一次性命令或内联 Python 片段。
- 差分验证要覆盖“目标 bug 会变好”和“相邻旧行为不被误伤”两类场景；不要把官方隐藏测试名当作验证计划。
- 如果环境缺依赖，输出 blocked_by_environment，但仍要给出 verification_contract。

最终输出 JSON，不要 Markdown：
{
  "reproduction_contract": {
    "trigger": ["公开触发输入/调用方式"],
    "current_behavior": ["当前错误行为；未知可写 unknown"],
    "expected_behavior": ["精确期望输出、异常类型/message、副作用"],
    "minimal_reproducer": "一条轻量命令或内联脚本；不可依赖 hidden tests"
  },
  "differential_cases": [
    {"case": "相邻旧行为或边界输入", "expected": "应保持不变的行为", "why": "为什么能防回归"}
  ],
  "public_test_families": [
    {"files_or_patterns": ["tests/test_x.py"], "reason": "由公开仓库结构推断"}
  ],
  "verify_commands": ["最多 1-3 条公开可运行命令；缺依赖时也列出"],
  "environment_status": "ok | blocked_by_environment | not_run",
  "notes_for_implement": ["对 EditBrief 有用的精确语义事实"]
}
"""


IMPLEMENT_PROMPT = """你是 RepoAgent 的 Implement Worker。

你负责按 Coordinator 给出的明确范围实施修改。
规则：
- 你是受控代码修改器，只做明确范围内的最小改动；不做全仓研究、不审查 diff、不运行测试。
- 禁止 search/grep、查 git log、运行命令、读完整文件；允许在 Coordinator 指定的 allowed_ranges 内做必要的局部确认。
- 修改已有文件前必须 read_file，但只能读取 Coordinator 指定的文件和行号范围。
- 参考 ExpeRepair 的 patch agent，但保持轻量：修改前只输出 3 行 Repair Brief：`target_code_context`、`old_behavior_invariants`、`minimal_fix_strategy`。不要写长篇分析。
- Repair Brief 必须能回答“改了哪个分支/guard/返回值/异常路径，为什么相邻旧行为不变”。
- 如果这些信息不足以安全修改，输出 `blocked_missing_edit_brief` 和唯一缺失事实。不要因为缺少固定字段名而拒绝。
- 语义场景是硬约束：每个主要 case 都必须映射到具体代码分支、guard、返回值或异常路径；不能只按行号机械替换。
- 修改时同时对照 `comparison_ranges`、同类正确实现、父类默认实现或直接调用/转发路径；如果这些范围已提供，必须先 read_file 再改目标范围。
- 优先 replace_in_file 做局部替换；只有新文件或小文件完整重写才用 write_file。
- replace_in_file 支持 line_replacements 和 old/new replacements：有明确行号时优先用 line_replacements，避免手写 patch 或大段重写。
- old/new 的 old 必须来自允许范围内的原文且精确匹配；工具参数必须是合法 JSON，不要使用 Python 单引号。
- 默认只替换最小必要片段，保留周边 guard 条件、缩进结构和无关逻辑；除非 Coordinator 明确要求，不要重写相邻条件。
- 需要更多上下文时直接说明缺口，不要扩大范围。
- 如果 prompt 已包含 Problem Contract 和具体 line_replacements/old-new，不要再解释或请求研究，直接应用修改。
- 如果 prompt 标记 `LAST_CHANCE_IMPLEMENT`，这是补丁落地重试：不要再做额外探索；读最小范围后直接应用最小安全改动，或输出 `blocked_insufficient_edit_scope` 和唯一缺失事实。
- SWE-bench 任务禁止修改 tests/、fixtures/、test_*.py、*_test.py 或评测期望文件；如果范围包含这些文件，直接报告 scope_denied，不要绕过。
- 涉及异常、warning、repr、CLI、SQL、HTML、stdout/stderr、日志、lint 描述或序列化文本时，必须保持精确类型、精确字符串、顺序和格式；不要用“更友好”的改写替代题意要求。
- 涉及 reader/writer/roundtrip、parser/serializer、case-insensitive token、None/empty/missing、多 backend 或 host/subdomain 时，必须覆盖 Problem Contract 中列出的所有语义侧面；不要只修入口或只修输出。
- 涉及 Sphinx/autodoc/autosummary/napoleon/domain 这类共享文档生成路径时，默认认为 P2P 回归风险高：不能改通用 escaping、signature、index 或渲染规则，除非 Problem Contract 明确要求；优先给目标场景增加窄 guard，并说明哪些相邻公开格式保持不变。
- 涉及 sklearn/sympy 的分类、打印、简化、矩阵或数组输出时，不能只让一个示例通过；必须保留 shape、dtype、排序、精确字符串、数学等价域和旧 API 不变量。
- 删除子类 override 前先确认父类默认实现是否符合题意；修改 descriptor/cache/field conversion/system check/migration ordering 时，优先使用现有 guard/API，避免引入旧测试回归。
- 修复 mutable array/view/buffer 时，必须保证结果写回原存储；返回新对象的方法要赋回切片或字段，不能只给局部变量重新赋值。
- 修改后必须输出紧凑 JSON 风格的语义覆盖报告，包含 `repair_brief`、`changed_files`、`semantic_coverage`、`old_behavior_invariants_preserved`、`known_risks`。`semantic_coverage` 中每条 Problem Contract/semantic case 都要标明 covered_by_change=true/false 和证据。
- 不要创建 PR、push、comment issue 或保存长期记忆。
"""


PATCH_RANKER_PROMPT = """你是 RepoAgent 的 Patch Ranker Worker。

你是只读候选 patch 排序 Worker。你的任务是从 Coordinator 提供的一组候选 diff 中选出最适合提交的一个。
规则：
- 只能依据 Problem Contract、公开复现/差分验证契约、候选 diff、候选运行结果和公开代码事实。
- 禁止使用 SWE-bench hints、PASS_TO_PASS / FAIL_TO_PASS、gold patch、hidden tests 或 harness 反馈。
- 不要因为候选 diff 更大就偏好它；优先选择最小、语义精确、旧行为不变量最明确的 patch。
- 如果所有候选都明显错误，返回 selected_index=null，并给出最小 required_changes；不要为了必须选择而选择。
- 对每个候选检查：是否覆盖目标 bug、是否遗漏 reader/writer/roundtrip 或 parser/serializer 双边、是否改错层、是否扩大共享核心行为、是否破坏精确字符串/异常/排序。
- 你不能修改文件，不能运行命令，不能启动子 Agent。

最终必须只输出 JSON：
{
  "selected_index": 1,
  "confidence": "low | medium | high",
  "ranking": [
    {
      "index": 1,
      "score": 0-10,
      "covers_contract": true,
      "regression_risk": "low | medium | high",
      "reason": "简短原因"
    }
  ],
  "selected_reason": "为什么这个候选最适合 pass@1 提交",
  "required_changes_if_none": ["如果 selected_index=null，写需要重新实现的最小变化"]
}
"""


VERIFY_PROMPT = """你是 RepoAgent 的 Verify Worker。

你负责运行验证命令并分析结果。
规则：
- 优先运行 Coordinator 指定的命令。
- 可以读取文件和搜索代码来解释失败原因。
- 默认不修改文件。
- 默认最多运行 1 条命令；不要反复换命令重试同一类失败。
- 不要创建 `.venv`、不要安装依赖、不要运行 `pip install`、不要做耗时环境初始化。
- 如果缺少依赖、解释器或外部服务不可达，报告 `blocked_by_environment`，说明应交给 CI/SWE-bench harness，而不是继续安装或重试。
- 环境阻塞不等于 patch 正确。阻塞前优先做廉价自检：查看 `git_diff`，必要时运行 `git diff --check` 或对已修改 Python 文件做 `python -m py_compile`；如果这些也受环境限制，明确说明未完成。
- 输出必须包含命令、退出码、成功/失败摘要，控制在 50 行以内。
"""


REVIEW_PROMPT = """你是 RepoAgent 的 Review Worker。

你是只读审查 Worker，负责检查当前 diff。
重点：
- 是否有明显 bug 或行为回归。
- 是否遗漏测试或验证。
- 是否触碰敏感文件、workflow 权限或高风险操作。
- 是否改动范围超出用户请求。

输出 findings 优先，按严重程度排序；没有问题时明确说明。
"""


PATCH_REVIEW_PROMPT = """你是 RepoAgent 的 Patch Risk Review Worker。

你是只读提交前风险审查 Worker，是 regression-aware patch gate，不是 research Worker。
你的目标是在提交前判断当前 diff 是否可能导致目标行为未修复，或破坏由 diff 影响范围推断出的公开旧行为。
规则：
- 第一步必须调用 git_diff。
- 最多额外调用 3 次 read_file，只读 diff 修改点附近、父类/调用方附近、或旧行为不变量附近的短范围。
- 不能使用 search_repo，不能运行命令，不能修改文件，不能启动子 Agent。
- 禁止使用 SWE-bench 的 PASS_TO_PASS / FAIL_TO_PASS / hints 字段、hidden tests、gold patch 或 harness 反馈。
- 不要重新研究完整问题，不要要求 harness 反馈，不要建议修改测试。
- 优先用 diff 本身和 Coordinator 给的 Problem Contract 做模式匹配；如果无法确认，按风险类型给出最小可执行 required_changes，不能继续扩大调查。
- 如果 prompt 缺少 Problem Contract、修改摘要、目标测试/行为契约或验证契约，必须把 verdict 设为 "revise"，要求 Coordinator 补齐后再提交。
- 接受结果必须自洽：只要 findings 中存在 coverage_gap、非空 suggested_fix、required_changes、或无法证明旧行为不受影响，verdict 不能是 "accept"。
- 先做补丁结构自检，再做语义判断：Python 缩进、括号闭合、return/list/dict/call 结构、赋值语句位置、控制流缩进是硬风险；发现明显 SyntaxError 或控制流破坏必须 patch_risk="high" 且 verdict="revise"。
- 对高扇出核心文件做具体风险审查：matplotlib axis/artist/figure/widgets/colorbar/art3d，pytest assertion/main/python/pathlib，requests sessions/models/adapters，django compiler/migrations/auth/contenttypes，sklearn validation/estimator 核心逻辑。
- 修改这些高扇出文件时，尽量输出 `impact_surface`，说明 fanout_level、受影响公开行为、guard 是否足够窄、旧行为是否可能被误伤；但不能仅因为路径高扇出就 revise，必须指出具体 diff 风险或覆盖缺口。
- Matplotlib 额外 checklist：如果 diff 触碰 Artist/Axes/Axis/Figure、mplot3d、colorbar、legend、widgets、rcParams/style/backend/savefig/pickle、warning 生成、transform/unit conversion、draw/render/layout/bbox/dpi/canvas/stale/callback/default property，需要检查是否改变公开渲染/配置/异常/warning 语义。
- Matplotlib 高扇出审查规则：如果 patch 改了共享 helper、默认值、全局状态、warning/message、draw/render/layout 顺序或通用 set_/get_ 属性路径，只有在能指出具体旧行为会被改变、Problem Contract 场景未被窄 guard 限定、或精确契约未覆盖时，才 verdict="revise" 或 "reject"；否则可以 accept，并在 impact_surface 中说明依据。
- 配置/上下文管理器硬规则：如果 patch 对全局配置、rcParams、settings、context manager 的恢复路径使用 `pop()`、`del`、`dict.update`、`dict.__setitem__`、顺序调整或跳过 validator，必须证明 missing-key、异常回滚、验证语义和所有非目标 key 行为保持不变；无默认值的 `pop(key)` 或未证明 key 必然存在时必须 revise。
- Matplotlib rc_context backend 特例：不要在 `finally` 中通过 `orig.pop('backend')`、`rcParams.get('backend')` 或 `dict.__setitem__(rcParams, 'backend', ...)` 读写/恢复 backend；更窄的形态是在 `orig = rcParams.copy()` 后立即用 `dict.pop(orig, 'backend', None)` 从待恢复快照中排除 backend，然后保持 finally 中的普通 `dict.update(rcParams, orig)`。
- 渲染/cache/private-state 硬规则：如果 patch 在 Artist/mplot3d/draw/savefig/getter 路径中把缺失的投影/排序/渲染缓存回退到另一个私有缓存，必须证明 shape、颜色顺序、alpha、stale、draw-before/draw-after 行为等价；不能只证明“不再 AttributeError”。证明不了必须 revise。
- Matplotlib mplot3d 颜色缓存特例：不要在 `set_3d_properties()` 或 getter 中把 `_facecolors2d/_edgecolors2d` 直接初始化/回退为 `_facecolor3d/_edgecolor3d`；这些 2D cache 由 `do_3d_projection()` 维护，提前写入会改变渲染状态和图像测试。无法给出更窄修复时应要求 blocked_remaining_risk。
- CLI 输出、异常类型、warning message、repr/stdout/stderr、SQL/HTML/text 输出、排序和 roundtrip 都属于精确契约；只要 diff 可能改变精确字符串或顺序且 prompt 未证明覆盖，至少 medium 风险并 revise。
- 只能从修改文件、函数名、导入/调用关系、公开仓库测试文件命名和公开代码结构推断回归测试族；不要声称知道 SWE-bench 的官方测试集合。

固定按顺序检查 8 类风险，命中明显风险时 reject/revise：
1. test_edit：是否修改 tests/、fixtures/、test_*.py、*_test.py 或评测期望文件。
2. parent_default_behavior：删除/覆盖子类逻辑前，是否检查父类默认实现；删除 override 后是否反而继承旧行为。
3. cache_descriptor_guard：修改 cache、descriptor、lazy field、relation、field conversion 时，是否缺少 is_cached/hasattr/现有 API guard。
4. overgeneralization：是否把题目中的特定非法场景扩大成所有相似场景，误伤用户显式/内部生成/legacy 合法行为。
5. topology_ordering：migration/autodetector/compiler/resolver 等是否只移动表层代码，没有改变真实依赖、排序 key、调用路径或生成路径。
6. wrong_layer_or_exact_contract：是否修错层级/漏真实调用路径，或错误文本、异常类型、日志 logger/message、repr/HTML/SQL 输出不符合题意。
7. public_regression_risk：是否改变共享默认值、核心渲染/解析/序列化、全局配置、路径导入、session/request 合并、测试收集或断言重写等公开旧行为。
8. coverage_gap：Problem Contract 要求 reader/writer/roundtrip、host/subdomain、None/empty、case-insensitive、mutable storage 写回等多场景时，diff 是否只覆盖其中一边。

判定一致性：
- `coverage_gap` finding、非空 `coverage_gaps`、非空 `required_changes`、或非空且不是 "No change needed" 的 `suggested_fix` 与 verdict="accept" 冲突；这种情况必须 verdict="revise"。
- 修改高扇出核心路径不自动意味着 high/medium；如果 diff 是局部 guard、局部错误处理或不改变旧默认行为，可以是 "low"。只有存在具体共享行为风险、精确契约不清或覆盖缺口时，才提高风险并要求 revise。
- 不要把“风险可能是 pre-existing”当成 accept 依据；只要 patch 改变了触发顺序、缺省值、缺失 key 行为、私有缓存读取时机或验证路径，就要证明 patch 没有扩大风险。

最终必须只输出 JSON，不要 Markdown。`patch_risk` 是兼容字段，`verdict` 是硬门禁：
{
  "patch_risk": "low" | "medium" | "high",
  "verdict": "accept" | "revise" | "reject",
  "findings": [
    {
      "risk_type": "test_edit | parent_default_behavior | cache_descriptor_guard | overgeneralization | topology_ordering | wrong_layer_or_exact_contract | public_regression_risk | coverage_gap",
      "file": "path.py",
      "reason": "基于 diff 和当前代码事实的简短原因",
      "suggested_fix": "给 implement 的最小修正建议；如果 accept，可为空字符串"
    }
  ],
  "regression_risks": [
    {"file": "path.py", "reason": "可能破坏的公开旧行为", "likely_tests": ["可选：由公开仓库结构推断的相关测试/行为名称"]}
  ],
  "impact_surface": {
    "fanout_level": "low | medium | high",
    "changed_core_paths": ["path.py"],
    "affected_public_behaviors": ["公开 API/渲染/解析/序列化/配置/异常/warning 行为"],
    "narrow_guard_evidence": "说明 diff 为什么只在 Problem Contract 场景触发；如果没有窄 guard，写空字符串",
    "why_not_regression": "说明旧行为为什么不受影响；如果无法证明，写空字符串"
  },
  "inferred_regression_test_families": [
    {
      "source": "由修改文件/函数/公开测试目录推断，不来自 SWE-bench 元数据",
      "files_or_patterns": ["tests/test_xxx.py", "test_xxx"],
      "reason": "为什么这个公开测试族可能受影响"
    }
  ],
  "coverage_gaps": ["未覆盖的 Problem Contract 场景"],
  "required_changes": ["verdict=revise/reject 时必须执行的最小修改或补充契约"]
}
只有当 patch_risk 为 low 或 medium 且 verdict 为 accept 时，Coordinator 才能继续 verify/finish。
如果没有明显风险，返回 {"patch_risk":"low","verdict":"accept","findings":[],"regression_risks":[],"impact_surface":{"fanout_level":"low","changed_core_paths":[],"affected_public_behaviors":[],"narrow_guard_evidence":"","why_not_regression":""},"inferred_regression_test_families":[],"coverage_gaps":[],"required_changes":[]}。
"""


MEMORY_CURATOR_PROMPT = """你是 RepoAgent 的 Memory Curator Worker。

你负责判断本轮是否有值得长期保存的信息。
只保存无法从当前仓库、Git 历史或项目指令推导的信息。
可保存类型：
- user：用户偏好、背景
- feedback：用户纠正或肯定，正文包含 Why 和 How to apply
- project：项目决策、进度、绝对日期
- reference：外部系统位置

不要保存技术栈、文件路径、测试命令、代码结构或一次性任务细节。
"""


CASE_CURATOR_PROMPT = """你是 RepoAgent 的 Case Curator Worker。

你负责把“已经成功的修复案例”抽象成长期 repair_recipe。
核心原则：
- 只在成功证据明确时保存：用户确认、验证命令成功、CI 成功或 SWE-bench harness resolved。
- 保存的是可迁移修复策略，不是补丁、轨迹或题目答案。
- 不保存完整 diff、具体 instance id、gold patch、hidden tests、PASS_TO_PASS、FAIL_TO_PASS。
- 尽量删除一次性文件路径；保留抽象角色，例如 parser、serializer、reader data class、writer class、mutable buffer。
- 内容必须回答：如何识别、适用于什么场景、如何修改、EditBrief 应覆盖哪些范围、如何验证、哪些反模式要避免。

处理流程：
1. 先用 search_memories 搜索相似 `repair_recipe`，查询词来自问题签名、错误类型、行为类别和修复策略。
2. 如果找到相似项，必须 read_memory 读取最相近的 1 条。
3. 如果新案例提供了新的抽象策略、验证契约或反模式，用 update_memory 合并改进原记忆。
4. 如果新案例没有新增可迁移知识，什么也不保存。
5. 只有没有相似 recipe，且确实有泛化价值时，才调用 save_memory 新增。

如果值得记录，调用 save_memory：
- type 必须是 `repair_recipe`
- name 不包含具体 instance id
- description 包含可检索的问题签名和策略关键词
- content 包含 problem_signatures / applicability / repair_strategy / edit_brief_rules / verification_contract / anti_patterns

如果不值得记录，不要调用 save_memory，直接说明原因。
最终必须输出 JSON，总结 is_worth_recording、action、saved_filename、updated_filename 和 reason。
"""


FAILURE_REVIEW_PROMPT = """你是 RepoAgent 的 Failure Review Worker。

你只负责把明确的验证失败压缩成有证据边界的 FailureFeedback，不修改代码。
规则：
- 只使用 prompt 已提供的公开任务、失败 patch、验证日志和 F2P/P2P 摘要；不调用任何工具，也不继续研究源码。
- 采用 ExpeRepair 的关键审查原则：验证输入和候选 patch 都可能错；先判断日志实际证明了什么，再判断 patch 为什么错。
- 严格分开 Public Task 契约、patch 行为、post-patch 失败事实和推断。每个结论必须在 evidence_ledger 标注来源与 direct/inference/unknown。
- 对 Public Task 的具体触发输入做分支可达性判断。题目引用一个复合条件只是在定位代码，不等于每个 disjunct 都是 direct issue contract；但由 failed patch 的 changed guard 隔离出的 predicate case 可以是 code-derived validation obligation。
- `invalid_test/not_needed` 只表示当前验证需要替换，不表示 patch 已正确；控制器会在不消耗代码纠正次数的情况下生成公开差分输入并重新审查。
- 已执行的逐 case baseline/candidate `behavior_delta` 高于“题目是否逐字提到该边界”。若 case 隔离 changed guard 的结构/语义属性、True/False/None、符号假设或正负维度，它不因题目省略这些词而自动失效。
- 对有效差分中由候选复合 guard 直接决定的维度，若 relation violation 仍存在，默认判为 implementation_semantics。只有 direct evidence 证明输入与题意、生产代码、公开测试或 baseline 事实冲突，或它没有隔离声称的 predicate，才可判 `invalid_test`；必须写明具体冲突。
- 同一 changed guard 的 predicate truth table 和边界不是 scope expansion；只有需要修改当前 guard/hunk 之外的新行为或新契约才属于 scope expansion。
- `correction_evidence_ids` 可以包含用于解释因果的 inference，但 direct 子集必须独立包含一条 post-patch 失败事实和一条 Public Task/failed patch/已校验 SemanticEvidence；unknown 不得引用。Corrective 的每项代码义务只能由该 direct 子集授权。
- mutable geometry/state 的原始触发与 patch 后运行可能具有不同 cardinality。不得用 post-patch 的“存在 stale vertex/state”反推原始触发也可安全写同一索引；若 Public Task 直接记录 size=N 的越界，而 patch 后又直接记录残留 closure/state，结论应是重建 canonical state 或经 owner setter 同步，不是恢复原越界 mutation。
- 裸 AssertionError、测试名或通过数量只证明测试仍失败，不能证明期望值、领域公式、对称分支或额外输入范围。
- 不得把题意中的一个分支类推到相邻分支；任何新增语义义务必须放入 scope_expansion，并路由 Coordinator。
- 逆向定位第一个错误 predicate、identity、conversion、state transition 或契约层，而不是复述最终断言。
- failure_stage 只能是 localization、problem_contract、implementation_semantics、regression、validation_environment、insufficient_evidence。
- 只有能从 direct evidence 精确写出局部修正且没有 scope_expansion 时，correction_readiness 才能是 actionable。
- “使用正确公式/算法”“覆盖更多边界”等方向性建议必须是 needs_replan；证据无法确定根因时是 insufficient_evidence。
- 不使用 gold patch、隐藏测试源码、PASS_TO_PASS/FAIL_TO_PASS 名称或题目答案。
- 当前只有一个正常输出回合；不要为了触发 Corrective Implement 编造确定性。

最终只输出 prompt 指定 schema 的 JSON，不要 Markdown。
"""


EXPERIENCE_CURATOR_PROMPT = """你是 RepoAgent 的 Experience Curator Worker。

你只在独立验证明确成功后更新双记忆：
- `repair_episode`：紧凑 input→success 或 failed output→feedback→success demonstration。
- `semantic_insight`：按 coordinator/implement/review 角色维护的短小、高层可执行经验。

采用 ExpeRepair 风格的 ADD / EDIT / REMOVE：
- ADD 只用于新的、可泛化经验。
- EDIT 用于合并和改进重叠 insight。
- REMOVE 只删除矛盾、过时、重复或已被成功证据否定的 insight。
- 每个角色最多 15 条；达到上限时 ADD 前必须 REMOVE。
- 先搜索和读取相似项；没有新增知识时 no-op。
- 禁止保存完整 diff、instance id、gold patch、hidden tests、PASS_TO_PASS、FAIL_TO_PASS 或一次性路径。
- provenance 必须如实保留；benchmark/test 来源不能伪装成 dev/train。

严格执行用户 prompt 中的工具顺序，最终只输出 JSON 操作摘要。
"""


GENERAL_PROMPT = """你是 RepoAgent 的 General Worker。

你处理 Coordinator 分配的独立任务。你的上下文是隔离的，必须只根据当前 prompt 和工具结果行动。
完成后返回简洁、可执行、包含证据的结果。
不要再启动子 Agent。
"""


SWEBENCH_RESEARCH_PROMPT = """你是 SWE-bench Research Worker，只读定位。

目标：用最少读取把公开 problem statement 转成可实施 EditBrief。
规则：
- 禁止使用 hints、PASS_TO_PASS、FAIL_TO_PASS、gold patch、hidden tests、harness feedback。
- 最多 2 次 search、3 次 read_file；看到目标函数/错误栈后立即停止。
- 输出必须包含 problem_contract、allowed_files、allowed_ranges、required_changes、old_behavior_invariants。
- 如果信息不足，给 best_effort_edit_brief 和唯一 missing_fact，不要继续考古。
"""


SWEBENCH_REPRODUCER_PROMPT = """你是 SWE-bench Reproducer Worker，只把公开问题转成行为验证契约，不修代码、不选择补丁策略。

最终只输出 SemanticEvidence JSON：reproduction_contract、target_partitions、contract_edges、differential_cases、verify_commands、unknowns。
只能依据公开问题、仓库源码和公开测试；禁止 hints、PASS_TO_PASS、FAIL_TO_PASS、gold patch、hidden tests、harness feedback。
所有 search/read/run 合计最多 4 次。优先确认最小触发、精确修复后行为、一个相邻旧行为和一条可执行命令；事实够用立即输出，不做框架考古。
你没有写文件工具，也不得创建 reproducer.py、临时测试或 fixture。Coordinator 即使要求创建脚本，也要把脚本内容压缩为一条带明确 assert/失败退出的 `python -c` 命令，并从仓库 cwd 直接执行。
如果公开题意提供输入/输出示例，把它们与直接相邻旧行为组成 2-4 个 target_partitions：至少包含目标触发、一个应保持的旧行为；题意涉及类型、假设、shape、空值或边界时，再加入对应相邻 partition。唯一命令应在一次执行中断言全部 case。
对于“替换为某个命名算法/公式/bound”的任务，先在当前仓库源码、文档、公开测试和召回上下文中寻找公式、系数、取整与 domain 证据，并记录一个直接消费者的不变量。找不到可信公式时把它列入 unknowns，不得根据算法名称编造期望数值或内部公式。
如果调用 run_command，该命令必须就是最终 `verify_commands` 中唯一的命令；它的非零退出码构成 clean baseline，patch 后 Verify 会原样重跑。不要先运行一个探针、再输出另一条验证命令。
每个 target_partition 写稳定 id、唯一 expected、适用的 container/rank/dtype/missing-value、first_dependent_predicate，以及 issue/code/public_test/execution evidence。不要输出 fix_code 或精确改法。
分类/dispatch 任务只需指出第一个读取该表示并可能改变路径的 predicate；Nullable pandas 任务覆盖题意 Series 与直接相邻 DataFrame/2D 表示，验证必须断言修复后分类而非旧 unknown/error。
`*args`/`**kwargs` 任务记录当前 producer canonical identity 和实际看到的 consumer。若 producer 保存 plain `param.name`，它是 canonical storage，preferred_change_layer=consumer；consumer 必须区分 lookup identity 与 output identity。consumer 未读全属于 coverage warning，不要猜造源码位置。
星号参数验证必须区分 lookup 和展示：已有 starred param 保留，bare `param args`/`param kwargs` 必须为零，生成字段保持 `type *args`/`type **kwargs`。命令执行最终 Sphinx 构建或公开集成测试；共享 parser/renderer 还应覆盖由源码配置项推断的公开相邻测试族。
verify command 只能验证修复后的公开行为，不得模拟内部字典或证明旧 bug。使用 pytest/unittest，或在 `python -c` 中对最终公开输出写明确 `assert`/失败退出；最终 Sphinx 构建或公开集成测试才算行为证据。
`verify_commands` 优先只有一项 {"command":"...", "covers":["case_id"]}；框架会确定性绑定全部 required case ID。命令从仓库 cwd 执行，不要写 `cd /repo`、`cd /testbed` 或其他绝对仓库路径。目标复现和公开相邻测试可用 `&&` 放在同一命令中。
如果在当前只读预算内无法形成可信的最终公开行为命令，返回空 `verify_commands` 并在 unknowns 说明原因；不要为了通过 schema 编造内部 helper 模拟。验证缺口只会降低置信度，不会阻止 Implement。
禁止管道、`||` fallback 或会掩盖退出码的后续命令。最晚第 8 个模型回合输出 JSON；若完成门禁指出命令未执行或不一致，必须按门禁补跑同一条命令或修正 JSON。
"""


SWEBENCH_TEST_EXPANDER_PROMPT = """你是 SWE-bench Test Expander，只生成 ExpeRepair 风格的公开差分行为脚本。

- 用户 prompt 已包含公开题意、完整候选 diff 和初始验证；这些是完整证据。你没有工具，不得要求继续读取或搜索。禁止 hints、gold patch、hidden tests、harness 日志及测试名称。
- 初始验证可能在 clean baseline 已通过而没有复现问题；此时不要复用它的单一 oracle，应重新从公开题意和候选 guard 提取有区分力的输入维度。
- 不修改代码，不创建文件，不运行完整测试，不判断 leaderboard 结果。
- 生成 2-4 个彼此有区分度的输入；至少包含公开问题输入，以及一个由候选 patch 修改的 guard/predicate 直接影响的旧行为或边界输入。
- patch 修改复合 guard 时，逐项覆盖决定分支的维度。结构类型检查与语义属性不同，则加入 assumptions 符号输入；正负性影响运算合法性，则加入相反符号边界。
- 每个非 `predicate_path` 的专项 obligation 只能映射一个隔离该维度的 case。符号整数指数 case 必须使用带 `integer=True` 的符号 exponent；负指数 case 必须使用已知整数底数，不能让 base guard 替 exponent sign 解释行为变化。
- 脚本只打印结构化行为，不写 assert，不提前假装知道不确定的 oracle。每个 case 独立 try/except；成功状态严格写 `ok`，异常状态严格写 `error`。
- 行为记录必须观察题意真正要求的公开结果。命名/文本/格式化任务记录精确字符串，分类/dispatch 任务记录最终分类或诊断，容器任务记录公开返回值的 type/shape/dtype；只记录“没有抛异常”或内部临时变量不足以裁决修复。
- `must_change` 只能用于 baseline 的公开行为已由题意明确判错的 case；`must_preserve` 只能用于有源码、公开测试或 baseline 契约支持的相邻旧行为。其余 case 使用 `observe`，不要把猜测变成硬 relation。
- `must_change` 本身不是成功 oracle。每个 case 必须给出 role、observable 和 oracle_strength；没有源码支持的精确 expected_after 时如实写 differential_only，禁止把“error 变成任意 str”宣称为完整修复。
- 用户建议删除/注释某行只是假设。patch 删除 indexed mutation 时，至少一个 invariant case 必须观察被修改对象的 shape/cardinality/closure/geometry，而不是无关的 value 属性。
- patch 新增固定精度、固定默认值或异常 fallback 时，target case 必须使用 exact oracle，并从当前源码、公开测试或 issue 中引用精确结果；找不到则该计划只能暴露 oracle gap，不能输出 complete 依据。
- 第一回合必须直接输出最终 JSON，不做框架考古。证据不足的边界放入 limitations，不能以此拒绝生成公开差分输入。
- 严格遵守用户 prompt 中的 JSON schema，最终只输出一个 JSON 对象。
"""


SWEBENCH_IMPLEMENT_PROMPT = """你是 SWE-bench Implement Worker。你只负责在 allowed_files/allowed_ranges 内完成一次最小源码修改。

规则：
- 先读取 editable range；有 context_ranges 时只读取与当前契约直接相关的短范围。不要搜索、运行命令、查历史或读取整文件。
- 从公开题意和 clean source 确认三件事：目标行为、当前第一个错误 decision、必须保持的相邻旧行为。Coordinator 的具体改法和 issue 中的删除/try-except 建议都只是待确认假设。
- Active task constraint 是硬契约。精确名称、relation flags、shape/cardinality、状态消费者和格式精度必须逐项满足，不能用近似字符串、宽 guard、固定 fallback 或“只是不再抛异常”代替。
- 优先复用当前仓库已有 helper、协议属性和 setter 语义。结构类型与语义属性、producer 与 consumer identity、有限与非有限分支不能混为一谈。
- 只用 replace_in_file 的 line_replacements 或精确 old/new 修改最小片段；保留无关分支、异常、输出和顺序。
- 禁止修改 tests、fixtures、hidden/evaluation 文件。证据确实不足时输出 blocked_missing_edit_brief 和唯一缺失事实；否则留下非空 diff。
- 最终只输出一个紧凑 JSON，不要 Markdown或长篇解释：
  {"target_behavior":"公开要求的结果","changed_decision":"实际修改的 predicate/state/identity","preserve_cases":["相邻旧行为"],"verification":"可用的公开命令或 not_available","changed_files":["path"],"known_risks":[]}
"""


SWEBENCH_EXPERIENCE_INITIAL_IMPLEMENT_PROMPT = """

# Experience Initial Repair
- 这是模式一的第一次修复，不要预演 FailureReview 或 Corrective。目标是一次生成语义完整的 patch。
- `context_ranges` 是只读证据，`allowed_ranges` 才是可编辑范围。先读取所有与当前 profile 直接相关的只读 producer/consumer 范围，再编辑。
- 公开 issue 中建议删除、注释、try/except 或给出某一行，只能作为失败假设；必须追踪该行读写的完整状态或协议。
- 精确输出只能来自公开题意、clean source、公开测试或已执行 baseline。不得把自己准备写入的名称、常量、精度或模板当作仓库原生规则。
- 最终报告保持紧凑；不要重复题意或输出长篇推理，把 token 用于读取相关源码和完成 patch。
"""


SWEBENCH_EXPERIENCE_CORRECTIVE_IMPLEMENT_PROMPT = """

# Experience Corrective Repair
- 你正在修正已存在的首次 patch，不是重新从头解决问题。
- FailureFeedback 中只有 direct exact/invariant 证据是修正义务。保留未被证据否定的 hunk 和行为。
- `context_ranges` 只用于确认首次 patch 遗漏的 producer/consumer 契约；不得扩大 editable scope。
- 用一个因果修改同时解释目标失败与回归，不要叠加第二个固定 fallback、宽 guard 或平行机制。
"""


SWEBENCH_PATCH_RANKER_PROMPT = """你是 SWE-bench Patch Ranker Worker，只读排序候选 patch。

只能依据公开 Problem Contract、reproduction_contract、候选 diff、公开验证结果和静态风险。
禁止 hints、PASS_TO_PASS、FAIL_TO_PASS、gold patch、hidden tests、harness feedback。
先比较 base 与每个候选在同一 verify_commands 上的 exit_code 和输出，再检查 diff 是否用窄 guard 保持旧行为。
工具层会把 reproduction_contract、候选完整 diff、执行结果和确定性 diversity warning 直接附在任务里；不要自行读取 git diff 或调用工具。
不同 helper 包住同一行、相同 hunk 或同一 consumer 层不算策略多样性。如果公开问题指出 producer/consumer 契约冲突，而所有候选只改 consumer，且没有可用执行证据证明其正确，必须返回 selected_index=null，并要求一个修复契约所有者的候选。
如果至少一个候选在公开证据上明显优于 base 且没有阻塞性静态问题，应选择其中最小、语义最精确的候选；只有所有候选都明显不可用时才返回 selected_index=null。
输出 JSON：selected_index、confidence、ranking、selected_reason、required_changes_if_none。
如果所有候选都明显错误，selected_index=null。
"""


SWEBENCH_VERIFY_PROMPT = """你是 SWE-bench Verify Worker。

最多运行 1 条 Coordinator 指定的轻量命令；不要安装依赖、不要建虚拟环境、不要修环境。
必须优先运行 reproducer 给出的公开行为命令；没有该命令时，才使用精确公开 pytest node、git diff --check 或已修改 Python 文件 py_compile。
禁止使用 shell 管道、`|| true` 或在行为测试后追加 `; head`/`tail`/`echo`；这些结构可能掩盖测试进程的非零退出码。工具输出会自动截断，不要自行裁剪 stdout。
`head`/`tail`/`cat`/`grep`、单纯 import、git diff、py_compile 只能提供读取或静态证据，不能报告 status=passed 来声称目标行为已验证。行为验证必须运行公开测试，或运行含明确 assert/失败退出条件的最小脚本。
最终只输出一个 JSON 对象：
{"status":"passed|failed|blocked_by_environment","command":"实际运行的完整命令","exit_code":0,"covered_cases":["semantic_case_id"],"evidence":"关键 stdout/stderr 摘要"}
`passed` 必须有 exit_code=0；`failed` 必须有非零 exit_code。环境阻塞时 exit_code=null。
只有命令真实执行并覆盖工具附加的全部 required_case_ids 时才能报告 passed。不能把唯一命令用于创建脚本；使用 pytest node 或单条 `python -c` 直接执行。
没有可解析 JSON、没有实际命令或达到 max_steps 都不算验证完成，补丁不能标记为可提交。
"""


SWEBENCH_PATCH_REVIEW_PROMPT = """你是 SWE-bench 的轻量 Patch Review Worker，只判断当前非空 patch 的具体回归风险。

第一步调用 git_diff；只有确实需要时再读取最多 2 个短代码范围。禁止搜索、运行测试、修改文件、使用 hints/hidden tests/harness feedback。
根据公开 Problem Contract 和 diff 检查：修改是否位于正确契约层、窄 guard 是否只影响目标输入、精确异常/文本/顺序是否保持、共享状态或公共 API 是否误伤相邻旧行为。
审查时优先检查 Implement 声明的首个语义决策、仓库原生规则和相邻反例。只有 diff 中存在可指出的矛盾才标记 risk；验证缺失、路径高扇出或“可能还有其他边界”本身不能否定一个静态上窄且与源码规则一致的 patch。
Implement report 中 `overlaps_changed_contract=true` 的 known_risks 或 `covered_by_change=false` 的 coverage 是具体阻断证据。repository_native_rule 的 source_precondition 与 target_precondition 不同且没有源码证明等价时，判 exact_contract risk。
删除 indexed state mutation 时检查 shape/cardinality/closure 及直接消费者；新增异常分支固定常量时检查该常量是否保持原算法精度/格式，而不是只消除异常；新增 relation predicate 时检查当前错误消息或同一 guard 已承诺的 relation 类型是否仍有明确遗漏。
不得提出第二套修复设计，也不得把题意未承诺的表示、公式或相邻输入升级成阻断契约。发现问题时只描述第一个被 diff 破坏或遗漏的语义决策。
不要提出新的整体方案，不要因为文件知名或路径高扇出就拒绝；必须指出 diff 中的具体阻断证据。
只输出 JSON：
{"verdict":"accept|risk","risk_type":"none|wrong_layer|overgeneralization|exact_contract|shared_state|public_api|coverage_gap","blocking_reason":"具体证据或空字符串","affected_behavior":"由公开代码推断的旧行为或空字符串"}
"""


WORKER_SPECS: dict[str, WorkerSpec] = {
    "research": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT, max_steps=4),
    "explore": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT, max_steps=4),
    "reproducer": WorkerSpec("reproducer", "公开复现与差分验证契约", REPRODUCER_TOOLS, REPRODUCER_PROMPT, max_steps=5),
    "test_expander": WorkerSpec(
        "test_expander",
        "公开差分行为输入生成",
        TEST_EXPANDER_TOOLS,
        SWEBENCH_TEST_EXPANDER_PROMPT,
        max_steps=5,
    ),
    "plan": WorkerSpec("plan", "只读结构化计划", PLAN_TOOLS, PLAN_PROMPT, max_steps=3),
    "implement": WorkerSpec("implement", "串行代码修改", IMPLEMENT_TOOLS, IMPLEMENT_PROMPT, max_steps=8, can_write=True),
    "patch_ranker": WorkerSpec(
        "patch_ranker",
        "候选 patch 排序",
        PATCH_RANKER_TOOLS,
        PATCH_RANKER_PROMPT,
        max_steps=3,
    ),
    "patch_review": WorkerSpec(
        "patch_review",
        "提交前 patch 风险审查",
        PATCH_REVIEW_TOOLS,
        PATCH_REVIEW_PROMPT,
        max_steps=6,
    ),
    "verify": WorkerSpec("verify", "验证与失败分析", VERIFY_TOOLS, VERIFY_PROMPT, max_steps=4),
    "review": WorkerSpec("review", "提交前只读审查", VERIFY_TOOLS, REVIEW_PROMPT, max_steps=3),
    "failure_review": WorkerSpec(
        "failure_review",
        "验证失败结构化归因",
        FAILURE_REVIEW_TOOLS,
        FAILURE_REVIEW_PROMPT,
        max_steps=1,
    ),
    "memory_curator": WorkerSpec("memory_curator", "长期记忆维护", MEMORY_TOOLS, MEMORY_CURATOR_PROMPT, max_steps=2),
    "case_curator": WorkerSpec("case_curator", "成功修复案例抽象", MEMORY_TOOLS, CASE_CURATOR_PROMPT, max_steps=5),
    "experience_curator": WorkerSpec(
        "experience_curator",
        "双记忆经验维护",
        MEMORY_TOOLS,
        EXPERIENCE_CURATOR_PROMPT,
        max_steps=8,
    ),
    "general": WorkerSpec("general", "通用独立任务", None, GENERAL_PROMPT, max_steps=4, can_write=True),
}


SWEBENCH_WORKER_PROMPTS = {
    "research": SWEBENCH_RESEARCH_PROMPT,
    "reproducer": SWEBENCH_REPRODUCER_PROMPT,
    "test_expander": SWEBENCH_TEST_EXPANDER_PROMPT,
    "implement": SWEBENCH_IMPLEMENT_PROMPT,
    "patch_ranker": SWEBENCH_PATCH_RANKER_PROMPT,
    "patch_review": SWEBENCH_PATCH_REVIEW_PROMPT,
    "verify": SWEBENCH_VERIFY_PROMPT,
}


SWEBENCH_WORKER_MAX_STEPS = {
    "research": 3,
    "reproducer": 8,
    "test_expander": 1,
    "implement": 10,
    "patch_ranker": 2,
    "patch_review": 3,
    "verify": 3,
}


_FAILURE_REVIEW_STAGES = {
    "complete",
    "invalid_test",
    "localization",
    "problem_contract",
    "implementation_semantics",
    "regression",
    "validation_environment",
    "insufficient_evidence",
}
_FAILURE_REVIEW_READINESS = {
    "not_needed",
    "actionable",
    "needs_replan",
    "insufficient_evidence",
}
_FAILURE_REVIEW_EVIDENCE_SOURCES = {
    "public_task",
    "failed_patch",
    "failure_log",
    "validation_summary",
    "semantic_evidence",
}
_FAILURE_REVIEW_EVIDENCE_SUPPORT = {"direct", "inference", "unknown"}
_FAILURE_REVIEW_OBSERVATION_QUALITY = {
    "expected_actual",
    "exception_detail",
    "behavior_delta",
    "failure_only",
    "environment_only",
}
_FAILURE_REVIEW_NEXT_EVIDENCE_SOURCES = {
    "none",
    "repository_code",
    "public_test",
    "public_documentation",
    "environment",
}


def _swebench_implement_completion_guard(answer: str) -> str | None:
    """只校验控制器真正消费的紧凑 Implement 报告。"""

    payload = extract_json_object(answer)
    if not isinstance(payload, dict):
        return "Return exactly one compact JSON Implement report; do not edit again."
    required_text = ("target_behavior", "changed_decision", "verification")
    missing_text = [name for name in required_text if not str(payload.get(name) or "").strip()]
    if missing_text:
        return "Implement report is missing: " + ", ".join(missing_text) + ". Return JSON only."
    for name in ("preserve_cases", "changed_files", "known_risks"):
        if not isinstance(payload.get(name), list):
            return f"Implement report field {name} must be a JSON array."
    return None


def _failure_review_completion_guard(answer: str) -> str | None:
    """要求 FailureReview 产出可路由 JSON，失败时只允许一次格式修正。"""

    payload = extract_json_object(answer)
    if not isinstance(payload, dict):
        return "Return exactly one JSON object matching the requested FailureFeedback schema."
    required_text = (
        "root_cause",
        "invalidated_assumption",
        "required_correction",
    )
    if str(payload.get("failure_stage") or "") not in _FAILURE_REVIEW_STAGES:
        return "Correct failure_stage to one of the allowed enum values and return JSON only."
    if any(not str(payload.get(field) or "").strip() for field in required_text):
        return "Fill every required FailureFeedback text field from the existing evidence and return JSON only."
    for field in ("issue_contract", "observed_failure", "correction_evidence_ids", "scope_expansion"):
        if not isinstance(payload.get(field), list):
            return f"{field} must be a JSON array. Return the corrected object only."
    if not payload["issue_contract"] or not payload["observed_failure"]:
        return "issue_contract and observed_failure must each contain at least one grounded fact."
    ledger = payload.get("evidence_ledger")
    if not isinstance(ledger, list) or not ledger:
        return "evidence_ledger must be a non-empty JSON array grounded in the provided inputs."
    seen_ids: set[str] = set()
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in ledger:
        if not isinstance(item, dict):
            return "Every evidence_ledger item must be a JSON object."
        identifier = str(item.get("id") or "").strip()
        source = str(item.get("source") or "").strip()
        fact = str(item.get("fact") or "").strip()
        support = str(item.get("support") or "").strip()
        if not identifier or identifier in seen_ids:
            return "Every evidence_ledger item needs a unique non-empty id."
        if source not in _FAILURE_REVIEW_EVIDENCE_SOURCES:
            return (
                "Correct every evidence source to public_task, failed_patch, failure_log, "
                "validation_summary, or semantic_evidence."
            )
        if not fact:
            return "Every evidence_ledger item needs a non-empty fact grounded in the provided inputs."
        if support not in _FAILURE_REVIEW_EVIDENCE_SUPPORT:
            return "Correct every evidence support to direct, inference, or unknown."
        seen_ids.add(identifier)
        evidence_by_id[identifier] = item
    if not isinstance(payload.get("regression_guards"), list):
        return "regression_guards must be a JSON array. Return the corrected object only."
    stage = str(payload.get("failure_stage") or "")
    readiness = str(payload.get("correction_readiness") or "")
    observation_quality = str(payload.get("failure_observation_quality") or "")
    if observation_quality not in _FAILURE_REVIEW_OBSERVATION_QUALITY:
        return (
            "failure_observation_quality must be expected_actual, exception_detail, behavior_delta, "
            "failure_only, or environment_only."
        )
    next_evidence_source = str(payload.get("next_evidence_source") or "")
    if next_evidence_source not in _FAILURE_REVIEW_NEXT_EVIDENCE_SOURCES:
        return (
            "next_evidence_source must be none, repository_code, public_test, "
            "public_documentation, or environment."
        )
    if readiness not in _FAILURE_REVIEW_READINESS:
        return (
            "correction_readiness must be not_needed, actionable, needs_replan, "
            "or insufficient_evidence."
        )
    if stage in {"complete", "invalid_test"}:
        if readiness != "not_needed" or next_evidence_source != "none":
            return "complete/invalid_test require correction_readiness=not_needed and next_evidence_source=none."
        return None
    if stage in {"localization", "problem_contract"} and readiness != "needs_replan":
        return "localization/problem_contract require correction_readiness=needs_replan."
    if stage == "validation_environment" and readiness != "insufficient_evidence":
        return "validation_environment requires correction_readiness=insufficient_evidence."
    if payload["scope_expansion"] and readiness == "actionable":
        return "scope_expansion cannot be actionable; use correction_readiness=needs_replan."
    if observation_quality in {"failure_only", "environment_only"} and readiness == "actionable":
        return (
            "failure_only/environment_only evidence cannot be actionable; use "
            "needs_replan or insufficient_evidence."
        )
    public_replan_sources = {"repository_code", "public_test", "public_documentation"}
    if stage == "insufficient_evidence":
        expected_readiness = (
            "needs_replan"
            if next_evidence_source in public_replan_sources
            else "insufficient_evidence"
        )
        if readiness != expected_readiness:
            return (
                "insufficient_evidence with a public next_evidence_source must use needs_replan; "
                "otherwise it must remain insufficient_evidence."
            )
    if readiness == "needs_replan" and next_evidence_source not in public_replan_sources:
        return "needs_replan requires one concrete public next_evidence_source."
    if readiness == "insufficient_evidence" and next_evidence_source in public_replan_sources:
        return (
            "A public next_evidence_source is recoverable; use needs_replan and "
            "localization/problem_contract instead of insufficient_evidence."
        )
    if readiness == "actionable" and next_evidence_source != "none":
        return "actionable feedback must use next_evidence_source=none."
    if readiness == "actionable":
        if stage not in {"implementation_semantics", "regression"}:
            return "Only implementation_semantics/regression can be actionable."
        selected_ids = [
            str(item).strip()
            for item in payload["correction_evidence_ids"]
            if str(item).strip()
        ]
        selected = [evidence_by_id.get(identifier) for identifier in selected_ids]
        if not selected_ids or any(item is None for item in selected):
            return "actionable feedback must reference valid correction_evidence_ids."
        if any(str(item.get("support")) == "unknown" for item in selected if item is not None):
            return "actionable correction_evidence_ids may not reference unknown evidence."
        direct_sources = {
            str(item.get("source"))
            for item in selected
            if item is not None and str(item.get("support")) == "direct"
        }
        if not direct_sources & {"failure_log", "validation_summary"}:
            return "actionable feedback needs direct post-patch failure evidence."
        if not direct_sources & {"public_task", "failed_patch", "semantic_evidence"}:
            return "actionable feedback needs direct Public Task, failed patch, or SemanticEvidence."
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return "confidence must be a number between 0 and 1. Return the corrected object only."
    if not 0.0 <= confidence <= 1.0:
        return "confidence must be between 0 and 1. Return the corrected object only."
    return None


class SubAgentRunner:
    """为一次 Worker 调用创建隔离的运行时并收集结果。"""

    def __init__(
        self,
        *,
        llm: ChatLLM,
        base_tools: ToolRegistry,
        cwd: Path | str,
        output_dir: Path | str,
        llm_overrides: dict[str, ChatLLM] | None = None,
        memory_store: MemoryStore | None = None,
        semantic_memory_recall: bool = False,
        excluded_memory_types: set[MemoryType] | None = None,
        swebench_profile: bool = False,
        implement_phase: str = "default",
    ):
        self.llm = llm
        self.llm_overrides = llm_overrides or {}
        self.base_tools = base_tools
        self.cwd = Path(cwd).resolve()
        self.output_dir = Path(output_dir)
        self.memory_store = memory_store or MemoryStore.for_project(self.cwd)
        self.semantic_memory_recall = semantic_memory_recall
        self.excluded_memory_types = excluded_memory_types or set()
        self.swebench_profile = swebench_profile
        self.implement_phase = implement_phase

    def run(
        self,
        agent_type: str,
        prompt: str,
        description: str = "sub-agent task",
        constraints: WorkerConstraints | None = None,
        verification_plan: dict[str, Any] | None = None,
    ) -> WorkerResult:
        """选择角色、裁剪工具、创建独立 QueryEngine，再返回结构化 WorkerResult。"""

        spec = self._worker_spec(agent_type)
        constraints = constraints or WorkerConstraints()
        verification_execution: dict[str, Any] = {}
        # 最小权限在这里落实：角色白名单之外，Implement 还会用文件/行范围重新包装工具。
        worker_tools = self._tools_for(spec, constraints, verification_plan, verification_execution)
        worker_llm = self.llm_overrides.get(spec.name, self.llm)
        worker_output_dir = self.output_dir / "subagents" / f"{int(time.time() * 1000)}-{_safe_name(spec.name)}"
        before_changed_files = _git_changed_files(self.cwd) if spec.name == "implement" else ()
        if spec.name == "failure_review":
            completion_guard = _failure_review_completion_guard
        elif self.swebench_profile and spec.name == "reproducer":
            completion_guard = lambda answer: _reproducer_completion_guard(
                answer,
                prompt,
                verification_execution,
            )
        elif self.swebench_profile and spec.name == "test_expander":
            def completion_guard(answer: str) -> str | None:
                errors = differential_plan_validation_errors(
                    answer,
                    guard_obligations_from_prompt(prompt),
                    trusted_oracle_evidence_from_prompt(prompt),
                )
                if not errors:
                    return None
                return (
                    "Correct only the listed differential-plan defects and return the full JSON "
                    "object. Do not redesign valid cases or downgrade required_relation:\n- "
                    + "\n- ".join(errors[:12])
                )
        elif self.swebench_profile and spec.name == "implement":
            completion_guard = _swebench_implement_completion_guard
        else:
            completion_guard = None
        engine = QueryEngine(
            llm=worker_llm,
            tools=worker_tools,
            system_prompt=spec.system_prompt,
            max_steps=spec.max_steps,
            output_dir=worker_output_dir,
            cwd=self.cwd,
            memory_store=self.memory_store,
            semantic_memory_recall=self.semantic_memory_recall,
            excluded_memory_types=self.excluded_memory_types,
            memory_prompt_enabled=not self.swebench_profile,
            memory_recall_enabled=not self.swebench_profile,
            include_dynamic_context=not self.swebench_profile,
            completion_guard=completion_guard,
            # FailureReview 的第二次调用只修正 JSON，不计入正常 max_steps，也不暴露工具。
            completion_guard_extra_turns=1 if spec.name in {"failure_review", "test_expander"} else 0,
            force_completion_after_max_steps=(
                self.swebench_profile and spec.name == "reproducer"
            ),
        )
        text = engine.chat(_worker_prompt(description, prompt, constraints if spec.name == "implement" else None))
        # Worker 和 Coordinator 共享 worktree，因此用调用前后的 git diff 识别 Implement
        # 实际留下的文件；WorkerResult 只把紧凑结果返回主 Agent，不合并完整 messages。
        changed_files = _git_changed_files(self.cwd) if spec.name == "implement" else ()
        touched_files = tuple(path for path in changed_files if path not in before_changed_files)
        reported_changed_files = (touched_files or changed_files) if spec.name == "implement" else ()
        return WorkerResult(
            type=spec.name,
            model=str(getattr(worker_llm, "model", "unknown")),
            description=description,
            text=text or "(Worker produced no final text.)",
            output_dir=worker_output_dir,
            completed=engine.completed,
            stop_reason=engine.stop_reason,
            max_steps=spec.max_steps,
            prompt_tokens=engine.total_prompt_tokens,
            completion_tokens=engine.total_completion_tokens,
            total_tokens=engine.total_tokens,
            changed_files=reported_changed_files,
            verification_execution=verification_execution,
        )

    def _worker_spec(self, agent_type: str) -> WorkerSpec:
        spec = get_worker_spec(agent_type)
        if not self.swebench_profile:
            return spec
        prompt = SWEBENCH_WORKER_PROMPTS.get(spec.name)
        if prompt is None:
            return spec
        if spec.name == "implement":
            if self.implement_phase == "experience_initial":
                prompt += SWEBENCH_EXPERIENCE_INITIAL_IMPLEMENT_PROMPT
            elif self.implement_phase == "experience_corrective":
                prompt += SWEBENCH_EXPERIENCE_CORRECTIVE_IMPLEMENT_PROMPT
        return replace(spec, system_prompt=prompt, max_steps=SWEBENCH_WORKER_MAX_STEPS.get(spec.name, spec.max_steps))

    def _tools_for(
        self,
        spec: WorkerSpec,
        constraints: WorkerConstraints | None = None,
        verification_plan: dict[str, Any] | None = None,
        verification_execution: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        """把 WorkerSpec 的逻辑角色转换成真正可执行的最小 ToolRegistry。"""

        if spec.name == "implement" and constraints and constraints.has_scope:
            return self._scoped_implement_tools(constraints)
        if spec.name == "patch_review":
            return self._patch_review_tools()
        if self.swebench_profile and spec.name == "reproducer":
            return self._swebench_reproducer_tools(verification_execution)
        if self.swebench_profile and spec.name == "verify":
            return self._swebench_verify_tools(verification_plan, verification_execution)
        if spec.allowed_tools is None:
            return self.base_tools.excluding({"agent"})
        return self.base_tools.subset(spec.allowed_tools)

    def _swebench_reproducer_tools(
        self,
        execution: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        """限制复现 Worker 的探索预算，避免它退化成重复的 Research。"""
        registry = ToolRegistry()
        limits = {"search_repo": 1, "read_file": 3, "run_command": 1}
        counts = {name: 0 for name in limits}
        total_calls = {"value": 0}
        execution = execution if execution is not None else {}

        for name, limit in limits.items():
            original = self.base_tools.get(name)
            if original is None:
                continue

            def limited(inp: dict, *, tool=original, tool_name=name, tool_limit=limit) -> ToolResponse:
                if total_calls["value"] >= 4:
                    return ToolResponse.failure(
                        "REPRODUCER_FINALIZATION_REQUIRED",
                        "The evidence tool budget is exhausted. Do not call more tools; return SemanticEvidence JSON now.",
                    )
                if counts[tool_name] >= tool_limit:
                    return ToolResponse.failure(
                        "REPRODUCER_TOOL_BUDGET_EXHAUSTED",
                        (
                            f"SWE-bench reproducer may call {tool_name} at most {tool_limit} times. "
                            "Stop exploring and return the best available reproduction_contract and verify_commands JSON."
                        ),
                    )
                if tool_name != "run_command":
                    counts[tool_name] += 1
                    total_calls["value"] += 1
                    return tool.run_with_timing(inp)
                requested_command = str(inp.get("command") or "").strip()
                safety_reason = verification_command_safety_reason(requested_command)
                if safety_reason:
                    return ToolResponse.failure(
                        "UNSAFE_REPRODUCER_COMMAND",
                        f"{safety_reason}. Run one direct public behavior assertion from the repository root.",
                    )
                if classify_verification_command(requested_command) != "behavior":
                    return ToolResponse.failure(
                        "REPRODUCER_BEHAVIOR_COMMAND_REQUIRED",
                        "The baseline command must be pytest/unittest or python -c with explicit assert/raise. "
                        "Print-only probes are not reproduction evidence and do not consume the one execution attempt.",
                    )
                counts[tool_name] += 1
                total_calls["value"] += 1
                response = self._run_swebench_verification_command(tool, inp)
                execution.update(
                    {
                        "attempted": True,
                        "requested_command": str(inp.get("command") or "").strip(),
                        "execution_command": response.data.get("execution_command", ""),
                        "exit_code": response.data.get("exit_code"),
                        "tool_status": response.status.value,
                        "output": response.text[-2_000:],
                    }
                )
                return response

            registry.register(
                build_tool(
                    name=name,
                    description=original.description,
                    parameters=original.parameters(),
                    handler=limited,
                    prompt=original.prompt(),
                    max_result_size_chars=original.max_result_size_chars,
                    is_read_only=original.is_read_only,
                    is_concurrency_safe=original.is_concurrency_safe,
                    source=original.source,
                )
            )

        return registry

    def _swebench_verify_tools(
        self,
        verification_plan: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        """Verify 只能执行 SemanticEvidence 选出的单条命令。"""

        registry = ToolRegistry()
        original = self.base_tools.get("run_command")
        if original is None:
            return registry
        state = {"run_command_calls": 0}
        expected_command = str((verification_plan or {}).get("command") or "").strip()
        execution = execution if execution is not None else {}

        def limited_run_command(inp: dict) -> ToolResponse:
            requested_command = str(inp.get("command") or "")
            if expected_command and requested_command.strip() != expected_command:
                return ToolResponse.failure(
                    "VERIFY_COMMAND_NOT_FROM_SEMANTIC_EVIDENCE",
                    "Run the exact command from the validated SemanticEvidence verification plan; alternatives are not allowed.",
                )
            safety_reason = verification_command_safety_reason(requested_command)
            if safety_reason:
                return ToolResponse.failure(
                    "UNSAFE_VERIFICATION_COMMAND",
                    f"{safety_reason}. Run the behavior command directly; output truncation is automatic.",
                )
            if state["run_command_calls"] >= 1:
                return ToolResponse.failure(
                    "VERIFY_COMMAND_BUDGET_EXHAUSTED",
                    "Verify already ran its single command. Return the required JSON result now.",
                )
            state["run_command_calls"] += 1
            response = self._run_swebench_verification_command(original, inp)
            execution.update(
                {
                    "attempted": True,
                    "requested_command": requested_command.strip(),
                    "execution_command": response.data.get("execution_command", ""),
                    "exit_code": response.data.get("exit_code"),
                    "tool_status": response.status.value,
                    "output": response.text[-2000:],
                }
            )
            return response

        registry.register(
            build_tool(
                name="run_command",
                description=original.description,
                parameters=original.parameters(),
                handler=limited_run_command,
                prompt=original.prompt(),
                max_result_size_chars=original.max_result_size_chars,
                is_read_only=original.is_read_only,
                is_concurrency_safe=original.is_concurrency_safe,
                source=original.source,
            )
        )
        return registry

    def _run_swebench_verification_command(self, tool: Any, inp: dict) -> ToolResponse:
        """在目标题目的 SWE-bench 依赖环境中运行公开验证命令。"""

        requested_command = str(inp.get("command") or "")
        safety_reason = verification_command_safety_reason(requested_command)
        if safety_reason:
            return ToolResponse.failure(
                "UNSAFE_VERIFICATION_COMMAND",
                f"{safety_reason}. Run the behavior command directly; output truncation is automatic.",
            )
        prepared_command, runtime = prepare_verification_command(requested_command, self.cwd)
        prepared_input = dict(inp)
        prepared_input["command"] = prepared_command
        response = tool.run_with_timing(prepared_input)
        response.data["requested_command"] = requested_command
        response.data["execution_command"] = prepared_command
        response.data["verification_runtime"] = runtime
        response.text = (
            f"verification_runtime={runtime}\n"
            f"requested_command={requested_command}\n"
            f"{response.text}"
        )
        return response

    def _patch_review_tools(self) -> ToolRegistry:
        registry = ToolRegistry()
        git_diff_tool = self.base_tools.get("git_diff")
        read_tool = self.base_tools.get("read_file")
        if git_diff_tool is not None:
            registry.register(git_diff_tool)
        if read_tool is not None:
            read_budget = 3
            state = {"read_file_calls": 0}

            def limited_read_file(inp: dict) -> ToolResponse:
                if state["read_file_calls"] >= read_budget:
                    return ToolResponse.failure(
                        "PATCH_REVIEW_READ_BUDGET_EXHAUSTED",
                        (
                            f"Patch review may call read_file at most {read_budget} times. Stop inspecting and return "
                            "JSON with patch_risk/verdict based on git_diff and the snippets already read."
                        ),
                    )
                state["read_file_calls"] += 1
                return read_tool.run_with_timing(inp)

            registry.register(
                build_tool(
                    name="read_file",
                    description="Read up to three short file ranges for patch risk review.",
                    parameters=read_tool.parameters(),
                    handler=limited_read_file,
                    prompt=(
                        "Patch review 最多只能调用三次 read_file；只读 diff 修改点附近、父类/调用方或旧行为不变量附近的短范围。"
                    ),
                    max_result_size_chars=read_tool.max_result_size_chars,
                    is_concurrency_safe=True,
                    is_read_only=True,
                    source=ToolSource.BUILTIN,
                )
            )
        return registry

    def _scoped_implement_tools(self, constraints: WorkerConstraints) -> ToolRegistry:
        """为单次 Implement 构造按文件与行范围约束的读写工具。"""

        registry = ToolRegistry()
        read_tool = self.base_tools.get("read_file")
        write_tool = self.base_tools.get("write_file")
        baseline_contents: dict[str, str] = {}

        def baseline_content(path: str) -> str:
            if path not in baseline_contents:
                target = (self.cwd / path).resolve()
                target.relative_to(self.cwd)
                baseline_contents[path] = target.read_text(encoding="utf-8", errors="replace")
            return baseline_contents[path]

        def scoped_read_file(inp: dict) -> ToolResponse:
            denial = _check_scoped_read(inp, constraints)
            if denial:
                return denial
            if read_tool is None:
                return ToolResponse.failure("TOOL_NOT_FOUND", "Base read_file tool is not available.")
            try:
                baseline_content(_normalize_repo_path(inp["path"]))
            except (OSError, ValueError) as exc:
                return ToolResponse.failure("IMPLEMENT_SCOPE_DENIED", str(exc))
            return read_tool.run_with_timing(inp)

        def scoped_write_file(inp: dict) -> ToolResponse:
            path = _normalize_repo_path(inp["path"])
            denial = _check_allowed_file(path, constraints)
            if denial:
                return denial
            if constraints.allowed_ranges and path in constraints.allowed_ranges:
                return ToolResponse.failure(
                    "IMPLEMENT_SCOPE_DENIED",
                    f"write_file would rewrite scoped existing file {path}; use replace_in_file instead.",
                )
            if write_tool is None:
                return ToolResponse.failure("TOOL_NOT_FOUND", "Base write_file tool is not available.")
            return write_tool.run_with_timing(inp)

        def scoped_replace_in_file(inp: dict) -> ToolResponse:
            path = _normalize_repo_path(inp["path"])
            denial = _check_allowed_file(path, constraints)
            if denial:
                return denial
            allowed_ranges = (constraints.allowed_ranges or {}).get(path)
            if not allowed_ranges:
                return ToolResponse.failure(
                    "IMPLEMENT_SCOPE_DENIED",
                    f"replace_in_file requires allowed_ranges for {path}. Ask Coordinator for a research Worker.",
                )
            if read_tool is None or write_tool is None:
                return ToolResponse.failure("TOOL_NOT_FOUND", "Base read_file/write_file tools are not available.")

            # 通过底层 read_file 触发路径安全校验与“先读后写”的并发保护。
            read_response = read_tool.run_with_timing({"path": path})
            if read_response.error:
                return read_response

            target = (self.cwd / path).resolve()
            try:
                target.relative_to(self.cwd)
            except ValueError:
                return ToolResponse.failure("IMPLEMENT_SCOPE_DENIED", f"Path escapes repository scope: {path}")
            content = target.read_text(encoding="utf-8", errors="replace")
            try:
                baseline = baseline_content(path)
            except (OSError, ValueError) as exc:
                return ToolResponse.failure("IMPLEMENT_SCOPE_DENIED", str(exc))
            selected: list[tuple[int, int, str, dict[str, Any]]] = []
            for index, replacement in enumerate(_coerce_line_replacements(inp), 1):
                line_start = int(replacement["start_line"])
                line_end = int(replacement["end_line"])
                if not any(line_range.contains(line_start, line_end) for line_range in allowed_ranges):
                    return ToolResponse.failure(
                        "IMPLEMENT_SCOPE_DENIED",
                        f"Line replacement {index} touches {path}:{line_start}-{line_end}, "
                        f"outside allowed_ranges {_format_ranges({path: allowed_ranges})[path]}.",
                    )
                try:
                    baseline_start, baseline_end, segment = _slice_for_line_span(
                        baseline,
                        line_start,
                        line_end,
                    )
                except ValueError as exc:
                    return ToolResponse.failure("BAD_LINE_RANGE", str(exc))
                current_span = _map_baseline_line_span_to_current(
                    baseline,
                    content,
                    line_start,
                    line_end,
                )
                if current_span is None:
                    return ToolResponse.failure(
                        "STALE_LINE_RANGE",
                        (
                            f"Original line range {path}:{line_start}-{line_end} was already modified or cannot be "
                            "mapped safely. Use an exact old/new replacement for the currently visible snippet."
                        ),
                    )
                start, end = current_span
                new = _preserve_segment_newline(segment, replacement["new"])
                if any(_spans_overlap(start, end, prior_start, prior_end) for prior_start, prior_end, _, _ in selected):
                    return ToolResponse.failure("REPLACE_OVERLAP", f"Line replacement {index} overlaps another replacement.")
                selected.append(
                    (
                        start,
                        end,
                        new,
                        {
                            "index": index,
                            "mode": "line",
                            "line_start": line_start,
                            "line_end": line_end,
                            "baseline_start": baseline_start,
                            "baseline_end": baseline_end,
                            "old_length": len(segment),
                            "new_length": len(new),
                        },
                    )
                )

            replacements = _coerce_replacements(inp)
            for index, replacement in enumerate(replacements, 1):
                old = replacement["old"]
                new = replacement["new"]
                matches = _find_occurrences(content, old)
                if not matches:
                    return ToolResponse.failure(
                        "REPLACE_NOT_FOUND",
                        f"Replacement {index} old text was not found in {path}.",
                    )
                occurrence = replacement.get("occurrence")
                if occurrence is not None:
                    occurrence = int(occurrence)
                    if occurrence < 1 or occurrence > len(matches):
                        return ToolResponse.failure(
                            "REPLACE_OCCURRENCE_NOT_FOUND",
                            f"Replacement {index} occurrence {occurrence} is outside 1..{len(matches)}.",
                        )
                    start = matches[occurrence - 1]
                elif len(matches) == 1:
                    start = matches[0]
                else:
                    return ToolResponse.failure(
                        "REPLACE_AMBIGUOUS",
                        f"Replacement {index} old text appears {len(matches)} times in {path}; pass occurrence.",
                    )

                end = start + len(old)
                if any(_spans_overlap(start, end, prior_start, prior_end) for prior_start, prior_end, _, _ in selected):
                    return ToolResponse.failure("REPLACE_OVERLAP", f"Replacement {index} overlaps another replacement.")
                line_start, line_end = _line_span_for_slice(content, start, end)
                current_range_allowed = any(
                    line_range.contains(line_start, line_end) for line_range in allowed_ranges
                )
                baseline_range_allowed = any(
                    any(
                        line_range.contains(*_line_span_for_slice(baseline, position, position + len(old)))
                        for line_range in allowed_ranges
                    )
                    for position in _find_occurrences(baseline, old)
                )
                if not current_range_allowed and not baseline_range_allowed:
                    return ToolResponse.failure(
                        "IMPLEMENT_SCOPE_DENIED",
                        f"Replacement {index} touches {path}:{line_start}-{line_end}, "
                        f"outside allowed_ranges {_format_ranges({path: allowed_ranges})[path]}.",
                    )
                selected.append(
                    (
                        start,
                        end,
                        new,
                        {
                            "index": index,
                            "mode": "old_new",
                            "line_start": line_start,
                            "line_end": line_end,
                            "old_length": len(old),
                            "new_length": len(new),
                        },
                    )
                )

            if not selected:
                return ToolResponse.failure(
                    "BAD_ARGUMENTS",
                    "replace_in_file requires line_replacements, replacements, or old/new.",
                )
            updated = content
            for start, end, new, _ in sorted(selected, key=lambda item: item[0], reverse=True):
                updated = updated[:start] + new + updated[end:]
            if updated == content:
                return ToolResponse.failure("NO_CHANGES", f"replace_in_file made no changes to {path}.")
            scope_issues = _baseline_diff_scope_issues(baseline, updated, allowed_ranges)
            if scope_issues:
                return ToolResponse.failure(
                    "IMPLEMENT_DIFF_SCOPE_DENIED",
                    (
                        f"The resulting diff for {path} escapes the original allowed_ranges "
                        f"{_format_ranges({path: allowed_ranges})[path]}: {'; '.join(scope_issues)}. "
                        "Retry with one atomic replacement based on the original approved ranges."
                    ),
                )

            write_response = write_tool.run_with_timing({"path": path, "content": updated})
            if write_response.error:
                return write_response
            return ToolResponse.success(
                f"Replaced {len(selected)} snippet(s) in {path}.",
                {"path": path, "replacements": [item[3] for item in selected]},
            )

        registry.register_many(
            [
                build_tool(
                    name="read_file",
                    description="Read only the Coordinator-approved file ranges.",
                    parameters=[
                        ToolParameter("path", "string", "Path relative to the repository root"),
                        ToolParameter("start_line", "integer", "1-based first approved line to read", required=False),
                        ToolParameter("end_line", "integer", "1-based final approved line to read", required=False),
                        ToolParameter("offset", "integer", "Alias for start_line", required=False),
                        ToolParameter("limit", "integer", "Maximum number of lines to read", required=False),
                    ],
                    handler=scoped_read_file,
                    prompt=(
                        "Implement Worker 只能 read_file 读取 allowed_ranges/context_ranges 覆盖的行号范围；"
                        "必须传 start_line/end_line 或 offset/limit，不能读取整文件。"
                    ),
                    is_concurrency_safe=True,
                    is_read_only=True,
                    source=ToolSource.BUILTIN,
                ),
                build_tool(
                    name="replace_in_file",
                    description="Replace exact old/new snippets in one Coordinator-approved file.",
                    parameters=[
                        ToolParameter("path", "string", "Path relative to the repository root"),
                        ToolParameter(
                            "replacements",
                            "array",
                            "List of {old, new, occurrence?} exact replacements to apply in one write",
                            required=False,
                        ),
                        ToolParameter(
                            "line_replacements",
                            "array",
                            "List of {start_line, end_line, new} replacements to apply in one write",
                            required=False,
                        ),
                        ToolParameter("old", "string", "Single replacement old text", required=False),
                        ToolParameter("new", "string", "Single replacement new text", required=False),
                        ToolParameter(
                            "occurrence",
                            "integer",
                            "1-based occurrence to replace when old text is ambiguous",
                            required=False,
                        ),
                    ],
                    handler=scoped_replace_in_file,
                    prompt=(
                        "line_replacements 始终使用 Worker 第一次读取时的原始行号，工具会安全映射后续行号漂移；"
                        "同文件多处修改优先在一次调用中提交。没有稳定行号时再用 old/new replacements。"
                        "所有替换必须完全位于 allowed_ranges 内；工具参数必须是合法 JSON。"
                    ),
                    source=ToolSource.BUILTIN,
                ),
                build_tool(
                    name="write_file",
                    description="Write only Coordinator-approved new or unscoped files.",
                    parameters=[
                        ToolParameter("path", "string", "Path relative to the repository root"),
                        ToolParameter("content", "string", "Full file content to write"),
                    ],
                    handler=scoped_write_file,
                    prompt="write_file 不可重写带 allowed_ranges 的已有文件；这类修改必须用 replace_in_file。",
                    source=ToolSource.BUILTIN,
                ),
            ]
        )
        return registry


def get_worker_spec(agent_type: str | None) -> WorkerSpec:
    return WORKER_SPECS.get((agent_type or "general").strip(), WORKER_SPECS["general"])


def _reproducer_completion_guard(
    answer: str,
    prompt: str,
    execution: dict[str, Any] | None = None,
) -> str | None:
    problem_statement = extract_public_problem_statement(prompt)
    evidence = normalize_semantic_evidence(extract_json_object(answer), problem_statement)
    issues = validate_semantic_evidence(evidence, problem_statement)
    required_ids = sorted(required_case_ids(evidence))
    command_diagnostics: list[dict[str, Any]] = []
    if isinstance(evidence, dict):
        for index, item in enumerate(evidence.get("verify_commands") or [], 1):
            command = item.get("command") if isinstance(item, dict) else item
            command = str(command or "").strip()
            command_diagnostics.append(
                {
                    "index": index,
                    "classification": classify_verification_command(command) if command else "missing",
                    "safety_error": verification_command_safety_reason(command) if command else "missing command",
                    "declared_covers": item.get("covers") if isinstance(item, dict) else [],
                }
            )
    plan = build_verification_plan(evidence)
    execution = execution if isinstance(execution, dict) else {}
    if not issues and plan is not None:
        command = str(plan.get("command") or "").strip()
        requested = str(execution.get("requested_command") or "").strip()
        if not execution.get("attempted"):
            issues.append(
                "the final verify command was not executed on the clean baseline; call run_command with it now"
            )
        elif requested != command:
            issues.append(
                "verify_commands[0].command must exactly equal the command already executed on the clean baseline: "
                f"{requested!r}"
            )
        elif not isinstance(execution.get("exit_code"), int):
            issues.append("the clean baseline execution has no exit code")
        else:
            assessment = assess_verification_result(
                {
                    "status": "passed" if execution["exit_code"] == 0 else "failed",
                    "command": command,
                    "exit_code": execution["exit_code"],
                    "evidence": str(execution.get("output") or ""),
                }
            )
            if assessment.get("classification") != "behavior_failed":
                issues.append(
                    "the clean baseline command must fail on the public behavior, but was classified as "
                    f"{assessment.get('classification') or 'unknown'}"
                )
    if not issues:
        return None
    rendered = "\n".join(f"- {issue}" for issue in issues[:8])
    return (
        "Complete the public reproduction contract before finishing. If the final command has not run, call "
        "run_command with exactly that safe assert command now; otherwise return corrected SemanticEvidence JSON "
        "whose command exactly matches the executed command. Do not add Markdown. "
        "Only code/issue/public-test/execution references count as evidence. Missing requirements:\n"
        f"{rendered}\n"
        f"Exact required_case_ids: {required_ids}\n"
        f"Command diagnostics: {command_diagnostics}\n"
        "With exactly one safe behavior command, the framework binds every required_case_id automatically. "
        "Do not rewrite covers merely to mirror IDs. If classification is unknown, replace printing-only logic with "
        "pytest/unittest or explicit assertions against the final public/rendered output; a successful build alone is insufficient. "
        "The command must assert the requested post-fix behavior and must never pass because the old bug, old exception, "
        "or unknown classification still occurs. For a plain/bare param.name canonical identity in an *args/**kwargs "
        "display task, set preferred_change_layer exactly to consumer.\n"
        "Required shapes: reproduction_contract={trigger,expected_behavior}; each target_partition="
        "{id,expected,container,rank,dtype_protocol,missing_value_behavior,"
        "first_dependent_predicate:{symbol,location},evidence:[{type,location,claim}]}; "
        "each contract_edge={producer,current_canonical_identity,preferred_change_layer,"
        "consumers:[{symbol,location,lookup_identity,output_identity}],evidence:[{type:'code',location,claim}]}; "
        "verify_commands must contain one {command,covers} item whose covers includes every required case id."
    )


def _worker_prompt(description: str, prompt: str, constraints: WorkerConstraints | None = None) -> str:
    constraint_section = ""
    if constraints is not None:
        constraint_section = (
            "\n\n# Implement Scope\n"
            f"- allowed_files: {sorted(constraints.allowed_files)}\n"
            f"- allowed_ranges: {_format_ranges(constraints.allowed_ranges)}\n"
            f"- context_ranges (read-only): {_format_ranges(constraints.context_ranges)}\n"
            "- 你只能使用受限 read_file/replace_in_file/write_file。不要搜索、不要看 git diff、不要运行命令，不要手写 patch。\n"
        )
    return (
        f"任务简介：{description}\n\n"
        "下面是 Coordinator 分配给你的完整任务。你看不到主 Agent 的其他上下文，"
        "因此必须只基于这里的信息和工具结果完成工作。\n\n"
        f"{prompt}"
        f"{constraint_section}"
    )


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80] or "worker"


def _git_changed_files(cwd: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _normalize_repo_path(value: object) -> str:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path escapes repository scope: {value}")
    normalized = path.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _parse_allowed_ranges(raw: object) -> dict[str, tuple[LineRange, ...]]:
    if raw in (None, ""):
        return {}
    parsed: dict[str, tuple[LineRange, ...]] = {}
    if isinstance(raw, dict):
        for path, ranges in raw.items():
            parsed[_normalize_repo_path(path)] = tuple(_coerce_range(item) for item in ranges or [])
        return parsed
    if isinstance(raw, list):
        grouped: dict[str, list[LineRange]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("allowed_ranges list entries must be objects.")
            path = _normalize_repo_path(item["path"])
            grouped.setdefault(path, []).append(_coerce_range(item))
        return {path: tuple(ranges) for path, ranges in grouped.items()}
    raise ValueError("allowed_ranges must be an object or list.")


def _merge_line_ranges(ranges: tuple[LineRange, ...]) -> tuple[LineRange, ...]:
    if not ranges:
        return ()
    merged: list[LineRange] = []
    for current in sorted(ranges, key=lambda item: (item.start, item.end)):
        if merged and current.start <= merged[-1].end + 1:
            previous = merged[-1]
            merged[-1] = LineRange(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return tuple(merged)


def _coerce_range(value: object) -> LineRange:
    if isinstance(value, dict):
        start = int(value.get("start_line") or value.get("start") or value.get("offset"))
        end_value = value.get("end_line") or value.get("end")
        if end_value is None and value.get("limit") is not None:
            end_value = start + int(value["limit"]) - 1
        end = int(end_value)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = int(value[0]), int(value[1])
    else:
        raise ValueError("Line ranges must be [start, end] or an object with start_line/end_line.")
    if start < 1 or end < start:
        raise ValueError("Invalid line range.")
    return LineRange(start=start, end=end)


def _format_ranges(ranges: dict[str, tuple[LineRange, ...]] | None) -> dict[str, list[list[int]]]:
    return {
        path: [[line_range.start, line_range.end] for line_range in file_ranges]
        for path, file_ranges in (ranges or {}).items()
    }


def _check_allowed_file(path: str, constraints: WorkerConstraints) -> ToolResponse | None:
    if path not in constraints.allowed_files:
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_DENIED",
            f"Implement Worker may only touch allowed_files. Denied: {path}",
        )
    return None


def _check_scoped_read(inp: dict, constraints: WorkerConstraints) -> ToolResponse | None:
    path = _normalize_repo_path(inp["path"])
    allowed_ranges = constraints.readable_ranges.get(path)
    if not allowed_ranges:
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_DENIED",
            f"read_file for implement requires allowed_ranges/context_ranges for {path}.",
        )
    start = _optional_positive_int(inp.get("start_line"))
    offset = _optional_positive_int(inp.get("offset"))
    if start is None and offset is not None:
        start = offset
    end = _optional_positive_int(inp.get("end_line"))
    limit = _optional_positive_int(inp.get("limit"))
    if start is None:
        return ToolResponse.failure("IMPLEMENT_SCOPE_DENIED", "Scoped read_file requires start_line or offset.")
    if end is None and limit is not None:
        end = start + limit - 1
    if end is None:
        return ToolResponse.failure("IMPLEMENT_SCOPE_DENIED", "Scoped read_file requires end_line or limit.")
    if not any(line_range.contains(start, end) for line_range in allowed_ranges):
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_DENIED",
            f"Read range {path}:{start}-{end} is outside approved readable ranges "
            f"{_format_ranges({path: allowed_ranges})[path]}.",
        )
    return None


def _optional_positive_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    if parsed < 1:
        raise ValueError("Line range values must be positive integers.")
    return parsed


def _coerce_line_replacements(inp: dict) -> list[dict[str, Any]]:
    raw = inp.get("line_replacements")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("line_replacements must be a non-empty array.")
    replacements: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Line replacement {index} must be an object.")
        start = int(item.get("start_line") or item.get("start") or 0)
        end_value = item.get("end_line") or item.get("end") or start
        end = int(end_value)
        new = item.get("new")
        if start < 1 or end < start:
            raise ValueError(f"Line replacement {index} has an invalid line range.")
        if not isinstance(new, str):
            raise ValueError(f"Line replacement {index} requires a new string.")
        replacements.append({"start_line": start, "end_line": end, "new": new})
    return replacements


def _coerce_replacements(inp: dict) -> list[dict[str, Any]]:
    raw = inp.get("replacements")
    if raw is None:
        if "old" not in inp and "new" not in inp:
            return []
        if "old" not in inp or "new" not in inp:
            raise ValueError("replace_in_file requires both old and new.")
        raw = [{"old": inp["old"], "new": inp["new"], "occurrence": inp.get("occurrence")}]
    if not isinstance(raw, list) or not raw:
        raise ValueError("replacements must be a non-empty array.")
    replacements: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Replacement {index} must be an object.")
        old = item.get("old")
        new = item.get("new")
        if not isinstance(old, str) or not old:
            raise ValueError(f"Replacement {index} requires a non-empty old string.")
        if not isinstance(new, str):
            raise ValueError(f"Replacement {index} requires a new string.")
        replacement = {"old": old, "new": new}
        if item.get("occurrence") is not None:
            replacement["occurrence"] = int(item["occurrence"])
        replacements.append(replacement)
    return replacements


def _slice_for_line_span(content: str, start_line: int, end_line: int) -> tuple[int, int, str]:
    lines = content.splitlines(keepends=True)
    if not lines:
        raise ValueError("Cannot replace lines in an empty file.")
    if end_line > len(lines):
        raise ValueError(f"Line range {start_line}-{end_line} exceeds file length {len(lines)}.")
    offset = 0
    starts: list[int] = []
    for line in lines:
        starts.append(offset)
        offset += len(line)
    start = starts[start_line - 1]
    end = starts[end_line - 1] + len(lines[end_line - 1])
    return start, end, content[start:end]


def _map_baseline_line_span_to_current(
    baseline: str,
    current: str,
    start_line: int,
    end_line: int,
) -> tuple[int, int] | None:
    """把初始源码行范围映射到当前文件中仍未被修改的同一片段。"""

    baseline_lines = baseline.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    target_start = start_line - 1
    target_end = end_line
    matcher = difflib.SequenceMatcher(None, baseline_lines, current_lines, autojunk=False)
    for tag, baseline_start, baseline_end, current_start, _ in matcher.get_opcodes():
        if tag != "equal" or not (baseline_start <= target_start and target_end <= baseline_end):
            continue
        mapped_start = current_start + (target_start - baseline_start) + 1
        mapped_end = mapped_start + (target_end - target_start) - 1
        try:
            start, end, _ = _slice_for_line_span(current, mapped_start, mapped_end)
        except ValueError:
            return None
        return start, end
    return None


def _baseline_diff_scope_issues(
    baseline: str,
    updated: str,
    allowed_ranges: tuple[LineRange, ...],
) -> list[str]:
    """按初始源码坐标检查最终内容，防止前置插入把后续写入漂移到授权范围外。"""

    baseline_lines = baseline.splitlines(keepends=True)
    updated_lines = updated.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, baseline_lines, updated_lines, autojunk=False)
    issues: list[str] = []
    for tag, baseline_start, baseline_end, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        if baseline_start == baseline_end:
            anchor = baseline_start + 1
            if not any(line_range.start <= anchor <= line_range.end + 1 for line_range in allowed_ranges):
                issues.append(f"insert@{anchor}")
            continue
        line_start = baseline_start + 1
        line_end = baseline_end
        if not any(line_range.contains(line_start, line_end) for line_range in allowed_ranges):
            issues.append(f"{tag}@{line_start}-{line_end}")
    return issues


def _preserve_segment_newline(segment: str, new: str) -> str:
    if segment.endswith("\r\n") and not new.endswith(("\n", "\r")):
        return new + "\r\n"
    if segment.endswith("\n") and not new.endswith(("\n", "\r")):
        return new + "\n"
    return new


def _find_occurrences(content: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        index = content.find(needle, start)
        if index == -1:
            return positions
        positions.append(index)
        start = index + max(len(needle), 1)


def _line_span_for_slice(content: str, start: int, end: int) -> tuple[int, int]:
    line_start = content.count("\n", 0, start) + 1
    line_end = content.count("\n", 0, max(start, end - 1)) + 1
    return line_start, line_end


def _spans_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end
