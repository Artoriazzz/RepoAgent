from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


_STATIC_COMMAND_PATTERNS = (
    r"\bgit\s+diff\s+--check\b",
    r"\bpy_compile\b",
    r"\bcompileall\b",
    r"\b(?:ruff|flake8|pylint|mypy)\b",
)

_BEHAVIOR_COMMAND_PATTERNS = (
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


def prepare_verification_command(
    command: str,
    repo_path: str | Path,
    *,
    docker_image: str | None = None,
    interpreter: str | None = None,
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
        apply_host_patch = (
            "git -c safe.directory=/repo-agent-host -C /repo-agent-host "
            "diff --binary -- . ':!.env*' | git apply --whitespace=nowarn -"
        )
        container_command = f"{apply_host_patch} && {inner_command}"
        prepared = " ".join(
            (
                "docker run --rm --network none",
                f"-v {shlex.quote(repo)}:/repo-agent-host:ro",
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
