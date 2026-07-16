from pathlib import Path

import pytest

from repo_agent.core.verification import (
    assess_verification_result,
    classify_verification_command,
    prepare_verification_command,
    verification_command_safety_reason,
)


def test_behavior_test_can_make_patch_submission_ready():
    result = assess_verification_result(
        {
            "status": "passed",
            "command": "python -m pytest tests/test_public_api.py::test_case -q",
            "exit_code": 0,
            "evidence": "1 passed",
        }
    )

    assert result["classification"] == "behavior_passed"
    assert result["submission_ready"] is True
    assert result["reject_patch"] is False


def test_static_or_read_only_success_does_not_prove_behavior():
    static = assess_verification_result(
        {"status": "passed", "command": "python -m py_compile app.py", "exit_code": 0, "evidence": "ok"}
    )
    read_only = assess_verification_result(
        {"status": "passed", "command": "head -20 app.py", "exit_code": 0, "evidence": "source"}
    )

    assert static["classification"] == "static_passed"
    assert static["submission_ready"] is False
    assert static["preserve_patch"] is True
    assert read_only["classification"] == "weak_passed"
    assert read_only["submission_ready"] is False


def test_environment_failure_preserves_patch():
    result = assess_verification_result(
        {
            "status": "failed",
            "command": "python -m pytest tests/test_api.py -q",
            "exit_code": 127,
            "evidence": "/bin/sh: python: not found",
        }
    )

    assert result["classification"] == "environment_blocked"
    assert result["reject_patch"] is False
    assert result["preserve_patch"] is True


def test_assert_bearing_inline_script_is_behavior_check():
    assert classify_verification_command("python3 -c \"from app import f; assert f(1) == 2\"") == "behavior"


def test_assert_bearing_inline_script_with_python_warning_flags_is_behavior_check():
    command = 'python -W ignore -c "from app import f; assert f(1) == 2"'

    assert classify_verification_command(command) == "behavior"


def test_inline_script_with_nonzero_sys_exit_failure_path_is_behavior_check():
    command = "python -c \"import sys; result = 2; sys.exit(1) if result != 2 else sys.exit(0)\""

    assert classify_verification_command(command) == "behavior"


def test_inline_script_with_only_success_exit_is_not_behavior_check():
    assert classify_verification_command('python -c "import sys; sys.exit(0)"') == "unknown"


def test_verification_rejects_exit_status_masking_shell_control():
    assert verification_command_safety_reason("python -m pytest tests/test_api.py | head -80")
    assert verification_command_safety_reason("python -m pytest tests/test_api.py || true")
    assert verification_command_safety_reason("python -m pytest tests/test_api.py; echo done")
    assert verification_command_safety_reason("cd repo && python -m pytest tests/test_api.py") is None
    assert verification_command_safety_reason("python -c \"assert 'a|b' == 'a|b'\"") is None
    assert verification_command_safety_reason("cat > /tmp/check.py << 'EOF'\nassert True\nEOF")


def test_masked_behavior_exit_code_is_not_submission_ready():
    result = assess_verification_result(
        {
            "status": "passed",
            "command": "python -m pytest tests/test_api.py 2>&1 | head -80",
            "exit_code": 0,
            "evidence": "output was truncated",
        }
    )

    assert result["classification"] == "exit_status_untrustworthy"
    assert result["submission_ready"] is False
    assert result["reject_patch"] is False


def test_prepare_verification_command_uses_swebench_image_and_testbed_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    prepared, runtime = prepare_verification_command(
        f"cd {repo} && python -c 'assert True'",
        repo,
        docker_image="sweb.eval.x86_64.project__repo-1:latest",
    )

    assert runtime == "swebench_docker"
    assert f"-v {repo}:/repo-agent-host:ro" in prepared
    assert "git -c safe.directory=/repo-agent-host" in prepared
    assert "git apply --whitespace=nowarn" in prepared
    assert "cd /testbed && python -c" in prepared
    assert "--network none" in prepared
    assert str(repo) not in prepared.split("bash -lc", 1)[1]


def test_prepare_verification_command_rejects_unsafe_image(tmp_path):
    with pytest.raises(ValueError, match="Invalid verification Docker image"):
        prepare_verification_command(
            "python -c 'assert True'",
            tmp_path,
            docker_image="image; touch /tmp/unsafe",
        )


def test_prepare_verification_command_rewrites_bare_python_without_docker(tmp_path):
    prepared, runtime = prepare_verification_command(
        "python -c 'assert True' && python3 -V",
        tmp_path,
        docker_image="",
        interpreter="/opt/repo agent/bin/python",
    )

    assert runtime == "local_interpreter"
    assert prepared.startswith("'/opt/repo agent/bin/python' -c")
    assert prepared.endswith("python3 -V")
