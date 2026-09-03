# Private runtime data (not redistributable by default)

Place the full local engineering datasets here. These files are **gitignored**
and must not be published until source licensing and provenance are audited.

## Expected contents

Copy or symlink from your local PyThrust install
(`C:\Users\Campbell\Documents\PyThrust` or `PyThrust_Cursor`):

```text
data/private/
  component_warehouse.sqlite          # full component warehouse
  propulsion_performance_cache.sqlite # derived propulsion curve cache
  pythrust/                           # local package + motor/prop aero data
    data/
      motors/
      propellers/apc_202602/
  optimizer_results/
    drone_optimizer_integrated/       # optional regression reference pool
```

## How the v0.7 overlay finds data today

The Streamlit v0.7 code resolves paths from `legacy/streamlit/` as `PROJECT_ROOT`:

| Runtime path | Role |
|---|---|
| `legacy/streamlit/component_data/component_warehouse.sqlite` | Warehouse |
| `legacy/streamlit/component_data/propulsion_performance_cache.sqlite` | Curve cache |
| `legacy/streamlit/pythrust/` | PyThrust package + motor/APC aero DBs |
| `legacy/streamlit/optimizer_results/drone_optimizer_integrated/` | Results / physical pool |

For local regression against v0.7 **without changing path logic**, either:

1. Copy/symlink the private files into those `legacy/streamlit/...` locations, or
2. Later (Phase 2+) make paths configurable via env vars while preserving defaults.

## Policy

- Full warehouse and raw source datasets: **private**
- Derived caches: usable as regression artifacts, but **reproducible** — not authoritative engineering source
- Public GitHub: only a **sanitized bootstrap** under `data/public/` after licensing audit
- Provenance must remain traceable (`source_manifest`, adapter registry, warehouse builder notes)
