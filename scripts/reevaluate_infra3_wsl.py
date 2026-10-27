"""修复三道历史任务的测试环境，并串行重评已有补丁。"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


INSTANCES = ["psf__requests-3362", "matplotlib__matplotlib-26020", "pytest-dev__pytest-8365"]
ROOT = Path("/home/artoria/workspace/SWE-bench")
SOURCE = ROOT / "agent_work/runs/lite110to199-infra10-retry-glm-20261023"
OUTPUT = ROOT / "agent_work/runs/infra3-test-environment-repair-20261023"
IMAGE_TAG = "test-environment-repair-20261023"


def summarize_results():
    previous = {row["instance_id"]: row for row in json.loads((SOURCE / "new_results.json").read_text())}
    cases = []
    for instance in INSTANCES:
        directory = ROOT / "logs/run_evaluation" / OUTPUT.name / "repoagent-glm-5.3-flash" / instance
        report = json.loads((directory / "report.json").read_text())[instance]
        cases.append({"instance_id": instance, "resolved": report["resolved"],
                      "previous_resolved": previous[instance]["resolved"],
                      "tests_status": {key: {status: len(tests) for status, tests in value.items()}
                                       for key, value in report["tests_status"].items()},
                      "report_path": str(directory / "report.json")})
    summary = {"source_run": str(SOURCE), "cases": cases,
               "resolved": sum(case["resolved"] for case in cases),
               "total": len(cases), "reused_patches": True}
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    import docker
    import pandas as pd
    from swebench.harness.repoagent_environment import runtime_container_environment

    OUTPUT.mkdir(parents=True, exist_ok=True)
    client = docker.from_env(timeout=300)
    proxy = "http://127.0.0.1:7897"
    env = runtime_container_environment({}, {"HTTPS_PROXY": proxy})
    rows = json.loads((SOURCE / "new_results.json").read_text())
    config = json.loads((SOURCE / "experiment.json").read_text())
    dataset = pd.read_parquet(config["dataset"])
    selected = dataset[dataset.instance_id.isin(INSTANCES)]
    (OUTPUT / "dataset.json").write_text(selected.to_json(orient="records"))
    predictions = []
    manifest = {}
    for instance in INSTANCES:
        (OUTPUT / "progress.txt").write_text(f"preparing {instance}\n")
        print(f"Preparing {instance}", flush=True)
        old = next(row for row in rows if row["instance_id"] == instance)
        patch = (Path(old["output_dir"]) / "pass-1/model.patch").read_text()
        if not patch.strip():
            raise ValueError(f"Missing saved patch: {instance}")
        predictions.append({"instance_id": instance, "model_name_or_path": "repoagent-glm-5.3-flash",
                            "model_patch": patch})
        base = client.images.get(f"sweb.eval.x86_64.{instance}:latest")
        files = {}
        dockerfile = f"FROM {base.id}\n"
        dockerfile += "ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PIP_DEFAULT_TIMEOUT=45 PIP_RETRIES=3\n"
        python = "/opt/miniconda3/envs/testbed/bin/python"
        if instance.startswith("psf__requests"):
            dockerfile += (f'RUN {python} -m pip install "pytest-httpbin==2.0.0" "pytest-mock==3.14.0" '
                           '"Flask==2.2.5" "Werkzeug==2.2.3"\n')
            smoke = [python, "-m", "pytest", "-q", "tests/test_requests.py", "--collect-only"]
        elif instance.startswith("matplotlib"):
            files["repair.py"] = Path(__file__).with_name("repair_swebench_test_environment.py").read_bytes()
            dockerfile += ("COPY repair.py /opt/repoagent/repair.py\n"
                           f"RUN {python} /opt/repoagent/repair.py --constraints /opt/repoagent/test-constraints.txt\n"
                           "ENV PIP_CONSTRAINT=/opt/repoagent/test-constraints.txt PIP_BUILD_CONSTRAINT=/opt/repoagent/test-constraints.txt\n")
            smoke = [python, "-c", "import warnings,matplotlib,numpy,pandas; warnings.simplefilter('error',DeprecationWarning); print(matplotlib.__version__,numpy.__version__,pandas.__version__)"]
        else:
            row = selected[selected.instance_id == instance].iloc[0]
            mirror = ROOT / "agent_work/repo_cache/pytest-dev__pytest.git"
            tag = subprocess.check_output(["git", "-C", str(mirror), "describe", "--tags", "--abbrev=0",
                                           row.base_commit], text=True).strip()
            bundle = OUTPUT / "pytest-version.bundle"
            subprocess.run(["git", "-C", str(mirror), "bundle", "create", str(bundle), tag], check=True)
            files["version.bundle"] = bundle.read_bytes()
            # 恢复真实历史标签，让评测中的再次安装也能推导正确版本。
            dockerfile += ("COPY version.bundle /opt/repoagent/version.bundle\n"
                           "RUN cd /testbed && git fetch /opt/repoagent/version.bundle 'refs/tags/*:refs/tags/*' "
                           f"&& {python} -m pip install -e .\n")
            smoke = [python, "-m", "pytest", "--collect-only", "-q", "testing/test_tmpdir.py"]
        files["Dockerfile"] = dockerfile.encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        archive.seek(0)
        image = f"sweb.eval.x86_64.{instance}:{IMAGE_TAG}"
        with (OUTPUT / f"{instance}-build.log").open("w") as log:
            for event in client.api.build(fileobj=archive, custom_context=True, tag=image, rm=True,
                                          network_mode="host", decode=True,
                                          buildargs={"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}):
                log.write(event.get("stream", ""))
                log.flush()
                if event.get("error"):
                    raise RuntimeError(event["error"])
        container = client.containers.create(image, ["tail", "-f", "/dev/null"],
                                              environment=env, network_mode="host")
        try:
            container.start()
            result = container.exec_run(["timeout", "180", *smoke], workdir="/testbed")
            (OUTPUT / f"{instance}-smoke.log").write_bytes(result.output)
            if result.exit_code:
                raise RuntimeError(f"Smoke check failed: {instance}: {result.output.decode(errors='replace')[-3000:]}")
        finally:
            container.remove(force=True)
        manifest[instance] = {"base_image_id": base.id, "image": image, "smoke_exit_code": result.exit_code}
        (OUTPUT / "environment.json").write_text(json.dumps(manifest, indent=2))
        print(f"Environment ready: {instance}", flush=True)
    (OUTPUT / "predictions.json").write_text(json.dumps(predictions, indent=2))
    (OUTPUT / "progress.txt").write_text("evaluating 3 saved patches, serial harness\n")
    environment = {**os.environ, "SWEBENCH_DOCKER_NETWORK": "host", "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                   "http_proxy": proxy, "https_proxy": proxy, "PYTHONUNBUFFERED": "1",
                   "SWEBENCH_REPO_MIRROR_ROOT": str(ROOT / "agent_work/repo_cache"),
                   "SWEBENCH_RAW_URL_PREFIX": ""}
    environment.pop("SWEBENCH_TEST_PROXY", None)
    command = [sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", str(OUTPUT / "dataset.json"),
               "--predictions_path", str(OUTPUT / "predictions.json"), "--max_workers", "1", "--timeout", "900",
               "--namespace", "none", "--instance_image_tag", IMAGE_TAG, "--cache_level", "instance",
               "--run_id", OUTPUT.name, "--report_dir", str(OUTPUT)]
    result = subprocess.run(command, cwd=ROOT, env=environment)
    if result.returncode == 0:
        summarize_results()
    (OUTPUT / "progress.txt").write_text(f"harness finished exit_code={result.returncode}; inspect reports\n")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
