#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap. Safe to rerun.
# Does not download private warehouse / PyThrust datasets.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DRONE_OPTIMIZER_DATA_ROOT="${DRONE_OPTIMIZER_DATA_ROOT:-$ROOT/legacy/streamlit}"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip wheel
python -m pip install -e ".[dev]"

if [[ -f frontend/package-lock.json ]]; then
  (cd frontend && npm ci)
elif [[ -f frontend/package.json ]]; then
  (cd frontend && npm install)
fi

WAREHOUSE="$DRONE_OPTIMIZER_DATA_ROOT/component_data/component_warehouse.sqlite"
PYTHRUST="$DRONE_OPTIMIZER_DATA_ROOT/pythrust"
if [[ ! -f "$WAREHOUSE" ]] || [[ ! -d "$PYTHRUST" ]]; then
  echo "NOTE: private runtime data is not in this checkout."
  echo "  Expected warehouse: $WAREHOUSE"
  echo "  Expected pythrust:  $PYTHRUST"
  echo "  Catalog, evaluate, and optimizer jobs need that data on the agent VM."
  echo "  See docs/cloud-agents.md."
fi

echo "Cloud Agent install finished."
