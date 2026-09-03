"""Component catalog endpoints for searchable selectors."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel

from optimizer.components import warehouse
from optimizer.paths import get_data_root

router = APIRouter(prefix="/catalog", tags=["catalog"])


class CatalogItem(BaseModel):
    id: str
    label: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    mass_g: Optional[float] = None
    extra: dict[str, Any] = {}


class CatalogResponse(BaseModel):
    items: list[CatalogItem]
    total: int


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


@router.get("/motors", response_model=CatalogResponse)
def catalog_motors(
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
):
    """Motors that exist in the local PyThrust physics database."""
    root = get_data_root()
    motor_dir = root / "pythrust" / "data" / "motors"
    items: list[CatalogItem] = []
    if motor_dir.exists():
        from pythrust.motors import MotorDatabase

        db = MotorDatabase()
        db.load(motor_dir)
        motors = db.search(
            min_kv=1,
            max_kv=100000,
            min_max_current=0,
            min_weight=0,
            max_weight=100000,
        )
        ql = q.lower().strip()
        for m in motors:
            label = f"{m.manufacturer} {m.name} | {float(m.kv):.0f} Kv | {float(m.weight_g):.0f} g"
            hay = f"{m.id} {m.manufacturer} {m.name}".lower()
            if ql and ql not in hay:
                continue
            items.append(
                CatalogItem(
                    id=m.id,
                    label=label,
                    manufacturer=m.manufacturer,
                    model=m.name,
                    mass_g=float(m.weight_g),
                    extra={
                        "kv": float(m.kv),
                        "max_current_a": float(m.max_current),
                    },
                )
            )
    return CatalogResponse(items=items[:limit], total=len(items))


@router.get("/propellers", response_model=CatalogResponse)
def catalog_props(q: str = "", limit: int = Query(50, ge=1, le=200)):
    root = get_data_root()
    prop_dir = root / "pythrust" / "data" / "propellers" / "apc_202602"
    items: list[CatalogItem] = []
    if prop_dir.exists():
        from pythrust.propellers import PropellerDatabase

        db = PropellerDatabase()
        db.load(prop_dir, strict=False)
        ql = q.lower().strip()
        for pid in db.list_propellers():
            entry = db.get(pid)
            model = entry.metadata.model
            diam = float(entry.metadata.diameter_in)
            label = f"{model} | {diam:.2f} in"
            hay = f"{pid} {model}".lower()
            if ql and ql not in hay:
                continue
            items.append(
                CatalogItem(
                    id=pid,
                    label=label,
                    manufacturer="APC",
                    model=model,
                    extra={"diameter_in": diam},
                )
            )
    return CatalogResponse(items=items[:limit], total=len(items))


@router.get("/battery-cells", response_model=CatalogResponse)
def catalog_cells(q: str = "", limit: int = Query(50, ge=1, le=200)):
    db = get_data_root() / "component_data" / "component_warehouse.sqlite"
    df = warehouse.component_rows(db, "battery_cells")
    if df.empty:
        return CatalogResponse(items=[], total=0)
    mask = warehouse._numeric_complete(
        df,
        [
            "nominal_voltage_v",
            "capacity_ah",
            "max_cont_discharge_a",
            "mass_g",
        ],
    )
    df = df.loc[mask].copy()
    ql = q.lower().strip()
    items: list[CatalogItem] = []
    for _, r in df.iterrows():
        cid = str(r.get("canonical_id") or "")
        if not cid:
            continue
        mfr = str(r.get("manufacturer") or "")
        model = str(r.get("model") or "")
        hay = f"{cid} {mfr} {model}".lower()
        if ql and ql not in hay:
            continue
        mass = _num(pd.Series([r.get("mass_g")])).iloc[0]
        items.append(
            CatalogItem(
                id=cid,
                label=f"{mfr} {model}",
                manufacturer=mfr,
                model=model,
                mass_g=float(mass) if pd.notna(mass) else None,
                extra={
                    "capacity_ah": float(_num(pd.Series([r.get("capacity_ah")])).iloc[0])
                    if pd.notna(_num(pd.Series([r.get("capacity_ah")])).iloc[0])
                    else None,
                },
            )
        )
    return CatalogResponse(items=items[:limit], total=len(items))


@router.get("/escs", response_model=CatalogResponse)
def catalog_escs(q: str = "", limit: int = Query(50, ge=1, le=200)):
    db = get_data_root() / "component_data" / "component_warehouse.sqlite"
    df = warehouse.component_rows(db, "escs")
    if df.empty:
        return CatalogResponse(items=[], total=0)
    mask = warehouse._numeric_complete(
        df, ["continuous_current_a", "max_cells", "mass_g"]
    )
    df = df.loc[mask].copy()
    ql = q.lower().strip()
    items: list[CatalogItem] = []
    for _, r in df.iterrows():
        cid = str(r.get("canonical_id") or "")
        if not cid:
            continue
        mfr = str(r.get("manufacturer") or "")
        model = str(r.get("model") or "")
        hay = f"{cid} {mfr} {model}".lower()
        if ql and ql not in hay:
            continue
        cont = _num(pd.Series([r.get("continuous_current_a")])).iloc[0]
        mass = _num(pd.Series([r.get("mass_g")])).iloc[0]
        items.append(
            CatalogItem(
                id=cid,
                label=f"{mfr} {model} | {cont:.0f} A" if pd.notna(cont) else f"{mfr} {model}",
                manufacturer=mfr,
                model=model,
                mass_g=float(mass) if pd.notna(mass) else None,
                extra={
                    "continuous_current_a": float(cont) if pd.notna(cont) else None,
                },
            )
        )
    return CatalogResponse(items=items[:limit], total=len(items))
