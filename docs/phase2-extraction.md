# Phase 2 — Extract optimizer package

## What changed

Moved v0.7 numerical logic into [`optimizer/`](../optimizer/) while preserving formulas.

| Before | After |
|---|---|
| `examples/quadrotor_integrated_*.py` | `optimizer/propulsion/integrated.py` |
| `examples/quadrotor_fast_adaptive_*.py` | `optimizer/propulsion/guide.py` |
| `drone_optimizer/db.py` | `optimizer/components/warehouse.py` |
| `drone_optimizer/screening.py` | `optimizer/components/screening.py` |
| `drone_optimizer/quality.py` | `optimizer/components/quality.py` |

Legacy paths are **thin shims** so Streamlit and CLI keep working.

## Path resolution

Engines no longer use `Path(__file__).parents[1]`. They use
`optimizer.paths.DATA_ROOT`, overridable by `DRONE_OPTIMIZER_DATA_ROOT`
(defaults to `legacy/streamlit`).

## Public API

`optimizer.engine.evaluate_fixed_design(...)` — Case A exact calculator path.
`optimizer.engine.rerank_designs(...)` — weight-only rerank.
`optimizer.engine.physical_pool_key(...)` — cache invalidation key.

Batteries / frame / ranking / caching / validation subpackages currently
**re-export** from the integrated module so the target tree exists without
splitting formulas prematurely.
