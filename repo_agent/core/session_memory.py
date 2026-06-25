from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAX_SUMMARY_CHARS = 1_200


@dataclass
class ShortTermMemory:
    """当前 run 内的具体工作记忆。"""

    user_goal: str = ""
    current_plan: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    repo_path: str = ""
    branch: str = ""
    initial_status: str = ""
    files_read: list[dict[str, Any]] = field(default_factory=list)
    files_edited: list[dict[str, Any]] = field(default_factory=list)
    searches: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    validation_commands: list[dict[str, Any]] = field(default_factory=list)
    latest_validation_result: str = ""
    sensitive_paths_skipped: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    requires_user_confirmation: list[str] = field(default_factory=list)
    next_action: str = ""
    blocking_reason: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def start_turn(self, user_message: str) -> None:
        if not self.user_goal:
            self.user_goal = user_message
        self.next_action = "继续根据最新用户请求推进任务"
        self.updated_at = time.time()

    def handle_event(self, event: Any) -> None:
        event_type = getattr(getattr(event, "type", None), "value", str(getattr(event, "type", "")))
        if event_type == "tool_call":
            self._record_tool_call(event.payload)
        elif event_type == "tool_result":
            self._record_tool_result(event.payload)
        elif event_type == "finish":
            answer = str(event.payload.get("answer") or "")
            if answer:
                self.done.append(_limit(answer, 300))
                self.next_action = "任务已结束，等待用户下一步指令"
        self.updated_at = time.time()

    def save(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "short_memory.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "short_memory.md").write_text(self.to_markdown(), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": {
                "user_goal": self.user_goal,
                "current_plan": self.current_plan,
                "done": self.done[-20:],
                "open_questions": self.open_questions[-20:],
            },
            "repo_context": {
                "repo_path": self.repo_path,
                "branch": self.branch,
                "initial_status": self.initial_status,
            },
            "files": {
                "read": self.files_read[-50:],
                "edited": self.files_edited[-50:],
            },
            "evidence": {
                "searches": self.searches[-50:],
                "diagnostics": self.diagnostics[-50:],
            },
            "validation": {
                "commands": self.validation_commands[-50:],
                "latest_result": self.latest_validation_result,
            },
            "risk": {
                "sensitive_paths_skipped": self.sensitive_paths_skipped[-20:],
                "blocked_actions": self.blocked_actions[-20:],
                "requires_user_confirmation": self.requires_user_confirmation[-20:],
            },
            "handoff": {
                "next_action": self.next_action,
                "blocking_reason": self.blocking_reason,
            },
            "timestamps": {
                "started_at": self.started_at,
                "updated_at": self.updated_at,
            },
        }

    def to_markdown(self) -> str:
        data = self.to_dict()
        return (
            "# Short-term Memory\n\n"
            f"## Task\n- Goal: {data['task']['user_goal']}\n"
            f"- Next action: {data['handoff']['next_action']}\n"
            f"- Blocking reason: {data['handoff']['blocking_reason'] or '(none)'}\n\n"
            f"## Files Read\n{_bullets(self.files_read[-10:], 'path', 'summary')}\n\n"
            f"## Files Edited\n{_bullets(self.files_edited[-10:], 'path', 'change_summary')}\n\n"
            f"## Validation\n{_bullets(self.validation_commands[-10:], 'command', 'summary')}\n"
        )

    def compact_summary(self) -> str:
        return _limit(self.to_markdown(), 4_000)

    def _record_tool_call(self, payload: dict[str, Any]) -> None:
        tool = payload.get("tool")
        arguments = _parse_arguments(payload.get("arguments"))
        if tool in {"write_file", "apply_patch"}:
            path = arguments.get("path") or _first_patch_path(arguments.get("patch", ""))
            self.next_action = f"观察 {tool} 结果，确认 {path or '补丁'} 是否成功"
        elif tool == "run_command":
            self.next_action = f"等待验证命令结果：{arguments.get('command', '')}"

    def _record_tool_result(self, payload: dict[str, Any]) -> None:
        tool = str(payload.get("tool") or "")
        response = payload.get("response") or {}
        text = str(response.get("text") or "")
        data = response.get("data") or {}
        status = str(response.get("status") or "")

        if status == "error":
            error = response.get("error") or {}
            code = error.get("code") or "TOOL_ERROR"
            self.diagnostics.append({"source": tool, "summary": _limit(f"{code}: {text}", 500)})
            if code in {"PERMISSION_DENIED", "READ_REQUIRED", "STALE_READ", "UNSAFE_PATCH"}:
                self.blocked_actions.append(_limit(f"{tool}: {text}", 300))

        if tool == "read_file":
            path = data.get("path") or _path_from_text(text)
            self._upsert(self.files_read, "path", path, {"path": path, "summary": _summarize_text(text)})
        elif tool in {"write_file", "apply_patch"}:
            paths = data.get("paths") if isinstance(data.get("paths"), list) else []
            path = data.get("path") or (paths[0] if paths else "") or _first_patch_path(data.get("patch", "")) or _path_from_text(text)
            self.files_edited.append(
                {"path": path or tool, "reason": f"{tool} succeeded" if status != "error" else f"{tool} failed", "change_summary": _limit(text, 500)}
            )
        elif tool == "search_repo":
            self.searches.append({"query": data.get("query", ""), "result_summary": _summarize_text(text)})
        elif tool == "run_command":
            exit_code = data.get("exit_code")
            command = data.get("command") or _command_from_output(text)
            summary = _summarize_text(text)
            self.validation_commands.append({"command": command, "exit_code": exit_code, "summary": summary})
            self.latest_validation_result = f"{command}: exit_code={exit_code}; {summary}"
        elif tool in {"get_issue", "download_workflow_logs", "get_repo_metadata"}:
            self.diagnostics.append({"source": tool, "summary": _summarize_text(text)})
        elif tool == "finish":
            self.done.append(_limit(text, 400))

    @staticmethod
    def _upsert(items: list[dict[str, Any]], key: str, value: Any, item: dict[str, Any]) -> None:
        if not value:
            items.append(item)
            return
        for index, existing in enumerate(items):
            if existing.get(key) == value:
                items[index] = item
                return
        items.append(item)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _summarize_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return _limit(cleaned, MAX_SUMMARY_CHARS)


def _limit(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(1, (max_chars - 40) // 2)
    return f"{text[:keep]} [... {len(text) - keep * 2} chars ...] {text[-keep:]}"


def _bullets(items: list[dict[str, Any]], key: str, value: str) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item.get(key, '')}: {item.get(value, '')}" for item in items)


def _path_from_text(text: str) -> str:
    match = re.search(r"([\w./-]+\.[\w-]+)", text)
    return match.group(1) if match else ""


def _first_patch_path(patch: str) -> str:
    for line in str(patch).splitlines():
        if line.startswith("+++ b/"):
            return line[6:]
    return ""


def _command_from_output(text: str) -> str:
    return "command" if text else ""
