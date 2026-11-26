from __future__ import annotations

import ast
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_STATIC_COMMAND_PATTERNS = (
    r"\bgit\s+diff\s+--check\b",
    r"\bpy_compile\b",
    r"\bcompileall\b",
    r"\b(?:ruff|flake8|pylint|mypy)\b",
)

_BEHAVIOR_COMMAND_PATTERNS = (
    r"\bpython(?:3)?\s+(?:\./)?tests/runtests\.py(?:\s|$)",
    r"\bpython(?:3)?\s+(?:\./)?manage\.py\s+test(?:\s|$)",
    r"\bpytest\b",
    r"\bpython(?:3)?\s+-m\s+unittest\b",
    r"\b(?:tox|nox)\b",
    r"\bmake\s+(?:test|check)\b",
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b",
    r"\b(?:cargo|go)\s+test\b",
    r"\b(?:mvn|gradle|gradlew)\s+test\b",
    r"\brspec\b",
)

_READ_ONLY_COMMAND_PATTERNS = (
    r"(?:^|&&|;)\s*(?:head|tail|cat|sed|grep|rg)\b",
    r"(?:^|&&|;)\s*git\s+(?:status|diff|show)\b",
)

_ENVIRONMENT_FAILURE_PATTERNS = (
    r"command not found",
    r"/bin/(?:ba)?sh:.*\bnot found\b",
    r"no such file or directory",
    r"modulenotfounderror:\s*no module named",
    r"could not find a version that satisfies",
    r"connection (?:refused|timed out)",
)

_DOCKER_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?")


