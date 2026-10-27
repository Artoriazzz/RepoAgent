"""冻结新提示词及框架修复，后台重跑 110–199 中剩余的 41 题。"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path("/home/artoria/workspace/SWE-bench")
RUNS = ROOT / "agent_work/runs"
OUTPUT = RUNS / "unresolved41-state-contract-glm-20261023"
PAYLOAD = ROOT / "agent_work/staging/state-contract-20261023"


def main():
    previous = RUNS / "lite110to199-infra10-retry-glm-20261023"
    original_config = json.loads((previous / "experiment.json").read_text())
    original_source = Path(original_config["source_root"])
    for name, digest in original_config["source_sha256"].items():
        if hashlib.sha256((original_source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Original frozen source changed: {name}")
    rows = json.loads((previous / "results.json").read_text())
    repaired_root = RUNS / "infra3-test-environment-repair-20261023"
    repaired = json.loads((repaired_root / "summary.json").read_text())
    corrections = {row["instance_id"]: row for row in repaired["cases"]}
    for row in rows:
        if row["instance_id"] in corrections:
            row["resolved"] = corrections[row["instance_id"]]["resolved"]
            row["environment_reevaluation"] = corrections[row["instance_id"]]
    failed = [row["instance_id"] for row in rows if not row.get("resolved")]
    if len(rows) != 90 or len(failed) != 41:
        raise RuntimeError(f"Unexpected selection: {len(failed)} failed out of {len(rows)}")
    OUTPUT.mkdir(exist_ok=False)
    source = OUTPUT / "source"
    shutil.copytree(original_source, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    replacements = {"repo_agent/core/" + name: PAYLOAD / name
                    for name in ("coordinator.py", "static_analysis.py", "experience.py")}
    changes = []
    for name, incoming in replacements.items():
        shutil.copyfile(incoming, source / name)
        changes.append({"path": name, "previous_sha256": original_config["source_sha256"][name],
                        "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest()})
    runner = OUTPUT / "runner"
    runner.mkdir()
    for name in ("resume_lite_parallel_wsl.py", "run_failed5_preflight_wsl.py",
                 "repair_swebench_test_environment.py", "reuse_lite_initial_generation.py",
                 "resume_lite_harness_errors_wsl.py"):
        incoming = PAYLOAD / name
        shutil.copyfile(incoming if incoming.exists() else previous / "runner" / name, runner / name)
    baseline = OUTPUT / "baseline"
    baseline.mkdir()
    config = {**original_config, "source_root": str(source), "previous_run": str(previous),
              "environment_reevaluation": str(repaired_root), "source_changes": changes}
    config["source_sha256"] = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                               for name in original_config["source_sha256"]}
    (baseline / "experiment.json").write_text(json.dumps(config, indent=2))
    (baseline / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    (OUTPUT / "instances.json").write_text(json.dumps(failed, indent=2))
    (OUTPUT / "source_changes.json").write_text(json.dumps(changes, indent=2))
    command = [str(ROOT / ".venv/bin/python"), "-u", str(runner / "resume_lite_parallel_wsl.py"),
               "--previous-run", str(baseline), "--source-root", str(source),
               "--output-dir", str(OUTPUT), "--wheelhouse", str(ROOT / "agent_work/build-wheelhouse"),
               "--build-assets", str(ROOT / "agent_work/build-assets"),
               "--apt-mirror", "https://mirrors.ustc.edu.cn/ubuntu", "--retry-unresolved",
               "--start", "110", "--stop", "200", "--additional", "0", "--clean-test-environment"]
    with (OUTPUT / "nohup.out").open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    (OUTPUT / "batch.pid").write_text(str(process.pid) + "\n")
    print(json.dumps({"pid": process.pid, "output": str(OUTPUT), "new_cases": len(failed),
                      "parallel_cases": 2, "source_changes": changes}, indent=2), flush=True)


if __name__ == "__main__":
    main()
