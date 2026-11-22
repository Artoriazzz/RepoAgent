"""Run infrastructure retries and evaluate saved patches without model feedback."""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench")
RUNS = ROOT / "agent_work/runs"
OUT = RUNS / "lite119-infra-recovery-20261121"
OUT.mkdir(exist_ok=True)
os.chdir(ROOT)
previous = RUNS / "lite-remaining119-from-issue38-resume-20261120"
source = OUT / "source"
sys.path.insert(0, str(source))
sys.path.insert(0, str(source / "scripts"))
sys.path.extend(str(p) for p in Path("/home/artoria/workspace/RepoAgent/RepoAgent/.venv/lib").glob("python*/site-packages"))
import swebench_batch as batch

# Reuse the frozen launcher helpers without executing its batch loop.
code = (previous / "launcher.py").read_text().split("with ThreadPoolExecutor(max_workers=2) as ex:")[0]
code = code.replace("(p/'selection.json').write_text(json.dumps(ids))", "")
ns = {}
exec(compile(code, str(previous / "launcher.py"), "exec"), ns)
ns.update(p=OUT, src=source)
ns["env"]["PYTHONPATH"] = str(source)
audit = json.loads((OUT / "audit.json").read_text())
selection = json.loads((OUT / "selection.json").read_text())
progress = OUT / "progress.json"
state = json.loads(progress.read_text()) if progress.exists() else {"completed": [], "errors": []}
state.update(stage="cleanup")
state.pop("stop_reason", None)


def save():
    (OUT / "progress.json").write_text(json.dumps(state, indent=2))


def space():
    if __import__("shutil").disk_usage("/mnt/d").free < 12 * 1024**3:
        raise RuntimeError("D: free space below 12 GiB; stopped before new task")


def summary_files():
    for name in ("lite-remaining119-from-issue38-20261120",
                 "lite-remaining119-from-issue38-resume-20261120"):
        yield from (RUNS / name).glob("cases/*/results/*/summary.json")


safe = {i for i, v in audit["instances"].items()
        if v["classification"] in {"resolved", "confirmed_failure"}}
for f in summary_files():
    row = json.loads(f.read_text())
    h = (row.get("passes") or [{}])[-1].get("harness") or {}
    text = Path(h["test_output_path"]).read_text(errors="replace") if h.get("test_output_path") and Path(h["test_output_path"]).exists() else ""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    failures = [x for k in ("FAIL_TO_PASS", "PASS_TO_PASS")
                for x in (h.get("instance_report", {}).get("tests_status", {}).get(k, {}).get("failure") or [])]
    if row.get("resolved") or (row.get("status") == "agent_ok"
            and h.get("instance_report", {}).get("patch_successfully_applied")
            and failures and all("FAILED " + x in text for x in failures)
            and not re.search(r"(?m)^ERROR:|^ERROR collecting", text)):
        safe.add(row["instance_id"])
safe.difference_update(selection["evaluation"])
safe.difference_update(selection["generation"])
images = subprocess.check_output(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], text=True).splitlines()
state.setdefault("removed_images", [])
for ref in images:
    m = re.fullmatch(r"sweb\.eval\.(?:x86_64|arm64)\.([\w.-]+):latest", ref)
    if m and m[1] in safe:
        result = subprocess.run(["docker", "image", "rm", ref], capture_output=True, text=True)
        if result.returncode == 0:
            state["removed_images"].append(ref)
subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "pip", "cache", "purge"], check=False)
# Only dangling leaves and unused build cache; never prune named images or volumes.
for label, cmd in (
    ("dangling-images", ["docker", "image", "prune", "-f"]),
    ("build-cache", ["docker", "builder", "prune", "-f"]),
):
    with (OUT / (label + ".log")).open("a") as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)
save()

# Only infrastructure script changes: classic solver, exact legacy setuptools from pip.
prepare_code = code[code.index("def prepare(i):"):code.index("def run(i):")]
prepare_code = prepare_code.replace("spec.setup_env_script", "spec.setup_env_script.replace('setuptools==38.2.4', '').replace('conda activate testbed', 'conda activate testbed\\npython -m pip install setuptools==38.2.4', 1) if i == 'astropy__astropy-7746' else spec.setup_env_script", 1)
prepare_code = prepare_code.replace("remote_connect_timeout_secs: 15", "solver: classic\\nremote_connect_timeout_secs: 15")
exec(compile(prepare_code, "infrastructure-preflight", "exec"), ns)
prepare = ns["prepare"]


