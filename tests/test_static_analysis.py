from repo_agent.core.static_analysis import newly_undefined_names


def test_newly_undefined_names_detects_removed_local_definition():
    base = """def render(value):
    has_description = set()
    return value in has_description
"""
    current = """def render(value):
    return value in has_description
"""

    issues = newly_undefined_names(base, current, "module.py")

    assert [(issue.name, issue.scope) for issue in issues] == [("has_description", "render")]


def test_newly_undefined_names_ignores_preexisting_dynamic_global():
    source = """def plugin_hook(value):
    return injected_runtime(value)
"""

    assert newly_undefined_names(source, source, "module.py") == []
