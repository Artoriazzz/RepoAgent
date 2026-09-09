from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from repo_agent.core.tool import Tool, ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool


SENSITIVE_NAMES = {".env", ".env.local", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
LIST_FILES_LIMIT = 120
READ_FILE_RESULT_LIMIT = 12_000
SEARCH_RESULT_LIMIT = 10_000
COMMAND_RESULT_LIMIT = 10_000
DIFF_RESULT_LIMIT = 16_000


@dataclass
class RepoWorkspace:
    """仓库工具共享的安全边界和运行状态。

    它负责路径限制、敏感文件拒绝、读后写 freshness 和修改文件数量限制。需要注意：
    shell 命令黑名单只是应用层护栏，不能代替 Docker/容器或低权限 OS 用户隔离。
    """

    repo_path: Path
    output_dir: Path
    max_modified_files: int = 20
    read_file_mtimes: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo_path = self.repo_path.resolve()
        self.output_dir = self.output_dir.resolve()
        if not self.repo_path.exists():
            raise ValueError(f"Repo path does not exist: {self.repo_path}")

    def resolve(self, path: str) -> Path:
        # 先 resolve 再 relative_to，可同时拦截 ../ 越界和指向仓库外部的符号链接。
        candidate = (self.repo_path / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            candidate.relative_to(self.repo_path)
        except ValueError as exc:
            raise ValueError(f"Path escapes repository: {path}") from exc
        if set(candidate.parts) & SENSITIVE_NAMES:
            raise ValueError(f"Refusing sensitive path: {path}")
        return candidate

    def remember_read(self, path: Path) -> None:
        if path.exists():
            self.read_file_mtimes[str(path)] = path.stat().st_mtime_ns

    def require_fresh_read(self, path: Path, operation: str) -> ToolResponse | None:
        # 这是乐观并发控制：read_file 记录版本（mtime），写入时确认期间没有外部变化。
        # 它避免 Agent 根据旧上下文覆盖用户的新修改，但不提供数据库式事务或自动回滚。
        if not path.exists():
            return None
        remembered = self.read_file_mtimes.get(str(path))
        if remembered is None:
            return ToolResponse.failure(
                "READ_REQUIRED",
                f"{operation} requires reading existing file first: {path.relative_to(self.repo_path)}",
            )
        current = path.stat().st_mtime_ns
        if current != remembered:
            return ToolResponse.failure(
                "STALE_READ",
                f"File changed after read, please call read_file again: {path.relative_to(self.repo_path)}",
            )
        return None

    def run(self, command: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        self.ensure_safe_command(command)
        return subprocess.run(
            command,
            cwd=self.repo_path,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )

    def ensure_safe_command(self, command: str) -> None:
        # 这里有意只做高风险模式拦截。基于字符串的 denylist 无法证明任意 shell 命令
        # 安全，生产部署还必须依靠容器、低权限用户、网络策略和资源限制。
        lowered = command.strip().lower()
        if lowered in {"python", "python3", "bash", "sh", "vim", "vi", "nano"}:
            raise ValueError(f"Interactive command is blocked: {command}")
        if re.search(r"(^|\s)(cat|less|more|sed|awk|grep|rg)\b.*(^|/)\.env(\.|$|\s)", lowered):
            raise ValueError("Refusing to read .env-style files.")
        if re.search(r"\bgit\s+push\b", lowered) and (
            re.search(r"\b(main|master)\b", lowered) or "--force" in lowered or "-f" in lowered.split()
        ):
            raise ValueError("Refusing direct push to protected/default branch.")
        # `\brm` 会误匹配 Docker 的 `--rm`；这里只接受独立 shell token。
        if re.search(r"(?:^|[\s;&|])rm\s+-[^\n]*r", lowered):
            raise ValueError("Recursive deletion is blocked.")

    def changed_files(self) -> list[str]:
        result = self.run("git diff --name-only", timeout=20)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def enforce_change_limit(self) -> None:
        files = self.changed_files()
        if len(files) > self.max_modified_files:
            raise ValueError(f"Too many modified files: {len(files)} > {self.max_modified_files}")


def build_repo_tools(workspace: RepoWorkspace) -> list[Tool]:
    """构造绑定到同一个 RepoWorkspace 的仓库工具集合。

    闭包共享 read_file_mtimes 和修改状态，因此 read_file 与 write/apply_patch 之间能够
    执行 freshness 检查；换一个 Workspace 就会得到完全隔离的工具状态。
    """

    def finish(inp: dict) -> ToolResponse:
        answer = inp["answer"]
        patch = _git_diff(workspace)
        workspace.output_dir.mkdir(parents=True, exist_ok=True)
        (workspace.output_dir / "model.patch").write_text(patch, encoding="utf-8")
        validation = {
            "answer": answer,
            "edited_files": workspace.changed_files(),
            "patch_saved": str(workspace.output_dir / "model.patch"),
        }
        (workspace.output_dir / "validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return ToolResponse.success(answer, validation)

    def list_files(inp: dict) -> ToolResponse:
        max_files = int(inp.get("max_files") or LIST_FILES_LIMIT)
        files: list[str] = []
        for path in workspace.repo_path.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                files.append(path.relative_to(workspace.repo_path).as_posix())
            if len(files) >= max_files:
                return ToolResponse.partial("\n".join(files), {"truncated": True, "files": files})
        return ToolResponse.success("\n".join(files), {"files": files})

    def read_file(inp: dict) -> ToolResponse:
        path = inp["path"]
        target = workspace.resolve(path)
        content = target.read_text(encoding="utf-8", errors="replace")
        workspace.remember_read(target)
        text, range_data = _slice_numbered_lines(content, inp)
        data = {"path": path}
        data.update(range_data)
        return ToolResponse.success(text, data)

    def write_file(inp: dict) -> ToolResponse:
        path = inp["path"]
        content = inp["content"]
        target = workspace.resolve(path)
        read_error = workspace.require_fresh_read(target, "write_file")
        if read_error:
            return read_error
        _ensure_safe_content(path, content)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        workspace.remember_read(target)
        workspace.enforce_change_limit()
        line_count = len(content.splitlines()) or 1
        return ToolResponse.success(f"Wrote {path} ({line_count} lines)", {"path": path, "line_count": line_count})

    def run_command(inp: dict) -> ToolResponse:
        command = inp["command"]
        timeout = int(inp.get("timeout") or 60)
        result = workspace.run(command, timeout=timeout)
        text = f"exit_code={result.returncode}\n{result.stdout}"
        status_data = {
            "command": command,
            "exit_code": result.returncode,
            "output_chars": len(result.stdout),
        }
        if result.returncode == 0:
            return ToolResponse.success(text, status_data)
        return ToolResponse.partial(text, status_data)

    def search_repo(inp: dict) -> ToolResponse:
        query = inp["query"]
        file_glob = inp.get("file_glob") or ""
        glob = f" -g {shlex.quote(file_glob)}" if file_glob else ""
        command = (
            "if command -v rg >/dev/null 2>&1; then "
            f"rg -n --hidden -g '!.git' -g '!.env*'{glob} -- {shlex.quote(query)} .; "
            "else "
            f"grep -RIn --exclude-dir=.git --exclude='.env*' -- {shlex.quote(query)} .; "
            "fi"
        )
        result = workspace.run(command, timeout=30)
        return ToolResponse.success(result.stdout, {"query": query, "file_glob": file_glob, "exit_code": result.returncode})

    def apply_patch(inp: dict) -> ToolResponse:
        patch = inp["patch"]
        if ".env" in patch:
            return ToolResponse.failure("UNSAFE_PATCH", "Patch touches .env-style files.")
        for relative_path in _paths_from_patch(patch):
            # patch 涉及的每个已有文件都必须先被当前 Workspace 读取。只在 prompt 中要求
            # “先读后写”不够，因为模型可能忘记或被工具输出中的文本诱导。
            target = workspace.resolve(relative_path)
            read_error = workspace.require_fresh_read(target, "apply_patch")
            if read_error:
                return read_error
        patch_path = workspace.output_dir / "agent.patch"
        workspace.output_dir.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8")
        result = workspace.run(f"git apply {shlex.quote(str(patch_path))}", timeout=30)
        if result.returncode == 0:
            for relative_path in _paths_from_patch(patch):
                target = workspace.resolve(relative_path)
                workspace.remember_read(target)
        workspace.enforce_change_limit()
        if result.returncode == 0:
            return ToolResponse.success("Patch applied.", {"paths": _paths_from_patch(patch)})
        return ToolResponse.failure("PATCH_FAILED", result.stdout)

    def git_diff(inp: dict) -> ToolResponse:
        return ToolResponse.success(_git_diff(workspace))

    return [
        build_tool(
            name="finish",
            description="Finish the task, save model.patch and validation.json, and return final answer.",
            parameters=[ToolParameter("answer", "string", "Final summary for the user")],
            handler=finish,
            prompt="完成任务时调用 finish，并在 answer 中包含修改摘要和验证结果。",
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="list_files",
            description="List repository files.",
            parameters=[ToolParameter("max_files", "integer", "Maximum number of files to return", required=False)],
            handler=list_files,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="read_file",
            description="Read a file from the repository with line numbers. Supports optional line ranges.",
            parameters=[
                ToolParameter("path", "string", "Path relative to the repository root"),
                ToolParameter("start_line", "integer", "1-based first line to read", required=False),
                ToolParameter("end_line", "integer", "1-based final line to read, inclusive", required=False),
                ToolParameter("offset", "integer", "Alias for start_line", required=False),
                ToolParameter("limit", "integer", "Maximum number of lines to read", required=False),
            ],
            handler=read_file,
            prompt="修改已有文件前必须先调用 read_file；可用 start_line/end_line 或 offset/limit 精确读取；返回内容带行号，编辑时不要把行号写回文件。",
            max_result_size_chars=READ_FILE_RESULT_LIMIT,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="write_file",
            description="Write a file in the repository. Existing files must be read first.",
            parameters=[
                ToolParameter("path", "string", "Path relative to the repository root"),
                ToolParameter("content", "string", "Full file content to write"),
            ],
            handler=write_file,
            prompt="write_file 适合创建新文件或完整重写小文件；重写已有文件前必须先 read_file。",
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="run_command",
            description="Run a guarded shell command in the repository.",
            parameters=[
                ToolParameter("command", "string", "Shell command to run"),
                ToolParameter("timeout", "integer", "Timeout in seconds", required=False),
            ],
            handler=run_command,
            max_result_size_chars=COMMAND_RESULT_LIMIT,
            is_read_only=_is_read_only_command,
            is_concurrency_safe=_is_read_only_command,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="search_repo",
            description="Search repository text with ripgrep or grep.",
            parameters=[
                ToolParameter("query", "string", "Search query"),
                ToolParameter("file_glob", "string", "Optional file glob filter", required=False),
            ],
            handler=search_repo,
            max_result_size_chars=SEARCH_RESULT_LIMIT,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="apply_patch",
            description="Apply a unified diff patch. Existing touched files must be read first.",
            parameters=[ToolParameter("patch", "string", "Unified diff patch")],
            handler=apply_patch,
            prompt="apply_patch 前必须先 read_file 读取将被修改的已有文件；old 内容不匹配时让工具失败，而不是猜测。",
            source=ToolSource.BUILTIN,
        ),
        build_tool(
            name="git_diff",
            description="Return current repository diff.",
            parameters=[],
            handler=git_diff,
            max_result_size_chars=DIFF_RESULT_LIMIT,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.BUILTIN,
        ),
    ]


def register_repo_tools(registry: ToolRegistry, workspace: RepoWorkspace) -> None:
    registry.register_many(build_repo_tools(workspace))


def _git_diff(workspace: RepoWorkspace) -> str:
    result = workspace.run("git diff -- . ':!.env*'", timeout=30)
    return result.stdout


def _number_lines(content: str) -> str:
    lines = content.splitlines()
    if content.endswith("\n"):
        lines.append("")
    return "\n".join(f"{index + 1:4d} | {line}" for index, line in enumerate(lines))


def _slice_numbered_lines(content: str, inp: dict) -> tuple[str, dict]:
    lines = content.splitlines()
    if content.endswith("\n"):
        lines.append("")
    total_lines = len(lines)
    start_line = _optional_positive_int(inp.get("start_line"))
    end_line = _optional_positive_int(inp.get("end_line"))
    offset = _optional_positive_int(inp.get("offset"))
    limit = _optional_positive_int(inp.get("limit"))

    if start_line is None and offset is not None:
        start_line = offset
    if start_line is None:
        start_line = 1
    if end_line is None and limit is not None:
        end_line = start_line + limit - 1
    if end_line is None:
        end_line = total_lines
    if end_line < start_line:
        raise ValueError("end_line must be greater than or equal to start_line.")

    bounded_start = min(start_line, total_lines + 1)
    bounded_end = min(end_line, total_lines)
    selected = lines[bounded_start - 1 : bounded_end]
    text = "\n".join(f"{index:4d} | {line}" for index, line in enumerate(selected, start=bounded_start))
    return text, {
        "start_line": bounded_start,
        "end_line": bounded_end,
        "total_lines": total_lines,
        "truncated": bounded_start != 1 or bounded_end != total_lines,
    }


def _optional_positive_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    if parsed < 1:
        raise ValueError("Line range values must be positive integers.")
    return parsed


def _paths_from_patch(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        if value and value not in paths:
            paths.append(value)
    return paths


def _is_read_only_command(inp: dict) -> bool:
    command = str(inp.get("command") or "").strip().lower()
    read_only_prefixes = (
        "ls",
        "pwd",
        "git diff",
        "git status",
        "git log",
        "git show",
        "rg",
        "grep",
        "pytest --collect-only",
    )
    return any(command == prefix or command.startswith(prefix + " ") for prefix in read_only_prefixes)


def _ensure_safe_content(path: str, content: str) -> None:
    normalized = Path(path)
    if ".github" in normalized.parts and "workflows" in normalized.parts:
        risky_patterns = [
            r"(?im)^\s*permissions:\s*write-all\s*$",
            r"(?im)^\s*contents:\s*write\s*$",
            r"(?im)^\s*id-token:\s*write\s*$",
        ]
        if any(re.search(pattern, content) for pattern in risky_patterns):
            raise ValueError("Refusing workflow with broad write permissions.")
