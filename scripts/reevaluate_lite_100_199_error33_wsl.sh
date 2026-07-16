#!/usr/bin/env bash
set -Eeuo pipefail

# Re-evaluate the 33 SWE-bench Lite harness-error predictions from rows 100-199.
# This script does not rerun RepoAgent. It filters the existing predictions,
# fills missing local git mirrors, then invokes the official SWE-bench harness.

SWEBENCH_ROOT="${SWEBENCH_ROOT:-/home/artoria/workspace/SWE-bench}"
PREV_RUN="${PREV_RUN:-lite-test100to199-unresolved56-patchreview-gate-max18}"
RUN_NAME="${RUN_NAME:-lite-test100to199-error33-reeval-proxyfixed}"
MODEL="${MODEL:-deepseek-v4-flash}"
PROXY="${PROXY:-http://127.0.0.1:7897}"
TIMEOUT="${TIMEOUT:-1200}"
MAX_WORKERS="${MAX_WORKERS:-1}"

WORK_DIR="${WORK_DIR:-$SWEBENCH_ROOT/agent_work}"
PREV_OUTPUT_DIR="$WORK_DIR/runs/$PREV_RUN"
OUTPUT_DIR="${OUTPUT_DIR:-$WORK_DIR/runs/$RUN_NAME}"
LOG_DIR="${LOG_DIR:-$WORK_DIR/logs/$RUN_NAME}"
MIRROR_DIR="${MIRROR_DIR:-$SWEBENCH_ROOT/git_mirrors}"
DATASET="${DATASET:-$SWEBENCH_ROOT/test-00000-of-00001.parquet}"
PYTHON="${PYTHON:-$SWEBENCH_ROOT/.venv/bin/python}"

LOG_FILE="$LOG_DIR/run.log"
PROGRESS_FILE="$LOG_DIR/progress.txt"
PID_FILE="$LOG_DIR/run.pid"
IDS_FILE="$LOG_DIR/error33_ids.txt"
PREDICTIONS_FILE="$OUTPUT_DIR/error33_predictions.json"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR" "$MIRROR_DIR"
echo "$$" > "$PID_FILE"

{
  echo "started_at=$(date -Is)"
  echo "pid=$$"
  echo "run_name=$RUN_NAME"
  echo "prev_run=$PREV_RUN"
  echo "dataset=$DATASET"
  echo "predictions_file=$PREDICTIONS_FILE"
  echo "log_file=$LOG_FILE"
  echo "progress_file=$PROGRESS_FILE"
  echo "proxy=$PROXY"
} >> "$LOG_FILE"

echo "preparing inputs at $(date -Is)" > "$PROGRESS_FILE"

"$PYTHON" - "$PREV_OUTPUT_DIR" "$IDS_FILE" "$PREDICTIONS_FILE" <<'PY' >> "$LOG_FILE"
import json
import sys
from pathlib import Path

prev_output = Path(sys.argv[1])
ids_file = Path(sys.argv[2])
predictions_file = Path(sys.argv[3])

summary = json.loads((prev_output / "batch_summary.json").read_text(encoding="utf-8"))
predictions = json.loads((prev_output / "predictions.json").read_text(encoding="utf-8"))
prediction_by_id = {row["instance_id"]: row for row in predictions}

ids = [
    row["instance_id"]
    for row in summary["instances"]
    if row["passes"][0]["harness"].get("error")
]
missing = [iid for iid in ids if iid not in prediction_by_id]
if missing:
    raise SystemExit(f"missing predictions for: {missing}")
