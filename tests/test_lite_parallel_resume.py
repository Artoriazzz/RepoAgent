import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def load_runner():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("lite_parallel_resume", scripts / "resume_lite_parallel_wsl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_resume_only_pending_and_environment_errors_before_new_cases():
    runner = load_runner()
    rows = [{"instance_id": str(i)} for i in range(110)]
    previous = [{"instance_id": str(i), "status": "agent_ok", "resolved": i % 2 == 0}
                for i in range(40, 65)]
    for row in previous[:4]:
        row["status"] = "environment_error"
    chosen = runner.plan_instances(rows, previous)
    assert len(chosen) == 49
    assert [r["instance_id"] for r in chosen] == [str(i) for i in list(range(40, 44)) + list(range(65, 110))]


def test_completed_semantic_failure_is_not_rerun_but_harness_error_is():
    runner = load_runner()
    assert runner.reusable_result({"status": "agent_ok", "resolved": False,
                                  "passes": [{"harness": {"error": False}}]})
    assert not runner.reusable_result({"status": "agent_ok", "passes": [{"harness": {"error": True}}]})
    assert not runner.reusable_result({"status": "api_error"})


def test_infrastructure_retry_includes_nested_harness_errors_but_not_semantic_failures():
    runner = load_runner()
    assert runner.infrastructure_error({"status": "environment_error"})
    assert runner.infrastructure_error({"status": "agent_ok", "passes": [{"harness": {"error": True}}]})
    assert not runner.infrastructure_error({"status": "agent_ok", "resolved": False,
                                            "passes": [{"harness": {"error": False}}]})


def test_retry_status_selects_only_api_failures_and_preserves_dataset_order():
    runner = load_runner()
    rows = [{"instance_id": str(i)} for i in range(5)]
    previous = [{"instance_id": "3", "status": "api_error"},
                {"instance_id": "1", "status": "api_error"},
                {"instance_id": "2", "status": "environment_error"},
                {"instance_id": "4", "status": "agent_ok", "api_errors": [{"stage": "curator"}]}]
    assert runner.select_status_retries(rows, previous, {"api_error"}) == [rows[1], rows[3]]


def test_first_round_score_does_not_count_corrective_success():
    runner = load_runner()
    result = {"passes": [{"initial_harness": {"resolved": False}, "harness": {"resolved": True}}]}
    assert runner.initial_resolved(result) is False


def test_unresolved_retry_includes_invalid_patch_and_keeps_dataset_order():
    runner = load_runner()
    rows = [{"instance_id": str(i)} for i in range(5)]
    previous = [{"instance_id": "3", "resolved": False, "status": "invalid_patch"},
                {"instance_id": "1", "resolved": False, "status": "agent_ok"},
                {"instance_id": "2", "resolved": True, "status": "agent_ok"}]
    assert runner.select_unresolved(rows, previous) == [rows[1], rows[3]]


def test_new_range_schedules_exactly_twenty_unseen_cases():
    runner = load_runner()
    rows = [{"instance_id": str(i)} for i in range(300)]
    previous = [{"instance_id": str(i), "status": "agent_ok"} for i in range(110)]
    assert runner.plan_instances(rows, previous, 110, 130, 0) == rows[110:130]


def test_extra_retries_and_last_hundred_are_unique():
    runner = load_runner()
    rows = [{"instance_id": str(i)} for i in range(300)]
    chosen = runner.prepend_extra_instances(rows, rows[200:300], ["151", "152", "151"])
    assert len(chosen) == 102
    assert chosen[:2] == rows[151:153]
    assert chosen[2:] == rows[200:300]
    with pytest.raises(ValueError, match="Unknown extra"):
        runner.prepend_extra_instances(rows, [], ["missing"])


def test_last_hundred_repo_import_checks_exist():
    runner = load_runner()
    assert "sympy/sympy" in runner.REPO_IMPORTS
    assert "sphinx-doc/sphinx" in runner.REPO_IMPORTS


def test_sympy_preflight_does_not_require_pytest():
    runner = load_runner()
    assert "pytest" not in runner.REPO_IMPORTS["sympy/sympy"]
    assert "bin/test" in runner.REPO_IMPORTS["sympy/sympy"]


def test_cached_baseline_uses_install_commit_only_for_matching_provenance():
    runner = load_runner()
    row = {"base_commit": "abc"}
    assert "'--exit-code','HEAD'" in runner.cached_baseline_check(row, {"repoagent.base-commit": "abc"})
    for labels in ({}, {"repoagent.base-commit": "other"}):
        assert "'--exit-code','abc'" in runner.cached_baseline_check(row, labels)
    assert "'--is-ancestor','abc','HEAD'" in runner.cached_baseline_check(row, {})


@pytest.mark.parametrize("clone_url", ["https://github.com/django/django",
                                      "http://127.0.0.1:18080/django__django.git",
                                      "http://172.17.0.1:18080/django__django.git"])
@pytest.mark.parametrize("cached_assets", [False, True])
@pytest.mark.parametrize("saved_context", [False, True])
def test_preflight_embeds_local_wheels_without_disabling_build_isolation(tmp_path, monkeypatch, clone_url, cached_assets, saved_context):
    runner = load_runner()
    helper = sys.modules["run_failed5_preflight_wsl"]
    instance = "django__django-1"
    image = "sweb.eval.x86_64." + instance + ":latest"
    source = tmp_path / "logs/build_images/instances" / image.replace(":", "__")
    source.mkdir(parents=True)
    (tmp_path / "agent_work").mkdir()
    setup = f"git clone -o origin  --single-branch {clone_url} /testbed && break\ngit reset --hard abc\napt-get -y update\npython -m pip install -e .\n"
    dockerfile = "FROM base\nCOPY ./setup_repo.sh /root/\nRUN /bin/bash /root/setup_repo.sh\n"
    if saved_context:
        (source / "setup_repo.sh").write_text(setup)
        (source / "Dockerfile").write_text(dockerfile)
    else:
        spec = SimpleNamespace(install_repo_script=setup, instance_dockerfile=dockerfile,
                               env_image_key="cached-env")
        monkeypatch.setitem(sys.modules, "swebench.harness.test_spec.test_spec",
                            SimpleNamespace(make_test_spec=lambda row: spec))
        monkeypatch.setitem(sys.modules, "swebench.harness.docker_build",
                            SimpleNamespace(build_env_images=Mock(side_effect=AssertionError("cached env should be reused"))))
        monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(ImageNotFound=FileNotFoundError))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "wheel.whl").write_bytes(b"fixture")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "freetype-2.6.1.tar.gz").write_bytes(b"fixture")

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "init", "--bare"]:
            Path(command[3]).mkdir()
        return SimpleNamespace(returncode=0)

    def build(**kwargs):
        context = Path(kwargs["path"])
        dockerfile = (context / "Dockerfile").read_text()
        assert "PIP_NO_INDEX=1" in dockerfile
        assert "PIP_FIND_LINKS=/opt/repoagent-wheelhouse" in dockerfile
        assert "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
        assert "--no-build-isolation" not in dockerfile
        setup = (context / "setup_repo.sh").read_text()
        assert "git clone --no-hardlinks -o origin --single-branch /root/repo.bundle /testbed && break" in setup
        assert clone_url not in setup
        assert (context / "wheelhouse/wheel.whl").read_bytes() == b"fixture"
        if cached_assets:
            assert "https://mirrors.ustc.edu.cn/ubuntu" in setup
            assert "COPY ./build-assets /opt/repoagent-build-assets" in dockerfile
            assert 'tar -xzf "$archive" -C /testbed/build' in setup
        return iter([])

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    client = SimpleNamespace(api=SimpleNamespace(build=build),
                             containers=SimpleNamespace(run=Mock(return_value=b"imports_ok")),
                             images=SimpleNamespace(get=Mock(return_value=SimpleNamespace(id="image-id"))))
    with (tmp_path / "build.log").open("w") as log:
        result = helper.prepare_image({"instance_id": instance,
                                       "repo": "matplotlib/matplotlib" if cached_assets else "django/django",
                                       "base_commit": "abc"},
                                      tmp_path, client, log, imports="import django", wheelhouse=wheels,
                                      apt_mirror="https://mirrors.ustc.edu.cn/ubuntu" if cached_assets else None,
                                      build_assets=assets if cached_assets else None)
    assert result["status"] == "ready"
    assert result["generated_build_scripts"] is (not saved_context)
