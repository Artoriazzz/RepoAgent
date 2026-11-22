"""Re-evaluate saved pytest Lite patches after restoring SCM version metadata."""

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
import pandas as pd
import swebench_batch as batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--fixed-scm-version")
    options = parser.parse_args()
    instance_id = options.instance_id
    if not instance_id.startswith("pytest-dev__pytest-") or Path(options.output_name).name != options.output_name:
        parser.error("expected a pytest-dev Lite ID and one output directory name")
    out = RUNS / options.output_name
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    audit = json.loads(Path("/tmp/repoagent-audit-lite129-result.json").read_text())["rows"]
    entry = audit[instance_id]
    source_summary = RUNS / entry["run"] / "cases" / instance_id / "results" / instance_id / "summary.json"
    if not source_summary.exists():
        provenance = json.loads((RUNS / "lite119-infra-recovery-20261121/audit.json").read_text())["instances"][instance_id]
        source_summary = next((RUNS / provenance["run"]).glob("**/" + instance_id + "/summary.json"))
    row = json.loads(source_summary.read_text())
    patch = row["model_patch"]
    assert patch.strip(), instance_id
    version = pd.read_parquet(ROOT / "test-00000-of-00001.parquet", columns=["instance_id", "version"])
    version = str(version.set_index("instance_id").loc[instance_id, "version"])
    if options.fixed_scm_version:
        version = options.fixed_scm_version
    client = docker.from_env(timeout=1800)
    image = "sweb.eval.x86_64." + instance_id + ":latest"
    original = client.images.get(image)
    # Keep the previous image addressable; a failed preflight must not replace it.
    backup = "repoagent-pytest-before-version-fix:" + instance_id.split("-")[-1]
    original.tag(backup)
    install_scm = (
        "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple "
        "/opt/miniconda3/envs/testbed/bin/python -m pip install -q vcs-versioning==2.6.0; "
    )
    command = ["bash", "-ec", "cd /testbed; git diff --exit-code; "
        f"git tag {version}; "
        + install_scm
        + "/opt/miniconda3/envs/testbed/bin/python setup.py egg_info; "
        f"/opt/miniconda3/envs/testbed/bin/python -c \"import pytest; assert pytest.__version__ == '{version}', pytest.__version__\"; "
        "git diff --exit-code"]
    metadata_env = {"SETUPTOOLS_SCM_PRETEND_VERSION": version} if options.fixed_scm_version else {}
    container = client.containers.create(image, command, working_dir="/testbed", network_mode="host", environment=metadata_env)
    try:
        container.start()
        status = container.wait(timeout=1800)
        (out / "version-preflight.log").write_bytes(container.logs())
        if status["StatusCode"]:
            raise RuntimeError("version preflight failed")
        repaired = container.commit(repository="repoagent-pytest-version-fix/" + instance_id, tag="verified")
    finally:
        container.remove(force=True)
    preflight = client.containers.run(repaired.id, ["bash", "-ec",
        "cd /testbed; /opt/miniconda3/envs/testbed/bin/python -m pip install -e . --no-build-isolation --no-deps -q; "
        "/opt/miniconda3/envs/testbed/bin/python -m pytest --version"],
        network_mode="host", remove=True)
    (out / "reinstall-preflight.log").write_bytes(preflight)
    repaired.tag(image.rsplit(":", 1)[0], tag="latest")
    (out / "image-manifest.json").write_text(json.dumps({"instance_id": instance_id,
        "original_image": original.id, "backup_tag": backup, "repaired_image": repaired.id,
        "version": version, "source_summary": str(source_summary)}, indent=2))

    cfg = json.loads((RUNS / "lite-101to150-public-no-memory-20261112/lane-100/command.json").read_text())
    args = batch.build_parser().parse_args(cfg[cfg.index("--dataset"):])
    args.run_id = options.output_name
    args.harness_python = ROOT / ".venv/bin/python"
    args.swebench_dir = ROOT
    args.cache_level = "instance"
    args.docker_network = "host"
    env = dict(os.environ)
    env.update(batch.read_env_file(RUNS / "lite-201to300-opencode-go/.env"))
    result = batch.run_harness_for_instance(row, patch, out / "evaluation", args, env, 0)
    (out / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"instance_id": instance_id, "resolved": result.get("resolved"),
        "error": result.get("error"), "report_path": result.get("report_path")}), flush=True)


if __name__ == "__main__":
    main()
