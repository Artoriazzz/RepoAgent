"""失败反馈需要新文件时，由只读协调阶段确认并授权范围。"""

from pathlib import Path

from repo_agent.core.semantic_evidence import extract_json_object
from repo_agent.core.subagent import WorkerConstraints
from repo_agent.core.experience import required_edit_scope_issues

REPLAN_PROMPT = """你负责根据已验证失败反馈重新定位并决定局部修复，不修改代码。
你只有当前任务、失败 patch、失败归因和本次源码读取结果，没有其他会话上下文。
使用最多 5 次精确生产源码 search/read；不要读取测试、环境文件、历史或记忆。
先读取反馈指出的新文件和直接调用方，确认错误契约及相邻旧行为。
你负责决定修法：不能把关键假设或方案选择交给实现阶段。
日志输入未知时不能把 issue 示例当作实际失败输入。单个输入推演正确不证明整个函数正确；先处理已读源码中的相关反例，不重复成功示例或搜索重复定义来证明环境错误。复制/切片后的检查是否仍观察原对象、yield 恢复后是否会直接结束，均按实际代码核对；确认违反题意的路径即可决定修正，不要求猜中隐藏输入。保留旧行为是保持合法输入的输出，不是冻结有缺陷的分支。没有模块加载、依赖或执行故障的直接证据，不把定位转为环境排查。
算法缺失定义时指出具体数学事实，不请求隐藏参考补丁或上游合并提交，也不从少数失败数值猜新公式。
若直接源码不支持反馈，或关键契约仍未知，输出 blocked 和具体原因。
只有已读取源码支持精确修改时才能 approved。最多授权 4 个生产文件，每段最多 600 行，总计最多 1500 行。
edit_brief 必须包含 problem_contract、observed_facts（路径和行号）、required_changes（证据引用）、old_behavior_invariants 和 scope。
scope 仅授权确定的修改点和必要只读上下文；不要为了避免范围限制授权整文件。
required_edits 列出本轮确定方案的全部必要修改位置（当前源码行号）；若改变调用参数，核对直接调用方及子类重写。全部必要位置必须被 allowed_ranges 覆盖，否则 blocked，不能只授权半套方案。
历史 patch 已在工作区保留；allowed_files 只列本轮实际要编辑的文件，不是最终累计 patch 的文件名单。保留旧改动无需再次授权；只有决定修改或撤回旧改动时，才先读取旧文件并说明理由后重新授权。
只输出 JSON：
{"status":"approved|blocked","reason":"证据结论","edit_brief":"完整自包含的确定修改指令",
 "required_edits":[{"path":"path","start_line":1,"end_line":20,"change":"必要修改"}],
 "allowed_files":["path"],"allowed_ranges":{"path":[[1,20]]},"context_ranges":{}}
"""


def validate_replan(text: str, repo: Path, read_files: set[str]) -> str | None:
    data = extract_json_object(text)
    if not isinstance(data, dict) or data.get("status") not in {"approved", "blocked"}:
        return "Return one JSON object with status=approved or blocked."
    if data["status"] == "blocked":
        return None if data.get("reason") else "Explain the missing source fact in reason."
    if not isinstance(data.get("edit_brief"), str) or not data["edit_brief"].strip():
        return "approved requires a self-contained edit_brief."
    try:
        constraints = WorkerConstraints.from_tool_input(data)
    except (ValueError, TypeError, KeyError):
        return "Return valid allowed_files and line ranges."
    if not constraints.allowed_files or len(constraints.allowed_files) > 4:
        return "Authorize 1-4 production files."
    total = 0
    for name, ranges in (constraints.readable_ranges or {}).items():
        path = (repo / name).resolve()
        parts = Path(name).parts
        if not path.is_relative_to(repo.resolve()) or not path.is_file() or any(
            part in {"tests", "fixtures", ".git"} or part.startswith("test_") for part in parts
        ) or path.name.startswith(".env"):
            return "Only existing production source files inside the repository are allowed."
        if name in constraints.allowed_files:
            if name not in read_files:
                return f"Read {name} before authorizing edits."
            if name not in (constraints.allowed_ranges or {}):
                return f"Missing editable ranges for {name}."
        lines = len(path.read_text(errors="replace").splitlines())
        for span in ranges:
            if span.start < 1 or span.end > lines or span.end - span.start + 1 > 600:
                return "Use valid narrow source line ranges."
            total += span.end - span.start + 1
    if set(constraints.allowed_files) - set(constraints.allowed_ranges or {}):
        return "Every editable file needs a line range."
    if total > 1500:
        return "Narrow total source scope to at most 1500 lines."
    if required_edit_scope_issues(data.get("required_edits") or [], data):
        return "Authorize every required edit location, or return blocked."
    return None
