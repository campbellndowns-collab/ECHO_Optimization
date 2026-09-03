from __future__ import annotations
import pandas as pd
from .quality import add_scores

def _num(v, default=None):
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(v)
    except Exception:
        return default

def enumerate_custom_packs(
    cells: pd.DataFrame,
    series_min: int = 4,
    series_max: int = 12,
    parallel_max: int = 20,
    overhead_fraction: float = 0.10,
    max_pack_mass_kg: float = 8.0,
):
    if cells.empty:
        return pd.DataFrame()
    rows = []
    for c in cells.to_dict("records"):
        vn = _num(c.get("nominal_voltage_v"))
        ah = _num(c.get("capacity_ah"))
        imax = _num(c.get("max_cont_discharge_a"))
        mass_g = _num(c.get("mass_g"))
        if not all(x is not None and x > 0 for x in (vn, ah, imax, mass_g)):
            continue
        for s in range(series_min, series_max + 1):
            for p in range(1, parallel_max + 1):
                count = s * p
                pack_mass = count * mass_g / 1000.0 * (1.0 + overhead_fraction)
                if pack_mass > max_pack_mass_kg:
                    break
                voltage = s * vn
                capacity = p * ah
                energy = voltage * capacity
                rows.append({
                    "manufacturer": c.get("manufacturer"),
                    "cell_model": c.get("model"),
                    "topology": f"{s}S{p}P",
                    "series_cells": s,
                    "parallel_cells": p,
                    "cell_count": count,
                    "voltage_nominal_v": voltage,
                    "capacity_ah": capacity,
                    "energy_wh": energy,
                    "max_cont_current_a": p * imax,
                    "pack_mass_kg": pack_mass,
                    "cell_confidence": c.get("confidence_score"),
                })
    return pd.DataFrame(rows)

def fast_component_prescreen(motors, props, cells, packs, escs, constraints):
    min_prop = float(constraints["min_prop_in"])
    max_prop = float(constraints["max_prop_in"])
    max_auw = float(constraints["max_auw_kg"])
    fixed_mass = float(constraints["fixed_mass_kg"])
    overhead = float(constraints["pack_mass_overhead_fraction"])

    m = add_scores(motors, "motors") if not motors.empty else motors.copy()
    p = add_scores(props, "props") if not props.empty else props.copy()
    c = add_scores(cells, "battery_cells") if not cells.empty else cells.copy()
    bp = add_scores(packs, "battery_packs") if not packs.empty else packs.copy()
    e = add_scores(escs, "escs") if not escs.empty else escs.copy()

    if not m.empty:
        m["kv_num"] = pd.to_numeric(m["kv_rpm_per_v"], errors="coerce")
        m["mass_g_num"] = pd.to_numeric(m["mass_g"], errors="coerce")
        m = m[m["kv_num"].between(80, 1200) & m["mass_g_num"].between(20, 1200)]

    if not p.empty:
        p["diameter_num"] = pd.to_numeric(p["diameter_in"], errors="coerce")
        p["pitch_num"] = pd.to_numeric(p["pitch_in"], errors="coerce")
        p["aero_num"] = pd.to_numeric(p["aero_data_available"], errors="coerce").fillna(0)
        p = p[p["diameter_num"].between(min_prop, max_prop) & (p["aero_num"] >= 1)]
        p = p[
            p["pitch_num"].isna()
            | p["diameter_num"].isna()
            | ((p["pitch_num"] / p["diameter_num"]) <= 0.85)
        ]

    if not c.empty:
        c = c[c["confidence_band"].isin(["Ready", "Screening"])]
        custom = enumerate_custom_packs(
            c,
            overhead_fraction=overhead,
            max_pack_mass_kg=max(0.5, max_auw - fixed_mass),
        )
    else:
        custom = pd.DataFrame()

    if not bp.empty:
        bp["mass_kg"] = pd.to_numeric(bp["mass_g"], errors="coerce") / 1000.0
        bp = bp[
            bp["confidence_band"].isin(["Ready", "Screening"])
            & (bp["mass_kg"] <= max(0.5, max_auw - fixed_mass))
        ]

    if not e.empty:
        e["continuous_current_num"] = pd.to_numeric(e["continuous_current_a"], errors="coerce")
        e = e[
            e["confidence_band"].isin(["Ready", "Screening"])
            & e["continuous_current_num"].notna()
        ]

    if not m.empty:
        m = m.sort_values(["confidence_score", "mass_g_num"], ascending=[False, True]).head(250)
    if not p.empty:
        p = p.sort_values(["confidence_score", "diameter_num"], ascending=[False, False]).head(120)
    if not custom.empty:
        custom["specific_energy_whkg"] = custom["energy_wh"] / custom["pack_mass_kg"]
        custom = custom.sort_values(["specific_energy_whkg", "pack_mass_kg"], ascending=[False, True]).head(300)
    if not bp.empty:
        bp = bp.sort_values(["confidence_score", "mass_kg"], ascending=[False, True]).head(200)
    if not e.empty:
        e = e.sort_values(["confidence_score", "continuous_current_num"], ascending=[False, False]).head(160)

    return {
        "motors": m,
        "props": p,
        "cells": c,
        "custom_packs": custom,
        "commercial_packs": bp,
        "escs": e,
    }
