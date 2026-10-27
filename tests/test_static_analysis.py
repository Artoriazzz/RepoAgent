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


def test_import_insertion_does_not_turn_python2_compatibility_name_into_new_error():
    source = "is_py2 = False\ndef prepare_url(url):\n    if is_py2:\n        return unicode(url)\n    return url\n"
    assert newly_undefined_names(source, "import socket\n" + source, "models.py") == []


def test_same_method_name_in_another_class_does_not_hide_new_undefined_name():
    source = "class A:\n    def render(self):\n        return runtime_hook()\n"
    current = source + "\nclass B:\n    def render(self):\n        return runtime_hook()\n"
    issues = newly_undefined_names(source, current, "module.py")
    assert [(i.name, i.scope) for i in issues] == [("runtime_hook", "B.render")]


def test_new_undefined_name_still_reports_current_location_after_import_insertion():
    source = "def render():\n    return 1\n"
    current = "import socket\n\ndef render():\n    return missing_name\n"
    issues = newly_undefined_names(source, current, "module.py")
    assert [(i.name, i.scope, i.lineno) for i in issues] == [("missing_name", "render", 3)]
