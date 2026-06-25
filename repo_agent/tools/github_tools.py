from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from io import BytesIO

from repo_agent.core.tool import Tool, ToolParameter, ToolRegistry, ToolResponse, ToolSource, build_tool

ISSUE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def build_github_tools(token: str | None = None) -> list[Tool]:
    client = GitHubClient(token=token)

    def get_issue(inp: dict) -> ToolResponse:
        issue_url = inp["issue_url"]
        return ToolResponse.success(json.dumps(client.get_issue(issue_url), ensure_ascii=False, indent=2))

    def get_repo_metadata(inp: dict) -> ToolResponse:
        repo = inp["repo"]
        return ToolResponse.success(json.dumps(client.get_repo_metadata(repo), ensure_ascii=False, indent=2))

    def download_workflow_logs(inp: dict) -> ToolResponse:
        repo = inp["repo"]
        run_id = inp["run_id"]
        return ToolResponse.success(client.download_workflow_logs(repo, run_id))

    return [
        build_tool(
            name="get_issue",
            description="Read a GitHub issue by URL.",
            parameters=[ToolParameter("issue_url", "string", "GitHub issue URL")],
            handler=get_issue,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.INTEGRATION,
        ),
        build_tool(
            name="get_repo_metadata",
            description="Read GitHub repository metadata.",
            parameters=[ToolParameter("repo", "string", "owner/repo or GitHub repository URL")],
            handler=get_repo_metadata,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.INTEGRATION,
        ),
        build_tool(
            name="download_workflow_logs",
            description="Download GitHub Actions workflow logs.",
            parameters=[
                ToolParameter("repo", "string", "owner/repo or GitHub repository URL"),
                ToolParameter("run_id", "integer", "GitHub Actions workflow run id"),
            ],
            handler=download_workflow_logs,
            is_concurrency_safe=True,
            is_read_only=True,
            source=ToolSource.INTEGRATION,
        ),
    ]


def register_github_tools(registry: ToolRegistry, token: str | None = None) -> None:
    registry.register_many(build_github_tools(token=token))


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")

    def request(self, path: str) -> bytes:
        req = urllib.request.Request(f"https://api.github.com/{path.lstrip('/')}", headers=self.headers())
        try:
            with urllib.request.urlopen(req) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub request failed: {exc.code} {detail}") from exc

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-agent-plain",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_issue(self, issue_url: str) -> dict:
        owner, repo, issue_number = parse_issue_url(issue_url)
        data = json.loads(self.request(f"/repos/{owner}/{repo}/issues/{issue_number}").decode("utf-8"))
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "body": data.get("body"),
            "state": data.get("state"),
            "html_url": data.get("html_url"),
        }

    def get_repo_metadata(self, repo: str) -> dict:
        owner, name = parse_owner_repo(repo)
        data = json.loads(self.request(f"/repos/{owner}/{name}").decode("utf-8"))
        return {
            "full_name": data.get("full_name"),
            "default_branch": data.get("default_branch"),
            "private": data.get("private"),
            "language": data.get("language"),
            "html_url": data.get("html_url"),
        }

    def download_workflow_logs(self, repo: str, run_id: int) -> str:
        owner, name = parse_owner_repo(repo)
        payload = self.request(f"/repos/{owner}/{name}/actions/runs/{run_id}/logs")
        if not zipfile.is_zipfile(BytesIO(payload)):
            return payload.decode("utf-8", errors="replace")
        sections: list[str] = []
        with zipfile.ZipFile(BytesIO(payload)) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith("/"):
                    continue
                sections.append(f"===== {name} =====\n{zf.read(name).decode('utf-8', errors='replace')}")
        return "\n\n".join(sections)


def parse_issue_url(issue_url: str) -> tuple[str, str, str]:
    match = ISSUE_RE.search(issue_url)
    if not match:
        raise ValueError(f"Invalid GitHub issue URL: {issue_url}")
    return match.group(1), match.group(2), match.group(3)


def parse_owner_repo(value: str) -> tuple[str, str]:
    if "/issues/" in value:
        owner, repo, _ = parse_issue_url(value)
        return owner, repo
    if "github.com" in value:
        match = re.search(r"github\.com/([^/]+)/([^/]+)", value)
        if match:
            return match.group(1), match.group(2).removesuffix(".git")
    parts = value.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1].removesuffix(".git")
    raise ValueError(f"Expected owner/repo or GitHub URL, got: {value}")
