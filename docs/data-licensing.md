# Data licensing and provenance

## Current policy (owner-directed)

Treat the **full component warehouse** and **raw source datasets** as **PRIVATE**
until a licensing/provenance audit is complete.

Public availability of a source on the web does **not** imply redistribution
rights for a public GitHub repository.

## Layers

| Layer | Location | Git | Purpose |
|---|---|---|---|
| Engineering source (v0.7) | `legacy/streamlit/` | tracked | Reference implementation |
| Private full datasets | `data/private/` (+ runtime paths under `legacy/streamlit/`) | **ignored** | Local/dev/prod optimization |
| Public bootstrap | `data/public/` | tracked only after audit | Portfolio demo / CI smoke |
| Derived caches | propulsion SQLite, physical pool CSV | **ignored** | Speed + regression reference; must remain reproducible |

## Provenance to preserve

The warehouse builder and `source_manifest` / adapter registry already track:

- source name
- URL / origin notes
- eligibility flags
- confidence / checked dates where present

Do not strip these fields during migration. The future API/UI should expose:

- source
- data confidence
- price sourced vs planning estimate
- whether aero/electrical model data are complete

## Before any public publish

Audit at least:

1. PyThrust-bundled motor JSON and APC aerodynamic tables
2. `data_seed/apc_catalog_202602.csv`
3. `data_seed/uav_real_cell_catalog_v1.csv`
4. Any scraped vendor pack/ESC/price rows in the warehouse
5. TUM / lygte / celldb / USU legacy imports used by the builder

Until that audit lands, keep full DBs out of the public remote.
