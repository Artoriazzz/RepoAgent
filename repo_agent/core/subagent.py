from __future__ import annotations

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
from repo_agent.core.semantic_evidence import (
    extract_public_problem_statement,
    extract_json_object,
    implementation_semantic_issues,
    normalize_semantic_evidence,
    required_case_ids,
    validate_semantic_evidence,
)
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool
from repo_agent.core.verification import (
    classify_verification_command,
    prepare_verification_command,
    verification_command_safety_reason,
)


SubAgentType = Literal[
    "research",
    "reproducer",
    "plan",
    "implement",
    "patch_ranker",
    "patch_review",
    "verify",
    "review",
    "memory_curator",
    "case_curator",
    "general",
]


@dataclass(frozen=True)
class WorkerSpec:
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
    allowed_files: frozenset[str] = frozenset()
    allowed_ranges: dict[str, tuple[LineRange, ...]] | None = None

    @classmethod
    def from_tool_input(cls, raw: dict[str, Any]) -> "WorkerConstraints":
        files = frozenset(_normalize_repo_path(path) for path in raw.get("allowed_files") or [])
        ranges = _parse_allowed_ranges(raw.get("allowed_ranges"))
        return cls(allowed_files=files, allowed_ranges=ranges)

    @property
    def has_scope(self) -> bool:
        return bool(self.allowed_files)


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

PATCH_REVIEW_TOOLS = {
    "read_file",
    "git_diff",
}

PATCH_RANKER_TOOLS: set[str] = set()