if len(ids) != 33:
    raise SystemExit(f"expected 33 harness-error ids, got {len(ids)}")

ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
predictions_file.write_text(
    json.dumps([prediction_by_id[iid] for iid in ids], ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"wrote {len(ids)} ids to {ids_file}")
print(f"wrote filtered predictions to {predictions_file}")
PY

mirror_repo() {
  local slug="$1"
  local url="$2"
  local target="$MIRROR_DIR/$slug.git"
  if [[ -d "$target" ]]; then
    echo "mirror present: $slug" >> "$LOG_FILE"
  else
    echo "mirror cloning: $slug from $url at $(date -Is)" | tee -a "$LOG_FILE"
    for attempt in 1 2 3; do
      rm -rf "$target.tmp"
      if git -c http.proxy="$PROXY" -c https.proxy="$PROXY" clone --mirror "$url" "$target.tmp" >> "$LOG_FILE" 2>&1; then
        mv "$target.tmp" "$target"
        break
      fi
      rm -rf "$target.tmp"
      if [[ "$attempt" -eq 3 ]]; then
        echo "mirror clone failed: $slug" | tee -a "$LOG_FILE"
        return 1
      fi
      sleep $((attempt * 20))
    done
  fi
  git --git-dir="$target" update-server-info >> "$LOG_FILE" 2>&1 || true
}

echo "mirroring repos at $(date -Is)" > "$PROGRESS_FILE"
mirror_repo "django__django" "https://github.com/django/django.git"
mirror_repo "mwaskom__seaborn" "https://github.com/mwaskom/seaborn.git"
mirror_repo "psf__requests" "https://github.com/psf/requests.git"
mirror_repo "pydata__xarray" "https://github.com/pydata/xarray.git"
mirror_repo "pytest-dev__pytest" "https://github.com/pytest-dev/pytest.git"
mirror_repo "scikit-learn__scikit-learn" "https://github.com/scikit-learn/scikit-learn.git"
mirror_repo "matplotlib__matplotlib" "https://github.com/matplotlib/matplotlib.git"

export HTTP_PROXY="$PROXY"
export HTTPS_PROXY="$PROXY"
export http_proxy="$PROXY"
export https_proxy="$PROXY"
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,raw.githubusercontent.com}"
export no_proxy="${no_proxy:-$NO_PROXY}"
export SWEBENCH_NETWORK_HARDENING="${SWEBENCH_NETWORK_HARDENING:-1}"
export SWEBENCH_RAW_RETRIES="${SWEBENCH_RAW_RETRIES:-10}"
export SWEBENCH_GIT_MIRROR_BASE="${SWEBENCH_GIT_MIRROR_BASE:-http://127.0.0.1:18080}"
export SWEBENCH_PASS_PROXY_TO_DOCKER="${SWEBENCH_PASS_PROXY_TO_DOCKER:-0}"
export SWEBENCH_DOCKER_BUILD_NETWORK="${SWEBENCH_DOCKER_BUILD_NETWORK:-host}"
export SWEBENCH_DOCKER_NETWORK="${SWEBENCH_DOCKER_NETWORK:-host}"

mapfile -t IDS < "$IDS_FILE"
COMMAND=(
  "$PYTHON" -m swebench.harness.run_evaluation
  --dataset_name "$DATASET"
  --predictions_path "$PREDICTIONS_FILE"
  --max_workers "$MAX_WORKERS"
  --instance_ids "${IDS[@]}"
  --run_id "$RUN_NAME"
  --namespace none
  --cache_level env
  --timeout "$TIMEOUT"
  --report_dir "$OUTPUT_DIR"
)

{
  echo "evaluation_started_at=$(date -Is)"
  printf "command="
  printf "%q " "${COMMAND[@]}"
  echo
} >> "$LOG_FILE"
echo "evaluating 33 predictions at $(date -Is)" > "$PROGRESS_FILE"

set +e
(
  cd "$SWEBENCH_ROOT"
  "${COMMAND[@]}"
) >> "$LOG_FILE" 2>&1
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
  echo "done 33/33 at $(date -Is)" > "$PROGRESS_FILE"
else
  echo "failed status=$status at $(date -Is)" > "$PROGRESS_FILE"
fi

echo "finished_at=$(date -Is) status=$status" >> "$LOG_FILE"
exit "$status"
