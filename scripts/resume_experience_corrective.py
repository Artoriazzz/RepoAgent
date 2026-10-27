"""从已保存的失败归因继续一次纠正，使用独立工作副本保留原实验。"""

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import swebench_batch as batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--review-again", action="store_true", help="Diagnostic-only: review the saved final harness before correction")
    parser.add_argument("--corrective-reasoning-effort", choices=("low", "high", "max"), default="low")
    parser.add_argument("--llm-timeout", type=float, default=120)
    parser.add_argument("--llm-max-retries", type=int, default=1)
    resume, remaining = parser.parse_known_args()
    if resume.llm_timeout <= 0 or resume.llm_max_retries < 0:
        parser.error("llm-timeout must be positive and llm-max-retries non-negative")
    args = batch.build_parser().parse_args(remaining)
    args.implement_reasoning_effort = resume.corrective_reasoning_effort
    if args.repair_mode != "experience-build" or not args.evaluate:
        parser.error("resume requires experience-build and --evaluate")
    if not args.memory_provenance.strip():
        parser.error("resume requires explicit memory provenance")
    rows = batch.load_instances(args.dataset, args.instance_id, args.limit, args.start_index)
    if len(rows) != 1:
        parser.error("resume exactly one instance at a time")
    row = rows[0]
    if resume.review_again:
        previous = json.loads((resume.source_run / "resume_summary.json").read_text())
        if previous.get("instance_id") != row["instance_id"]:
            raise ValueError("Saved run instance differs from requested instance")
        source = Path(previous["source_run"]) / row["instance_id"] / "pass-1"
        patch = (resume.source_run / "model.patch").read_text()
        validation = batch.build_experience_harness_feedback(previous["harness"], patch)
        if not validation["feedback_eligible"]:
            raise ValueError("Saved final harness has no eligible behavioral feedback")
        report_text = previous.get("correction", {}).get("worker_result", {}).get("text", "{}")
        implementation_report = json.loads(report_text)
    else:
        source = resume.source_run / row["instance_id"] / "pass-1"
        feedback_path = source / "experience-correction/failure-review/failure_feedback.json"
        feedback = json.loads(feedback_path.read_text())
        if batch.validated_failure_feedback_route(feedback) != "corrective_implement":
            raise ValueError("Saved feedback does not authorize correction")
        patch = (source / "model.patch").read_text()
    task = (source / "task.md").read_text()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    if args.env_file:
        env.update(batch.read_env_file(args.env_file))
    if args.proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = args.proxy
    # 国内 GLM 接口直连，仓库及 harness 下载继续使用原代理。
    api_host = urlparse(env.get("OPENAI_BASE_URL", "")).hostname
    if api_host == "open.bigmodel.cn":
        exclusions = set()
        for name in ("NO_PROXY", "no_proxy"):
            exclusions.update(value.strip() for value in env.get(name, "").split(",") if value.strip())
        exclusions.add(api_host)
        for name in ("NO_PROXY", "no_proxy"):
            env[name] = ",".join(sorted(exclusions))
    env["REPO_AGENT_LLM_TIMEOUT_SECONDS"] = str(resume.llm_timeout)
    env["REPO_AGENT_LLM_MAX_RETRIES"] = str(resume.llm_max_retries)
    repo = output / "repo"
    original_repo = args.work_dir / "repos" / row["instance_id"]
    subprocess.run(["git", "clone", "--no-hardlinks", str(original_repo), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", row["base_commit"]], check=True)
    subprocess.run(["git", "-C", str(repo), "apply", "-"], input=patch, text=True, check=True)
    summary = {"source_run": str(resume.source_run), "instance_id": row["instance_id"],
               "leaderboard_eligible": False, "status": "corrective_running",
               "diagnostic_extra_round": resume.review_again,
               "request_config": {"implement_reasoning_effort": args.implement_reasoning_effort,
                                  "timeout_seconds": resume.llm_timeout,
                                  "max_retries": resume.llm_max_retries,
                                  "glm_direct": api_host == "open.bigmodel.cn"}}

    def save():
        (output / "resume_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    save()
    if resume.review_again:
        summary["status"] = "failure_review_running"
        save()
        print("Starting diagnostic failure review from saved final harness", flush=True)
        correction = batch.run_experience_correction(
            row, repo, output / "experience-correction", task, patch, validation,
            implementation_report, args, env
        )
    else:
        print("Starting corrective implementation from saved feedback", flush=True)
        correction = batch.run_inline_experience_corrective(
            row, repo, output / "corrective-implement", task, patch, feedback, args, env
        )
    final_patch = correction.pop("model_patch")
    summary["correction"] = correction
    (output / "model.patch").write_text(final_patch)
    summary["status"] = "corrective_finished"
    save()
    if correction.get("agent_exit_code") != 0 or not final_patch.strip():
        print("Correction failed; inspect resume_summary.json and agent_stdout.txt", flush=True)
        return 1
    print("Running final harness", flush=True)
    summary["status"] = "harness_running"
    save()
    harness = batch.run_harness_for_instance(row, final_patch, output / "final-evaluation", args, env, 2 if resume.review_again else 1)
    summary["harness"] = harness
    summary["status"] = "finished"
    save()
    if harness.get("resolved") and not harness.get("error") and not resume.review_again:
        print("Resolved; recording validated experience", flush=True)
        summary["curator"] = batch.run_experience_curator_for_success(
            row, repo, output, task=task, failed_patch=patch, successful_patch=final_patch,
            feedback_file=feedback_path, validation=harness, args=args, env=env
        )
        save()
    print("Finished; resolved=" + str(harness.get("resolved")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
