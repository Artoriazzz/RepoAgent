"""Re-evaluate saved Sphinx patches after the verbose pytest parser fix."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench/agent_work/runs")
OUT = ROOT / "sphinx-verbose-parser-reeval-20261122"
OUT.mkdir(parents=True, exist_ok=True)
IDS = ["sphinx-doc__sphinx-10451", "sphinx-doc__sphinx-7686",
    "sphinx-doc__sphinx-7738", "sphinx-doc__sphinx-8273",
    "sphinx-doc__sphinx-8282", "sphinx-doc__sphinx-8506"]
state = {"selected": IDS, "completed": [], "errors": []}


def save():
    (OUT / "progress.json").write_text(json.dumps(state, indent=2))


save()
for instance_id in IDS:
    name = "lite-envfix-" + instance_id.replace("__", "-") + "-parser2-20261122"
    result_path = ROOT / name / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        state["completed"].append({"instance_id": instance_id,
            "resolved": result.get("resolved"), "error": result.get("error")})
        save()
        continue
    with (OUT / (instance_id + ".log")).open("w") as log:
        process = subprocess.run([sys.executable, "/tmp/repoagent-reevaluate-lite-env.py",
            "--instance-id", instance_id, "--output-name", name],
            stdout=log, stderr=subprocess.STDOUT)
    if process.returncode or not result_path.exists():
        state["errors"].append({"instance_id": instance_id, "exit_code": process.returncode})
    else:
        result = json.loads(result_path.read_text())
        state["completed"].append({"instance_id": instance_id,
            "resolved": result.get("resolved"), "error": result.get("error")})
    save()
(OUT / "finished.txt").touch()
