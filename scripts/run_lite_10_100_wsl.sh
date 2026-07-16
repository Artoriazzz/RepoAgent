#!/usr/bin/env bash
set -Eeuo pipefail

# Run SWE-bench Lite rows 10-99 (zero-based), continuing after the first 10.
# This is an experimental continuous-evolution run by default. For leaderboard-style
# frozen runs, execute with EVOLVE_MEMORY=0 and use a clean/frozen memory scope.

REPOAGENT_ROOT="${REPOAGENT_ROOT:-/home/artoria/workspace/RepoAgent/RepoAgent}"
SWEBENCH_ROOT="${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}"
DATASET="${DATASET:-$SWEBENCH_ROOT/test-00000-of-00001.parquet}"
REPO_AGENT_BIN="${REPO_AGENT_BIN:-$REPOAGENT_ROOT/.venv/bin/repo-agent}"
SWEBENCH_PY="${SWEBENCH_PY:-$SWEBENCH_ROOT/.venv/bin/python}"

MODEL="${MODEL:-deepseek-v4-flash}"
START_INDEX="${START_INDEX:-10}"
LIMIT="${LIMIT:-90}"
MAX_STEPS="${MAX_STEPS:-12}"
TIMEOUT="${TIMEOUT:-1800}"
HARNESS_TIMEOUT="${HARNESS_TIMEOUT:-900}"
PROXY="${PROXY:-http://127.0.0.1:7897}"

MEMORY_ROOT="${MEMORY_ROOT:-$SWEBENCH_ROOT/agent_work/memory}"
MEMORY_SCOPE="${MEMORY_SCOPE:-recipe-evolve10-global}"
EVOLVE_MEMORY="${EVOLVE_MEMORY:-1}"
SKIP_EXISTING="${SKIP_EXISTING:-1}"

RUN_NAME="${RUN_NAME:-lite-test10to99-evolve-recipe-applicability2-pass1-max12}"
WORK_DIR="${WORK_DIR:-$SWEBENCH_ROOT/agent_work}"
OUTPUT_DIR="${OUTPUT_DIR:-$WORK_DIR/runs/$RUN_NAME}"
LOG_DIR="${LOG_DIR:-$WORK_DIR/logs/$RUN_NAME}"
LOG_FILE="$LOG_DIR/run.log"
PROGRESS_FILE="$LOG_DIR/progress.txt"
PROGRESS_TSV="$LOG_DIR/progress.tsv"
PID_FILE="$LOG_DIR/run.pid"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"
echo "$$" > "$PID_FILE"

if [[ ! -x "$REPO_AGENT_BIN" ]]; then
  echo "repo-agent executable not found: $REPO_AGENT_BIN" >&2
  exit 2
fi
if [[ ! -x "$SWEBENCH_PY" ]]; then
  echo "SWE-bench python not found: $SWEBENCH_PY" >&2
  exit 2
fi
if [[ ! -f "$DATASET" ]]; then
  echo "Dataset not found: $DATASET" >&2
  exit 2
fi

COMMAND=(
  "$SWEBENCH_PY" "$REPOAGENT_ROOT/scripts/swebench_batch.py"
  --dataset "$DATASET"
  --repo-agent-bin "$REPO_AGENT_BIN"
  --model "$MODEL"
  --work-dir "$WORK_DIR"
  --output-dir "$OUTPUT_DIR"
  --env-file "$REPOAGENT_ROOT/.env"
  --proxy "$PROXY"
  --start-index "$START_INDEX"
  --limit "$LIMIT"
  --max-steps "$MAX_STEPS"
  --timeout "$TIMEOUT"
  --evaluate
  --swebench-dir "$SWEBENCH_ROOT"
  --run-id "$RUN_NAME"
  --harness-timeout "$HARNESS_TIMEOUT"
  --cache-level env
  --namespace none
  --docker-network host
  --memory-root "$MEMORY_ROOT"
  --memory-scope "$MEMORY_SCOPE"
)

if [[ "$SKIP_EXISTING" == "1" ]]; then
  COMMAND+=(--skip-existing)
fi
if [[ "$EVOLVE_MEMORY" == "1" ]]; then
  COMMAND+=(--evolve-memory)
fi

{
  echo "started_at=$(date -Is)"
  echo "pid=$$"
  echo "run_name=$RUN_NAME"
  echo "start_index=$START_INDEX"
  echo "limit=$LIMIT"
  echo "output_dir=$OUTPUT_DIR"
  echo "log_file=$LOG_FILE"
  echo "progress_file=$PROGRESS_FILE"
  echo "memory_scope=$MEMORY_SCOPE"
  echo "evolve_memory=$EVOLVE_MEMORY"
  printf "command="
  printf "%q " "${COMMAND[@]}"
  echo
} > "$LOG_DIR/run.meta"

echo -e "timestamp\tindex\ttotal\tinstance_id\tstatus_line" > "$PROGRESS_TSV"
echo "started $(date -Is); waiting for first instance" > "$PROGRESS_FILE"

current_index="0"
current_total="$LIMIT"
current_instance=""

set +e
"${COMMAND[@]}" 2>&1 | while IFS= read -r line; do
  ts="$(date '+%F %T')"
  printf '[%s] %s\n' "$ts" "$line" | tee -a "$LOG_FILE"

  if [[ "$line" =~ ^\[([0-9]+)/([0-9]+)\]\ (.+)$ ]]; then
    current_index="${BASH_REMATCH[1]}"
    current_total="${BASH_REMATCH[2]}"
    current_instance="${BASH_REMATCH[3]}"
    echo "running $current_index/$current_total $current_instance at $(date -Is)" > "$PROGRESS_FILE"
  elif [[ "$line" =~ ^[[:space:]]status= ]]; then
    echo "finished $current_index/$current_total $current_instance: ${line#  } at $(date -Is)" > "$PROGRESS_FILE"
    printf '%s\t%s\t%s\t%s\t%s\n' "$ts" "$current_index" "$current_total" "$current_instance" "${line#  }" >> "$PROGRESS_TSV"
  fi
done
status=${PIPESTATUS[0]}
set -e

if [[ "$status" -eq 0 ]]; then
  echo "completed $(date -Is); summary=$OUTPUT_DIR/batch_summary.json" > "$PROGRESS_FILE"
else
  echo "failed status=$status at $(date -Is); inspect $LOG_FILE" > "$PROGRESS_FILE"
fi

exit "$status"