MEMORY_TOOLS = {
    "list_memories",
    "read_memory",
    "search_memories",
    "save_memory",
    "update_memory",
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
每个 target_partition 写稳定 id、唯一 expected、适用的 container/rank/dtype/missing-value、first_dependent_predicate，以及 issue/code/public_test/execution evidence。不要输出 fix_code 或精确改法。
分类/dispatch 任务只需指出第一个读取该表示并可能改变路径的 predicate；Nullable pandas 任务覆盖题意 Series 与直接相邻 DataFrame/2D 表示，验证必须断言修复后分类而非旧 unknown/error。
`*args`/`**kwargs` 任务记录当前 producer canonical identity 和实际看到的 consumer。若 producer 保存 plain `param.name`，它是 canonical storage，preferred_change_layer=consumer；consumer 必须区分 lookup identity 与 output identity。consumer 未读全属于 coverage warning，不要猜造源码位置。
星号参数验证必须区分 lookup 和展示：已有 starred param 保留，bare `param args`/`param kwargs` 必须为零，生成字段保持 `type *args`/`type **kwargs`。命令执行最终 Sphinx 构建或公开集成测试；共享 parser/renderer 还应覆盖由源码配置项推断的公开相邻测试族。
verify command 只能验证修复后的公开行为，不得模拟内部字典或证明旧 bug。使用 pytest/unittest，或在 `python -c` 中对最终公开输出写明确 `assert`/失败退出；最终 Sphinx 构建或公开集成测试才算行为证据。
`verify_commands` 优先只有一项 {"command":"...", "covers":["case_id"]}；框架会确定性绑定全部 required case ID。命令从仓库 cwd 执行，不要写 `cd /repo`、`cd /testbed` 或其他绝对仓库路径。目标复现和公开相邻测试可用 `&&` 放在同一命令中。
如果在当前只读预算内无法形成可信的最终公开行为命令，返回空 `verify_commands` 并在 unknowns 说明原因；不要为了通过 schema 编造内部 helper 模拟。验证缺口只会降低置信度，不会阻止 Implement。
禁止管道、`||` fallback 或会掩盖退出码的后续命令。最晚第 6 个模型回合输出 JSON；结构错误时只剩一次无工具纯 JSON 修正回合。
"""


SWEBENCH_IMPLEMENT_PROMPT = """你是 SWE-bench Implement Worker，负责在给定 allowed_files/allowed_ranges 内做最小源码修改。

规则：
- 不做全仓搜索、不运行命令、不查 git log、不读整文件。
- 修改前读取指定范围，确认目标行为、精确错误文本/异常/输出和旧行为不变量。
- 采用 ExpeRepair 的精准补丁思想，但保持轻量：修改前只输出 3 行 Repair Brief：target_code_context、old_behavior_invariants、minimal_fix_strategy。
- Coordinator 给出的精确改法只是待验证假设。第一次写入前必须输出紧凑的 `semantic_challenge` 语义账本，逐条用 allowed_ranges 代码或公开 reproducer 证据标注 proven/contradicted/unproven；关键假设无法证明时阻塞，不要替错误 EditBrief 补写一份自洽故事。
- 对工具附加的 SemanticEvidence.target_partitions，逐行核对 container、rank、dtype protocol、missing-value behavior、first_dependent_predicate 和 normalization。Series 的 `.dtype` 证据不能证明 DataFrame 的 `.dtypes` 已覆盖；一维成功不能证明二维、多输出、稀疏或 nullable 表示已覆盖。规范化必须位于第一个依赖该语义的 predicate 之前。
- 对 SemanticEvidence.contract_edges，先核对 producer 的 current canonical identity/value 与每个直接 consumer 的 lookup/render/write 约定。未看到 consumer 范围时不得改共享映射键、返回类型或持久化身份；应阻塞并报告缺少的 consumer scope。展示用 `*`、转义或别名不能覆盖 canonical storage key；实现中必须分别保存 `canonical_lookup_key` 与 `matched_display_name`，前者只能用于查找，后者必须传播到关联字段、渲染或写入点。不得在输出阶段用 canonical key 重新构造展示身份。
- 只改 allowed_ranges 内的精确原文片段；优先 line_replacements 或 exact old/new。
- 优先 replace_in_file 的 line_replacements 或精确 old/new；保留无关逻辑和缩进结构。
- 不要为了让一个示例通过而修改共享默认行为。格式化、解析、序列化、类型/shape 判断等共享路径必须使用窄 guard，并保留相邻旧行为。
- 修复契约所有者，而不是只包住下游症状：当 producer 的公开返回类型/异常/副作用与 consumer 约束冲突时，先确认 producer 的 sibling API 和方法语义，再决定修 producer 还是做下游兼容。
- 分类、dispatch 或 validation 由多个 predicate 顺序决定时，规范化必须发生在第一个依赖该语义的 predicate 之前；在最终分支里补转换通常已经太晚。
- `*args`、`**kwargs`、转义标识符、显示标签等同时具有“查找键”和“渲染文本”时，保持两者分离；fallback 匹配成功后必须把实际匹配键传播到后续写入/渲染，不能只取到值后继续使用旧键。允许在先保存完整 `matched_display_name` 后，用受控的 `lstrip('*')` 派生 canonical lookup key；禁止只保留 stripped name、全局 replace 或提前 escaping。
- 在 Sphinx `augment_descriptions_with_types` 中增加参数 alias 时，只能局部扩展参数名的 lookup/display 映射；必须原样保留现有 `type`、`return/returns`、`rtype` 分支及其 `has_description`/`has_type` 更新。不得用新的参数循环替换字段扫描循环，也不得把 `has_type.add('return')` 移入参数循环。
- 接受 Series/DataFrame、1D/2D、dense/sparse、nullable/native dtype 等多种表示的 API，必须逐项覆盖题意涉及的表示，并优先复用仓库现有 validation/normalization helper；单一 `hasattr(dtype)` 转换不能证明二维容器已覆盖。
- 不可逆转换、unsupported operation 或异常分支不能用固定精度、固定默认值或宽泛 except 吞掉；除非公开契约明确规定常量，否则从邻接边界/原算法推导结果，并保持精确输出。
- 删除序列元素、闭合点、缓存写入或状态更新前，必须证明消费者的 shape、cardinality、closure、ordering 和 lifecycle 不变量仍成立；“不再报错”不等于语义正确。
- 多个模板变量或集合分别表示 all/filtered/imported/local members 时，复用现有过滤 helper 和既有语义；不要假设它们应共享同一过滤结果。
- 禁止修改 tests、fixtures、hidden/evaluation 期望文件。
- 如果 EditBrief 不足，输出 blocked_missing_edit_brief 和唯一缺失事实；否则必须留下非空 diff。
- 最终输出 repair_brief、changed_files、semantic_coverage、old_behavior_invariants_preserved、known_risks。
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
不要提出新的整体方案，不要因为文件知名或路径高扇出就拒绝；必须指出 diff 中的具体阻断证据。
只输出 JSON：
{"verdict":"accept|risk","risk_type":"none|wrong_layer|overgeneralization|exact_contract|shared_state|public_api|coverage_gap","blocking_reason":"具体证据或空字符串","affected_behavior":"由公开代码推断的旧行为或空字符串"}
"""


WORKER_SPECS: dict[str, WorkerSpec] = {
    "research": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT, max_steps=4),
    "explore": WorkerSpec("research", "只读事实收集", RESEARCH_TOOLS, RESEARCH_PROMPT, max_steps=4),
    "reproducer": WorkerSpec("reproducer", "公开复现与差分验证契约", REPRODUCER_TOOLS, REPRODUCER_PROMPT, max_steps=5),
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
    "memory_curator": WorkerSpec("memory_curator", "长期记忆维护", MEMORY_TOOLS, MEMORY_CURATOR_PROMPT, max_steps=2),
    "case_curator": WorkerSpec("case_curator", "成功修复案例抽象", MEMORY_TOOLS, CASE_CURATOR_PROMPT, max_steps=5),
    "general": WorkerSpec("general", "通用独立任务", None, GENERAL_PROMPT, max_steps=4, can_write=True),
}


SWEBENCH_WORKER_PROMPTS = {
    "research": SWEBENCH_RESEARCH_PROMPT,
    "reproducer": SWEBENCH_REPRODUCER_PROMPT,
    "implement": SWEBENCH_IMPLEMENT_PROMPT,
    "patch_ranker": SWEBENCH_PATCH_RANKER_PROMPT,
    "patch_review": SWEBENCH_PATCH_REVIEW_PROMPT,
    "verify": SWEBENCH_VERIFY_PROMPT,
}


SWEBENCH_WORKER_MAX_STEPS = {
    "research": 3,
    "reproducer": 6,
    "implement": 10,
    "patch_ranker": 2,
    "patch_review": 3,
    "verify": 3,
}


class SubAgentRunner:
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

    def run(
        self,
        agent_type: str,
        prompt: str,
        description: str = "sub-agent task",
        constraints: WorkerConstraints | None = None,
        verification_plan: dict[str, Any] | None = None,
    ) -> WorkerResult:
        spec = self._worker_spec(agent_type)
        constraints = constraints or WorkerConstraints()
        verification_execution: dict[str, Any] = {}
        worker_tools = self._tools_for(spec, constraints, verification_plan, verification_execution)
        worker_llm = self.llm_overrides.get(spec.name, self.llm)
        worker_output_dir = self.output_dir / "subagents" / f"{int(time.time() * 1000)}-{_safe_name(spec.name)}"
        before_changed_files = _git_changed_files(self.cwd) if spec.name == "implement" else ()
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
            # SWE-bench 中 Reproducer 只提供软证据；格式或验证命令失败不能阻断 Implement。
            completion_guard=None,
            completion_guard_extra_turns=0,
            force_completion_after_max_steps=(
                self.swebench_profile and spec.name == "reproducer"
            ),
        )
        text = engine.chat(_worker_prompt(description, prompt, constraints if spec.name == "implement" else None))
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
        return replace(spec, system_prompt=prompt, max_steps=SWEBENCH_WORKER_MAX_STEPS.get(spec.name, spec.max_steps))

    def _tools_for(
        self,
        spec: WorkerSpec,
        constraints: WorkerConstraints | None = None,
        verification_plan: dict[str, Any] | None = None,
        verification_execution: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        if spec.name == "implement" and constraints and constraints.has_scope:
            return self._scoped_implement_tools(constraints)
        if spec.name == "patch_review":
            return self._patch_review_tools()
        if self.swebench_profile and spec.name == "reproducer":
            return self._swebench_reproducer_tools()
        if self.swebench_profile and spec.name == "verify":
            return self._swebench_verify_tools(verification_plan, verification_execution)
        if spec.allowed_tools is None:
            return self.base_tools.excluding({"agent"})
        return self.base_tools.subset(spec.allowed_tools)

    def _swebench_reproducer_tools(self) -> ToolRegistry:
        """限制复现 Worker 的探索预算，避免它退化成重复的 Research。"""
        registry = ToolRegistry()
        limits = {"search_repo": 1, "read_file": 3, "run_command": 1}
        counts = {name: 0 for name in limits}
        total_calls = {"value": 0}

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
                counts[tool_name] += 1
                total_calls["value"] += 1
                if tool_name != "run_command":
                    return tool.run_with_timing(inp)
                return self._run_swebench_verification_command(tool, inp)

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
        registry = ToolRegistry()
        read_tool = self.base_tools.get("read_file")
        write_tool = self.base_tools.get("write_file")

        def scoped_read_file(inp: dict) -> ToolResponse:
            denial = _check_scoped_read(inp, constraints)
            if denial:
                return denial
            if read_tool is None:
                return ToolResponse.failure("TOOL_NOT_FOUND", "Base read_file tool is not available.")
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
                    start, end, segment = _slice_for_line_span(content, line_start, line_end)
                except ValueError as exc:
                    return ToolResponse.failure("BAD_LINE_RANGE", str(exc))
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
                if not any(line_range.contains(line_start, line_end) for line_range in allowed_ranges):
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
                        "Implement Worker 只能 read_file 读取 allowed_ranges 覆盖的行号范围；"
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
                        "优先用 line_replacements 按行替换；没有稳定行号时再用 old/new replacements。"
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


def _reproducer_completion_guard(answer: str, prompt: str) -> str | None:
    problem_statement = extract_public_problem_statement(prompt)
    evidence = normalize_semantic_evidence(extract_json_object(answer), problem_statement)
    issues = implementation_semantic_issues(validate_semantic_evidence(evidence, problem_statement))
    if not issues:
        return None
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
    rendered = "\n".join(f"- {issue}" for issue in issues[:8])
    return (
        "Return corrected SemanticEvidence JSON now. Do not call any more tools and do not add Markdown. "
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
    denial = _check_allowed_file(path, constraints)
    if denial:
        return denial
    allowed_ranges = (constraints.allowed_ranges or {}).get(path)
    if not allowed_ranges:
        return ToolResponse.failure(
            "IMPLEMENT_SCOPE_DENIED",
            f"read_file for implement requires allowed_ranges for {path}. Ask Coordinator for a research Worker.",
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
            f"Read range {path}:{start}-{end} is outside allowed_ranges {_format_ranges({path: allowed_ranges})[path]}.",
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
