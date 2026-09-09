#!/usr/bin/env bash
set -euo pipefail

REPO_AGENT_ROOT=${REPO_AGENT_ROOT:-/home/artoria/workspace/RepoAgent/RepoAgent}
SWEBENCH_ROOT=${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}
WORK_DIR=${WORK_DIR:-$SWEBENCH_ROOT/agent_work}
RUN_NAME=${RUN_NAME:-INVALID-experience-build-four-prioritized-harness-v46-20260807}
OUTPUT_DIR=${OUTPUT_DIR:-$WORK_DIR/runs/$RUN_NAME}
PROXY=${PROXY:-http://127.0.0.1:7897}
MIRROR_BASE=${MIRROR_BASE:-http://172.17.0.1:18080}
MEMORY_PROVENANCE=${MEMORY_PROVENANCE:-swebench-lite-prioritized-harness-v46}
NAMESPACE=${NAMESPACE:-swebench}
INSTANCE_IDS=${INSTANCE_IDS:-"django__django-15819 django__django-16816 matplotlib__matplotlib-22711 matplotlib__matplotlib-22835"}

read -r -a INSTANCE_ID_LIST <<< "$INSTANCE_IDS"
INSTANCE_ARGS=()
for instance_id in "${INSTANCE_ID_LIST[@]}"; do
  INSTANCE_ARGS+=(--instance-id "$instance_id")
done

mkdir -p "$OUTPUT_DIR"

export SWEBENCH_GIT_MIRROR_BASE="$MIRROR_BASE"

exec "$REPO_AGENT_ROOT/.venv/bin/python" \
  "$REPO_AGENT_ROOT/scripts/swebench_batch.py" \
  --dataset "$SWEBENCH_ROOT/test-00000-of-00001.parquet" \
  --repo-agent-bin "$REPO_AGENT_ROOT/.venv/bin/repo-agent" \
  --model deepseek-v4-flash \
  --implement-model deepseek-v4-flash \
  --reasoning-effort high \
  --implement-reasoning-effort max \
  --work-dir "$WORK_DIR" \
  --output-dir "$OUTPUT_DIR" \
  --env-file "$REPO_AGENT_ROOT/.env" \
  --proxy "$PROXY" \
  --offline-cache \
  "${INSTANCE_ARGS[@]}" \
  --max-steps 12 \
  --repair-mode experience-build \
  --timeout 2700 \
  --evaluate \
  --swebench-dir "$SWEBENCH_ROOT" \
  --harness-python "$SWEBENCH_ROOT/.venv/bin/python" \
  --run-id "$RUN_NAME" \
  --harness-timeout 1800 \
  --cache-level instance \
  --namespace "$NAMESPACE" \
  --docker-network host \
  --image-build-timeout 3600 \
  --candidate-mode off \
  --memory-provenance "$MEMORY_PROVENANCE"
