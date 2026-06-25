from repo_agent.tools import RepoWorkspace, ToolImportContext, assemble_tool_pool


def test_catalog_filters_and_partitions_tools(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    registry = assemble_tool_pool(
        ToolImportContext(
            workspace=RepoWorkspace(repo, tmp_path / "out"),
            denied_tools={"run_command"},
        )
    )
    names = registry.names()

    assert "run_command" not in names
    assert "read_file" in names
    assert "get_issue" in names
    assert names.index("write_file") < names.index("get_issue")


def test_catalog_simple_mode_excludes_integrations(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    registry = assemble_tool_pool(
        ToolImportContext(
            workspace=RepoWorkspace(repo, tmp_path / "out"),
            simple_mode=True,
        )
    )

    assert "read_file" in registry.names()
    assert "get_issue" not in registry.names()
