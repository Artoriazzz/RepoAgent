from __future__ import annotations

import hashlib
import json
import math
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
MAX_SEMANTIC_INSIGHTS_PER_ROLE = 15


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    REPAIR_RECIPE = "repair_recipe"
    REPAIR_EPISODE = "repair_episode"
    SEMANTIC_INSIGHT = "semantic_insight"
    GLOBAL_EXPERIENCE = "global_experience"
    REPOSITORY_EXPERIENCE = "repository_experience"


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    path: Path
    name: str
    description: str
    type: MemoryType
    mtime: float
    role: str = ""
    provenance: str = ""


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
    """基于 Markdown 文件的长期记忆仓库。

    这一层只负责持久化、建立索引和检索候选记忆，不判断记忆是否正确、
    是否适用于当前任务。可信度由写入前的独立验证保证，适用性由上层 Agent 判断。
    """

    def __init__(self, memory_dir: Path, global_memory_dir: Path | None = None):
        self.memory_dir = memory_dir
        # 直接构造 MemoryStore 时保持单目录兼容；按 project/scope 构造时，
        # global experience 会落到所有仓库共享的目录。
        self.global_memory_dir = global_memory_dir or memory_dir

    @classmethod
    def for_project(cls, cwd: str | Path, root: str | Path | None = None) -> "MemoryStore":
        # 交互式本地仓库使用真实路径隔离，避免不同项目之间互相污染记忆。
        project_hash = hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:16]
        base = Path(root or os.environ.get("REPOAGENT_HOME", Path.home() / ".repoagent"))
        return cls(
            base / "projects" / project_hash / "memory",
            base / "global" / "memory",
        )

    @classmethod
    def for_scope(cls, scope: str, root: str | Path | None = None) -> "MemoryStore":
        """按稳定逻辑 scope 共享记忆，例如 `repo:astropy/astropy`。

        SWE-bench 这类实验会为每个 instance 创建不同 checkout 路径，不能用路径 hash
        作为长期记忆边界；scope 让同一仓库或同一实验组可以复用抽象 repair recipe。
        """
        scope_key = _slugify(scope)
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:8]
        base = Path(root or os.environ.get("REPOAGENT_HOME", Path.home() / ".repoagent"))
        return cls(
            base / "scopes" / f"{scope_key}-{scope_hash}" / "memory",
            base / "global" / "memory",
        )

    def save_memory(
        self,
        name: str,
        description: str,
        type: str,
        content: str,
        *,
        role: str = "",
        provenance: str = "",
    ) -> str:
        memory_type = MemoryType(type)
        target_dir = (
            self.global_memory_dir
            if memory_type == MemoryType.GLOBAL_EXPERIENCE
            else self.memory_dir
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{memory_type.value}_{_slugify(name)}.md"
        path = target_dir / filename
        path.write_text(
            format_frontmatter(
                {
                    "name": name,
                    "description": description,
                    "type": memory_type.value,
                    "created_at": _now_iso(),
                    **({"role": role} if role else {}),
                    **({"provenance": provenance} if provenance else {}),
                },
                content,
            ),
            encoding="utf-8",
        )
        self.update_index()
        return filename

    def update_memory(
        self,
        filename: str,
        *,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
        role: str | None = None,
        provenance: str | None = None,
    ) -> str:
        path = self._resolve_memory_path(filename)
        parsed = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        meta = dict(parsed.meta)
        if name is not None:
            meta["name"] = name
        if description is not None:
            meta["description"] = description
        if role is not None:
            meta["role"] = role
        if provenance is not None:
            meta["provenance"] = provenance
        if content is None:
            content = parsed.body
        meta["updated_at"] = _now_iso()
        path.write_text(format_frontmatter(meta, content), encoding="utf-8")
        self.update_index()
        return path.name

    def delete_memory(self, filename: str) -> str:
        path = self._resolve_memory_path(filename)
        path.unlink()
        self.update_index()
        return path.name

    def similar_repair_recipes(self, name: str, description: str, limit: int = 3) -> list[MemoryHeader]:
        query_terms = _similarity_terms(f"{name} {description}")
        if not query_terms:
            return []
        scored: list[tuple[int, float, MemoryHeader]] = []
        for header in self.list_memories():
            if header.type != MemoryType.REPAIR_RECIPE:
                continue
            haystack = f"{header.filename} {header.name} {header.description}".lower()
            score = sum(1 for term in query_terms if term in haystack)
            threshold = 2 if len(query_terms) <= 4 else 3
            if score >= threshold:
                scored.append((score, header.mtime, header))
        return [header for _, _, header in sorted(scored, key=lambda item: (-item[0], -item[1]))[:limit]]

    def similar_memories(
        self,
        name: str,
        description: str,
        *,
        memory_type: MemoryType,
        role: str = "",
        limit: int = 3,
    ) -> list[MemoryHeader]:
        query_terms = _similarity_terms(f"{name} {description}")
        if not query_terms:
            return []
        scored: list[tuple[int, float, MemoryHeader]] = []
        for header in self.list_memories():
            if header.type != memory_type or (role and header.role != role):
                continue
            haystack = f"{header.filename} {header.name} {header.description}".lower()
            score = sum(1 for term in query_terms if term in haystack)
            threshold = 2 if len(query_terms) <= 4 else 3
            if score >= threshold:
                scored.append((score, header.mtime, header))
        return [header for _, _, header in sorted(scored, key=lambda item: (-item[0], -item[1]))[:limit]]

    def count(self, memory_type: MemoryType, *, role: str = "") -> int:
        return sum(
            1
            for memory in self.list_memories()
            if memory.type == memory_type and (not role or memory.role == role)
        )

    def list_memories(self) -> list[MemoryHeader]:
        memories: list[MemoryHeader] = []
        for memory_dir in self._memory_dirs():
            if not memory_dir.is_dir():
                continue
            for path in sorted(memory_dir.glob("*.md")):
                if path.name == "MEMORY.md":
                    continue
                header = self._read_header(path)
                if header:
                    memories.append(header)
        return memories

    def read_memory(self, filename: str) -> str:
        path = self._resolve_memory_path(filename)
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

    def format_index(self, excluded_types: set[MemoryType] | None = None) -> str:
        lines = ["# Memory Index", ""]
        excluded = excluded_types or set()
        for memory in self.list_memories():
            if memory.type in excluded:
                continue
            role = f", role={memory.role}" if memory.role else ""
            lines.append(
                f"- **[{memory.name}]({memory.filename})** ({memory.type.value}{role}) — {memory.description}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def update_index(self) -> None:
        memories = self.list_memories()
        for memory_dir in self._memory_dirs():
            memory_dir.mkdir(parents=True, exist_ok=True)
            lines = ["# Memory Index", ""]
            for memory in memories:
                if memory.path.parent != memory_dir:
                    continue
                role = f", role={memory.role}" if memory.role else ""
                lines.append(
                    f"- **[{memory.name}]({memory.filename})** ({memory.type.value}{role}) — {memory.description}"
                )
            (memory_dir / "MEMORY.md").write_text(
                "\n".join(lines).rstrip() + "\n", encoding="utf-8"
            )

    def build_prompt_section(self, excluded_types: set[MemoryType] | None = None) -> str:
        index = self.format_index(excluded_types) if excluded_types else self.load_index()
        body = f"""# Memory System

你拥有一个跨会话的文件式长期记忆目录：`{self.memory_dir}`。

## Memory Types
- `user`：用户身份、偏好、知识背景。
- `feedback`：用户对 Agent 行为的纠正或肯定，正文必须包含 Why 和 How to apply。
- `project`：无法从当前仓库推导的项目动态、决策、截止日期；相对日期必须写成绝对日期。
- `reference`：外部系统或资料的位置，例如 CI 面板、部署平台、设计文档 URL。
- `repair_recipe`：成功修复案例抽象出的可复用策略，只记录问题识别、修复思路、验证契约和反模式。
- `repair_episode`：紧凑的成功示例，或“失败输出 → 反馈 → 成功输出”示例；不得包含完整轨迹或 benchmark 答案。
- `semantic_insight`：按 Coordinator / Implement / Review 角色维护的短小、可执行高层经验。
- `global_experience`：跨仓库复用的问题机制、适用条件、通用修复步骤、验证思路和错误做法。
- `repository_experience`：当前仓库的模块/API、调用关系、可复用工具函数、特有契约和公开验证方法。

## What To Save
- 只保存无法通过读取当前仓库、Git 历史或项目指令重新推导的信息。
- 用户明确说“记住”“以后都这样”“我的偏好是”时，优先考虑保存。
- 成功修复后的 `repair_recipe` 必须是抽象策略，不得保存完整 diff、具体 instance、gold patch、hidden test 或一次性文件路径。
- `repair_episode` 只保存问题签名、失败假设、反馈、结构化修改方式和验证证据。
- `semantic_insight` 使用 ADD / EDIT / REMOVE 维护，每个角色最多 {MAX_SEMANTIC_INSIGHTS_PER_ROLE} 条。
- 使用 `save_memory` 保存新长期记忆，使用 `update_memory` 改进已有长期记忆，使用 `list_memories` / `read_memory` / `search_memories` 查看长期记忆。

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
        memory_type: MemoryType | None = None,
        excluded_types: set[MemoryType] | None = None,
    ) -> list[RecalledMemory]:
        """先按类型和去重集合筛选，再排序并加载有限条候选记忆。

        recall 只回答“哪些记忆可能相关”，不能把被召回内容当作当前仓库事实；
        注入 prompt 后仍需用当前任务和源码完成 applicability check。
        """
        already = already_surfaced or set()
        headers = [header for header in self.list_memories() if str(header.path) not in already]
        if memory_type is not None:
            headers = [header for header in headers if header.type == memory_type]
        if excluded_types:
            headers = [header for header in headers if header.type not in excluded_types]
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

    def recall_repair_recipes(self, query: str, limit: int = 3, side_query: SideQueryFn | None = None) -> list[RecalledMemory]:
        return self.recall(query, side_query=side_query, limit=limit, memory_type=MemoryType.REPAIR_RECIPE)

    def recall_repair_episodes(self, query: str, limit: int = 3) -> list[RecalledMemory]:
        return self._recall_bm25(query, MemoryType.REPAIR_EPISODE, limit=limit)

    def recall_semantic_insights(
        self,
        query: str,
        *,
        role: str = "",
        limit: int = MAX_SEMANTIC_INSIGHTS_PER_ROLE,
    ) -> list[RecalledMemory]:
        return self._recall_bm25(query, MemoryType.SEMANTIC_INSIGHT, limit=limit, role=role)

    def recall_experiences(
        self,
        query: str,
        *,
        memory_type: MemoryType,
        limit: int = 1,
    ) -> list[RecalledMemory]:
        if memory_type not in {
            MemoryType.GLOBAL_EXPERIENCE,
            MemoryType.REPOSITORY_EXPERIENCE,
        }:
            raise ValueError("experience recall requires a global or repository experience type")
        return self._recall_bm25(query, memory_type, limit=limit)

    def search(self, query: str, limit: int = MAX_RECALLED_MEMORIES, memory_type: MemoryType | None = None) -> list[MemoryHeader]:
        headers = self.list_memories()
        if memory_type is not None:
            headers = [header for header in headers if header.type == memory_type]
        return _select_by_keywords(query, headers)[:limit]

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
            role=parsed.meta.get("role", ""),
            provenance=parsed.meta.get("provenance", ""),
        )

    def _recall_bm25(
        self,
        query: str,
        memory_type: MemoryType,
        *,
        limit: int,
        role: str = "",
    ) -> list[RecalledMemory]:
        # BM25 同时检索名称、摘要和正文；它是可解释的词法召回，不是语义真值判定。
        headers = [
            header
            for header in self.list_memories()
            if header.type == memory_type and (not role or header.role == role)
        ]
        if not headers or not query.strip():
            return []
        documents: list[str] = []
        usable_headers: list[MemoryHeader] = []
        for header in headers:
            try:
                content = self.read_memory(header.filename)
            except OSError:
                continue
            documents.append(f"{header.name}\n{header.description}\n{content}")
            usable_headers.append(header)
        ranked = _select_by_bm25(query, usable_headers, documents)
        return [
            RecalledMemory(
                header=header,
                content=self.read_memory(header.filename),
                warning=memory_freshness_warning(header.mtime),
            )
            for header in ranked[:limit]
        ]

    def _resolve_memory_path(self, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError(f"Memory path escapes memory directory: {filename}")
        for memory_dir in self._memory_dirs():
            path = (memory_dir / filename).resolve()
            if path.is_file():
                return path
        raise FileNotFoundError(filename)

    def _memory_dirs(self) -> tuple[Path, ...]:
        if self.global_memory_dir == self.memory_dir:
            return (self.memory_dir,)
        return (self.memory_dir, self.global_memory_dir)


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


def _select_by_bm25(query: str, headers: list[MemoryHeader], documents: list[str]) -> list[MemoryHeader]:
    """按错误文本、API 名和行为词做轻量 BM25，避免依赖外部向量服务。"""

    query_terms = list(dict.fromkeys(_bm25_terms(query)))
    tokenized = [_bm25_terms(document) for document in documents]
    if not query_terms or not tokenized:
        return []
    average_length = sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized))
    document_frequency = {
        term: sum(1 for tokens in tokenized if term in tokens)
        for term in query_terms
    }
    k1 = 1.5
    b = 0.75
    scored: list[tuple[float, float, MemoryHeader]] = []
    for header, tokens in zip(headers, tokenized):
        score = 0.0
        token_counts = {term: tokens.count(term) for term in query_terms}
        for term in query_terms:
            frequency = token_counts[term]
            if not frequency:
                continue
            doc_frequency = document_frequency[term]
            inverse_frequency = math.log(
                1 + (len(tokenized) - doc_frequency + 0.5) / (doc_frequency + 0.5)
            )
            length_factor = 1 - b + b * len(tokens) / max(1.0, average_length)
            score += inverse_frequency * frequency * (k1 + 1) / (frequency + k1 * length_factor)
        if score > 0:
            scored.append((score, header.mtime, header))
    return [header for _, _, header in sorted(scored, key=lambda item: (-item[0], -item[1]))]


def _bm25_terms(text: str) -> list[str]:
    terms = [term.lower() for term in re.findall(r"[a-zA-Z0-9_]+", text) if len(term) > 1]
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    terms.extend(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return [term for term in terms if len(term) > 1]


def _query_terms(query: str) -> set[str]:
    terms = {term.lower() for term in re.findall(r"[a-zA-Z0-9_]+", query) if len(term) > 1}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", query))
    terms.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {term for term in terms if len(term) > 1}


_SIMILARITY_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "must",
    "should",
    "usually",
    "require",
    "requires",
    "required",
    "fix",
    "fixes",
    "cover",
    "covers",
    "support",
    "supports",
    "behavior",
    "behaviour",
    "checks",
    "check",
    "consistent",
    "consistently",
    "repair_recipe",
}


def _similarity_terms(query: str) -> set[str]:
    return {
        term
        for term in _query_terms(query)
        if len(term) >= 4 and term not in _SIMILARITY_STOPWORDS
    }


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
