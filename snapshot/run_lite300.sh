#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/runs/lite300"
PYTHON="$ROOT/.venv/bin/python"
AGENT="$ROOT/.venv/bin/repo-agent"

if [[ ! -x "$PYTHON" || ! -x "$AGENT" ]]; then
    printf '%s\n' "请先在 WSL 为本快照安装独立 .venv，并安装此目录的 repo-agent。" >&2
    exit 1
fi

mkdir -p "$RUN/results" "$RUN/logs" "$RUN/work" "$RUN/harness"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

"$PYTHON" -u "$ROOT/scripts/swebench_batch.py" "$@" \
    --repo-agent-bin "$AGENT" \
    --start-index 0 --limit 300 \
    --work-dir "$RUN/work" \
    --output-dir "$RUN/results" \
    --swebench-dir "$RUN/harness" \
    --run-id frozen-20261123-lite300 \
    2>&1 | tee -a "$RUN/logs/run.log"
