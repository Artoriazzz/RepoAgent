"""从已有逐题结果恢复，跳过有效完成题，两路执行剩余题并追加新题。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading

from run_failed5_preflight_wsl import clean_cached_test_image, prepare_image


REPO_IMPORTS = {
    "django/django": "import django; import django.test",
    "matplotlib/matplotlib": "import matplotlib; import matplotlib.pyplot; import matplotlib.testing; import pytest",
    "mwaskom/seaborn": "import seaborn; import pytest",
    "pallets/flask": "import flask; import pytest",
    "psf/requests": "import requests; import pytest",
    "pydata/xarray": "import xarray; import pytest",
    "pylint-dev/pylint": "import pylint; import astroid; import pytest",
    "pytest-dev/pytest": "import pytest; import _pytest",
    "scikit-learn/scikit-learn": "import sklearn; import sklearn.utils; import pytest",
    "sphinx-doc/sphinx": "import sphinx; import pytest",
    "sympy/sympy": ("import sympy,mpmath; import sympy.utilities.runtests; "
                    "from pathlib import Path; assert Path('/testbed/bin/test').is_file()"),
}


def reusable_result(result):
    if result.get("status") in {"environment_error", "runner_failed", "api_error"}:
        return False
    first = (result.get("passes") or [{}])[0]
    return not (first.get("harness") or {}).get("error", False)


def infrastructure_error(result):
    return (result.get("status") in {"environment_error", "runner_failed", "api_error"}
            or any((item.get(key) or {}).get("error", False)
                   for item in result.get("passes", []) for key in ("initial_harness", "harness")))


def plan_instances(rows, previous, start=40, stop=80, additional=30):
    completed = {r["instance_id"] for r in previous if reusable_result(r)}
    old = rows[start:stop]
    pending = [r for r in old if r["instance_id"] not in completed]
    return pending + rows[stop:stop + additional]


def initial_resolved(result):
    first = (result.get("passes") or [{}])[0]
    return bool(first.get("initial_harness", first.get("harness", {})).get("resolved"))


def prepend_extra_instances(rows, selected, extra_ids):
    by_id = {row["instance_id"]: row for row in rows}
    unknown = set(extra_ids) - by_id.keys()
    if unknown:
        raise ValueError(f"Unknown extra instances: {sorted(unknown)}")
    ordered = dict.fromkeys(extra_ids + [row["instance_id"] for row in selected])
    return [by_id[instance] for instance in ordered]


def select_status_retries(rows, previous, statuses):
    retry_ids = {r["instance_id"] for r in previous if r.get("status") in statuses}
    return [r for r in rows if r["instance_id"] in retry_ids]


def select_unresolved(rows, previous):
    failed = {result["instance_id"] for result in previous if not result.get("resolved", False)}
    return [row for row in rows if row["instance_id"] in failed]


def cached_baseline_check(row, labels):
    base = row["base_commit"]
    # 官方安装会提交 setup.py/tox.ini 等兼容修改；带基线标记的镜像核对安装后的干净 HEAD。
    reference = "HEAD" if labels.get("repoagent.base-commit") == base else base
    return ("import subprocess; subprocess.run(['git','-C','/testbed','merge-base',"
            f"'--is-ancestor','{base}','HEAD'],check=True); "
            "subprocess.run(['git','-C','/testbed','diff',"
            f"'--exit-code','{reference}'],check=True); ")


def main():
    import docker
    import pandas as pd

    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-run", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--retry-status", action="append", choices=["api_error", "environment_error", "runner_failed"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--start", type=int, default=40)
    parser.add_argument("--stop", type=int, default=80)
    parser.add_argument("--additional", type=int, default=30)
    parser.add_argument("--extra-instance-id", action="append", default=[])
    parser.add_argument("--reuse-initial-instance-id", action="append", default=[])
    parser.add_argument("--requests-extra-hosts", type=Path)
    parser.add_argument("--apt-mirror")
    parser.add_argument("--build-assets", type=Path)
    parser.add_argument("--retry-infra", action="store_true")
    parser.add_argument("--retry-unresolved", action="store_true")
    parser.add_argument("--canonical-environment", action="store_true")
    parser.add_argument("--clean-test-environment", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.retry_unresolved and (args.retry_status or args.retry_infra):
        parser.error("--retry-unresolved cannot be combined with infrastructure/status retry")
    swebench = Path("/home/artoria/workspace/SWE-bench")
    live = Path("/home/artoria/workspace/RepoAgent/RepoAgent")
    work = swebench / "agent_work"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    old_config = json.loads((args.previous_run / "experiment.json").read_text())
    for name, digest in old_config["source_sha256"].items():
        if hashlib.sha256((args.source_root / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Frozen source changed: {name}")
    dataset = Path(old_config["dataset"])
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != old_config["dataset_sha256"]:
        raise SystemExit("Dataset changed")
    rows = pd.read_parquet(dataset).to_dict("records")
    if not 0 <= args.start < args.stop <= args.stop + args.additional <= len(rows):
        parser.error("Expected 0 <= start < stop <= stop + additional <= dataset size")
    previous = json.loads((args.previous_run / "results.json").read_text())
    if args.retry_unresolved or args.retry_status or args.retry_infra:
        selected = (select_unresolved(rows, previous) if args.retry_unresolved else
                    [r for r in rows if r["instance_id"] in
                     {p["instance_id"] for p in previous if infrastructure_error(p)}]
                    if args.retry_infra else select_status_retries(rows, previous, set(args.retry_status)))
        target_ids = old_config["target_ids"]
        selected_ids = {r["instance_id"] for r in selected}
        reused = [r for r in previous if r["instance_id"] not in selected_ids]
    else:
        selected = plan_instances(rows, previous, args.start, args.stop, args.additional)
        target_ids = [r["instance_id"] for r in rows[args.start:args.stop + args.additional]]
        reused = [r for r in previous if r["instance_id"] in target_ids and reusable_result(r)]
    selected = prepend_extra_instances(rows, selected, args.extra_instance_id)
    target_ids = list(dict.fromkeys(args.extra_instance_id + target_ids))
    reused = [r for r in reused if r["instance_id"] not in args.extra_instance_id]
    if set(args.reuse_initial_instance_id) - {r["instance_id"] for r in previous}:
        parser.error("Explicit initial reuse requires an existing result")
    config = {
        "previous_run": str(args.previous_run), "source_root": str(args.source_root),
        "source_sha256": old_config["source_sha256"], "dataset": str(dataset),
        "dataset_sha256": old_config["dataset_sha256"],
        "scheduled_ids": [r["instance_id"] for r in selected],
        "reused_ids": [r["instance_id"] for r in reused], "target_ids": target_ids,
        "parallel_cases": 2, "repair_mode": "experience-build", "model": "glm-5.3-flash",
        "max_steps": 16, "coordinator_reasoning_effort": "high", "other_reasoning_effort": "low",
        "memory_recall": False, "memory_write": True, "diagnostic_only": True,
        "retry_statuses": args.retry_status or [],
        "retry_infra": args.retry_infra, "canonical_environment": args.canonical_environment,
        "retry_unresolved": args.retry_unresolved,
        "reuse_initial_generation": not args.retry_unresolved,
        "reuse_initial_ids": args.reuse_initial_instance_id,
        "requests_extra_hosts": json.loads(args.requests_extra_hosts.read_text()) if args.requests_extra_hosts else {},
        "review_max_output_tokens": os.getenv("REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS"),
        "clean_test_environment": args.clean_test_environment,
        "cache_level": "instance",
        "network_no_proxy": os.getenv("REPO_AGENT_NETWORK_NO_PROXY", "open.bigmodel.cn"),
        "llm_timeout_seconds": os.getenv("REPO_AGENT_LLM_TIMEOUT_SECONDS", "300"),
        "llm_max_retries": os.getenv("REPO_AGENT_LLM_MAX_RETRIES", "1"),
        "apt_mirror": args.apt_mirror,
        "build_assets_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(args.build_assets.iterdir()) if p.is_file()}
                               if args.build_assets else {},
        "runner_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                          for name in ["resume_lite_parallel_wsl.py", "run_failed5_preflight_wsl.py",
                                       "reuse_lite_initial_generation.py", "resume_lite_harness_errors_wsl.py"]
                          + (["repair_swebench_test_environment.py"] if args.clean_test_environment else [])},
        "wheelhouse_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(args.wheelhouse.glob("*.whl"))},
    }
    config_path = output / "experiment.json"
    if config_path.exists():
        if not args.resume or json.loads(config_path.read_text()) != config:
            raise SystemExit("Existing experiment requires --resume with identical configuration")
    else:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    results_path = output / "new_results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    previous_by_id = {r["instance_id"]: r for r in previous}
    done = {r["instance_id"] for r in results}
    queue = Queue()
    for row in selected:
        if row["instance_id"] not in done:
            queue.put(row)
    lock = threading.Lock()
    lanes = {}
    order = {instance: index for index, instance in enumerate(target_ids)}
    env = {**os.environ, "PYTHONPATH": str(args.source_root),
           "NO_PROXY": config["network_no_proxy"], "no_proxy": config["network_no_proxy"],
           "REPO_AGENT_LLM_TIMEOUT_SECONDS": config["llm_timeout_seconds"],
           "REPO_AGENT_LLM_MAX_RETRIES": config["llm_max_retries"],
           "SWEBENCH_REPO_MIRROR_ROOT": str(work / "repo_cache"), "SWEBENCH_RAW_URL_PREFIX": ""}
    proxy = "http://127.0.0.1:7897"
    env.update({name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
    env.update(SWEBENCH_PASS_PROXY_TO_DOCKER="1", SWEBENCH_DOCKER_BUILD_NETWORK="host",
               SWEBENCH_DOCKER_NETWORK="host", SWEBENCH_GIT_PROXY=proxy)
    # 预检与 Harness 使用同一份本地源码读取依赖文件，保证环境标识一致。
    os.environ.update({key: value for key, value in env.items()
                       if key.startswith("SWEBENCH_") or "proxy" in key.lower()})

    def save_json(name, value):
        path = output / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(path)

    def save_results():
        merged = sorted(reused + results, key=lambda r: order[r["instance_id"]])
        save_json("new_results.json", results)
        save_json("results.json", merged)
        save_json("summary.json", {
            "completed": len(merged), "total": len(target_ids), "reused": len(reused),
            "new_completed": len(results), "new_total": len(selected),
            "new_initial_resolved": sum(initial_resolved(r) for r in results),
            "new_final_resolved": sum(bool(r.get("resolved")) for r in results),
            "initial_resolved": sum(initial_resolved(r) for r in merged),
            "final_resolved": sum(bool(r.get("resolved")) for r in merged),
            "environment_errors": sum(r.get("status") == "environment_error" for r in merged),
            "api_errors": sum(r.get("status") == "api_error" for r in merged),
            "infrastructure_errors": sum(infrastructure_error(r) for r in merged),
            "diagnostic_only": True,
        })
        predictions = []
        for result in merged:
            root = Path(result.get("output_dir") or output / "cases" / result["instance_id"])
            for path in [root / "predictions.json", root.parent / "predictions.json"]:
                if path.exists():
                    predictions.extend(json.loads(path.read_text()))
                    break
        save_json("predictions.json", predictions)

    def progress(lane, row, stage):
        with lock:
            lanes[lane] = {"instance_id": row["instance_id"], "stage": stage,
                           "timestamp": datetime.now(timezone.utc).isoformat()}
            save_json("progress.json", {"lanes": lanes, "new_completed": len(results),
                                        "new_total": len(selected)})
            print(f"{lanes[lane]['timestamp']} {lane} {row['instance_id']} {stage}", flush=True)

    def run_lane(lane):
        client = docker.from_env()
        while True:
            try:
                row = queue.get_nowait()
            except Empty:
                return
            instance = row["instance_id"]
            case = output / "cases" / instance
            case.mkdir(parents=True, exist_ok=True)
            progress(lane, row, "preflight")
            try:
                imports = REPO_IMPORTS[row["repo"]]
                with (case / "environment.log").open("w") as log:
                    image_name = f"sweb.eval.x86_64.{instance}:latest"
                    try:
                        cached = client.images.get(image_name)
                    except docker.errors.ImageNotFound:
                        cached = None
                    if cached and not args.canonical_environment:
                        # 镜像不可变；复用前核对源码树及运行依赖，不重复构建。
                        check = (cached_baseline_check(row, cached.labels)
                                 + imports + "; print('baseline_and_imports_ok')")
                        data = client.containers.run(image_name,
                            ["/opt/miniconda3/envs/testbed/bin/python", "-c", check], remove=True)
                        log.write(data.decode())
                        environment = {"status": "ready", "image": image_name, "image_id": cached.id,
                                       "baseline": row["base_commit"], "cached": True}
                    else:
                        environment = prepare_image(row, swebench, client, log,
                                                    imports=imports, proxy=proxy,
                                                    wheelhouse=args.wheelhouse if row["repo"] == "django/django" else None,
                                                    apt_mirror=args.apt_mirror, build_assets=args.build_assets,
                                                    canonical=args.canonical_environment,
                                                    clean_tests=args.clean_test_environment)
                    if args.clean_test_environment:
                        cleaned = clean_cached_test_image(row, swebench, client, log, proxy=proxy)
                        environment.update(cleaned)
            except Exception as exc:
                environment = {"status": "error", "error": str(exc)}
            (case / "environment.json").write_text(json.dumps(environment, indent=2))
            if environment["status"] != "ready":
                result = {"instance_id": instance, "status": "environment_error",
                          "resolved": False, "environment": environment}
            else:
                progress(lane, row, "running")
                command = [str(live / ".venv/bin/python"), str(args.source_root / "scripts/swebench_batch.py"),
                    "--dataset", str(dataset), "--repo-agent-bin", str(live / ".venv/bin/repo-agent"),
                    "--model", "glm-5.3-flash", "--implement-model", "glm-5.3-flash",
                    "--reasoning-effort", "low", "--coordinator-reasoning-effort", "high",
                    "--implement-reasoning-effort", "low", "--max-steps", "16",
                    "--work-dir", str(work), "--output-dir", str(case),
                    "--env-file", str(live / ".env"), "--proxy", "http://127.0.0.1:7897",
                    "--offline-cache", "--instance-id", instance, "--repair-mode", "experience-build",
                    "--timeout", "3600", "--evaluate", "--swebench-dir", str(swebench),
                    "--harness-python", str(swebench / ".venv/bin/python"),
                    "--run-id", output.name, "--harness-timeout", "900", "--cache-level", "instance",
                    "--namespace", "none", "--docker-network", "host", "--candidate-mode", "off",
                    "--no-prebuild-instance-image", "--no-repair-memory",
                    "--memory-root", str(output / lane / "isolated-memory"), "--memory-scope", row["repo"],
                    "--memory-provenance", "swebench-test-diagnostic"]
                old = previous_by_id.get(instance, {})
                old_pass = (old.get("passes") or [{}])[0]
                if (instance in args.reuse_initial_instance_id or
                        (not args.retry_unresolved and (old_pass.get("harness") or {}).get("error"))):
                    previous_file = case / "reused_initial_result.json"
                    previous_file.write_text(json.dumps(old, ensure_ascii=False, indent=2))
                    command = [str(live / ".venv/bin/python"),
                               str(Path(__file__).parent / "reuse_lite_initial_generation.py"),
                               "--source-root", str(args.source_root),
                               "--previous-result", str(previous_file), "--"] + command[2:]
                case_env = {**env, "REPO_AGENT_LLM_LOG_DIR": str(case / "llm-requests")}
                # 仅测试容器使用域名隧道；应用层代理保持清空，下载代理不受影响。
                case_env.pop("SWEBENCH_TEST_PROXY", None)
                case_env.pop("SWEBENCH_TEST_EXTRA_HOSTS", None)
                if row["repo"] == "psf/requests" and config["requests_extra_hosts"]:
                    case_env["SWEBENCH_TEST_EXTRA_HOSTS"] = json.dumps(config["requests_extra_hosts"])
                with (case / "run.log").open("w") as log:
                    proc = subprocess.run(command, cwd=args.source_root,
                        env=case_env,
                        stdout=log, stderr=subprocess.STDOUT)
                path = case / "batch_summary.json"
                result = json.loads(path.read_text())["instances"][0] if path.exists() else {
                    "instance_id": instance, "status": "runner_failed", "resolved": False}
                result["process_exit_code"] = proc.returncode
            with lock:
                results.append(result)
                save_results()
            progress(lane, row, f"finished status={result.get('status')} resolved={result.get('resolved')}")

    with lock:
        save_results()
    print(f"Reused {len(reused)} valid results; scheduling {len(selected)} cases, two workers.", flush=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_lane, lane) for lane in ["lane1", "lane2"]]
        for future in futures:
            future.result()
    print("Both lanes finished.", flush=True)


if __name__ == "__main__":
    main()