def test_overlay_digest(overlay: Path | None) -> str | None:
    if overlay is None:
        return None
    digest = hashlib.sha256()
    for path in sorted(overlay.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(overlay).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


@contextmanager
def temporary_test_overlay(repo: Path, overlay: Path | None):
    """本地验证临时加入独立测试文件，执行结束后移除，保留原有文件。"""
    files, directories = [], []
    try:
        if overlay is not None:
            for source in sorted(overlay.rglob("*")):
                if not source.is_file():
                    continue
                target = repo / source.relative_to(overlay)
                if not target.resolve().is_relative_to(repo.resolve()) or target.exists():
                    raise ValueError(f"Public tests cannot overwrite repository files: {target}")
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    directories.append(directory)
                shutil.copyfile(source, target)
                files.append(target)
        yield
    finally:
        for path in reversed(files):
            path.unlink()
        for directory in reversed(directories):
            # 测试运行器可能生成缓存；只移除空目录，不删除其他产物。
            if not any(directory.iterdir()):
                directory.rmdir()


def prepare_verification_command(
    command: str,
    repo_path: str | Path,
    *,
    docker_image: str | None = None,
    interpreter: str | None = None,
    apply_host_patch: bool = True,
    test_overlay: Path | None = None,
) -> tuple[str, str]:
    """为验证命令选择确定运行时，返回执行命令和运行时类型。"""

    image = (docker_image if docker_image is not None else os.getenv("REPO_AGENT_VERIFY_DOCKER_IMAGE", "")).strip()
    repo = str(Path(repo_path).resolve())
    if image:
        if not _DOCKER_IMAGE_PATTERN.fullmatch(image):
            raise ValueError(f"Invalid verification Docker image: {image}")
        # 保留镜像中已构建的 /testbed，只把 host diff 应用到容器临时层；直接覆盖
        # /testbed 会遮住 C 扩展等未被 git 跟踪的构建产物。
        inner_command = command.replace(repo, "/testbed")
        host_diff = (
            subprocess.run(
                ["git", "diff", "--binary", "--", ".", ":!.env*"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if apply_host_patch
            else None
        )
        if host_diff is not None and host_diff.returncode == 0 and host_diff.stdout.strip():
            apply_host_patch = (
                "git -c safe.directory=/repo-agent-host -C /repo-agent-host "
                "diff --binary -- . ':!.env*' | git apply --whitespace=nowarn -"
            )
            container_command = f"{apply_host_patch} && {inner_command}"
        else:
            # 干净基线没有 patch；对空输入运行 git apply 会返回 128，误报环境失败。
            container_command = inner_command
        overlay_mount = ""
        if test_overlay is not None:
            overlay_mount = f"-v {shlex.quote(str(test_overlay.resolve()))}:/repo-agent-public-tests:ro"
            container_command = "cp -R /repo-agent-public-tests/. /testbed/ && " + container_command
        prepared = " ".join(
            (
                "docker run --rm --network none",
                f"-v {shlex.quote(repo)}:/repo-agent-host:ro",
                overlay_mount,
                "-w /testbed",
                "-e PYTHONDONTWRITEBYTECODE=1",
                shlex.quote(image),
                "bash -lc",
                shlex.quote(container_command),
            )
        )
        return prepared, "swebench_docker"

    executable = shlex.quote(interpreter or sys.executable)
    prepared = re.sub(
        r"(^|[;&|(\s])python(?=\s)",
        lambda match: f"{match.group(1)}{executable}",
        command,
    )
    if test_overlay is not None:
        prepared = "PYTHONDONTWRITEBYTECODE=1 " + prepared
    return prepared, "local_interpreter"


def classify_verification_command(command: str) -> str:
    """Return behavior, static, read_only, or unknown for a verify command."""

    normalized = command.strip().lower()
    if any(re.search(pattern, normalized) for pattern in _STATIC_COMMAND_PATTERNS):
        return "static"
    if any(re.search(pattern, normalized) for pattern in _BEHAVIOR_COMMAND_PATTERNS):
        return "behavior"
    if re.search(
        r"\bpython(?:3)?(?:\s+-w\s+\S+|\s+-[bioqrsvux])*\s+(?:-c|-)(?:\s|$)",
        normalized,
    ):
        has_failure_signal = bool(
            re.search(r"\b(?:assert|raise\s+(?:assertionerror|systemexit))\b", normalized)
            or re.search(r"\bsys\.exit\s*\(\s*[1-9]\d*\s*\)", normalized)
        )
        if has_failure_signal:
            return "behavior"
    if any(re.search(pattern, normalized) for pattern in _READ_ONLY_COMMAND_PATTERNS):
        return "read_only"
    return "unknown"


def verification_command_safety_reason(command: str) -> str | None:
    """拒绝会让 shell 返回后续命令状态、从而掩盖测试失败的控制结构。"""

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return "verification command has invalid shell quoting"

    if "|" in tokens:
        return "shell pipelines are forbidden because the last process can hide the test command exit code"
    if "||" in tokens:
        return "'||' fallback commands are forbidden because they can turn a failed verification into exit code 0"
    if "cd" in tokens:
        return "directory changes are forbidden; verification already runs from the repository root"
    normalized = command.strip().lower()
    if re.match(r"^(?:cat|tee)\b", normalized) and (">" in command or "<<" in command):
        return "the verification command only creates a file instead of executing a behavior check"
    if "<<" in command and not re.search(r"\b(?:python|python3|pytest|unittest|tox|nox)\b", normalized.split("<<", 1)[0]):
        return "a standalone heredoc does not execute the verification behavior"
    if ";" in tokens:
        separator = tokens.index(";")
        before = " ".join(tokens[:separator])
        after = " ".join(tokens[separator + 1 :])
        if classify_verification_command(before) == "behavior" and after:
            return "commands after a behavior check are forbidden because their exit code can hide the test failure"
    return None


def public_assertion_contract_reason(command: str, expected_behavior: str) -> str | None:
    """检查内联断言是否直接表达新行为，而不是旧值或宽松替代条件。"""

    expected = expected_behavior.strip().lower()
    if not expected:
        return "expected_behavior must state the exact public result or exception"
    normalized = command.strip().lower()
    if re.search(r"\bpython(?:3)?\b", normalized) and "-c" in normalized:
        source, source_error = _inline_python_source(command)
        if source_error:
            return source_error
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"the inline Python verification has invalid syntax: {exc.msg}"
        assertions = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
        if not assertions:
            return "an inline public verification must contain an explicit assert"
        old_value = re.search(
            r"\binstead\s+of\s+([+-]?\d+(?:\.\d+)?|true|false|none|['\"][^'\"]+['\"])",
            expected,
        )
        for assertion in assertions:
            expression = ast.unparse(assertion.test).lower()
            if re.search(r"\b(?:old|baseline|before|previous)(?:_[a-z0-9]+)?\b", expression):
                return "the assertion compares the old/baseline behavior instead of the requested new behavior"
            if isinstance(assertion.test, ast.Compare) and any(
                isinstance(operator, ast.NotEq) for operator in assertion.test.ops
            ) and not re.search(
                r"\b(?:not equal|must not|should not|nonzero|non-zero|different from)\b",
                expected,
            ):
                return "a weak '!=' assertion does not prove the exact requested public behavior"
            if old_value and re.search(
                rf"==\s*{re.escape(old_value.group(1))}(?:\D|$)",
                expression,
            ):
                return "the assertion checks the explicitly superseded old value"
    return None


def _inline_python_source(command: str) -> tuple[str, str | None]:
    """从 shell 命令中提取 `python -c` 的源码参数。"""

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return "", f"the inline Python verification has invalid shell quoting: {exc}"
    try:
        command_index = tokens.index("-c")
    except ValueError:
        return "", "the inline Python verification is missing the -c source argument"
    if command_index + 1 >= len(tokens):
        return "", "the inline Python verification is missing source code after -c"
    return tokens[command_index + 1], None


def behavior_failure_evidence_reason(
    command: str,
    evidence: str,
    exit_code: int | None,
    expected_behavior: str = "",
    public_problem: str = "",
) -> str | None:
    """确认非零退出来自公开行为，而不是脚本构造、导入或环境异常。"""

    if not isinstance(exit_code, int) or exit_code == 0:
        return "the clean baseline did not exit with a behavioral failure"
    normalized = command.strip().lower()
    output = evidence.lower()
    if re.search(r"\bpython(?:3)?\b", normalized) and "-c" in normalized:
        if "assertionerror" in output:
            return None
        # Public reproducers can intentionally trigger the exception named in
        # the issue before reaching a later assertion (for example an
        # IndexError during widget construction or a ValueError from a norm).
        # Accept that only when the expected contract names the same type.
        exception_names = {
            name.lower()
            for name in re.findall(r"(?m)^\s*([A-Z][A-Za-z0-9_.]*Error):", evidence)
        }
        expected_names = {
            name.lower()
            for name in re.findall(
                r"\b([A-Z][A-Za-z0-9_.]*Error)\b",
                f"{expected_behavior}\n{public_problem}",
            )
        }
        if exception_names & expected_names:
            return None
        if not expected_names and exception_names & {"indexerror", "valueerror", "typeerror"}:
            return (
                "the inline command raised a runtime exception before its assertion; "
                "state that exact public exception in expected_behavior before registering it"
            )
        if exception_names:
            return (
                "the inline command raised an exception before its public assertion and it was not named by "
                "expected_behavior; "
                "fix the command input/API instead of treating the traceback as reproduction"
            )
        return (
            "the inline command exited before its public assertion failed; "
            "fix the command input/API instead of treating the traceback as reproduction"
        )
    if re.search(r"\b(?:pytest|unittest|tox|nox)\b", normalized):
        if any(
            marker in output
            for marker in (
                "error collecting",
                "collection errors",
                "modulenotfounderror",
                "importerror while importing",
            )
        ):
            return "the test command failed during collection/import rather than at a public behavior assertion"
        if not any(marker in output for marker in (" failed", "failed ", "failure", "assertionerror")):
            return "the test output does not contain a failed public behavior assertion"
    return None


def assess_verification_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Grade verification evidence without confusing environment failure with a bad patch."""

    value = result if isinstance(result, dict) else {}
    status = str(value.get("status") or "").strip().lower()
    command = str(value.get("command") or "").strip()
    exit_code = value.get("exit_code")
    evidence = str(value.get("evidence") or "").strip()
    command_kind = classify_verification_command(command) if command else "missing"

    if status not in {"passed", "failed", "blocked_by_environment"} or not command:
        return _assessment(
            "unparseable",
            command_kind,
            conclusive=False,
            submission_ready=False,
            reject_patch=False,
            reason="verification result is missing a valid status or command",
        )

    unsafe_reason = verification_command_safety_reason(command)
    if unsafe_reason:
        return _assessment(
            "exit_status_untrustworthy",
            command_kind,
            conclusive=False,
            submission_ready=False,
            reject_patch=False,
            reason=unsafe_reason,
        )

    environment_blocked = (
        status == "blocked_by_environment"
        or exit_code == 127
        or any(re.search(pattern, evidence.lower()) for pattern in _ENVIRONMENT_FAILURE_PATTERNS)
    )
    if environment_blocked:
        return _assessment(
            "environment_blocked",
            command_kind,
            conclusive=False,
            submission_ready=False,
            reject_patch=False,
            reason="the command could not evaluate patch behavior in the current environment",
        )

    if command_kind == "behavior":
        if status == "passed" and exit_code == 0:
            return _assessment(
                "behavior_passed",
                command_kind,
                conclusive=True,
                submission_ready=True,
                reject_patch=False,
                reason="a behavior-checking command passed",
            )
        return _assessment(
            "behavior_failed",
            command_kind,
            conclusive=True,
            submission_ready=False,
            reject_patch=True,
            reason="a behavior-checking command failed",
        )

    if command_kind == "static":
        passed = status == "passed" and exit_code == 0
        return _assessment(
            "static_passed" if passed else "static_failed",
            command_kind,
            conclusive=True,
            submission_ready=False,
            reject_patch=not passed,
            reason=(
                "static evidence passed but does not prove the requested behavior"
                if passed
                else "a static integrity check failed"
            ),
        )

    return _assessment(
        "weak_passed" if status == "passed" and exit_code == 0 else "weak_failed",
        command_kind,
        conclusive=False,
        submission_ready=False,
        reject_patch=False,
        reason="the command does not contain a trustworthy behavioral assertion",
    )


def _assessment(
    classification: str,
    command_kind: str,
    *,
    conclusive: bool,
    submission_ready: bool,
    reject_patch: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "command_kind": command_kind,
        "conclusive": conclusive,
        "submission_ready": submission_ready,
        "reject_patch": reject_patch,
        "preserve_patch": not reject_patch,
        "reason": reason,
    }
