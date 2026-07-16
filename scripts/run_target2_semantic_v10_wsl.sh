#!/usr/bin/env bash
set -euo pipefail

REPO_AGENT_ROOT=${REPO_AGENT_ROOT:-/home/artoria/workspace/RepoAgent/RepoAgent}
SWEBENCH_ROOT=${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}
WORK_DIR=${WORK_DIR:-$SWEBENCH_ROOT/agent_work}
RUN_ID=${RUN_ID:-lite-target2-semantic-v29-flash-coordinator-pro-implement}
OUTPUT_DIR=${OUTPUT_DIR:-$WORK_DIR/runs/$RUN_ID}
MODEL=${MODEL:-deepseek-v4-flash}
IMPLEMENT_MODEL=${IMPLEMENT_MODEL:-deepseek-v4-pro}
PROXY=${PROXY:-http://127.0.0.1:7897}
MAX_STEPS=${MAX_STEPS:-30}
AGENT_TIMEOUT=${AGENT_TIMEOUT:-2400}
HARNESS_TIMEOUT=${HARNESS_TIMEOUT:-1800}

mkdir -p "$OUTPUT_DIR"
echo "[$(date --iso-8601=seconds)] starting $RUN_ID" | tee "$OUTPUT_DIR/progress.txt"

set +e
"$SWEBENCH_ROOT/.venv/bin/python" "$REPO_AGENT_ROOT/scripts/swebench_batch.py" \
  --dataset "$SWEBENCH_ROOT/test-00000-of-00001.parquet" \
  --repo-agent-bin "$REPO_AGENT_ROOT/.venv/bin/repo-agent" \
  --model "$MODEL" \
  --implement-model "$IMPLEMENT_MODEL" \
  --work-dir "$WORK_DIR" \
  --output-dir "$OUTPUT_DIR" \
  --env-file "$REPO_AGENT_ROOT/.env" \
  --proxy "$PROXY" \
  --offline-cache \
  --instance-id scikit-learn__scikit-learn-25638 \
  --instance-id sphinx-doc__sphinx-10451 \
  --max-steps "$MAX_STEPS" \
  --timeout "$AGENT_TIMEOUT" \
  --evaluate \
  --swebench-dir "$SWEBENCH_ROOT" \
  --run-id "$RUN_ID" \
  --harness-timeout "$HARNESS_TIMEOUT" \
  --cache-level env \
  --namespace none \
  --docker-network host \
  --candidate-mode off \
  --no-repair-memory \
  2>&1 | tee "$OUTPUT_DIR/run.log"
status=${PIPESTATUS[0]}
set -e

echo "[$(date --iso-8601=seconds)] finished exit=$status" | tee -a "$OUTPUT_DIR/progress.txt" "$OUTPUT_DIR/run.log"
exit "$status"