def repaired_prepare(i):
    space()
    # A prior Astropy build changed tracked packaging files; restore the exact baseline tree.
    if i == "astropy__astropy-14182":
        import docker
        client = docker.from_env(timeout=600)
        image = "sweb.eval.x86_64." + i + ":latest"
        commit = ns["rows"].loc[i]["base_commit"]
        container = client.containers.create(image, ["bash", "-ec",
            "git restore --source=" + commit + " --staged --worktree -- . && git commit --allow-empty -m restore-baseline && git diff --exit-code " + commit + " HEAD"],
            working_dir="/testbed", network_disabled=True)
        try:
            container.start()
            if container.wait()["StatusCode"]:
                raise RuntimeError(container.logs().decode(errors="replace"))
            container.commit(repository=image.rsplit(":", 1)[0], tag="latest")
        finally:
            container.remove()
    prepare(i)


try:
    state["stage"] = "generation_retries"
    save()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = []
        for i in selection["generation"]:
            existing = OUT / "cases" / i / "results" / i / "summary.json"
            if existing.exists() and json.loads(existing.read_text()).get("status") not in {"api_error", "runner_error", "timeout"}:
                continue
            if len(pending) >= 2:
                pending.pop(0).result()
            try:
                repaired_prepare(i)
            except Exception as exc:
                state["errors"].append({"instance_id": i, "stage": "prepare", "error": str(exc)})
                save()
                if "D: free space" in str(exc):
                    raise
                continue
            pending.append(pool.submit(ns["run"], i))
        for job in pending:
            job.result()
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
    for i in selection["evaluation"]:
        saved = OUT / "evaluation" / i / "result.json"
        if saved.exists() and not json.loads(saved.read_text()).get("error", True):
            continue
        space()
        entry = audit["instances"][i]
        f = RUNS / entry["run"] / "cases" / i / "results" / i / "summary.json"
        if not f.exists():
            f = next((RUNS / entry["run"]).glob("**/" + i + "/summary.json"))
        prior = json.loads(f.read_text())
        patch = prior["model_patch"]
        assert patch.strip(), i
        if i.startswith("pytest-dev__"):
            # Restore distribution version metadata, not production/test behavior.
            import docker
            client = docker.from_env(timeout=600)
            image = "sweb.eval.x86_64." + i + ":latest"
            version = str(ns["rows"].loc[i]["version"])
            c = client.containers.create(image, ["bash", "-ec",
                "cd /testbed && SETUPTOOLS_SCM_PRETEND_VERSION=" + version
                + " /opt/miniconda3/envs/testbed/bin/python -m pip install --no-deps --no-build-isolation -e ."
                + " && /opt/miniconda3/envs/testbed/bin/python -m pytest --version"],
                network_mode="host")
            try:
                c.start()
                status = c.wait()
                log = OUT / "evaluation" / i / "version-repair.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_bytes(c.logs())
                if status["StatusCode"] != 0:
                    raise RuntimeError("pytest metadata repair failed; see " + str(log))
                c.commit(repository=image.rsplit(":", 1)[0], tag="latest")
            except Exception as exc:
                state["errors"].append({"instance_id": i, "stage": "version_repair", "error": str(exc)})
                save()
                continue
            finally:
                c.remove()
        try:
            result = batch.run_harness_for_instance(prior, patch, OUT / "evaluation" / i, args, env, 0)
        except (subprocess.SubprocessError, OSError) as exc:
            state["errors"].append({"instance_id": i, "stage": "harness", "error": str(exc)})
            save()
            continue
        (OUT / "evaluation" / i / "result.json").write_text(json.dumps(result, indent=2))
        state["completed"].append({"instance_id": i, "resolved": result.get("resolved"), "error": result.get("error")})
        save()
    state["stage"] = "finished"
except Exception as exc:
    state["stage"] = "stopped"
    state["stop_reason"] = repr(exc)
    raise
finally:
    save()
