"""Run the non-pytest environment re-evaluations with saved patches."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/artoria/workspace/SWE-bench/agent_work/runs")
OUT = ROOT / "lite42-environment-reeval-20261122"
OUT.mkdir(parents=True, exist_ok=True)
rows = json.loads(Path("/tmp/repoagent-audit-lite129-result.json").read_text())["rows"]
matplotlib = sorted(i for i, row in rows.items() if "matplotlib_scm_warning" in row.get("signatures", []))
sphinx = sorted(i for i, row in rows.items() if i.startswith("sphinx-doc__") and row.get("patch_chars"))
requests = sorted(i for i in rows if i.startswith("psf__requests-") and i != "psf__requests-863")
ids = matplotlib + sphinx + requests + ["pallets__flask-4045", "astropy__astropy-14182"]
assert len(ids) == 24 and len(set(ids)) == 24
progress = OUT / "progress.json"
state = json.loads(progress.read_text()) if progress.exists() else {"selected": ids, "completed": [], "errors": []}


def save():
    progress.write_text(json.dumps(state, indent=2))


save()
for instance_id in ids:
    if any(row["instance_id"] == instance_id for row in state["completed"]):
        continue
    if shutil.disk_usage("/mnt/d").free < 12 * 1024**3:
        state["errors"].append({"instance_id": instance_id, "error": "D: below 12 GiB"})
        save()
        break
    name = "lite-envfix-" + instance_id.replace("__", "-") + "-20261122"
    report = ROOT / name / "result.json"
    if report.exists():
        data = json.loads(report.read_text())
        state["completed"].append({"instance_id": instance_id,
            "resolved": data.get("resolved"), "error": data.get("error"), "run": name})
        save()
        continue
    command = [sys.executable, "/tmp/repoagent-reevaluate-lite-env.py",
        "--instance-id", instance_id, "--output-name", name]
    log_path = OUT / (instance_id + ".log")
    with log_path.open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    state["errors"] = [row for row in state["errors"] if row["instance_id"] != instance_id]
    if result.returncode or not report.exists():
        state["errors"].append({"instance_id": instance_id,
            "exit_code": result.returncode, "log": str(log_path)})
    else:
        data = json.loads(report.read_text())
        state["completed"].append({"instance_id": instance_id,
            "resolved": data.get("resolved"), "error": data.get("error"), "run": name})
    save()
(OUT / "finished.txt").touch()
