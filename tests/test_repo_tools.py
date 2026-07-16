import os
import subprocess

import pytest

from repo_agent.core.tool import ToolRegistry
from repo_agent.tools.repo_tools import RepoWorkspace, register_repo_tools


def test_repo_tools_write_diff_and_finish(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "RepoAgent Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "repoagent@example.test"], cwd=repo, check=True)
    (repo / "README.md").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

    registry = ToolRegistry()
    output_dir = tmp_path / "out"
    register_repo_tools(registry, RepoWorkspace(repo, output_dir))

    blocked_response = registry.execute("write_file", {"path": "README.md", "content": "new\n"})
    assert blocked_response.status == "error"

    read_response = registry.execute("read_file", {"path": "README.md"})
    assert read_response.status == "success"
    assert "1 | old" in read_response.text

    write_response = registry.execute("write_file", {"path": "README.md", "content": "new\n"})
    assert write_response.status == "success"

    diff_response = registry.execute("git_diff", {})
    assert "-old" in diff_response.text
    assert "+new" in diff_response.text

    finish_response = registry.execute("finish", {"answer": "done"})
    assert finish_response.status == "success"
    assert (output_dir / "model.patch").exists()


def test_write_file_detects_external_change_after_read(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "README.md"
    target.write_text("old\n", encoding="utf-8")

    registry = ToolRegistry()
    register_repo_tools(registry, RepoWorkspace(repo, tmp_path / "out"))

    assert registry.execute("read_file", {"path": "README.md"}).status == "success"
    target.write_text("external\n", encoding="utf-8")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    response = registry.execute("write_file", {"path": "README.md", "content": "agent\n"})

    assert response.status == "error"
    assert response.error["code"] == "STALE_READ"
    assert target.read_text(encoding="utf-8") == "external\n"


def test_read_file_supports_line_ranges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    registry = ToolRegistry()
    register_repo_tools(registry, RepoWorkspace(repo, tmp_path / "out"))

    response = registry.execute("read_file", {"path": "README.md", "start_line": 2, "end_line": 3})

    assert response.status == "success"
    assert "   2 | two" in response.text
    assert "   3 | three" in response.text
    assert "   1 | one" not in response.text
    assert response.data["total_lines"] == 5
    assert response.data["truncated"] is True


def test_read_file_offset_limit_counts_as_read_for_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "RepoAgent Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "repoagent@example.test"], cwd=repo, check=True)
    (repo / "README.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

    registry = ToolRegistry()
    register_repo_tools(registry, RepoWorkspace(repo, tmp_path / "out"))

    read_response = registry.execute("read_file", {"path": "README.md", "offset": 2, "limit": 1})
    assert read_response.status == "success"
    assert "   2 | two" in read_response.text

    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""
    patch_response = registry.execute("apply_patch", {"patch": patch})

    assert patch_response.status == "success"
    assert (repo / "README.md").read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_run_command_does_not_duplicate_full_output_in_data(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    registry = ToolRegistry()
    register_repo_tools(registry, RepoWorkspace(repo, tmp_path / "out"))

    response = registry.execute("run_command", {"command": "printf 'hello'"})

    assert response.status == "success"
    assert "hello" in response.text
    assert "output" not in response.data
    assert response.data["output_chars"] == 5


def test_shell_guard_allows_docker_rm_flag_but_blocks_recursive_rm(tmp_path):
    workspace = RepoWorkspace(tmp_path, tmp_path / "out")

    workspace.ensure_safe_command("docker run --rm --network none image true")

    with pytest.raises(ValueError, match="Recursive deletion is blocked"):
        workspace.ensure_safe_command("rm -rf build")
