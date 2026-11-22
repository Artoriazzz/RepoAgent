"""Run the pytest SCM repair on the remaining historical failures serially."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench/agent_work/runs")
OUT = ROOT / "pytest-version-metadata-group-20261122"
OUT.mkdir(parents=True, exist_ok=True)
rows = json.loads(Path("/tmp/repoagent-audit-lite129-result.json").read_text())["rows"]
ids = sorted(i for i in rows if i.startswith("pytest-dev__pytest-"))
progress = OUT / "progress.json"
state = json.loads(progress.read_text()) if progress.exists() else {"selected": ids, "completed": [], "errors": []}


def save():
    (OUT / "progress.json").write_text(json.dumps(state, indent=2))


save()
for instance_id in ids:
    if any(x["instance_id"] == instance_id for x in state["completed"]):
        continue
    state["errors"] = [x for x in state["errors"] if x["instance_id"] != instance_id]
    if instance_id == "pytest-dev__pytest-5221":
        state["completed"].append({"instance_id": instance_id, "pilot": True,
            "run": "pytest-version-metadata-pilot-20261122"})
        save()
        continue
    if shutil.disk_usage("/mnt/d").free < 12 * 1024**3:
        state["errors"].append({"instance_id": instance_id, "error": "D: below 12 GiB"})
        save()
        break
    name = "pytest-version-fix-" + instance_id.split("-")[-1] + "-20261122"
    report = ROOT / name / "result.json"
    if report.exists():
        data = json.loads(report.read_text())
        state["completed"].append({"instance_id": instance_id, "resolved": data.get("resolved"),
            "error": data.get("error"), "run": name})
        save()
        continue
    command = [sys.executable, "/tmp/repoagent-reevaluate-pytest-env.py",
        "--instance-id", instance_id, "--output-name", name]
    with (OUT / (instance_id + ".log")).open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode or not report.exists():
        state["errors"].append({"instance_id": instance_id, "exit_code": result.returncode,
            "log": str(OUT / (instance_id + ".log"))})
    else:
        data = json.loads(report.read_text())
        state["completed"].append({"instance_id": instance_id, "resolved": data.get("resolved"),
            "error": data.get("error"), "run": name})
    save()
(OUT / "finished.txt").touch()
