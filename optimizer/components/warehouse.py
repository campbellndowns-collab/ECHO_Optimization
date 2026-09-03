from __future__ import annotations
from pathlib import Path
import sqlite3
import pandas as pd

TABLES = {
    "motors": "motors_master",
    "props": "props_master",
    "battery_cells": "battery_cells_master",
    "battery_packs": "battery_packs_master",
    "escs": "escs_master",
    "prices": "prices_master",
    "sources": "source_manifest",
}

def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Warehouse not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con

def table_exists(con, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def read_table(db_path: Path, table: str, where: str = "", params=()):
    con = connect(db_path)
    try:
        if not table_exists(con, table):
            return pd.DataFrame()
        sql = f'SELECT * FROM "{table}"'
        if where:
            sql += " WHERE " + where
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()

def canonical_counts(db_path: Path):
    con = connect(db_path)
    rows = []
    try:
        for label, table in TABLES.items():
            if label in {"prices", "sources"} or not table_exists(con, table):
                continue
            total = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            canonical = con.execute(
                f'SELECT COUNT(DISTINCT canonical_id) FROM "{table}" '
                "WHERE canonical_id IS NOT NULL AND canonical_id <> ''"
            ).fetchone()[0]
            eligible = con.execute(
                f'SELECT COUNT(*) FROM "{table}" '
                "WHERE CAST(COALESCE(optimizer_eligible,'0') AS INTEGER)=1"
            ).fetchone()[0]
            rows.append({
                "dataset": label,
                "source_rows": int(total),
                "canonical_parts": int(canonical),
                "optimizer_eligible_rows": int(eligible),
            })
    finally:
        con.close()
    return pd.DataFrame(rows)

def source_counts(db_path: Path):
    con = connect(db_path)
    out = []
    try:
        for label, table in TABLES.items():
            if label in {"prices", "sources"} or not table_exists(con, table):
                continue
            sql = (
                f'SELECT COALESCE(source_name,\'\') AS source_name, '
                'COUNT(*) AS rows, '
                "SUM(CASE WHEN CAST(COALESCE(optimizer_eligible,'0') AS INTEGER)=1 "
                "THEN 1 ELSE 0 END) AS eligible "
                f'FROM "{table}" '
                "GROUP BY COALESCE(source_name,'') ORDER BY rows DESC"
            )
            for r in con.execute(sql):
                out.append({
                    "dataset": label,
                    "source_name": r["source_name"] or "(blank)",
                    "rows": int(r["rows"]),
                    "optimizer_eligible": int(r["eligible"] or 0),
                })
    finally:
        con.close()
    return pd.DataFrame(out)

def component_rows(db_path: Path, dataset: str, optimizer_only: bool = False):
    table = TABLES[dataset]
    if optimizer_only:
        return read_table(
            db_path, table,
            "CAST(COALESCE(optimizer_eligible,'0') AS INTEGER)=1"
        )
    return read_table(db_path, table)


def warehouse_modified_iso(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(
        db_path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="seconds")


def _numeric_complete(df: pd.DataFrame, fields: list[str]) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(True, index=df.index)
    for field in fields:
        if field not in df.columns:
            return pd.Series(False, index=df.index)
        vals = pd.to_numeric(df[field], errors="coerce")
        mask &= vals.notna() & (vals > 0)
    return mask


def searchable_counts(db_path: Path) -> pd.DataFrame:
    """
    Count rows that contain enough engineering information to enter the actual
    search pipeline. This is deliberately different from raw/canonical count.

    A record may be valuable for discovery but still be unusable in physics.
    """
    rows = []

    # Motors: PyThrust-style electrical model inputs.
    motors = component_rows(db_path, "motors")
    if not motors.empty:
        mask = _numeric_complete(
            motors,
            ["kv_rpm_per_v", "resistance_ohm", "max_current_a", "mass_g"],
        )
        if "no_load_current_a" in motors.columns:
            io = pd.to_numeric(motors["no_load_current_a"], errors="coerce")
            mask &= io.notna() & (io >= 0)
        rows.append({
            "dataset": "motors",
            "searchable_rows": int(mask.sum()),
            "definition": "Kv + resistance + no-load current + current limit + mass",
        })

    # Props: aerodynamic coefficient data and physical size/mass.
    props = component_rows(db_path, "props")
    if not props.empty:
        mask = _numeric_complete(
            props, ["diameter_in", "pitch_in", "mass_g"]
        )
        if "aero_data_available" in props.columns:
            aero = pd.to_numeric(
                props["aero_data_available"], errors="coerce"
            ).fillna(0)
            mask &= aero >= 1
        rows.append({
            "dataset": "props",
            "searchable_rows": int(mask.sum()),
            "definition": "Physical data + aerodynamic Ct/Cp dataset",
        })

    # Cells: enough to generate a real S/P pack.
    cells = component_rows(db_path, "battery_cells")
    if not cells.empty:
        mask = _numeric_complete(
            cells,
            [
                "nominal_voltage_v",
                "capacity_ah",
                "max_cont_discharge_a",
                "mass_g",
            ],
        )
        rows.append({
            "dataset": "battery_cells",
            "searchable_rows": int(mask.sum()),
            "definition": "Voltage + capacity + continuous current + mass",
        })

    # Commercial packs: directly usable pack electrical + mass model.
    packs = component_rows(db_path, "battery_packs")
    if not packs.empty:
        base = _numeric_complete(
            packs,
            ["series_cells", "voltage_nominal_v", "capacity_ah", "mass_g"],
        )
        if "max_cont_current_a" in packs.columns:
            direct = pd.to_numeric(
                packs["max_cont_current_a"], errors="coerce"
            )
        else:
            direct = pd.Series(float("nan"), index=packs.index)
        if "c_rating_cont" in packs.columns:
            c_rate = pd.to_numeric(
                packs["c_rating_cont"], errors="coerce"
            )
        else:
            c_rate = pd.Series(float("nan"), index=packs.index)
        base &= ((direct > 0) | (c_rate > 0))
        rows.append({
            "dataset": "battery_packs",
            "searchable_rows": int(base.sum()),
            "definition": "S count + voltage + capacity + mass + current/C rating",
        })

    # ESCs: voltage compatibility, continuous current, mass.
    escs = component_rows(db_path, "escs")
    if not escs.empty:
        mask = _numeric_complete(
            escs, ["continuous_current_a", "max_cells", "mass_g"]
        )
        rows.append({
            "dataset": "escs",
            "searchable_rows": int(mask.sum()),
            "definition": "Continuous current + max cell count + mass",
        })

    return pd.DataFrame(rows)


def warehouse_snapshot(db_path: Path) -> dict:
    counts = canonical_counts(db_path)
    searchable = searchable_counts(db_path)
    merged = counts.merge(searchable, on="dataset", how="left")
    records = merged.fillna("").to_dict("records")
    return {
        "warehouse_path": str(db_path),
        "modified_utc": warehouse_modified_iso(db_path),
        "datasets": records,
    }


def pricing_coverage_counts(db_path: Path) -> pd.DataFrame:
    """Count canonical component records with at least one positive known price."""
    con = connect(db_path)
    try:
        price_rows = (
            pd.read_sql_query('SELECT * FROM "prices_master"', con)
            if table_exists(con, "prices_master")
            else pd.DataFrame()
        )
    finally:
        con.close()

    priced_canonical = set()
    if not price_rows.empty and "canonical_id" in price_rows.columns:
        p = pd.to_numeric(price_rows.get("price_usd"), errors="coerce")
        for cid in price_rows.loc[p > 0, "canonical_id"].fillna("").astype(str):
            if cid:
                priced_canonical.add(cid)

    specs = {
        "motors": ("price_usd",),
        "props": ("price_usd",),
        "battery_cells": ("price_usd_each",),
        "battery_packs": ("price_usd",),
        "escs": ("price_usd",),
    }
    rows = []
    for dataset, price_fields in specs.items():
        df = component_rows(db_path, dataset)
        if df.empty:
            rows.append({
                "dataset": dataset,
                "canonical_parts": 0,
                "priced_canonical_parts": 0,
                "price_coverage_pct": 0.0,
            })
            continue

        canonical = (
            df["canonical_id"].fillna("").astype(str)
            if "canonical_id" in df.columns
            else pd.Series("", index=df.index)
        )
        direct_mask = pd.Series(False, index=df.index)
        for field in price_fields:
            if field in df.columns:
                direct_mask |= pd.to_numeric(
                    df[field], errors="coerce"
                ).fillna(0) > 0

        external_mask = canonical.isin(priced_canonical)
        priced_ids = set(canonical[direct_mask | external_mask])
        priced_ids.discard("")
        all_ids = set(canonical)
        all_ids.discard("")

        total = len(all_ids)
        priced = len(priced_ids)
        rows.append({
            "dataset": dataset,
            "canonical_parts": total,
            "priced_canonical_parts": priced,
            "price_coverage_pct": round(
                100.0 * priced / total, 1
            ) if total else 0.0,
        })

    return pd.DataFrame(rows)
