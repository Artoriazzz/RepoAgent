from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repo_agent.core.session_memory import ShortTermMemory
from repo_agent.core.tool import ToolResponse, ToolStatus


PERSIST_LARGE_RESULT_BYTES = 30 * 1024
MAX_RESULT_CHARS = 50_000
MICROCOMPACT_IDLE_S = 5 * 60
KEEP_RECENT_RESULTS = 3
SNIPPABLE_TOOLS = {"read_file", "search_repo", "list_files", "run_command", "download_workflow_logs"}
SNIP_PLACEHOLDER = "[Content snipped - re-run the tool if needed]"
OLD_RESULT_PLACEHOLDER = "[Old tool result cleared - re-run the tool if needed]"


@dataclass
class ContextConfig:
    effective_window: int = 128_000
    enable_auto_compact: bool = True


class ConversationContextManager:
    """对消息历史执行确定性的分层上下文治理。

    顺序从低损失到高损失：限制单条结果、移除重复旧 observation、空闲期清理，最后
    才用 ShortTermMemory 重建会话摘要。这里不调用 LLM 总结，行为可预测且没有额外费用。
    """

    def __init__(self, output_dir: Path | str, config: ContextConfig | None = None):
        self.output_dir = Path(output_dir)
        self.config = config or ContextConfig()
        self.last_input_token_count = 0
        self.last_api_call_time = 0.0
        self.compaction_failures = 0

    def before_model_call(self, messages: list[dict[str, Any]]) -> None:
        # 使用上一次模型调用报告的输入 token 估计当前压力，因此这是近似预算而非精确
        # tokenizer 预计算。每次请求前按从轻到重的顺序处理历史工具结果。
        self._budget_tool_results(messages)
        self._snip_stale_results(messages)
        self._microcompact(messages)

    def after_model_call(self, prompt_tokens: int) -> None:
        self.last_input_token_count = prompt_tokens
        self.last_api_call_time = time.time()

    def maybe_auto_compact(self, messages: list[dict[str, Any]], short_memory: ShortTermMemory) -> None:
        if not self.config.enable_auto_compact or self.compaction_failures >= 3:
            return
        if self.last_input_token_count <= self.config.effective_window * 0.85:
            return
        if len(messages) < 6:
            return

        try:
            # system prompt 保留长期规则，短期记忆保留任务状态，末尾消息保留最近交互。
            # 压缩会损失原始细节，所以只有窗口利用率超过 85% 时才触发。
            system_message = messages[0]
            trailing = messages[-2:]
            summary = short_memory.compact_summary()
            messages[:] = [
                system_message,
                {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
                {"role": "assistant", "content": "我已保留前文摘要和当前任务状态，可以继续。"},
                *trailing,
            ]
            self.last_input_token_count = 0
        except Exception:
            self.compaction_failures += 1

    def process_tool_response(self, tool_name: str, response: ToolResponse) -> ToolResponse:
        # 完整结果先落盘，再把可定位的路径和预览送回模型。这样既保留可追溯证据，
        # 又避免一次日志/搜索结果占满后续所有上下文。
        text = response.text
        if len(text.encode("utf-8")) > PERSIST_LARGE_RESULT_BYTES:
            response = self._persist_large_result(tool_name, response)
        if len(response.text) > MAX_RESULT_CHARS:
            original_len = len(response.text)
            response.text = _truncate_text(response.text, MAX_RESULT_CHARS, "truncated")
            response.data["truncated"] = True
            response.stats["truncated_chars"] = original_len - len(response.text)
            if response.status == ToolStatus.SUCCESS:
                response.status = ToolStatus.PARTIAL
        return response

    def _persist_large_result(self, tool_name: str, response: ToolResponse) -> ToolResponse:
        result_dir = self.output_dir / "tool-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{_safe_name(tool_name)}.txt"
        path = result_dir / filename
        path.write_text(response.text, encoding="utf-8")

        lines = response.text.splitlines()
        preview = "\n".join(lines[:200])
        size_kb = len(response.text.encode("utf-8")) / 1024
        response.text = (
            f"[Result too large: {size_kb:.1f} KB, {len(lines)} lines. "
            f"Full output saved to {path}. Use read_file if you need the full result.]\n\n"
            f"Preview (first 200 lines):\n{preview}"
        )
        response.data["full_result_path"] = str(path)
        response.data["persisted"] = True
        if response.status == ToolStatus.SUCCESS:
            response.status = ToolStatus.PARTIAL
        return response

    def _budget_tool_results(self, messages: list[dict[str, Any]]) -> None:
        utilization = self.last_input_token_count / self.config.effective_window if self.config.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15_000 if utilization > 0.70 else 30_000
        for message in messages:
            if message.get("role") != "tool":
                continue
            payload = _parse_tool_payload(message.get("content"))
            content = str(payload.get("text") or message.get("content") or "")
            if len(content) <= budget:
                continue
            payload["text"] = _truncate_text(content, budget, "budgeted")
            payload.setdefault("data", {})["context_budgeted"] = True
            message["content"] = json.dumps(payload, ensure_ascii=False, indent=2)

    def _snip_stale_results(self, messages: list[dict[str, Any]]) -> None:
        utilization = self.last_input_token_count / self.config.effective_window if self.config.effective_window else 0
        if utilization < 0.6:
            return
        tool_indexes = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
        keep = set(tool_indexes[-KEEP_RECENT_RESULTS:])
        seen_reads: set[str] = set()
        search_count = 0
        for index in reversed(tool_indexes):
            if index in keep:
                continue
            payload = _parse_tool_payload(messages[index].get("content"))
            tool_name = payload.get("tool") or payload.get("data", {}).get("tool")
            if tool_name not in SNIPPABLE_TOOLS:
                continue
            should_snip = False
            if tool_name == "read_file":
                path = str(payload.get("data", {}).get("path") or "")
                if path in seen_reads:
                    should_snip = True
                elif path:
                    seen_reads.add(path)
            elif tool_name == "search_repo":
                search_count += 1
                should_snip = search_count > 3
            else:
                should_snip = True
            if should_snip:
                payload["text"] = SNIP_PLACEHOLDER
                payload.setdefault("data", {})["snipped"] = True
                messages[index]["content"] = json.dumps(payload, ensure_ascii=False, indent=2)

    def _microcompact(self, messages: list[dict[str, Any]]) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_indexes = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
        for index in tool_indexes[:-KEEP_RECENT_RESULTS]:
            payload = _parse_tool_payload(messages[index].get("content"))
            payload["text"] = OLD_RESULT_PLACEHOLDER
            payload.setdefault("data", {})["microcompacted"] = True
            messages[index]["content"] = json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {"text": content, "data": {}}
        except json.JSONDecodeError:
            return {"text": content, "data": {}}
    return {"text": str(content), "data": {}}


def _truncate_text(text: str, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    keep_each = max(1, (max_chars - 80) // 2)
    omitted = len(text) - keep_each * 2
    return f"{text[:keep_each]}\n\n[... {label}: {omitted} chars omitted ...]\n\n{text[-keep_each:]}"


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80] or "tool"
