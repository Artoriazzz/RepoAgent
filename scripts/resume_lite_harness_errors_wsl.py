"""保留初始补丁，使用原冻结流程续跑 Harness 及其失败纠正。"""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch as patch_function


def saved_generation(batch, original_pass, saved_patch):
    def reuse(row, repo, output, task, args, env, round_index, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        batch.restore_worktree_patch(repo, saved_patch)
        result = deepcopy(original_pass)
        for key in ("harness", "initial_harness", "harness_feedback", "experience_correction",
                    "experience_correction_2", "experience_curator", "second_harness"):
            result.pop(key, None)
        result.update(model_patch=saved_patch, patch_chars=len(saved_patch), seconds=0,
                      output_dir=str(output), initial_generation_reused=True)
        (output / "task.md").write_text(task)
        (output / "model.patch").write_text(saved_patch)
        (output / "stage_attribution.json").write_text(json.dumps(result["stage_attribution"], indent=2))
        return result
    return reuse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--resume", action="store_true")
    options = parser.parse_args()
    source = options.source_run.resolve()
    frozen = source / "source"
    sys.path[:0] = [str(frozen), str(frozen / "scripts")]
    import swebench_batch as batch

    config = json.loads((source / "experiment.json").read_text())
    for name, digest in config["source_sha256"].items():
        if hashlib.sha256((frozen / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Frozen source changed: {name}")
    if config["repair_mode"] != "experience-build":
        raise ValueError("This continuation requires the original experience-build mode")
    original = json.loads((source / "results.json").read_text())
    selected = [r for r in original if (r.get("passes") or [{}])[0].get("harness", {}).get("error")]
    swebench = Path("/home/artoria/workspace/SWE-bench")
    work = swebench / "agent_work"
    live = Path("/home/artoria/workspace/RepoAgent/RepoAgent")
    output = work / "runs" / options.run_name
    output.mkdir(parents=True, exist_ok=options.resume)
    results_path = output / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    finished = {r["instance_id"] for r in results}
    (output / "instances.json").write_text(json.dumps([r["instance_id"] for r in selected], indent=2))
    env = {**os.environ, **batch.read_env_file(live / ".env"), "PYTHONPATH": str(frozen),
           "NO_PROXY": "open.bigmodel.cn", "no_proxy": "open.bigmodel.cn",
           "SWEBENCH_REPO_MIRROR_ROOT": str(work / "repo_cache"),
           "SWEBENCH_RAW_URL_PREFIX": "", "REPO_AGENT_LLM_TIMEOUT_SECONDS": "300",
           "REPO_AGENT_LLM_MAX_RETRIES": "1"}
    # 数据集交给含 pandas 的 harness 环境读取，控制器使用 Agent 的依赖环境。
    rows = json.loads(subprocess.check_output([
        str(swebench / ".venv/bin/python"), "-c",
        "import json,sys,pandas as pd; df=pd.read_parquet(sys.argv[1]); "
        "print(df[df.instance_id.isin(json.loads(sys.argv[2]))].to_json(orient='records'))",
        config["dataset"], json.dumps([r["instance_id"] for r in selected]),
    ], text=True))

    def progress(message):
        (output / "progress.txt").write_text(message + "\n")
        print(message, flush=True)

    for index, row in enumerate(rows, 1):
        instance = row["instance_id"]
        if instance in finished:
            continue
        old = next(r for r in selected if r["instance_id"] == instance)
        old_pass = old["passes"][0]
        saved_patch = (Path(old_pass["output_dir"]) / "model.patch").read_text()
        if not saved_patch.strip():
            raise ValueError(f"Missing initial patch for {instance}")
        case = output / instance
        case.mkdir()
        args = batch.build_parser().parse_args([
            "--dataset", config["dataset"], "--repo-agent-bin", str(live / ".venv/bin/repo-agent"),
            "--model", config["model"], "--implement-model", config["model"],
            "--reasoning-effort", config["other_reasoning_effort"],
            "--coordinator-reasoning-effort", config["coordinator_reasoning_effort"],
            "--implement-reasoning-effort", config["other_reasoning_effort"],
            "--max-steps", str(config["max_steps"]), "--timeout", "3600",
            "--work-dir", str(work), "--output-dir", str(case),
            "--proxy", "http://127.0.0.1:7897", "--offline-cache",
            "--repair-mode", "experience-build", "--evaluate",
            "--swebench-dir", str(swebench), "--harness-python", str(swebench / ".venv/bin/python"),
            "--run-id", options.run_name + "-" + str(index), "--harness-timeout", "900",
            "--cache-level", "env", "--namespace", "none", "--docker-network", "host",
            "--candidate-mode", "off", "--no-prebuild-instance-image", "--no-repair-memory",
            "--memory-root", str(source / "isolated-memory"), "--memory-scope", row["repo"],
            "--memory-provenance", "swebench-test-diagnostic",
        ])
        progress(f"running {index}/{len(rows)} {instance}: reuse initial patch -> harness -> correction if needed")
        with patch_function.object(batch, "run_agent_pass", saved_generation(batch, old_pass, saved_patch)):
            result = batch.run_instance_once(row, output / "repos" / instance, case, args,
                {**env, "REPO_AGENT_LLM_LOG_DIR": str(case / "llm-requests")})
        final_patch = result.pop("model_patch", "")
        result.update(source_run=str(source), reused_patch_sha256=hashlib.sha256(saved_patch.encode()).hexdigest())
        (case / "model.patch").write_text(final_patch)
        result = batch.strip_large_fields(result)
        (case / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        results.append(result)
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        overrides = {r["instance_id"]: r for r in results}
        merged = [overrides.get(r["instance_id"], r) for r in original]
        (output / "combined_results.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2))
        predictions = [batch.prediction_for(
            r["instance_id"], (Path(r["output_dir"]) / "model.patch").read_text(), config["model"]
        ) for r in merged]
        (output / "predictions.json").write_text(json.dumps(predictions, indent=2))
        first = sum(bool((r.get("passes") or [{}])[0].get("initial_harness",
                         (r.get("passes") or [{}])[0].get("harness", {})).get("resolved")) for r in merged)
        summary = {"resumed": len(results), "resume_total": len(rows), "total": len(merged),
                   "initial_resolved": first, "final_resolved": sum(bool(r.get("resolved")) for r in merged),
                   "harness_errors": sum(bool((r.get("passes") or [{}])[0].get("harness", {}).get("error")) for r in merged)}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        progress(f"finished {index}/{len(rows)} {instance} resolved={result.get('resolved')} status={result.get('status')}")
    progress(f"finished {len(results)}/{len(rows)} continuations; see summary.json for combined 20 results")


if __name__ == "__main__":
    main()
