"""从冻结源码运行 Lite 小批量实验，逐题预检并保存首轮和最终成绩。"""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from run_failed5_preflight_wsl import prepare_image


def main():
    import docker
    import pandas as pd

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--repair-mode", choices=["benchmark", "experience-build"], required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    live = Path("/home/artoria/workspace/RepoAgent/RepoAgent")
    swebench = Path("/home/artoria/workspace/SWE-bench")
    work = swebench / "agent_work"
    output = work / "runs" / args.run_name
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / "results.json"
    if results_path.exists() and not args.resume:
        raise SystemExit("Existing results found; use --resume with the same frozen source and arguments")
    dataset = swebench / "test-00000-of-00001.parquet"
    frame = pd.read_parquet(dataset)
    if args.start_index < 0 or args.limit < 1 or args.start_index + args.limit > len(frame):
        raise SystemExit("Requested range is outside the dataset")
    rows = frame.iloc[args.start_index:args.start_index + args.limit].to_dict("records")
    config = {
        "dataset": str(dataset), "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "instance_ids": [r["instance_id"] for r in rows], "start_index": args.start_index,
        "limit": args.limit, "repair_mode": args.repair_mode, "model": "glm-5.3-flash",
        "coordinator_reasoning_effort": "high", "other_reasoning_effort": "low", "max_steps": 16,
        "source_root": str(project), "source_sha256": {
            str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ["repo_agent", "scripts"] for p in sorted((project / folder).rglob("*.py"))
        },
    }
    config_path = output / "experiment.json"
    if args.resume and config_path.exists() and json.loads(config_path.read_text()) != config:
        raise SystemExit("Resume configuration/source differs from the saved experiment")
    config_path.write_text(json.dumps(config, indent=2))
    (output / "instances.json").write_text(json.dumps(config["instance_ids"], indent=2))
    results = json.loads(results_path.read_text()) if args.resume and results_path.exists() else []
    finished = {r["instance_id"] for r in results}
    environment_path = output / "environment.json"
    environments = json.loads(environment_path.read_text()) if environment_path.exists() else {}
    client = docker.from_env()
    env = {**os.environ, "PYTHONPATH": str(project), "NO_PROXY": "open.bigmodel.cn",
           "no_proxy": "open.bigmodel.cn", "REPO_AGENT_LLM_TIMEOUT_SECONDS": "300",
           "REPO_AGENT_LLM_MAX_RETRIES": "1", "SWEBENCH_RAW_PROXY": "http://127.0.0.1:7897",
           "SWEBENCH_RAW_URL_PREFIX": "https://ghproxy.net"}

    def progress(text):
        line = datetime.now(timezone.utc).isoformat() + " " + text
        (output / "progress.txt").write_text(line + "\n")
        print(line, flush=True)

    def save_result(result):
        results.append(result)
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        first = sum(bool((r.get("passes") or [{}])[0].get("initial_harness",
                         (r.get("passes") or [{}])[0].get("harness", {})).get("resolved")) for r in results)
        summary = {"completed": len(results), "total": len(rows), "initial_resolved": first,
                   "final_resolved": sum(bool(r.get("resolved")) for r in results),
                   "environment_errors": sum(r.get("status") == "environment_error" for r in results)}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        predictions = []
        for row in rows:
            path = output / row["instance_id"] / "predictions.json"
            if path.exists():
                predictions.extend(json.loads(path.read_text()))
        (output / "predictions.json").write_text(json.dumps(predictions, indent=2))

    for index, row in enumerate(rows, 1):
        instance = row["instance_id"]
        if instance in finished:
            continue
        progress(f"preflight {index}/{len(rows)} {instance}")
        try:
            imports = {"astropy/astropy": "import astropy, pytest",
                       "django/django": "import django; import django.test"}[row["repo"]]
            with (output / (instance + "-environment.log")).open("w") as log:
                environments[instance] = prepare_image(
                    row, swebench, client, log, imports=imports,
                    proxy="")
        except Exception as exc:
            environments[instance] = {"status": "error", "error": str(exc)}
        environment_path.write_text(json.dumps(environments, indent=2))
        if environments[instance]["status"] != "ready":
            save_result({"instance_id": instance, "status": "environment_error", "resolved": False,
                         "environment": environments[instance]})
            progress(f"finished {index}/{len(rows)} {instance} environment_error; agent not started")
            continue
        case = output / instance
        command = [str(live / ".venv/bin/python"), str(project / "scripts/swebench_batch.py"),
            "--dataset", str(dataset), "--repo-agent-bin", str(live / ".venv/bin/repo-agent"),
            "--model", "glm-5.3-flash", "--implement-model", "glm-5.3-flash",
            "--reasoning-effort", "low", "--coordinator-reasoning-effort", "high",
            "--implement-reasoning-effort", "low", "--max-steps", "16",
            "--work-dir", str(work), "--output-dir", str(case), "--env-file", str(live / ".env"),
            "--proxy", "http://127.0.0.1:7897", "--offline-cache", "--instance-id", instance,
            "--repair-mode", args.repair_mode, "--timeout", "3600", "--evaluate",
            "--swebench-dir", str(swebench), "--harness-python", str(swebench / ".venv/bin/python"),
            "--run-id", args.run_name + "-" + str(index), "--harness-timeout", "900",
            "--cache-level", "env", "--namespace", "none", "--docker-network", "host",
            "--candidate-mode", "off", "--no-prebuild-instance-image", "--no-repair-memory",
            "--memory-root", str(output / "isolated-memory"), "--memory-scope", row["repo"],
            "--memory-provenance", "swebench-test-diagnostic"]
        progress(f"running {index}/{len(rows)} {instance}")
        with (output / (instance + ".log")).open("w") as log:
            completed = subprocess.run(command, cwd=project,
                env={**env, "REPO_AGENT_LLM_LOG_DIR": str(case / "llm-requests")},
                stdout=log, stderr=subprocess.STDOUT)
        path = case / "batch_summary.json"
        result = json.loads(path.read_text())["instances"][0] if path.exists() else {
            "instance_id": instance, "status": "runner_failed", "resolved": False}
        result["process_exit_code"] = completed.returncode
        save_result(result)
        progress(f"finished {index}/{len(rows)} {instance} status={result.get('status')} resolved={result.get('resolved')}")
    progress(f"finished {len(results)}/{len(rows)} resolved={sum(bool(r.get('resolved')) for r in results)}")


if __name__ == "__main__":
    main()
