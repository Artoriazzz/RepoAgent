"""沿用双经验实验的冻结修复配置，只注入已检索出的全局 Top-1 经验。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys

from run_pinned20_wsl import SWE, WORK, LIVE, HELPERS, digest, memory_hashes, save_json


DEFAULT_BASELINE = WORK / "runs/lite-pinned20-pass1-glm-20261025"
DEFAULT_RETRIEVAL = WORK / "memory-retrieval/pinned20-vector-top1-20261026"
DEFAULT_OUTPUT = WORK / "runs/lite-global-only20-pass1-glm-20261026"


def global_only_selection(selection, retrieval_results):
    """先按检索结果选文件，再关联原始经验；不通过当前题号指定经验。"""
    ranked = {row["instance_id"]: row for row in retrieval_results}
    if set(ranked) != {row["id"] for row in selection["instances"]}:
        raise ValueError("Retrieval and repair cohorts differ")
    sources = {row["saved_memories"]["global_experience"]["filename"]:
               (row["id"], row["saved_memories"]["global_experience"])
               for row in selection["instances"]}
    result = deepcopy(selection)
    for row in result["instances"]:
        evidence = ranked[row["id"]]
        if evidence["query_status"] != "success":
            raise ValueError("Query extraction must have succeeded before repair")
        top = evidence["rankings"]["global_experience"][0]
        origin, memory = sources[top["filename"]]
        row["saved_memories"] = {"global_experience": deepcopy(memory)}
        row["global_memory_source_case_id"] = origin
        row["retrieval_evidence"] = deepcopy(top)
        row["operations"] = [op for op in row.get("operations", []) if op.get("type") == "global_experience"]
    return result


def copy_original_globals(selection, baseline, original_config, output):
    from repo_agent.core.memory import MemoryStore
    from pinned_memory_agent import inject_pinned_experiences

    manifest = {"diagnostic_only": True, "selection": "cached_vector_top1_global_only", "instances": {}}
    for row in selection["instances"]:
        saved = row["saved_memories"]["global_experience"]
        origin = row["global_memory_source_case_id"]
        root = baseline / "memory" / origin
        matches = list(root.rglob(saved["filename"]))
        if len(matches) != 1:
            raise ValueError("Original global experience is missing or ambiguous")
        source = matches[0]
        if digest(source) != original_config["memory_sha256"][origin][str(source.relative_to(root))]:
            raise ValueError("Original frozen global experience changed")
        store = MemoryStore.for_scope(row["repo"], root=output / "memory" / row["id"])
        store.global_memory_dir.mkdir(parents=True)
        shutil.copy2(source, store.global_memory_dir / saved["filename"])
        store.update_index()
        text, memories = inject_pinned_experiences(
            task="PUBLIC_TASK", memory_store=store, filenames=[saved["filename"]],
        )
        if len(memories) != 1 or saved["arguments"]["content"].strip() not in text:
            raise ValueError("Global experience is missing or clipped")
        if any(header.type.value == "repository_experience" for header in store.list_memories()):
            raise ValueError("Repository experience must not be present")
        manifest["instances"][row["id"]] = [saved["filename"]]
    return manifest


def preflight(output, config):
    import docker
    import pandas as pd
    from run_failed5_preflight_wsl import clean_cached_test_image
    from resume_lite_parallel_wsl import cached_baseline_check, REPO_IMPORTS

    directory = output / "preflight"
    directory.mkdir()
    rows = {r["instance_id"]: r for r in pd.read_parquet(config["dataset"]).to_dict("records")}

    def check(instance):
        row = rows[instance]
        client = docker.from_env()
        try:
            with (directory / f"{instance}.log").open("w") as log:
                clean = clean_cached_test_image(row, SWE, client, log, proxy="http://127.0.0.1:7897")
                image = client.images.get(f"sweb.eval.x86_64.{instance}:latest")
                script = cached_baseline_check(row, image.labels) + REPO_IMPORTS[row["repo"]]
                data = client.containers.run(image.id, ["/opt/miniconda3/envs/testbed/bin/python", "-c", script],
                                             working_dir="/testbed", remove=True)
                log.write(data.decode())
            return instance, {"status": "ready", "image_id": image.id, **clean}
        finally:
            client.close()

    save_json(output / "progress.json", {"phase": "environment_preflight", "completed": 0, "total": 20})
    evidence = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for instance, result in pool.map(check, config["target_ids"]):
            evidence[instance] = result
            save_json(output / "progress.json", {"phase": "environment_preflight", "completed": len(evidence), "total": 20})
            print(f"preflight {len(evidence)}/20 {instance}: ready", flush=True)
    save_json(output / "environment-preflight.json", evidence)


def launch(args):
    original_config = json.loads((args.baseline / "experiment.json").read_text())
    original = Path(original_config["source_root"])
    for name, expected in original_config["source_sha256"].items():
        if digest(original / name) != expected:
            raise ValueError(f"Frozen repair source changed: {name}")
    if digest(Path(original_config["dataset"])) != original_config["dataset_sha256"]:
        raise ValueError("Frozen dataset changed")
    retrieval_path = args.retrieval / "results.json"
    selection = global_only_selection(json.loads((args.baseline / "selection.json").read_text()),
                                      json.loads(retrieval_path.read_text()))
    if len(selection["instances"]) != 20:
        raise ValueError("Expected the same 20 cases")
    args.output.mkdir(parents=True, exist_ok=False)
    runner = args.output / "runner"
    runner.mkdir()
    # Agent 入口与环境辅助脚本使用原实验副本；批处理使用已修正完整文件哈希审计的版本。
    for name in HELPERS:
        shutil.copy2(args.baseline / "runner" / name, runner / name)
    for name in ("run_pinned20_wsl.py", Path(__file__).name):
        shutil.copy2(Path(__file__).with_name(name), runner / name)
    (runner / "pinned_memory_agent.py").chmod(0o755)
    sys.path[:0] = [str(original), str(runner)]
    manifest = copy_original_globals(selection, args.baseline, original_config, args.output)
    save_json(args.output / "selection.json", selection)
    save_json(args.output / "memory-manifest.json", manifest)
    config = {
        **{k: original_config[k] for k in (
            "dataset", "dataset_sha256", "source_sha256", "model", "max_steps",
            "coordinator_reasoning_effort", "other_reasoning_effort",
        )},
        "source_root": str(original), "target_ids": [r["id"] for r in selection["instances"]],
        "experiment": "cached_vector_global_top1_only_pass1",
        "comparison_run": str(args.baseline),
        "comparison_final_summary": str(args.baseline.parent / "lite-pinned20-external2-retry-glm-20261025/combined_summary.json"),
        "retrieval_results": str(retrieval_path), "retrieval_results_sha256": digest(retrieval_path),
        "retrieval": "frozen_vector_top1_global_only", "retrieval_performed_before_run": True,
        "repair_mode": "benchmark", "feedback_correction": False, "memory_write": False,
        "repository_experience_injection": False, "global_experience_limit": 1,
        "injected_fields": "original_global_memory_bytes_without_new_retrieval_fields",
        "parallel_cases": 2, "diagnostic_only": True, "leaderboard_eligible": False,
        "network_no_proxy": "localhost,127.0.0.1,::1,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn",
        "memory_sha256": {r["id"]: memory_hashes(args.output / "memory" / r["id"]) for r in selection["instances"]},
        "runner_sha256": {p.name: digest(p) for p in runner.iterdir() if p.is_file()},
    }
    save_json(args.output / "experiment.json", config)
    # 模型环境有 OpenAI 客户端，Harness 环境有 Docker 客户端；沿用已有两个环境。
    subprocess.run([
        str(SWE / ".venv/bin/python"), "-u", str(runner / Path(__file__).name),
        "--preflight", "--output", str(args.output),
    ], check=True)
    (args.output / "work").mkdir()
    (args.output / "work/repo_cache").symlink_to(WORK / "repo_cache", target_is_directory=True)
    with (args.output / "nohup.out").open("w") as log:
        process = subprocess.Popen([
            str(SWE / ".venv/bin/python"), "-u", str(runner / "run_pinned20_wsl.py"),
            "--worker", "--output-dir", str(args.output),
        ], cwd=SWE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    (args.output / "batch.pid").write_text(str(process.pid) + "\n")
    print(json.dumps({"pid": process.pid, "output": str(args.output), "cases": 20,
                      "parallel": 2, "environment_preflight": "20/20 ready"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.preflight:
        sys.path.insert(0, str(args.output / "runner"))
        preflight(args.output, json.loads((args.output / "experiment.json").read_text()))
    else:
        launch(args)
