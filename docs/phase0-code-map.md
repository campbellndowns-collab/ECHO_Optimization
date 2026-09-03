# Phase 0 — Real code map (v0.7 overlay)

Inspected from uploaded zip:
`Drone_Optimizer_v0_7_Fixed_Mixed_Optimization.zip`

This zip is a **software overlay** meant to sit on top of an existing local
PyThrust install. It does **not** include `pythrust/`, the SQLite warehouse,
propulsion cache, or `optimizer_results/`.

## Layout in this repo

```text
legacy/streamlit/          # v0.7 overlay (PROJECT_ROOT for current scripts)
  drone_optimizer/         # Streamlit UI + helpers
  examples/                # numerical engines
  tools/                   # warehouse builder
  data_seed/               # seed CSVs shipped with the overlay
  component_data/          # RUNTIME private DBs (gitignored)
  optimizer_results/       # RUNTIME private results (gitignored)
  pythrust/                # RUNTIME private package+data (gitignored; still missing)
```

## File map

| Concern | File(s) |
|---|---|
| **UI (Streamlit)** | [`drone_optimizer/app.py`](../legacy/streamlit/drone_optimizer/app.py) |
| **Launcher** | [`drone_optimizer/launcher.py`](../legacy/streamlit/drone_optimizer/launcher.py), Windows `.bat` helpers |
| **Job shell-out** | [`drone_optimizer/jobs.py`](../legacy/streamlit/drone_optimizer/jobs.py) — builds CLI for integrated optimizer |
| **Config paths** | [`drone_optimizer/config.json`](../legacy/streamlit/drone_optimizer/config.json) |
| **Component warehouse access** | [`drone_optimizer/db.py`](../legacy/streamlit/drone_optimizer/db.py) |
| **Searchable / evaluable filters** | `db.searchable_counts`, UI selectors in `app.py` |
| **Prescreen** | [`drone_optimizer/screening.py`](../legacy/streamlit/drone_optimizer/screening.py) |
| **Record quality scores** | [`drone_optimizer/quality.py`](../legacy/streamlit/drone_optimizer/quality.py) |
| **Diagnostics** | [`drone_optimizer/diagnostics.py`](../legacy/streamlit/drone_optimizer/diagnostics.py) |
| **Source adapter stubs** | [`drone_optimizer/source_adapters/`](../legacy/streamlit/drone_optimizer/source_adapters/) |
| **Warehouse builder** | [`tools/build_component_data_warehouse.py`](../legacy/streamlit/tools/build_component_data_warehouse.py) |
| **Primary numerical engine (v0.7)** | [`examples/quadrotor_integrated_real_component_optimizer.py`](../legacy/streamlit/examples/quadrotor_integrated_real_component_optimizer.py) |
| **Guide / legacy propulsion search** | [`examples/quadrotor_fast_adaptive_optimization.py`](../legacy/streamlit/examples/quadrotor_fast_adaptive_optimization.py) |
| **Battery logic** | integrated optimizer: `BatteryCandidate`, `build_custom_battery_candidates`, `build_locked_commercial_battery`, `build_commercial_battery_candidates`, `rank_batteries` |
| **Frame logic** | integrated optimizer: `estimate_frame_mass`, `_select_arm_tube`, `FRAME_*` constants |
| **Propulsion + OpenMDAO** | both example scripts: `build_problem` / `build_real_problem`, `RealAircraftPerfComp`, `solve_hover_throttle`, curve build |
| **Caching** | `PropulsionCurveCache`, `physical_pool_key`, `load_physical_pool`, `save_physical_pool` |
| **Weighted ranking** | `add_decision_matrix`, Streamlit `rerank_existing_designs` (weight-only) |
| **Component locks** | CLI flags in integrated `main()` + `jobs.run_detailed_optimizer` + UI mode selectors |
| **Cost / fallbacks** | `FALLBACK_PROPULSION_COST_USD`, `FRAME_PLANNING_COST_USD`, `CAMERA_AVIONICS_PLANNING_COST_USD`, `PriceResolver` |

## Coupling problems (confirmed from source)

1. **Streamlit UI shells out to a CLI script** (`jobs.run_detailed_optimizer` → `examples/quadrotor_integrated_...py`) instead of calling a pure Python API.
2. **Hard-coded `PROJECT_ROOT`** relative to `examples/` / `drone_optimizer/` — assumes PyThrust package and warehouse live beside the app.
3. **Two engines**: fast adaptive guide + integrated real-component optimizer; integrated loads the guide module dynamically (`load_legacy_module`).
4. **Ranking exists in two places**: engine `add_decision_matrix` and UI `rerank_existing_designs` (good intent; must stay numerically identical when extracted).
5. **Global mutation**: `REJECTION_COUNTS`, `EXCEPTION_DETAILS`, `_WAREHOUSE_CACHE`, OpenMDAO workdir env vars — unsafe for multi-user workers without isolation.
6. **Dict/CSV-centric results** rather than typed models at the boundary.

## Runtime data status (ingested from PyThrust_Cursor.zip)

Present locally (gitignored):

| Artifact | Path |
|---|---|
| PyThrust 0.2.2 + motors + APC aero | `legacy/streamlit/pythrust/` |
| Full warehouse | `legacy/streamlit/component_data/component_warehouse.sqlite` |
| Propulsion cache (derived) | `legacy/streamlit/component_data/propulsion_performance_cache.sqlite` |
| Physical design pool (derived) | `legacy/streamlit/optimizer_results/drone_optimizer_integrated/` |

Also mirrored under `data/private/`.

## Dependencies

Overlay UI:

```text
streamlit>=1.37,<2
pandas>=2.0
openpyxl>=3.1,<4
```

Numerical stack (Linux baseline used for Phase 1):

- `setuav-pythrust` / local package **0.2.2**
- OpenMDAO **3.45.0**
- See `docs/reference-linux-pip-freeze.txt`

## Warehouse note for commercial-pack locks

In the current warehouse, commercial packs with series/voltage/current often lack
`mass_g`, so `build_locked_commercial_battery()` returns `None`. Case A regression
therefore locks the known custom cell pack topology (`Molicel M65A 6S4P`) via the
exact evaluator — matching the reference top design physically.
