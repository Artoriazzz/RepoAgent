"""在 WSL 冻结当前代码，执行历史 71 题名单的前十题。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import traceback


SWE = Path("/home/artoria/workspace/SWE-bench")
AGENT_PYTHON = Path("/home/artoria/workspace/RepoAgent/RepoAgent/.venv/bin/python")
HARNESS_PYTHON = SWE / ".venv/bin/python"
ENV_FILE = Path("/home/artoria/workspace/RepoAgent/RepoAgent/.env")
SELECTION = SWE / "agent_work/runs/lite-vector35-pass1-glm-20261026/selection.json"
DATASET = SWE / "test-00000-of-00001.parquet"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare(source, instance, destination):
    import docker
    import pandas as pd

    sys.path.insert(0, str(source / "scripts"))
    from run_failed5_preflight_wsl import prepare_image

    row = pd.read_parquet(DATASET).set_index("instance_id").loc[instance].to_dict()
    row["instance_id"] = instance
    with (destination / "environment.log").open("w", buffering=1) as log:
        client = docker.from_env()
        result = prepare_image(row, SWE, client, log, canonical=True, proxy="",
                               imports="import django; from django.db import models")
        # 仅检查已存在的公开基础用例，镜像不加载官方新增测试。
        command = (f"git diff --exit-code {row['base_commit']} -- django tests && "
                   "python tests/runtests.py "
                   "invalid_models_tests.test_ordinary_fields.CharFieldTests.test_valid_field "
                   "--settings=test_sqlite --parallel 1 -v 1")
        output = client.containers.run(result["image"], ["bash", "-lc", command],
                                       working_dir="/testbed", network_disabled=True, remove=True)
        log.write(output.decode())
    save(destination / "environment-ready.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--offset", type=int, default=0, help="固定名单中的起始位置，从 0 开始，每批十题")
    parser.add_argument("--prepare-instance")
    args = parser.parse_args()
    if args.offset < 0:
        parser.error("--offset must be non-negative")
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    if args.prepare_instance:
        prepare(args.source, args.prepare_instance, run)
        return 0

    # 重启时仍使用该轮冻结代码，防止后续开发改变同批任务。
    source = run / "source"
    if not source.exists():
        shutil.copytree(args.source, source,
                        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".env", ".env.*"))
    selected = json.loads(SELECTION.read_text())["instances"][args.offset:args.offset + 10]
    assert len(selected) == 10 and all(item["repo"] == "django/django" for item in selected)
    manifest = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob("*.py")}
    save(run / "experiment.json", {"started_at": now(), "selection_source": str(SELECTION),
         "instances": selected, "selection_offset": args.offset,
         "workers": args.workers, "source_hashes": manifest,
         "model": "glm-5.3-flash", "reasoning_effort": "high", "max_steps": 24,
         "memory_recall": False, "feedback_source": "public-tests", "final_harness_only": True,
         "cache_level": "instance"})
    wrapper = run / "repo-agent.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(AGENT_PYTHON)) + ' -m repo_agent "$@"\n')
    wrapper.chmod(0o755)
    states = {item["id"]: {"index": index, "status": "queued"}
              for index, item in enumerate(selected, 1)}
    lock = threading.Lock()
    build_lock = threading.Lock()

    def update(instance, **values):
        with lock:
            states[instance].update(values, updated_at=now())
            finished = sum(v["status"] in {"done", "environment_error", "runner_error"} for v in states.values())
            save(run / "progress.json", {"updated_at": now(), "finished": finished,
                                         "total": 10, "instances": states})
            print(f"[{states[instance]['index']}/10] {instance}: {values.get('status', states[instance]['status'])}", flush=True)

    def execute(item):
        instance = item["id"]
        case_dir = run / "cases" / instance
        case_dir.mkdir(parents=True, exist_ok=True)
        summary = case_dir / "results" / instance / "summary.json"
        if summary.exists():
            update(instance, status="done", summary=str(summary), resumed=True)
            return
        try:
            with build_lock:
                update(instance, status="preparing_environment")
                with (case_dir / "preflight.log").open("w") as log:
                    subprocess.run([str(HARNESS_PYTHON), str(source / "scripts" / Path(__file__).name),
                                    "--source", str(source), "--run-dir", str(case_dir),
                                    "--prepare-instance", instance], stdout=log, stderr=subprocess.STDOUT,
                                   check=True, timeout=1800)
            update(instance, status="running")
            work = case_dir / "work"
            work.mkdir(exist_ok=True)
            mirror = work / "repo_cache"
            if not mirror.exists():
                mirror.symlink_to(SWE / "agent_work/repo_cache", target_is_directory=True)
            env = os.environ.copy()
            env.update(PYTHONPATH=str(source), PATH="/home/artoria/.local/bin:" + env.get("PATH", ""),
                       SWEBENCH_REPO_MIRROR_ROOT=str(SWE / "agent_work/repo_cache"))
            command = [str(AGENT_PYTHON), "-u", str(source / "scripts/swebench_batch.py"),
                "--dataset", str(DATASET), "--repo-agent-bin", str(wrapper),
                "--model", "glm-5.3-flash", "--reasoning-effort", "high",
                "--coordinator-reasoning-effort", "high", "--implement-reasoning-effort", "high",
                "--work-dir", str(work), "--output-dir", str(case_dir / "results"),
                "--env-file", str(ENV_FILE), "--proxy", "http://127.0.0.1:7897", "--offline-cache",
                "--instance-id", instance, "--limit", "1", "--repair-mode", "experience-build",
                "--feedback-source", "public-tests", "--no-repair-memory", "--memory-root", str(case_dir / "memory"),
                "--memory-provenance", "public-tests-independent-first10", "--candidate-mode", "off",
                "--max-steps", "24", "--timeout", "3600", "--prebuild-instance-image",
                "--image-build-timeout", "1800", "--evaluate", "--swebench-dir", str(SWE),
                "--harness-python", str(SWE / ".venv/bin/python"), "--run-id", run.name,
                # Harness 的全局清理会误删另一并行题刚创建的镜像，整批结束前保留实例缓存。
                "--harness-timeout", "900", "--cache-level", "instance", "--namespace", "none",
                "--docker-network", "host"]
            with (case_dir / "run.log").open("w") as log:
                result = subprocess.run(command, env=env, cwd=source, stdout=log, stderr=subprocess.STDOUT)
            if not summary.exists():
                raise RuntimeError(f"Batch exited {result.returncode} without summary")
            data = json.loads(summary.read_text())
            update(instance, status="done", agent_status=data.get("status"), resolved=data.get("resolved"),
                   seconds=data.get("seconds"), summary=str(summary), exit_code=result.returncode)
        except Exception as exc:
            (case_dir / "exception.log").write_text(traceback.format_exc())
            update(instance, status="environment_error" if not (case_dir / "environment-ready.json").exists()
                   else "runner_error", error=f"{type(exc).__name__}: {exc}")

    save(run / "progress.json", {"total": 10, "finished": 0, "instances": states})
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(execute, item) for item in selected]
        for future in as_completed(futures):
            future.result()
    predictions, summaries = [], []
    for item in selected:
        case_dir = run / "cases" / item["id"] / "results"
        if (case_dir / "predictions.json").exists():
            predictions.extend(json.loads((case_dir / "predictions.json").read_text()))
        summaries.append(states[item["id"]])
    save(run / "predictions.json", predictions)
    save(run / "summary.json", summaries)
    (run / "finished.txt").write_text(now() + "\n")
    return int(any(row["status"] != "done" for row in summaries))


if __name__ == "__main__":
    raise SystemExit(main())
