#!/usr/bin/env bash
set -euo pipefail

REPO_AGENT_ROOT=${REPO_AGENT_ROOT:-/home/artoria/workspace/RepoAgent/RepoAgent}
SWEBENCH_ROOT=${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}
WORK_DIR=${WORK_DIR:-$SWEBENCH_ROOT/agent_work}
RUN_ROOT=${RUN_ROOT:-$WORK_DIR/runs/lite-fixed10-bottleneck-diagnostic-v1}
MODEL=${MODEL:-deepseek-v4-flash}
PROXY=${PROXY:-http://127.0.0.1:7897}
MAX_STEPS=${MAX_STEPS:-30}
AGENT_TIMEOUT=${AGENT_TIMEOUT:-1800}
HARNESS_TIMEOUT=${HARNESS_TIMEOUT:-1800}

mkdir -p "$RUN_ROOT"

INSTANCE_IDS=(
  django__django-15819
  django__django-16400
  matplotlib__matplotlib-22711
  matplotlib__matplotlib-22835
  matplotlib__matplotlib-23299
  scikit-learn__scikit-learn-25500
  scikit-learn__scikit-learn-25638
  sphinx-doc__sphinx-10451
  sphinx-doc__sphinx-7686
  sphinx-doc__sphinx-7738
)

COMMON_ARGS=(
  --dataset "$SWEBENCH_ROOT/test-00000-of-00001.parquet"
  --repo-agent-bin "$REPO_AGENT_ROOT/.venv/bin/repo-agent"
  --model "$MODEL"
  --work-dir "$WORK_DIR"
  --env-file "$REPO_AGENT_ROOT/.env"
  --proxy "$PROXY"
  --offline-cache
  --max-steps "$MAX_STEPS"
  --timeout "$AGENT_TIMEOUT"
  --evaluate
  --swebench-dir "$SWEBENCH_ROOT"
  --harness-timeout "$HARNESS_TIMEOUT"
  --cache-level env
  --namespace none
  --docker-network host
  --candidate-mode off
  --no-repair-memory
)

for instance_id in "${INSTANCE_IDS[@]}"; do
  COMMON_ARGS+=(--instance-id "$instance_id")
done

run_group() {
  local name=$1
  shift
  mkdir -p "$RUN_ROOT/$name"
  echo "[$(date --iso-8601=seconds)] starting $name" | tee "$RUN_ROOT/progress.txt"
  "$SWEBENCH_ROOT/.venv/bin/python" "$REPO_AGENT_ROOT/scripts/swebench_batch.py" \
    "${COMMON_ARGS[@]}" \
    --output-dir "$RUN_ROOT/$name" \
    --run-id "lite-fixed10-diagnostic-$name" \
    --diagnostic-mode "$name" \
    "$@" \
    2>&1 | tee "$RUN_ROOT/$name/run.log"
  echo "[$(date --iso-8601=seconds)] completed $name" | tee "$RUN_ROOT/progress.txt"
}

run_group baseline
run_group gold_files
run_group gold_loci
run_group corrective --corrective-source-dir "$RUN_ROOT/baseline"

"$SWEBENCH_ROOT/.venv/bin/python" "$REPO_AGENT_ROOT/scripts/summarize_diagnostic_matrix.py" \
  --root "$RUN_ROOT" | tee "$RUN_ROOT/summary.log"

echo "[$(date --iso-8601=seconds)] matrix complete" | tee "$RUN_ROOT/progress.txt"
