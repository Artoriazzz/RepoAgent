"""按固定顺序串行重跑十道历史未解决题，独立保存日志与进度。"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

INSTANCES = [
    "sympy__sympy-19254",
    "sphinx-doc__sphinx-7975",
    "sympy__sympy-18835",
    "sphinx-doc__sphinx-7738",
    "scikit-learn__scikit-learn-25500",
    "sphinx-doc__sphinx-7686",
    "sympy__sympy-11400",
    "sympy__sympy-11897",
    "sympy__sympy-12236",
    "sphinx-doc__sphinx-8273",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", default="expbuild-unresolved10-glm-high-two-corrections-20261013")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    swebench = Path("/home/artoria/workspace/SWE-bench")
    work = swebench / "agent_work"
    output = work / "runs" / args.run_name
    output.mkdir(parents=True, exist_ok=False)
    (output / "instances.json").write_text(json.dumps(INSTANCES, indent=2))
    env = os.environ.copy()
    env.update(NO_PROXY="open.bigmodel.cn", no_proxy="open.bigmodel.cn",
               REPO_AGENT_LLM_TIMEOUT_SECONDS="120", REPO_AGENT_LLM_MAX_RETRIES="1",
               SWEBENCH_RAW_PROXY="http://127.0.0.1:7897", SWEBENCH_RAW_URL_PREFIX="https://ghproxy.net")
    results = []
    for index, instance in enumerate(INSTANCES, 1):
        label = f"[{index}/{len(INSTANCES)}] {instance}"
        message = f"running {label} {datetime.now().astimezone().isoformat()}"
        (output / "progress.txt").write_text(message + "\n")
        print(message, flush=True)
        case_output = output / instance
        command = [
            sys.executable, str(project / "scripts/swebench_batch.py"),
            "--dataset", str(swebench / "test-00000-of-00001.parquet"),
            "--repo-agent-bin", str(project / ".venv/bin/repo-agent"),
            "--model", "glm-5.3-flash", "--implement-model", "glm-5.3-flash",
            "--reasoning-effort", "low", "--coordinator-reasoning-effort", "high",
            "--implement-reasoning-effort", "low", "--work-dir", str(work),
            "--output-dir", str(case_output), "--env-file", str(project / ".env"),
            "--proxy", "http://127.0.0.1:7897", "--offline-cache",
            "--instance-id", instance, "--max-steps", "16",
            "--repair-mode", "experience-build", "--timeout", "3600", "--evaluate",
            "--swebench-dir", str(swebench), "--harness-python", str(swebench / ".venv/bin/python"),
            "--run-id", args.run_name + "-" + str(index), "--harness-timeout", "900",
            "--cache-level", "env", "--namespace", "none", "--docker-network", "host",
            "--candidate-mode", "off", "--memory-provenance", "swebench-test-diagnostic",
            "--prebuild-instance-image", "--image-build-timeout", "1800",
        ]
        with (output / (instance + ".log")).open("w") as log:
            completed = subprocess.run(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT)
        summary_path = case_output / "batch_summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text())
            result = summary["instances"][0]
        else:
            result = {"instance_id": instance, "status": "runner_failed", "resolved": False}
        result["process_exit_code"] = completed.returncode
        results.append(result)
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"finished {label} status={result.get('status')} resolved={result.get('resolved')}", flush=True)
    resolved = sum(bool(result.get("resolved")) for result in results)
    (output / "progress.txt").write_text(f"finished {len(results)}/10 resolved={resolved}/10\n")
    print(f"finished 10/10 resolved={resolved}/10", flush=True)


if __name__ == "__main__":
    main()
