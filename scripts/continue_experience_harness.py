"""复用已有初始 patch，从环境修复后的 harness 继续模式一。"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import swebench_batch as batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    resume, remaining = parser.parse_known_args()
    args = batch.build_parser().parse_args(remaining)
    if args.repair_mode != "experience-build" or not args.evaluate:
        parser.error("requires experience-build and --evaluate")
    rows = batch.load_instances(args.dataset, args.instance_id, args.limit, args.start_index)
    if len(rows) != 1:
        parser.error("continue exactly one instance")
    row = rows[0]
    source = resume.source_run / row["instance_id"] / "pass-1"
    patch = (source / "model.patch").read_text()
    if not patch.strip():
        raise ValueError("No saved patch to evaluate")
    task = (source / "task.md").read_text()
    attribution = json.loads((source / "stage_attribution.json").read_text())
    report = attribution.get("stages", {}).get("implementation", {}).get("report", {})
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    subprocess.run(["git", "clone", "--no-hardlinks", str(args.work_dir / "repos" / row["instance_id"]), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", row["base_commit"]], check=True)
    subprocess.run(["git", "-C", str(repo), "apply", "-"], input=patch, text=True, check=True)
    env = os.environ.copy()
    if args.env_file:
        env.update(batch.read_env_file(args.env_file))
    if args.proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = args.proxy
    summary = {"source_run": str(resume.source_run), "instance_id": row["instance_id"],
               "leaderboard_eligible": False, "rounds": [], "status": "running"}

    def save():
        (output / "model.patch").write_text(patch)
        (output / "continuation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    save()
    for index in range(3):
        print(f"Harness round {index + 1}/3", flush=True)
        harness = batch.run_harness_for_instance(row, patch, output / f"evaluation-{index}", args, env, index)
        current = {"harness": harness}
        summary["rounds"].append(current)
        summary["harness"] = harness
        save()
        feedback = batch.build_experience_harness_feedback(harness, patch)
        if not feedback["feedback_eligible"] or index == 2:
            break
        print(f"FailureReview / correction {index + 1}/2", flush=True)
        before = patch
        correction = batch.run_experience_correction(
            row, repo, output / f"correction-{index + 1}", task, patch, feedback, report, args, env
        )
        candidate = correction.pop("model_patch", before)
        current["correction"] = correction
        save()
        if not correction.get("attempted") or correction.get("agent_exit_code", 0) != 0 or candidate == before:
            break
        validation = batch.validate_model_patch(candidate, repo_dir=repo,
            agent_output_dir=Path(correction["output_dir"]), require_patch_review=False,
            reject_failed_verification=False, stage_attribution=correction.get("stage_attribution"))
        current["patch_validation"] = validation
        if validation.get("discard_patch"):
            batch.restore_worktree_patch(repo, before)
            break
        patch = candidate
        try:
            report = json.loads(correction.get("worker_result", {}).get("text", "{}"))
        except ValueError:
            report = {}
        save()
    summary["status"] = "finished"
    save()
    print(f"Finished; resolved={summary['harness'].get('resolved')} error={summary['harness'].get('error')}", flush=True)


if __name__ == "__main__":
    main()
