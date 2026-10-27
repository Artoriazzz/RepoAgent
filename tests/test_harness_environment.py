import importlib.util
from pathlib import Path

from repo_agent.benchmark.harness_environment import runtime_container_environment


def test_build_proxy_is_not_a_test_traffic_proxy():
    env = runtime_container_environment({}, {"HTTPS_PROXY": "http://proxy:7897", "NO_PROXY": "api.example"})
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == env["ALL_PROXY"] == ""
    assert env["http_proxy"] == env["https_proxy"] == ""
    assert env["NO_PROXY"] == ""
    assert env["PIP_PROXY"] == "http://proxy:7897"


def test_explicit_test_proxy_and_spec_environment_are_respected():
    env = runtime_container_environment({"HTTPBIN_URL": "http://httpbin.local"},
                                     {"SWEBENCH_TEST_PROXY": "http://test:8080",
                                      "SWEBENCH_TEST_NO_PROXY": "httpbin.local"})
    assert env["https_proxy"] == "http://test:8080"
    assert env["no_proxy"] == "httpbin.local"
    assert env["HTTPBIN_URL"] == "http://httpbin.local"


def test_pandas_pin_preserves_existing_numpy_and_python_compatibility():
    path = Path(__file__).resolve().parents[1] / "scripts/repair_swebench_test_environment.py"
    spec = importlib.util.spec_from_file_location("test_environment_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.matplotlib_constraints((3, 11), "1.25.2") == [
        "numpy==1.25.2", "pandas==2.2.3", "setuptools-scm==7.1.0"]
    assert "pandas==1.5.3" in module.matplotlib_constraints((3, 8), "1.23.5")
    assert "pandas==1.3.5" in module.matplotlib_constraints((3, 8), "1.19.2")
