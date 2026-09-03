from __future__ import annotations
from datetime import date, datetime
import pandas as pd

REQUIRED_FIELDS = {
    "motors": ["manufacturer","model","kv_rpm_per_v","resistance_ohm","max_current_a","mass_g"],
    "props": ["manufacturer","model","diameter_in","pitch_in","mass_g","aero_data_available"],
    "battery_cells": ["manufacturer","model","nominal_voltage_v","capacity_ah","max_cont_discharge_a","mass_g"],
    "battery_packs": ["manufacturer","model","series_cells","voltage_nominal_v","capacity_ah","mass_g"],
    "escs": ["manufacturer","model","continuous_current_a","max_cells","mass_g"],
}

def _present(v):
    if v is None:
        return False
    s = str(v).strip().lower()
    return bool(s and s not in {"none","nan","n/a","na"})

def _freshness(v):
    if not _present(v):
        return 0.25
    try:
        d = datetime.fromisoformat(str(v)[:10]).date()
        age = (date.today() - d).days
        return 1.0 if age <= 180 else 0.8 if age <= 365 else 0.55 if age <= 730 else 0.25
    except Exception:
        return 0.25

def score_record(row, dataset):
    required = REQUIRED_FIELDS.get(dataset, [])
    completeness = sum(_present(row.get(f)) for f in required) / len(required) if required else 0.0
    tier = str(row.get("data_quality_tier") or "").upper()
    source_score = 1.0 if tier == "A" else 0.78 if tier == "B" else 0.42
    license_score = 1.0 if str(row.get("license_status") or "").lower() == "known" else 0.55
    freshness = _freshness(row.get("checked_date"))
    score = 0.35*source_score + 0.40*completeness + 0.15*freshness + 0.10*license_score
    try:
        if int(float(row.get("optimizer_eligible") or 0)) == 1:
            score = max(score, 0.82)
    except Exception:
        pass
    band = "Ready" if score >= 0.85 else "Screening" if score >= 0.68 else "Discovery"
    return {
        "confidence_score": round(score, 3),
        "confidence_band": band,
        "field_completeness": round(completeness, 3),
    }

def add_scores(df: pd.DataFrame, dataset: str):
    if df.empty:
        return df.copy()
    rows = []
    for r in df.to_dict("records"):
        item = dict(r)
        item.update(score_record(item, dataset))
        rows.append(item)
    return pd.DataFrame(rows)

def readiness_summary(df: pd.DataFrame, dataset: str):
    if df.empty:
        return {"Ready":0,"Screening":0,"Discovery":0}
    s = add_scores(df, dataset)
    counts = s["confidence_band"].value_counts().to_dict()
    return {k:int(counts.get(k,0)) for k in ("Ready","Screening","Discovery")}
