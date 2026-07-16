#!/usr/bin/env bash
set -Eeuo pipefail

# Rerun the unresolved cases from SWE-bench Lite rows 100-199.
# The instance list is generated from /tmp/repoagent_unresolved_analysis_100_199.json,
# which is produced by the unresolved-analysis helper used in this experiment.

REPOAGENT_ROOT="${REPOAGENT_ROOT:-/home/artoria/workspace/RepoAgent/RepoAgent}"
SWEBENCH_ROOT="${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}"
DATASET="${DATASET:-$SWEBENCH_ROOT/test-00000-of-00001.parquet}"
REPO_AGENT_BIN="${REPO_AGENT_BIN:-$REPOAGENT_ROOT/.venv/bin/repo-agent}"
SWEBENCH_PY="${SWEBENCH_PY:-$SWEBENCH_ROOT/.venv/bin/python}"

MODEL="${MODEL:-deepseek-v4-flash}"
MAX_STEPS="${MAX_STEPS:-18}"
TIMEOUT="${TIMEOUT:-2400}"
HARNESS_TIMEOUT="${HARNESS_TIMEOUT:-1200}"
PROXY="${PROXY:-http://127.0.0.1:7897}"

MEMORY_ROOT="${MEMORY_ROOT:-$SWEBENCH_ROOT/agent_work/memory}"
MEMORY_SCOPE="${MEMORY_SCOPE:-recipe-evolve10-global}"
EVOLVE_MEMORY="${EVOLVE_MEMORY:-0}"
SKIP_EXISTING="${SKIP_EXISTING:-1}"
ANALYSIS_JSON="${ANALYSIS_JSON:-/tmp/repoagent_unresolved_analysis_100_199.json}"

RUN_NAME="${RUN_NAME:-lite-test100to199-unresolved56-patchreview-gate-max18}"
WORK_DIR="${WORK_DIR:-$SWEBENCH_ROOT/agent_work}"
OUTPUT_DIR="${OUTPUT_DIR:-$WORK_DIR/runs/$RUN_NAME}"
LOG_DIR="${LOG_DIR:-$WORK_DIR/logs/$RUN_NAME}"
LOG_FILE="$LOG_DIR/run.log"
PROGRESS_FILE="$LOG_DIR/progress.txt"
IDS_FILE="$LOG_DIR/unresolved56_ids.txt"
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
if [[ ! -f "$ANALYSIS_JSON" ]]; then
  echo "Unresolved analysis JSON not found: $ANALYSIS_JSON" >&2
  exit 2
fi

"$SWEBENCH_PY" - "$ANALYSIS_JSON" "$IDS_FILE" <<'PY'
import json
import sys
from pathlib import Path

analysis = Path(sys.argv[1])
ids_file = Path(sys.argv[2])
rows = json.loads(analysis.read_text(encoding="utf-8"))
ids = [row["iid"] for row in rows]
if len(ids) != 56:
    raise SystemExit(f"expected 56 unresolved ids, got {len(ids)}")
ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
print(f"wrote {len(ids)} ids to {ids_file}")
PY

export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,raw.githubusercontent.com}"
export no_proxy="${no_proxy:-$NO_PROXY}"
export SWEBENCH_DOCKER_BUILD_NETWORK="${SWEBENCH_DOCKER_BUILD_NETWORK:-host}"
export SWEBENCH_DOCKER_NETWORK="${SWEBENCH_DOCKER_NETWORK:-host}"
export SWEBENCH_NETWORK_HARDENING="${SWEBENCH_NETWORK_HARDENING:-1}"
export SWEBENCH_RAW_RETRIES="${SWEBENCH_RAW_RETRIES:-10}"
export SWEBENCH_GIT_MIRROR_BASE="${SWEBENCH_GIT_MIRROR_BASE:-http://127.0.0.1:18080}"
export SWEBENCH_PASS_PROXY_TO_DOCKER="${SWEBENCH_PASS_PROXY_TO_DOCKER:-0}"

COMMAND=(
  "$SWEBENCH_PY" "$REPOAGENT_ROOT/scripts/swebench_batch.py"
  --dataset "$DATASET"
  --repo-agent-bin "$REPO_AGENT_BIN"
  --model "$MODEL"
  --work-dir "$WORK_DIR"
  --output-dir "$OUTPUT_DIR"
  --env-file "$REPOAGENT_ROOT/.env"
  --proxy "$PROXY"
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

while IFS= read -r iid; do
  [[ -n "$iid" ]] && COMMAND+=(--instance-id "$iid")
done < "$IDS_FILE"

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
  echo "ids_file=$IDS_FILE"
  echo "output_dir=$OUTPUT_DIR"
  echo "log_file=$LOG_FILE"
  echo "progress_file=$PROGRESS_FILE"
  echo "memory_scope=$MEMORY_SCOPE"
  echo "evolve_memory=$EVOLVE_MEMORY"
  echo "max_steps=$MAX_STEPS"
  printf "command="
  printf "%q " "${COMMAND[@]}"
  echo
} >> "$LOG_FILE"

echo "starting 0/56 at $(date -Is)" > "$PROGRESS_FILE"

set +e
"${COMMAND[@]}" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" =~ ^\[([0-9]+)/([0-9]+)\]\ (.+)$ ]]; then
    echo "running ${BASH_REMATCH[1]}/${BASH_REMATCH[2]} ${BASH_REMATCH[3]} at $(date -Is)" > "$PROGRESS_FILE"
  elif [[ "$line" =~ ^Summary:\ (.+)$ ]]; then
    echo "done 56/56 at $(date -Is)" > "$PROGRESS_FILE"
  fi
done >> "$LOG_FILE"
status=${PIPESTATUS[0]}
set -e

if [[ "$status" -eq 0 ]]; then
  echo "done 56/56 at $(date -Is)" > "$PROGRESS_FILE"
else
  echo "failed status=$status at $(date -Is)" > "$PROGRESS_FILE"
fi
exit "$status"
