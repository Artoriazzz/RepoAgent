import subprocess
from pathlib import Path

import pytest

from repo_agent.core.verification import (
    assess_verification_result,
    behavior_failure_evidence_reason,
    classify_verification_command,
    prepare_verification_command,
    public_assertion_contract_reason,
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


def test_public_assertion_contract_rejects_weak_or_old_behavior_checks():
    assert public_assertion_contract_reason(
        'python -c "assert result != 0"',
        "result must equal 0.75",
    )
    assert public_assertion_contract_reason(
        'python -c "old = result(); assert old == 32"',
        "return 9 instead of 32",
    )
    assert public_assertion_contract_reason(
        'python -c "assert result() == 32"',
        "return 9 instead of 32",
    )
    assert (
        public_assertion_contract_reason(
            'python -c "assert result() == 9"',
            "return 9 instead of 32",
        )
        is None
    )


def test_public_assertion_contract_parses_multiline_python_source():
    command = """python -c "
from app import result
actual = result()
assert actual == 9, f'case=primary expected=9 actual={actual!r}'
witness = result('old')
assert witness == 4, f'case=witness expected=4 actual={witness!r}'
" """

    assert public_assertion_contract_reason(command, "return exactly 9") is None


def test_public_assertion_contract_reports_multiline_python_syntax_error():
    reason = public_assertion_contract_reason(
        'python -c "value = 1\\nassert value =="',
        "return exactly 1",
    )

    assert reason is not None
    assert "invalid syntax" in reason


def test_inline_behavior_failure_must_reach_the_assertion():
    command = 'python -c "value = broken(); assert value == 9"'

    assert behavior_failure_evidence_reason(command, "AssertionError", 1) is None
    reason = behavior_failure_evidence_reason(
        command,
        "AttributeError: 'int' object has no attribute 'abs'",
        1,
    )
    assert reason is not None
    assert "before its public assertion" in reason


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
    assert verification_command_safety_reason("cd repo && python -m pytest tests/test_api.py")
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
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

    prepared, runtime = prepare_verification_command(
        "python -c 'assert True'",
        repo,
        docker_image="sweb.eval.x86_64.project__repo-1:latest",
    )

    assert runtime == "swebench_docker"
    assert f"-v {repo}:/repo-agent-host:ro" in prepared
    assert "git apply" not in prepared
    assert "python -c" in prepared
    assert "--network none" in prepared
    assert str(repo) not in prepared.split("bash -lc", 1)[1]


def test_prepare_verification_command_can_skip_dirty_host_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    )
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")

    prepared, runtime = prepare_verification_command(
        "python -c 'print(1)'",
        repo,
        docker_image="sweb.eval.x86_64.project__repo-1:latest",
        apply_host_patch=False,
    )

    assert runtime == "swebench_docker"
    assert "git apply" not in prepared


def test_prepare_verification_command_applies_nonempty_host_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "RepoAgent Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "repoagent@example.test"], cwd=repo, check=True)
    source = repo / "app.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE)
    source.write_text("new\n", encoding="utf-8")

    prepared, _ = prepare_verification_command(
        "python -c 'assert True'",
        repo,
        docker_image="sweb.eval.x86_64.project__repo-1:latest",
    )

    assert "git -c safe.directory=/repo-agent-host" in prepared
    assert "git apply --whitespace=nowarn" in prepared


def test_verification_command_rejects_directory_changes():
    reason = verification_command_safety_reason(
        "cd /tmp/repo && python -c 'assert True'"
    )

    assert reason is not None
    assert "directory changes" in reason


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
