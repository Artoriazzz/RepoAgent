"""Evaluate a saved patch against a real local httpbin without proxy interference."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench")
RUNS = ROOT / "agent_work/runs"
OUT = RUNS / "requests863-local-httpbin-20261122"
SOURCE = RUNS / "lite119-infra-recovery-v3-20261121/source"
sys.path[:0] = [str(SOURCE), str(SOURCE / "scripts")]
sys.path.extend(str(p) for p in Path("/home/artoria/workspace/RepoAgent/RepoAgent/.venv/lib").glob("python*/site-packages"))
import docker
import swebench_batch as batch

os.chdir(ROOT)
image = "sweb.eval.x86_64.psf__requests-863:latest"
client = docker.from_env(timeout=600)
original = client.images.get(image)
# Use the test suite's existing HTTPBIN_URL setting. Do not rewrite tests or assertions.
c = client.containers.create(image, ["true"],
    environment={"HTTPBIN_URL": "http://127.0.0.1:18081/"})
try:
    c.start()
    assert c.wait()["StatusCode"] == 0
    updated = c.commit(repository=image.rsplit(":", 1)[0], tag="latest")
finally:
    c.remove()
(OUT / "image-manifest.json").write_text(json.dumps({
    "original": original.id, "local_httpbin_image": updated.id,
    "service": "httpbin==0.10.2", "url": "http://127.0.0.1:18081/",
}, indent=2))
environment = {k: "" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
preflight = client.containers.run(image, [
    "/opt/miniconda3/envs/testbed/bin/python", "-m", "pytest", "-q",
    "tests/test_requests.py::RequestsTestSuite::test_connection_keepalive_and_close",
    "tests/test_requests.py::RequestsTestSuite::test_useful_exception_for_invalid_port",
], network_mode="host", remove=True, environment=environment)
(OUT / "preflight.log").write_bytes(preflight)
print(preflight.decode(), flush=True)
cfg = json.loads((RUNS / "lite-101to150-public-no-memory-20261112/lane-100/command.json").read_text())
args = batch.build_parser().parse_args(cfg[cfg.index("--dataset"):])
args.run_id = OUT.name
args.harness_python = ROOT / ".venv/bin/python"
args.swebench_dir = ROOT
args.cache_level = "instance"
args.docker_network = "host"
env = dict(os.environ, SWEBENCH_TEST_PROXY="", SWEBENCH_TEST_NO_PROXY="")
env.update(batch.read_env_file(RUNS / "lite-201to300-opencode-go/.env"))
prior = json.loads((RUNS / "lite119-infra-recovery-20261121/audit.json").read_text())["instances"]["psf__requests-863"]
f = next((RUNS / prior["run"]).glob("**/psf__requests-863/summary.json"))
row = json.loads(f.read_text())
result = batch.run_harness_for_instance(row, row["model_patch"], OUT / "evaluation", args, env, 0)
(OUT / "result.json").write_text(json.dumps(result, indent=2))
print("Harness finished:", result.get("resolved"), "error:", result.get("error"), flush=True)
