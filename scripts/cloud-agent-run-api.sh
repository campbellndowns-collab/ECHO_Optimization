#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DRONE_OPTIMIZER_DATA_ROOT="${DRONE_OPTIMIZER_DATA_ROOT:-$ROOT/legacy/streamlit}"
if [[ -d "$DRONE_OPTIMIZER_DATA_ROOT/pythrust" ]]; then
  export PYTHONPATH="${DRONE_OPTIMIZER_DATA_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

exec uvicorn backend.app.main:app --host 127.0.0.1 --port 43127
