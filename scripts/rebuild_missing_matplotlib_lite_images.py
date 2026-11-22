"""Rebuild only the three deleted Lite instance images from the local mirror."""

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
from run_failed5_preflight_wsl import prepare_image

IDS = [
    "matplotlib__matplotlib-23913",
    "matplotlib__matplotlib-24334",
    "matplotlib__matplotlib-25079",
]
OUT = RUNS / "lite42-missing-matplotlib-build-20261122"
OUT.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)
rows = pd.read_parquet(ROOT / "test-00000-of-00001.parquet",
    columns=["instance_id", "repo", "base_commit", "version", "environment_setup_commit"]).set_index("instance_id")
client = docker.from_env(timeout=1800)
state = {"selected": IDS, "completed": [], "errors": []}


def save():
    (OUT / "progress.json").write_text(json.dumps(state, indent=2))


save()
for instance_id in IDS:
    row = rows.loc[instance_id].dropna().to_dict()
    row.update(instance_id=instance_id, FAIL_TO_PASS="[]", PASS_TO_PASS="[]", test_patch="")
    with (OUT / (instance_id + ".log")).open("w") as log:
        try:
            result = prepare_image(row, ROOT, client, log, imports="import matplotlib",
                canonical=True, proxy="", build_assets=ROOT / "agent_work/build-assets")
            state["completed"].append({"instance_id": instance_id, "image": result["image"],
                "image_id": result["image_id"]})
        except Exception as exc:
            state["errors"].append({"instance_id": instance_id, "error": repr(exc)})
    save()
(OUT / "finished.txt").touch()
