# Drone Optimizer

Portfolio-oriented migration of **Drone Optimizer v0.7** (Streamlit + PyThrust/OpenMDAO)
into a deployable web architecture:

`frontend (Next.js) → FastAPI → job worker → optimizer package + private component data`

## Status

**Phase 5 frontend is running.**

- Backend API: [Drone Optimizer API](http://127.0.0.1:43127)
- Frontend UI: [Drone Optimizer](http://127.0.0.1:43128)
- Tests: **19 passed** (regression + integration)
- Private data remains gitignored

Core workflow in the UI: lock/optimize selectors → fixed evaluate or async job → progress stages → results with instant weight rerank and trade plots.

### Run API locally

```bash
export DRONE_OPTIMIZER_DATA_ROOT=$PWD/legacy/streamlit
source legacy/streamlit/.venv/bin/activate
pip install -e ".[dev]"
uvicorn backend.app.main:app --host 127.0.0.1 --port 43127
```

Example:

```bash
curl -s http://127.0.0.1:43127/health
curl -s -X POST http://127.0.0.1:43127/evaluate -H 'Content-Type: application/json' -d @- <<'JSON'
{
  "motor_id": "NeuMotors_4610MC-207",
  "propeller_id": "APC_19x10E",
  "battery_cell_id": "fcde6dc9431046231f6f",
  "battery_topology": "6S4P",
  "esc_id": "4420b0e1b6c93b81ca45",
  "fixed_mass_kg": 0.5,
  "max_auw_kg": 10.0,
  "min_twr": 1.8
}
JSON
```

### Package layout

```text
optimizer/
  engine.py           # evaluate_fixed_design, rerank_designs
  paths.py            # DRONE_OPTIMIZER_DATA_ROOT → legacy/streamlit
  components/         # warehouse, screening, quality
  propulsion/         # guide + integrated engines (v0.7 code)
  batteries|frame|ranking|caching|validation/  # re-exports for now
backend/
  app/main.py         # FastAPI
  app/api/evaluate.py # POST /evaluate
```

### Run tests

```bash
export DRONE_OPTIMIZER_DATA_ROOT=$PWD/legacy/streamlit
python -m pytest tests/regression tests/integration -v
```

## Repository layout (target)

```text
legacy/streamlit/     # v0.7 reference overlay — do not delete
optimizer/            # extracted numerical package (Phase 2)
backend/              # FastAPI (Phase 3+)
frontend/             # Next.js (Phase 5)
tests/                # regression first (Phase 1)
data/private/         # full warehouse + caches (gitignored)
data/public/          # sanitized bootstrap only (after license audit)
docs/                 # architecture + licensing notes
```

## Private vs public data

| Use | Data |
|---|---|
| Local engineering / production | Full warehouse + PyThrust motor/APC data under `data/private/` (or symlinked into `legacy/streamlit/` paths) |
| Public GitHub | Sanitized subset in `data/public/` only after provenance audit |

See [`docs/data-licensing.md`](docs/data-licensing.md).

## What you still need to provide

Copy from `C:\Users\Campbell\Documents\PyThrust` or `PyThrust_Cursor` into this
environment (zip upload is fine):

1. `pythrust/` package including `data/motors/` and `data/propellers/apc_202602/`
2. `component_data/component_warehouse.sqlite`
3. Optional derived: `component_data/propulsion_performance_cache.sqlite`
4. Optional derived: `optimizer_results/drone_optimizer_integrated/`
5. Exact pip freeze / OpenMDAO + setuav-pythrust versions from the working `.venv`

Place runtime files where v0.7 expects them today:

```text
legacy/streamlit/pythrust/
legacy/streamlit/component_data/component_warehouse.sqlite
legacy/streamlit/component_data/propulsion_performance_cache.sqlite   # optional
legacy/streamlit/optimizer_results/drone_optimizer_integrated/        # optional
```

## Current code map

See [`docs/phase0-code-map.md`](docs/phase0-code-map.md).

## Cloud Agents (parallel work)

Committed environment files live under [`.cursor/`](.cursor/) so multiple Cloud
Agents can start from this GitHub repo at once (isolated VMs and branches).

See [`docs/cloud-agents.md`](docs/cloud-agents.md) for GitHub app setup, how to
launch several agents, and how to attach private warehouse data.

## Migration rule

Treat v0.7 as the numerical reference. Do not rewrite formulas, constraints,
scoring, cache invalidation, or lock behavior. Extract, wrap, and regression-test
before building the new API/UI.
