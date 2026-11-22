"""Repair infrastructure in isolation, then retry without hidden feedback."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench")
RUNS = ROOT / "agent_work/runs"
parser = argparse.ArgumentParser()
parser.add_argument("--output-name", default="lite119-infra-recovery-v2-20261121")
parser.add_argument("--retry-run", help="Select only infrastructure errors from this run")
options = parser.parse_args()
if Path(options.output_name).name != options.output_name:
    parser.error("output-name must be a directory name")
OUT = RUNS / options.output_name
SOURCE = OUT / "source"
os.chdir(ROOT)
sys.path[:0] = [str(SOURCE), str(SOURCE / "scripts")]
sys.path.extend(str(p) for p in Path("/home/artoria/workspace/RepoAgent/RepoAgent/.venv/lib").glob("python*/site-packages"))
import docker
import swebench_batch as batch

code = (RUNS / "lite-remaining119-from-issue38-resume-20261120/launcher.py").read_text()
code = code.split("with ThreadPoolExecutor(max_workers=2) as ex:")[0]
code = code.replace("(p/'selection.json').write_text(json.dumps(ids))", "")
ns = {}
exec(compile(code, "frozen-launcher-helpers", "exec"), ns)
ns.update(p=OUT, src=SOURCE)
ns["env"]["PYTHONPATH"] = str(SOURCE)
client = docker.from_env(timeout=1800)
prior = json.loads((RUNS / "lite119-infra-recovery-20261121/progress.json").read_text())
matplotlib = [x["instance_id"] for x in prior["errors"] if x["stage"] == "prepare" and x["instance_id"].startswith("matplotlib__")]
pytest = [x["instance_id"] for x in prior["errors"] if x["stage"] == "version_repair"]
requests = [x["instance_id"] for x in prior["completed"] if x.get("error")]
if options.retry_run:
    retry = json.loads((RUNS / options.retry_run / "progress.json").read_text())
    failed_ids = {x["instance_id"] for x in retry["errors"]}
    matplotlib = [i for i in matplotlib if i in failed_ids]
    pytest = [i for i in pytest if i in failed_ids]
    requests = [i for i in requests if i in failed_ids]
state = {"stage": "starting", "completed": [], "errors": []}


def save():
    (OUT / "progress.json").write_text(json.dumps(state, indent=2))


def space():
    import shutil
    if shutil.disk_usage("/mnt/d").free < 12 * 1024**3:
        raise RuntimeError("D: below 12 GiB, stop before next task")


def record_error(i, stage, exc):
    state["errors"].append({"instance_id": i, "stage": stage, "error": str(exc)})
    save()
    if "below 12 GiB" in str(exc):
        raise exc


def commit_checked(image, command, logfile, environment=None):
    c = client.containers.create(image, command, working_dir="/testbed",
                                 network_mode="host", environment=environment or {})
    try:
        c.start()
        status = c.wait(timeout=1800)
        logfile.parent.mkdir(parents=True, exist_ok=True)
        logfile.write_bytes(c.logs())
        if status["StatusCode"]:
            raise RuntimeError("Container repair failed: " + str(logfile))
        return c.commit(repository=image.rsplit(":", 1)[0], tag="latest").id
    finally:
        c.remove(force=True)


(OUT / "selection.json").write_text(json.dumps({"generation": matplotlib, "evaluation": pytest + requests}, indent=2))
save()
try:
    state["stage"] = "matplotlib_environment"
    save()
    for i in matplotlib:
        try:
            space()
            row = ns["rows"].loc[i].dropna().to_dict()
            row.update(instance_id=i, FAIL_TO_PASS="[]", PASS_TO_PASS="[]", test_patch="")
            spec = ns["make_test_spec"](row)
            # Modern standalone solver avoids the old libsolv assertion and classic solver OOM.
            with tempfile.TemporaryDirectory() as tmp:
                context = Path(tmp)
                import shutil
                shutil.copy("/tmp/repoagent-micromamba.tar.bz2", context / "micromamba.tar.bz2")
                script = spec.setup_env_script.replace("conda env create --file environment.yml",
                    "/opt/micromamba/bin/micromamba create -y -p /opt/miniconda3/envs/testbed -f environment.yml")
                script = script.replace("dependencies:\n", "dependencies:\n  - python=3.11\n", 1)
                # Conda packages do not use Python extras syntax. nbconvert
                # already depends on nbclient for notebook execution.
                script = script.replace("nbconvert[execute]", "nbconvert")
                script = script.replace("conda install python=3.11 -y",
                    "/opt/micromamba/bin/micromamba install -y -p /opt/miniconda3/envs/testbed python=3.11")
                (context / "setup_env.sh").write_text(script)
                df = spec.env_dockerfile.replace("COPY ./setup_env.sh /root/",
                    "COPY micromamba.tar.bz2 /tmp/micromamba.tar.bz2\n"
                    "RUN mkdir -p /opt/micromamba && tar -xjf /tmp/micromamba.tar.bz2 -C /opt/micromamba bin/micromamba && rm /tmp/micromamba.tar.bz2\n"
                    "ENV MAMBA_ROOT_PREFIX=/opt/miniconda3 MAMBA_DOWNLOAD_THREADS=2 MAMBA_EXTRACT_THREADS=1\n"
                    "ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple\n"
                    "COPY ./setup_env.sh /root/")
                (context / "Dockerfile").write_text(df)
                d = OUT / "cases" / i
                d.mkdir(parents=True, exist_ok=True)
                with (d / "solver-build.log").open("w") as log:
                    for event in client.api.build(path=str(context), tag=spec.env_image_key,
                            network_mode="host", rm=True, decode=True,
                            buildargs={"HTTP_PROXY": "http://127.0.0.1:7897", "HTTPS_PROXY": "http://127.0.0.1:7897"}):
                        log.write(event.get("stream", ""))
                        log.flush()
                        if event.get("error"):
                            raise RuntimeError(event["error"])
            ns["prepare"](i)
            ns["run"](i)
            state["completed"].append({"instance_id": i, "stage": "generation"})
            save()
        except Exception as exc:
            record_error(i, "environment_or_generation", exc)

    state["stage"] = "saved_patch_evaluation"
    save()
    argv = ns["base"]
    args = batch.build_parser().parse_args(argv[argv.index("--dataset"):])
    args.run_id = OUT.name
    args.harness_python = ROOT / ".venv/bin/python"
    args.swebench_dir = ROOT
    args.cache_level = "instance"
    env = dict(ns["env"])
    env.update(batch.read_env_file(RUNS / "lite-201to300-opencode-go/.env"))
    audit = json.loads((RUNS / "lite119-infra-recovery-20261121/audit.json").read_text())
    for i in pytest + requests:
        try:
            space()
            entry = audit["instances"][i]
            f = next((RUNS / entry["run"]).glob("**/" + i + "/summary.json"))
            row = json.loads(f.read_text())
            image = "sweb.eval.x86_64." + i + ":latest"
            d = OUT / "evaluation" / i
            d.mkdir(parents=True, exist_ok=True)
            if i in pytest:
                version = str(ns["rows"].loc[i]["version"])
                # This is an untracked SCM-generated artifact, not production source.
                program = (
                    "import pathlib,subprocess; "
                    "p=pathlib.Path('/testbed/src/_pytest/_version.py'); "
                    "assert not subprocess.check_output(['git','ls-files','--',str(p)],cwd='/testbed'); "
                    "p.write_text('version = __version__ = '+repr(" + repr(version) + ")+"
                    "'\\nversion_tuple = __version_tuple__ = ()\\n'); "
                    "import pytest; assert pytest.__version__ == " + repr(version) + "; print(pytest.__version__)"
                )
                commit_checked(image, ["/opt/miniconda3/envs/testbed/bin/python", "-c", program], d / "version-repair.log")
                commit_checked(image, ["bash", "-ec", "git diff --exit-code; /opt/miniconda3/envs/testbed/bin/python -m pytest --version"], d / "preflight.log")
            else:
                # Build-time download proxy is intentionally distinct from the test proxy.
                env["SWEBENCH_TEST_PROXY"] = "http://127.0.0.1:7897"
                env["SWEBENCH_TEST_NO_PROXY"] = "localhost,127.0.0.1,::1"
                probe = client.containers.run(image, ["/opt/miniconda3/envs/testbed/bin/python",
                    "-c", "import requests; r=requests.get('http://httpbin.org/get',timeout=15); r.raise_for_status(); payload=r.json() if callable(r.json) else r.json; assert payload['url']; print('httpbin reachable')"],
                    network_mode="host", remove=True,
                    environment={"HTTP_PROXY": env["SWEBENCH_TEST_PROXY"], "HTTPS_PROXY": env["SWEBENCH_TEST_PROXY"]})
                (d / "network-preflight.log").write_bytes(probe)
            patch = row["model_patch"]
            assert patch.strip()
            result = batch.run_harness_for_instance(row, patch, d, args, env, 0)
            (d / "result.json").write_text(json.dumps(result, indent=2))
            state["completed"].append({"instance_id": i, "stage": "evaluation",
                                       "resolved": result.get("resolved"), "error": result.get("error")})
            save()
        except Exception as exc:
            record_error(i, "saved_patch_evaluation", exc)
    state["stage"] = "finished"
except Exception as exc:
    state["stage"] = "stopped"
    state["stop_reason"] = repr(exc)
    raise
finally:
    save()
