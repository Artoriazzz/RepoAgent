from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from repo_agent.core.frontmatter import format_frontmatter, parse_frontmatter


MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MAX_MEMORY_BYTES_PER_FILE = 4_096
MAX_SESSION_MEMORY_BYTES = 60 * 1024
MAX_RECALLED_MEMORIES = 5


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    path: Path
    name: str
    description: str
    type: MemoryType
    mtime: float


@dataclass(frozen=True)
class RecalledMemory:
    header: MemoryHeader
    content: str
    warning: str = ""

    def injection_text(self) -> str:
        age = memory_age(self.header.mtime)
        freshness = f"{self.warning}\n\n" if self.warning else ""
        return (
            "<system-reminder>\n"
            f"{freshness}Memory (saved {age}): {self.header.path}\n\n"
            f"{self.content}\n"
            "</system-reminder>"
        )


SideQueryFn = Callable[[str, str], str]


class MemoryStore:
    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir

    @classmethod
    def for_project(cls, cwd: str | Path, root: str | Path | None = None) -> "MemoryStore":
        project_hash = hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:16]
        base = Path(root or os.environ.get("REPOAGENT_HOME", Path.home() / ".repoagent"))
        return cls(base / "projects" / project_hash / "memory")

    def save_memory(self, name: str, description: str, type: str, content: str) -> str:
        memory_type = MemoryType(type)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{memory_type.value}_{_slugify(name)}.md"
        path = self.memory_dir / filename
        path.write_text(
            format_frontmatter(
                {
                    "name": name,
                    "description": description,
                    "type": memory_type.value,
                    "created_at": _now_iso(),
                },
                content,
            ),
            encoding="utf-8",
        )
        self.update_index()
        return filename

    def list_memories(self) -> list[MemoryHeader]:
        if not self.memory_dir.is_dir():
            return []
        memories: list[MemoryHeader] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            header = self._read_header(path)
            if header:
                memories.append(header)
        return memories

    def read_memory(self, filename: str) -> str:
        path = (self.memory_dir / filename).resolve()
        try:
            path.relative_to(self.memory_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"Memory path escapes memory directory: {filename}") from exc
        if not path.is_file():
            raise FileNotFoundError(filename)
        return _limit_bytes(path.read_text(encoding="utf-8", errors="replace"), MAX_MEMORY_BYTES_PER_FILE)

    def load_index(self) -> str:
        path = self.memory_dir / "MEMORY.md"
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        if len(lines) > MAX_INDEX_LINES:
            content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
        return _limit_bytes(content, MAX_INDEX_BYTES)

    def update_index(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Memory Index", ""]
        for memory in self.list_memories():
            lines.append(f"- **[{memory.name}]({memory.filename})** ({memory.type.value}) — {memory.description}")
        (self.memory_dir / "MEMORY.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def build_prompt_section(self) -> str:
        index = self.load_index()
        body = f"""# Memory System

你拥有一个跨会话的文件式长期记忆目录：`{self.memory_dir}`。

## Memory Types
- `user`：用户身份、偏好、知识背景。
- `feedback`：用户对 Agent 行为的纠正或肯定，正文必须包含 Why 和 How to apply。
- `project`：无法从当前仓库推导的项目动态、决策、截止日期；相对日期必须写成绝对日期。
- `reference`：外部系统或资料的位置，例如 CI 面板、部署平台、设计文档 URL。

## What To Save
- 只保存无法通过读取当前仓库、Git 历史或项目指令重新推导的信息。
- 用户明确说“记住”“以后都这样”“我的偏好是”时，优先考虑保存。
- 使用 `save_memory` 保存长期记忆，使用 `list_memories` / `read_memory` / `search_memories` 查看长期记忆。

## What NOT To Save
- 不保存代码结构、技术栈、文件路径、测试命令、Git 历史和一次性任务细节。
- 不保存已经写在 REPOAGENT.md / AGENTS.md / CLAUDE.md 里的规则。

## Current Memory Index
{index if index else "(No memories saved yet.)"}"""
        return body

    def recall(
        self,
        query: str,
        already_surfaced: set[str] | None = None,
        side_query: SideQueryFn | None = None,
        limit: int = MAX_RECALLED_MEMORIES,
    ) -> list[RecalledMemory]:
        already = already_surfaced or set()
        headers = [header for header in self.list_memories() if str(header.path) not in already]
        if not headers or not query.strip():
            return []

        selected = _select_with_side_query(query, headers, side_query) if side_query else _select_by_keywords(query, headers)
        recalled: list[RecalledMemory] = []
        for header in selected[:limit]:
            try:
                content = self.read_memory(header.filename)
            except OSError:
                continue
            recalled.append(RecalledMemory(header=header, content=content, warning=memory_freshness_warning(header.mtime)))
        return recalled

    def search(self, query: str, limit: int = MAX_RECALLED_MEMORIES) -> list[MemoryHeader]:
        return _select_by_keywords(query, self.list_memories())[:limit]

    def _read_header(self, path: Path) -> MemoryHeader | None:
        content = path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_frontmatter(content)
        raw_type = parsed.meta.get("type", "")
        try:
            memory_type = MemoryType(raw_type)
        except ValueError:
            return None
        return MemoryHeader(
            filename=path.name,
            path=path,
            name=parsed.meta.get("name", path.stem),
            description=parsed.meta.get("description", ""),
            type=memory_type,
            mtime=path.stat().st_mtime,
        )


def format_memories_for_injection(memories: list[RecalledMemory]) -> str:
    return "\n\n".join(memory.injection_text() for memory in memories)


def memory_freshness_warning(mtime: float) -> str:
    days = int(max(0, (time.time() - mtime) // 86_400))
    if days <= 1:
        return ""
    return (
        f"This memory is {days} days old. Memories are point-in-time observations, "
        "not live state. Verify against current code before asserting as fact."
    )


def memory_age(mtime: float) -> str:
    days = int(max(0, (time.time() - mtime) // 86_400))
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _select_with_side_query(query: str, headers: list[MemoryHeader], side_query: SideQueryFn | None) -> list[MemoryHeader]:
    if side_query is None:
        return []
    manifest = "\n".join(
        f"- {header.filename}: [{header.type.value}] {header.name} — {header.description}" for header in headers
    )
    system = (
        "You select memories useful to a repository coding agent. "
        "Return JSON: {\"selected_memories\": [\"filename.md\"]}. "
        "Only include clearly useful memories, up to 5."
    )
    user = f"Query: {query}\n\nAvailable memories:\n{manifest}"
    try:
        text = side_query(system, user)
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        selected = set(parsed.get("selected_memories") or [])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
    return [header for header in headers if header.filename in selected]


def _select_by_keywords(query: str, headers: list[MemoryHeader]) -> list[MemoryHeader]:
    terms = _query_terms(query)
    if not terms:
        return []
    scored: list[tuple[int, float, MemoryHeader]] = []
    for header in headers:
        haystack = f"{header.filename} {header.name} {header.description} {header.type.value}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, header.mtime, header))
    return [header for _, _, header in sorted(scored, key=lambda item: (-item[0], -item[1]))]


def _query_terms(query: str) -> set[str]:
    terms = {term.lower() for term in re.findall(r"[a-zA-Z0-9_]+", query) if len(term) > 1}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", query))
    terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {term for term in terms if len(term) > 1}


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", value.strip().lower()).strip("_")
    return normalized[:80] or "memory"


def _limit_bytes(content: str, max_bytes: int) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    keep = max(1, (max_bytes - 80) // 2)
    prefix = encoded[:keep].decode("utf-8", errors="ignore")
    suffix = encoded[-keep:].decode("utf-8", errors="ignore")
    omitted = len(encoded) - keep * 2
    return f"{prefix}\n\n[... truncated {omitted} bytes ...]\n\n{suffix}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
