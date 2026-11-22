"""Repair a known Lite test environment and re-evaluate its saved patch."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench")
RUNS = ROOT / "agent_work/runs"
SOURCE = RUNS / "lite119-infra-recovery-v3-20261121/source"
sys.path[:0] = [str(SOURCE), str(SOURCE / "scripts")]
sys.path.extend(str(p) for p in Path("/home/artoria/workspace/RepoAgent/RepoAgent/.venv/lib").glob("python*/site-packages"))

import docker
import swebench_batch as batch

PIP = "/opt/miniconda3/envs/testbed/bin/python -m pip"
INDEX = "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple "
HTTPBIN = "http://127.0.0.1:18081/"


def source_row(instance_id, audit):
    entry = audit[instance_id]
    path = RUNS / entry["run"] / "cases" / instance_id / "results" / instance_id / "summary.json"
    if not path.exists():
        provenance = json.loads((RUNS / "lite119-infra-recovery-20261121/audit.json").read_text())["instances"][instance_id]
        path = next((RUNS / provenance["run"]).glob("**/" + instance_id + "/summary.json"))
    return path, json.loads(path.read_text())


def repair_spec(instance_id):
    if instance_id.startswith("matplotlib__"):
        command = f"{PIP} uninstall -y vcs-versioning; "
        if instance_id in {"matplotlib__matplotlib-24334", "matplotlib__matplotlib-25079", "matplotlib__matplotlib-26011"}:
            command += INDEX + f"{PIP} install -q pandas==1.5.3; "
        return "matplotlib", command
    if instance_id.startswith("sphinx-doc__"):
        if instance_id in {"sphinx-doc__sphinx-8273", "sphinx-doc__sphinx-8282"}:
            import shlex
            program = (
                "from pathlib import Path; "
                "p=Path('/opt/miniconda3/envs/testbed/bin/pytest'); "
                "p.write_text('#!/bin/sh\\nexec /opt/miniconda3/envs/testbed/bin/python -m pytest -v -rA \\\"$@\\\"\\n'); "
                "p.chmod(0o755)"
            )
            return "sphinx", "/opt/miniconda3/envs/testbed/bin/python -c " + shlex.quote(program) + "; "
        return "sphinx", ""
    if instance_id == "pallets__flask-4045":
        return "flask", INDEX + f"{PIP} install -q Werkzeug==2.0.3; "
    if instance_id == "astropy__astropy-14182":
        return "astropy", (
            "/opt/miniconda3/envs/testbed/bin/python -c \"from pathlib import Path; "
            "Path('/opt/repoagent-build-constraints.txt').write_text('setuptools==59.8.0\\nsetuptools-scm==7.1.0\\n')\"; "
        ) + INDEX + f"{PIP} install -q setuptools==59.8.0; "
    if instance_id.startswith("psf__requests-"):
        return "requests", ""
    raise ValueError(instance_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--requests-connect-relay", action="store_true")
    options = parser.parse_args()
    if Path(options.output_name).name != options.output_name:
        parser.error("output-name must be one directory name")
    instance_id = options.instance_id
    group, command = repair_spec(instance_id)
    if options.requests_connect_relay:
        command += "cp /etc/ssl/certs/ca-certificates.crt /testbed/requests/cacert.pem; "
    out = RUNS / options.output_name
    out.mkdir(parents=True, exist_ok=True)
    audit = json.loads(Path("/tmp/repoagent-audit-lite129-result.json").read_text())["rows"]
    source_path, row = source_row(instance_id, audit)
    patch = row["model_patch"]
    assert patch.strip(), instance_id
    os.chdir(ROOT)
    client = docker.from_env(timeout=1800)
    image = "sweb.eval.x86_64." + instance_id + ":latest"
    original = client.images.get(image)
    backup = "repoagent-env-before-fix:" + instance_id.replace("__", "-")
    try:
        client.images.get(backup)
    except docker.errors.ImageNotFound:
        original.tag(backup)
    extra_env = {}
    if group == "astropy":
        extra_env["PIP_CONSTRAINT"] = "/opt/repoagent-build-constraints.txt"
    if group == "sphinx":
        extra_env["PYTEST_ADDOPTS"] = "-v"
    if group == "requests":
        extra_env["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
        extra_env.update(HTTPBIN_URL="http://httpbin.org/", HTTP_PROXY="http://127.0.0.1:7897", HTTPS_PROXY="http://127.0.0.1:7897", ALL_PROXY="",
            http_proxy="", https_proxy="", all_proxy="")
        if options.requests_connect_relay:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                extra_env[key] = ""
    probe = {
        "matplotlib": "/opt/miniconda3/envs/testbed/bin/python -c 'import matplotlib; print(matplotlib.__version__)'",
        "sphinx": "test \"$PYTEST_ADDOPTS\" = -v",
        "flask": "/opt/miniconda3/envs/testbed/bin/python -c 'from werkzeug.urls import url_parse; print(url_parse)'",
        "astropy": "/opt/miniconda3/envs/testbed/bin/python -c 'from setuptools.dep_util import newer_group; print(newer_group)'",
        "requests": "/opt/miniconda3/envs/testbed/bin/python -c 'import requests; r=requests.get(\"http://127.0.0.1:18081/get\", timeout=15); r.raise_for_status(); print(r.status_code)'",
    }[group]
    container = client.containers.create(image, ["bash", "-ec", command + probe],
        working_dir="/testbed", network_mode="host", environment=extra_env)
    try:
        container.start()
        status = container.wait(timeout=1800)
        (out / "environment-preflight.log").write_bytes(container.logs())
        if status["StatusCode"]:
            raise RuntimeError("environment preflight failed")
        repaired = container.commit(repository="repoagent-repaired/" + instance_id, tag="verified")
    finally:
        container.remove(force=True)
    repaired.tag(image.rsplit(":", 1)[0], tag="latest")
    (out / "image-manifest.json").write_text(json.dumps({"instance_id": instance_id,
        "group": group, "original_image": original.id, "backup_tag": backup,
        "repaired_image": repaired.id, "source_summary": str(source_path),
        "certificate_refresh": "/testbed/requests/cacert.pem from system CA" if options.requests_connect_relay else None,
        "http_service": "local upstream httpbin HTTP; opaque CONNECT HTTPS" if options.requests_connect_relay else None}, indent=2))

    cfg = json.loads((RUNS / "lite-101to150-public-no-memory-20261112/lane-100/command.json").read_text())
    args = batch.build_parser().parse_args(cfg[cfg.index("--dataset"):])
    args.run_id = options.output_name
    args.harness_python = ROOT / ".venv/bin/python"
    args.swebench_dir = ROOT
    args.cache_level = "instance"
    args.docker_network = "host"
    env = dict(os.environ, SWEBENCH_TEST_PROXY="", SWEBENCH_TEST_NO_PROXY="",
        SWEBENCH_REPO_MIRROR_ROOT=str(ROOT / "agent_work/repo_cache"),
        SWEBENCH_RAW_PROXY="http://127.0.0.1:7897", HTTPS_PROXY="http://127.0.0.1:7897")
    env.update(batch.read_env_file(RUNS / "lite-201to300-opencode-go/.env"))
    if group == "requests" and not options.requests_connect_relay:
        env["SWEBENCH_TEST_PROXY"] = "http://127.0.0.1:7897"
        env["SWEBENCH_TEST_NO_PROXY"] = "localhost,127.0.0.1,::1"
    if options.requests_connect_relay:
        env["SWEBENCH_TEST_EXTRA_HOSTS"] = json.dumps({"httpbin.org": "127.0.0.2", "www.google.co.uk": "127.0.0.3", "www.google.com": "127.0.0.4"})
    result = batch.run_harness_for_instance(row, patch, out / "evaluation", args, env, 0)
    (out / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"instance_id": instance_id, "group": group,
        "resolved": result.get("resolved"), "error": result.get("error"),
        "report_path": result.get("report_path")}), flush=True)


if __name__ == "__main__":
    main()
