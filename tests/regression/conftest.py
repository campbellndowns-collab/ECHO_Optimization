"""Helpers for loading the extracted optimizer package (v0.7 numerical reference)."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = REPO_ROOT / "legacy" / "streamlit"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def require_runtime_data() -> None:
    warehouse = LEGACY_ROOT / "component_data" / "component_warehouse.sqlite"
    pythrust = LEGACY_ROOT / "pythrust" / "__init__.py"
    motors = LEGACY_ROOT / "pythrust" / "data" / "motors"
    apc = LEGACY_ROOT / "pythrust" / "data" / "propellers" / "apc_202602"
    missing = [
        str(p)
        for p in (warehouse, pythrust, motors, apc)
        if not p.exists()
    ]
    if missing:
        raise RuntimeError(
            "Private v0.7 runtime data missing (expected under legacy/streamlit/). "
            f"Missing: {missing}"
        )


@lru_cache(maxsize=1)
def load_integrated():
    require_runtime_data()
    from optimizer.propulsion import integrated as mod

    return mod


def nearly(a, b, rel=1e-6, abs_tol=1e-8) -> bool:
    if a is None or b is None:
        return a is b
    a = float(a)
    b = float(b)
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b), 1.0))
