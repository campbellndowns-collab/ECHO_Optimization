"""Runtime data-root resolution for the numerical engine.

During Phase 2 the authoritative private datasets still live beside the
Streamlit v0.7 overlay. Formulas are unchanged; only path resolution is
centralized so engines no longer assume ``Path(__file__).parents[1]``.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "legacy" / "streamlit"


def get_data_root() -> Path:
    override = os.environ.get("DRONE_OPTIMIZER_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_DATA_ROOT.resolve()


DATA_ROOT = get_data_root()

# Ensure local PyThrust package (bundled under the data root) is importable
# when the editable install is not active.
_pythrust_parent = str(DATA_ROOT)
if _pythrust_parent not in __import__("sys").path:
    __import__("sys").path.insert(0, _pythrust_parent)
