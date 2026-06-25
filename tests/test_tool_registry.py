from repo_agent.core.tool import ToolParameter, ToolRegistry, build_tool


def test_register_function_and_execute_json_args():
    registry = ToolRegistry()

    def echo(text: str) -> str:
        return text

    registry.register_function(echo)
    response = registry.execute("echo", '{"text": "hello"}')

    assert response.status == "success"
    assert response.text == "hello"
    assert registry.schemas()[0]["function"]["name"] == "echo"


def test_build_tool_defaults_fail_closed_and_truncates():
    tool = build_tool(
        name="long_output",
        description="Return long output",
        parameters=[],
        handler=lambda inp: "x" * 60_000,
    )
    registry = ToolRegistry()
    registry.register(tool)

    response = registry.execute("long_output", {})

    assert tool.is_read_only({}) is False
    assert tool.is_concurrency_safe({}) is False
    assert response.status == "partial"
    assert response.data["truncated"] is True
    assert "[... truncated" in response.text


def test_tool_argument_validation_returns_error_data():
    tool = build_tool(
        name="needs_text",
        description="Needs text",
        parameters=[ToolParameter("text", "string", "Text")],
        handler=lambda inp: inp["text"],
    )
    registry = ToolRegistry()
    registry.register(tool)

    response = registry.execute("needs_text", {})

    assert response.status == "error"
    assert response.error["code"] == "BAD_ARGUMENTS"
