from __future__ import annotations
from pathlib import Path
import importlib.util
import sqlite3

def run_diagnostics(project_root: Path, cfg: dict):
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

    warehouse = project_root / cfg["warehouse_path"]
    builder = project_root / cfg["builder_path"]
    detailed = project_root / cfg["detailed_optimizer_path"]
    legacy = project_root / cfg.get("legacy_optimizer_path", "")
    apc_candidates = [
        project_root / "apc_catalog_202602.csv",
        project_root / "pythrust" / "data" / "propellers" / "apc_catalog_202602.csv",
        project_root / "data_seed" / "apc_catalog_202602.csv",
    ]

    add("Component warehouse", warehouse.exists(), warehouse)
    add("Warehouse builder", builder.exists(), builder)
    add("Integrated optimizer", detailed.exists(), detailed)
    add("Bundled fast adaptive optimizer", legacy.exists(), legacy)
    add("APC physical catalog", any(p.exists() for p in apc_candidates),
        next((p for p in apc_candidates if p.exists()), apc_candidates[-1]))
    add("PyThrust package folder", (project_root / "pythrust").exists(), project_root / "pythrust")
    add("Motor database", (project_root / "pythrust" / "data" / "motors").exists(),
        project_root / "pythrust" / "data" / "motors")
    add("APC aero database", (project_root / "pythrust" / "data" / "propellers" / "apc_202602").exists(),
        project_root / "pythrust" / "data" / "propellers" / "apc_202602")

    for module in ("openmdao", "numpy", "pandas", "streamlit"):
        add(f"Python module: {module}", importlib.util.find_spec(module) is not None, module)

    if warehouse.exists():
        try:
            con = sqlite3.connect(str(warehouse))
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            con.close()
            required = {
                "motors_master", "props_master", "battery_cells_master",
                "battery_packs_master", "escs_master"
            }
            missing = sorted(required - tables)
            add("Warehouse schema", not missing,
                "All required tables present" if not missing else f"Missing: {', '.join(missing)}")
        except Exception as exc:
            add("Warehouse schema", False, exc)

    return checks
