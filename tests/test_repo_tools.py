import os
import subprocess

from repo_agent.core.tool import ToolRegistry
from repo_agent.tools.repo_tools import RepoWorkspace, register_repo_tools


def test_repo_tools_write_diff_and_finish(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
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
