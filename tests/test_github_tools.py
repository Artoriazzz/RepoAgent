import io
import zipfile

from repo_agent.tools.github_tools import GitHubClient, parse_issue_url, parse_owner_repo


def test_parse_github_inputs():
    assert parse_issue_url("https://github.com/owner/repo/issues/3") == ("owner", "repo", "3")
    assert parse_owner_repo("owner/repo") == ("owner", "repo")
    assert parse_owner_repo("https://github.com/owner/repo.git") == ("owner", "repo")


def test_download_workflow_logs_expands_zip(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("job/log.txt", "failed")

    client = GitHubClient(token="")
    monkeypatch.setattr(client, "request", lambda path: payload.getvalue())

    logs = client.download_workflow_logs("owner/repo", 123)

    assert "===== job/log.txt =====" in logs
    assert "failed" in logs
