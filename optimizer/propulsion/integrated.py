"""
Integrated real-component quadrotor optimizer.

Pipeline
--------
1. Reuse the fast adaptive real-motor + real-APC-prop optimizer as a propulsion
   guide. If its results are missing, run it automatically.
2. Read the component warehouse.
3. Generate/select real battery candidates:
   - custom packs from real cells
   - commercial battery packs
4. Select a compatible real ESC for each finalist.
5. Re-run PyThrust physics with FIXED real battery mass/energy/voltage and real
   ESC mass.
6. Physically size a concept carbon-fiber frame.
7. Rank all valid aircraft with an auditable weighted decision matrix.

This script is intentionally conservative:
* Custom battery packs use a configurable mass-overhead factor.
* Battery full-throttle current must stay within the published continuous limit
  unless a burst limit is explicitly available.
* ESC sizing is checked against actual full-throttle motor current after solve,
  with a configurable margin.
* Propeller performance is rejected when RPM is too far outside the source
  dataset range.
* Cost is a weighted decision criterion rather than a hard ceiling by default.
* Missing propulsion prices use explicit, labeled planning estimates rather
  than being treated as zero.

It still does NOT model:
* detailed battery voltage sag / OCV-vs-SOC curves
* temperature effects
* final center-joint/laminate structural FEA (arm tubes are concept-sized from
  bending stress and deflection)
* propeller structural RPM limits where manufacturer data are absent

Those are refinements, not database/application architecture rewrites.
"""

from __future__ import annotations

import argparse
import hashlib
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Optional

os.environ["OPENMDAO_REPORTS"] = "0"

import numpy as np
import openmdao.api as om

from optimizer.paths import DATA_ROOT as PROJECT_ROOT

# Guide engine lives in-package; path retained only for diagnostics/messages.
LEGACY_OPTIMIZER = Path(__file__).resolve().parent / "guide.py"
WAREHOUSE = PROJECT_ROOT / "component_data" / "component_warehouse.sqlite"
PROPULSION_CACHE_DB = (
    PROJECT_ROOT / "component_data" / "propulsion_performance_cache.sqlite"
)
PROPULSION_CURVE_SCHEMA_VERSION = 2
PROPULSION_CURVE_THROTTLES = np.linspace(0.02, 1.00, 33)
# Per-job isolation: worker processes set DRONE_OPTIMIZER_JOB_RESULTS.
RESULTS_DIR = Path(
    os.environ.get("DRONE_OPTIMIZER_JOB_RESULTS")
    or (PROJECT_ROOT / "optimizer_results" / "drone_optimizer_integrated")
)
PHYSICAL_POOL_CSV = RESULTS_DIR / "physical_design_pool.csv"
PHYSICAL_POOL_META = RESULTS_DIR / "physical_design_pool_metadata.json"
TEMP_DIR = RESULTS_DIR / "_openmdao_temp"
os.environ["OPENMDAO_WORKDIR"] = str(TEMP_DIR)

N_ROTORS = 4
G = 9.80665
ACCESSORY_POWER_W = 15.0

DEFAULT_BATTERY_USABLE_FRACTION_LIION = 0.82
DEFAULT_BATTERY_USABLE_FRACTION_LIPO = 0.80
DEFAULT_PACK_OVERHEAD_FRACTION = 0.10
DEFAULT_ESC_CURRENT_MARGIN = 1.15
DEFAULT_RPM_DATA_TOLERANCE = 0.10

# Concept-stage physical frame sizing defaults.
# Standard frame concept: carbon-fiber tube arms + carbon-fiber center/tray plates.
FRAME_MATERIAL_NAME = "Carbon fiber"
FRAME_CF_DENSITY_KG_M3 = 1600.0
FRAME_CF_EFFECTIVE_E_PA = 60.0e9
FRAME_CF_ALLOWABLE_BENDING_PA = 300.0e6
FRAME_MAX_ARM_DEFLECTION_RATIO = 0.010  # tip deflection <= 1% of arm length
DEFAULT_FRAME_PROP_CLEARANCE_M = 0.050
DEFAULT_FRAME_STRUCTURAL_MARGIN = 1.25
FRAME_CENTER_PLATE_THICKNESS_M = 0.0020
FRAME_BATTERY_TRAY_THICKNESS_M = 0.0015
FRAME_ASSUMED_PACK_BULK_DENSITY_KG_M3 = 900.0
FRAME_ASSUMED_PACK_THICKNESS_M = 0.050
FRAME_CENTER_MARGIN_M = 0.040
FRAME_HARDWARE_BASE_MASS_KG = 0.090
FRAME_LANDING_SUPPORT_MASS_KG = 0.050

# Discrete commercially plausible tube geometries (OD mm, wall mm).
FRAME_TUBE_CANDIDATES_MM = [
    (12, 1.0), (14, 1.0), (16, 1.0), (16, 1.5),
    (18, 1.0), (18, 1.5), (20, 1.0), (20, 1.5),
    (22, 1.0), (22, 1.5), (22, 2.0), (25, 1.0),
    (25, 1.5), (25, 2.0), (28, 1.5), (28, 2.0),
    (30, 1.5), (30, 2.0),
]

# Planning costs shared by every candidate aircraft.
FRAME_PLANNING_COST_USD = 220.0
CAMERA_AVIONICS_PLANNING_COST_USD = 700.0
FIXED_PLANNING_COST_USD = (
    FRAME_PLANNING_COST_USD + CAMERA_AVIONICS_PLANNING_COST_USD
)

# Fallback propulsion prices used only when a warehouse price is unavailable.
# These are explicitly marked estimated in the decision matrix.
FALLBACK_PROPULSION_COST_USD = {
    "motors": 180.0,   # four motors total
    "props": 70.0,     # four props total
    "battery": 250.0,
    "escs": 160.0,     # four ESCs total
}


MAX_BATTERY_CHOICES_PER_PARAMETRIC_RESULT = 5
MAX_REAL_SOLVES = 140
TOP_RESULTS_TO_SAVE = 100

SEARCH_PROFILES = {
    "standard": {
        "motor_candidates": 18,
        "prop_candidates": 10,
        "finalist_configs": 24,
        "finalists_per_motor": 4,
        "finalists_per_prop": 8,
        "guide_results": 25,
        "battery_choices_per_guide": 5,
        "max_real_solves": 140,
    },
    "thorough": {
        "motor_candidates": 36,
        "prop_candidates": 18,
        "finalist_configs": 60,
        "finalists_per_motor": 6,
        "finalists_per_prop": 12,
        "guide_results": 60,
        "battery_choices_per_guide": 10,
        "max_real_solves": 500,
    },
    "deep": {
        "motor_candidates": 64,
        "prop_candidates": 28,
        "finalist_configs": 110,
        "finalists_per_motor": 8,
        "finalists_per_prop": 18,
        "guide_results": 110,
        "battery_choices_per_guide": 16,
        "max_real_solves": 1000,
    },
}

_WAREHOUSE_CACHE = {}
REJECTION_COUNTS = {}
EXCEPTION_DETAILS = {}


def cleanup():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def reject(reason, detail=None):
    REJECTION_COUNTS[reason] = REJECTION_COUNTS.get(reason, 0) + 1
    if detail is not None:
        msg = (
            f"{type(detail).__name__}: {detail}"
            if isinstance(detail, BaseException)
            else str(detail)
        )
        EXCEPTION_DETAILS[msg] = EXCEPTION_DETAILS.get(msg, 0) + 1
    return None


@dataclass(frozen=True)
class BatteryCandidate:
    battery_id: str
    battery_type: str
    manufacturer: str
    model: str
    topology: str
    series_cells: int
    parallel_cells: Optional[int]
    nominal_voltage_v: float
    capacity_ah: float
    energy_wh: float
    mass_kg: float
    max_cont_current_a: float
    max_burst_current_a: Optional[float]
    price_usd: Optional[float]
    usable_fraction: float
    source_name: str
    confidence: str
    notes: str


@dataclass(frozen=True)
class EscCandidate:
    canonical_id: str
    manufacturer: str
    model: str
    continuous_current_a: float
    burst_current_a: Optional[float]
    min_cells: Optional[int]
    max_cells: int
    mass_each_kg: float
    price_each_usd: Optional[float]
    source_name: str
    confidence: str


def optional_float(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except Exception:
        return None


def optional_int(v):
    x = optional_float(v)
    return int(round(x)) if x is not None else None


def truthy(v):
    try:
        return int(float(v or 0)) == 1
    except Exception:
        return False


def load_legacy_module():
    """Load the guide/search engine from the extracted package."""
    from optimizer.propulsion import guide as module

    return module


def configure_search_depth(legacy, search_depth):
    global MAX_BATTERY_CHOICES_PER_PARAMETRIC_RESULT, MAX_REAL_SOLVES

    depth = str(search_depth or "thorough").lower()
    if depth not in SEARCH_PROFILES:
        raise ValueError(
            f"Unknown search depth {search_depth!r}. "
            f"Choose from: {', '.join(SEARCH_PROFILES)}"
        )

    profile = dict(SEARCH_PROFILES[depth])

    legacy.MAX_MOTOR_CANDIDATES = profile["motor_candidates"]
    legacy.MAX_PROP_CANDIDATES = profile["prop_candidates"]
    legacy.MAX_FINALIST_CONFIGS = profile["finalist_configs"]
    legacy.MAX_FINALISTS_PER_MOTOR = profile["finalists_per_motor"]
    legacy.MAX_FINALISTS_PER_PROP = profile["finalists_per_prop"]
    # Saving extra already-solved guide results is cheap and matters for
    # budget-constrained optimization: the best affordable design may not be in
    # the top few endurance-only guide results.
    legacy.GUIDE_RESULTS_TO_SAVE = 10000

    MAX_BATTERY_CHOICES_PER_PARAMETRIC_RESULT = profile[
        "battery_choices_per_guide"
    ]
    MAX_REAL_SOLVES = profile["max_real_solves"]

    return depth, profile


def ensure_parametric_results(force=False, search_depth="thorough", frame_model=None, min_twr=1.8, max_auw=10.0, fixed_mass_override=None, locked_motor_id=None, locked_prop_id=None, voltage_cases_override=None):
    legacy = load_legacy_module()
    depth, profile = configure_search_depth(legacy, search_depth)

    legacy.MIN_MAX_THRUST_TO_WEIGHT = float(min_twr)
    legacy.MAX_AUW_KG = float(max_auw)
    legacy.LOCKED_MOTOR_IDS = (
        [str(locked_motor_id)] if locked_motor_id else []
    )
    legacy.LOCKED_PROP_IDS = (
        [str(locked_prop_id)] if locked_prop_id else []
    )
    if voltage_cases_override:
        legacy.BATTERY_VOLTAGE_CASES_V = dict(voltage_cases_override)
    if fixed_mass_override is not None:
        legacy.FIXED_MASS_CASES_KG = [float(fixed_mass_override)]
        legacy.SCREENING_FIXED_MASS_KG = float(fixed_mass_override)

    frame_model = dict(frame_model or {})
    if frame_model:
        legacy.FRAME_PROP_CLEARANCE_M = frame_model["prop_clearance_m"]
        legacy.FRAME_STRUCTURAL_MARGIN = frame_model["structural_margin"]

    guide_dir = (
        PROJECT_ROOT
        / "optimizer_results"
        / "fast_real_motor_prop_price_screening"
    )
    csv_path = guide_dir / "guide_search_designs.csv"
    metadata_path = guide_dir / "guide_search_metadata.json"

    cached_profile_matches = False
    if csv_path.exists() and metadata_path.exists():
        try:
            old = json.loads(metadata_path.read_text(encoding="utf-8"))
            cached_profile_matches = (
                old.get("search_depth") == depth
                and old.get("profile") == profile
                and old.get("frame_model", {}) == frame_model
                and old.get("min_twr") == float(min_twr)
                and old.get("max_auw_kg") == float(max_auw)
                and old.get("fixed_mass_override") == (
                    None if fixed_mass_override is None else float(fixed_mass_override)
                )
                and old.get("locked_motor_id") == (
                    str(locked_motor_id) if locked_motor_id else None
                )
                and old.get("locked_prop_id") == (
                    str(locked_prop_id) if locked_prop_id else None
                )
                and old.get("voltage_cases_override") == (
                    dict(voltage_cases_override)
                    if voltage_cases_override else None
                )
            )
        except Exception:
            cached_profile_matches = False

    needs_run = force or not csv_path.exists() or not cached_profile_matches

    if needs_run:
        print(
            f"Running bundled propulsion guide search at '{depth}' depth..."
        )
        guide_dir.mkdir(parents=True, exist_ok=True)
        legacy.main()
        if csv_path.exists():
            metadata_path.write_text(
                json.dumps(
                    {
                        "search_depth": depth,
                        "profile": profile,
                        "frame_model": frame_model,
                        "min_twr": float(min_twr),
                        "max_auw_kg": float(max_auw),
                        "fixed_mass_override": (
                            None if fixed_mass_override is None
                            else float(fixed_mass_override)
                        ),
                        "locked_motor_id": (
                            str(locked_motor_id) if locked_motor_id else None
                        ),
                        "locked_prop_id": (
                            str(locked_prop_id) if locked_prop_id else None
                        ),
                        "voltage_cases_override": (
                            dict(voltage_cases_override)
                            if voltage_cases_override else None
                        ),
                        "created_unix_s": time.time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    else:
        print(
            f"Reusing cached '{depth}' propulsion guide search: {csv_path}"
        )

    if not csv_path.exists():
        raise RuntimeError(
            "Fast adaptive optimizer finished without creating expected CSV: "
            f"{csv_path}"
        )

    return legacy, csv_path, depth, profile


def read_csv_rows(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def warehouse_rows(table):
    if table in _WAREHOUSE_CACHE:
        return _WAREHOUSE_CACHE[table]
    if not WAREHOUSE.exists():
        raise FileNotFoundError(
            f"Component warehouse not found: {WAREHOUSE}. "
            "Build/refresh it from the Drone Optimizer Database tab."
        )
    con = sqlite3.connect(str(WAREHOUSE))
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
        _WAREHOUSE_CACHE[table] = rows
        return rows
    finally:
        con.close()



def warehouse_run_snapshot():
    from datetime import datetime, timezone

    snapshot = {
        "warehouse_path": str(WAREHOUSE),
        "modified_utc": (
            datetime.fromtimestamp(
                WAREHOUSE.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            if WAREHOUSE.exists()
            else None
        ),
        "tables": {},
    }

    if not WAREHOUSE.exists():
        return snapshot

    con = sqlite3.connect(str(WAREHOUSE))
    try:
        table_names = (
            "motors_master",
            "props_master",
            "battery_cells_master",
            "battery_packs_master",
            "escs_master",
        )
        for table in table_names:
            try:
                total = con.execute(
                    'SELECT COUNT(*) FROM "' + table + '"'
                ).fetchone()[0]
                canonical = con.execute(
                    'SELECT COUNT(DISTINCT canonical_id) FROM "' + table + '" '
                    "WHERE canonical_id IS NOT NULL AND canonical_id <> ''"
                ).fetchone()[0]
                eligible = con.execute(
                    'SELECT COUNT(*) FROM "' + table + '" '
                    "WHERE CAST(COALESCE(optimizer_eligible,'0') AS INTEGER)=1"
                ).fetchone()[0]
                snapshot["tables"][table] = {
                    "source_rows": int(total),
                    "canonical_parts": int(canonical),
                    "warehouse_optimizer_eligible_rows": int(eligible),
                }
            except Exception as exc:
                snapshot["tables"][table] = {"error": str(exc)}
    finally:
        con.close()

    return snapshot

def readiness(row, required_fields):
    complete = sum(
        1
        for f in required_fields
        if row.get(f) not in (None, "", "None", "nan", "N/A", "n/a")
    ) / max(len(required_fields), 1)

    tier = str(row.get("data_quality_tier") or "").upper()
    eligible = truthy(row.get("optimizer_eligible"))

    if eligible and complete >= 0.85:
        return "Ready"
    if tier in {"A", "B"} and complete >= 0.75:
        return "Screening"
    if complete >= 0.90:
        return "Screening"
    return "Discovery"


def dedupe_best(rows, id_field="canonical_id"):
    priority = {"Ready": 0, "Screening": 1, "Discovery": 2}
    best = {}
    for r in rows:
        key = r.get(id_field) or repr(sorted(r.items()))
        old = best.get(key)
        if old is None:
            best[key] = r
            continue
        if priority.get(r.get("_confidence", "Discovery"), 9) < priority.get(
            old.get("_confidence", "Discovery"), 9
        ):
            best[key] = r
    return list(best.values())


def build_custom_battery_candidates(
    target_series,
    target_energy_wh,
    target_mass_kg,
    pack_overhead_fraction=DEFAULT_PACK_OVERHEAD_FRACTION,
    locked_cell_id=None,
):
    rows = warehouse_rows("battery_cells_master")
    prepared = []
    required = [
        "manufacturer",
        "model",
        "nominal_voltage_v",
        "capacity_ah",
        "max_cont_discharge_a",
        "mass_g",
    ]

    for r in rows:
        if (
            locked_cell_id
            and str(r.get("canonical_id") or "") != str(locked_cell_id)
        ):
            continue
        conf = readiness(r, required)
        if conf == "Discovery":
            continue
        vn = optional_float(r.get("nominal_voltage_v"))
        ah = optional_float(r.get("capacity_ah"))
        imax = optional_float(r.get("max_cont_discharge_a"))
        mass_g = optional_float(r.get("mass_g"))
        if not all(x is not None and x > 0 for x in (vn, ah, imax, mass_g)):
            continue
        item = dict(r)
        item["_confidence"] = conf
        prepared.append(item)

    prepared = dedupe_best(prepared)

    candidates = []
    for r in prepared:
        vn = float(r["nominal_voltage_v"])
        ah = float(r["capacity_ah"])
        imax = float(r["max_cont_discharge_a"])
        mass_g = float(r["mass_g"])
        burst = optional_float(r.get("max_burst_discharge_a"))
        price_each = optional_float(r.get("price_usd_each"))

        # Target series is based on nominal 3.6-3.7 V cells. Keep exact target S.
        s = int(target_series)
        for p in range(1, 31):
            cells = s * p
            energy = s * vn * p * ah
            mass = cells * mass_g / 1000.0 * (1.0 + pack_overhead_fraction)

            # Search a useful band around the parametric optimum, with a lower
            # bound broad enough to reveal a lighter near-Pareto solution.
            if energy < max(0.45 * target_energy_wh, 120.0):
                continue
            if energy > 1.45 * target_energy_wh:
                break
            if mass > max(1.40 * target_mass_kg, target_mass_kg + 1.0):
                continue

            price = price_each * cells if price_each is not None else None
            candidates.append(
                BatteryCandidate(
                    battery_id=f"custom:{r.get('canonical_id')}:{s}S{p}P",
                    battery_type="Custom Li-ion",
                    manufacturer=str(r.get("manufacturer") or ""),
                    model=str(r.get("model") or ""),
                    topology=f"{s}S{p}P",
                    series_cells=s,
                    parallel_cells=p,
                    nominal_voltage_v=s * vn,
                    capacity_ah=p * ah,
                    energy_wh=energy,
                    mass_kg=mass,
                    max_cont_current_a=p * imax,
                    max_burst_current_a=(p * burst if burst is not None else None),
                    price_usd=price,
                    usable_fraction=DEFAULT_BATTERY_USABLE_FRACTION_LIION,
                    source_name=str(r.get("source_name") or ""),
                    confidence=str(r.get("_confidence")),
                    notes=f"{cells} cells; {pack_overhead_fraction:.0%} pack mass overhead",
                )
            )

    return candidates



def build_locked_commercial_battery(canonical_id):
    """
    Return one exact commercial pack by canonical ID without applying the
    guide-stage energy/mass proximity bands. Engineering validity is still
    checked by the real aircraft evaluator.
    """
    if not canonical_id:
        return None

    rows = warehouse_rows("battery_packs_master")
    required = [
        "manufacturer",
        "model",
        "series_cells",
        "voltage_nominal_v",
        "capacity_ah",
        "mass_g",
    ]

    matching = [
        r for r in rows
        if str(r.get("canonical_id") or "") == str(canonical_id)
    ]
    if not matching:
        return None

    prepared = []
    for r in matching:
        conf = readiness(r, required)
        if conf == "Discovery":
            continue
        item = dict(r)
        item["_confidence"] = conf
        prepared.append(item)

    prepared = dedupe_best(prepared)
    if not prepared:
        return None

    r = prepared[0]
    s = optional_int(r.get("series_cells"))
    voltage = optional_float(r.get("voltage_nominal_v"))
    ah = optional_float(r.get("capacity_ah"))
    mass_g = optional_float(r.get("mass_g"))
    if not all(
        x is not None and x > 0
        for x in (s, voltage, ah, mass_g)
    ):
        return None

    energy = optional_float(r.get("energy_wh"))
    if energy is None:
        energy = voltage * ah

    cont = optional_float(r.get("max_cont_current_a"))
    if cont is None:
        c_rate = optional_float(r.get("c_rating_cont"))
        cont = c_rate * ah if c_rate is not None else None
    if cont is None or cont <= 0:
        return None

    burst = optional_float(r.get("max_burst_current_a"))
    if burst is None:
        c_burst = optional_float(r.get("c_rating_burst"))
        burst = c_burst * ah if c_burst is not None else None

    return BatteryCandidate(
        battery_id=f"commercial:{r.get('canonical_id')}",
        battery_type="Commercial pack",
        manufacturer=str(r.get("manufacturer") or ""),
        model=str(r.get("model") or ""),
        topology=f"{s}S",
        series_cells=int(s),
        parallel_cells=optional_int(r.get("parallel_cells")),
        nominal_voltage_v=float(voltage),
        capacity_ah=float(ah),
        energy_wh=float(energy),
        mass_kg=float(mass_g) / 1000.0,
        max_cont_current_a=float(cont),
        max_burst_current_a=burst,
        price_usd=optional_float(r.get("price_usd")),
        usable_fraction=DEFAULT_BATTERY_USABLE_FRACTION_LIPO,
        source_name=str(r.get("source_name") or ""),
        confidence=str(r.get("_confidence")),
        notes="User-locked commercial pack from component warehouse",
    )


def locked_commercial_pack_voltage_case(canonical_id):
    pack = build_locked_commercial_battery(canonical_id)
    if pack is None:
        return None
    return {
        f"{pack.series_cells}S": float(pack.nominal_voltage_v)
    }


def build_commercial_battery_candidates(target_series, target_energy_wh, target_mass_kg):
    rows = warehouse_rows("battery_packs_master")
    required = [
        "manufacturer",
        "model",
        "series_cells",
        "voltage_nominal_v",
        "capacity_ah",
        "mass_g",
    ]

    candidates = []
    for r in rows:
        conf = readiness(r, required)
        if conf == "Discovery":
            continue

        s = optional_int(r.get("series_cells"))
        voltage = optional_float(r.get("voltage_nominal_v"))
        ah = optional_float(r.get("capacity_ah"))
        mass_g = optional_float(r.get("mass_g"))
        if not all(x is not None and x > 0 for x in (s, voltage, ah, mass_g)):
            continue
        if s != int(target_series):
            continue

        energy = optional_float(r.get("energy_wh"))
        if energy is None:
            energy = voltage * ah

        if energy < max(0.45 * target_energy_wh, 120.0):
            continue
        if energy > 1.45 * target_energy_wh:
            continue

        mass = mass_g / 1000.0
        if mass > max(1.40 * target_mass_kg, target_mass_kg + 1.0):
            continue

        cont = optional_float(r.get("max_cont_current_a"))
        if cont is None:
            c_rate = optional_float(r.get("c_rating_cont"))
            cont = c_rate * ah if c_rate is not None else None
        if cont is None or cont <= 0:
            continue

        burst = optional_float(r.get("max_burst_current_a"))
        if burst is None:
            c_burst = optional_float(r.get("c_rating_burst"))
            burst = c_burst * ah if c_burst is not None else None

        price = optional_float(r.get("price_usd"))
        candidates.append(
            BatteryCandidate(
                battery_id=f"commercial:{r.get('canonical_id')}",
                battery_type="Commercial pack",
                manufacturer=str(r.get("manufacturer") or ""),
                model=str(r.get("model") or ""),
                topology=f"{s}S",
                series_cells=s,
                parallel_cells=optional_int(r.get("parallel_cells")),
                nominal_voltage_v=voltage,
                capacity_ah=ah,
                energy_wh=energy,
                mass_kg=mass,
                max_cont_current_a=cont,
                max_burst_current_a=burst,
                price_usd=price,
                usable_fraction=DEFAULT_BATTERY_USABLE_FRACTION_LIPO,
                source_name=str(r.get("source_name") or ""),
                confidence=conf,
                notes="Commercial pack from component warehouse",
            )
        )

    return candidates


def rank_batteries(candidates, target_energy_wh, target_mass_kg):
    seen = set()
    ranked = []
    for b in candidates:
        # Avoid repeated source records representing the same physical topology.
        key = (
            b.battery_type,
            b.manufacturer.lower(),
            b.model.lower(),
            b.topology,
            round(b.energy_wh, 1),
            round(b.mass_kg, 3),
        )
        if key in seen:
            continue
        seen.add(key)

        energy_error = abs(b.energy_wh - target_energy_wh) / max(target_energy_wh, 1.0)
        mass_error = abs(b.mass_kg - target_mass_kg) / max(target_mass_kg, 0.1)
        specific_energy = b.energy_wh / max(b.mass_kg, 1e-6)
        confidence_bonus = 0.0 if b.confidence == "Ready" else 0.04
        score = 0.58 * energy_error + 0.30 * mass_error - 0.0005 * specific_energy + confidence_bonus
        ranked.append((score, b))

    ranked.sort(key=lambda x: x[0])
    return [b for _, b in ranked]


def load_esc_candidates():
    rows = warehouse_rows("escs_master")
    required = [
        "manufacturer",
        "model",
        "continuous_current_a",
        "max_cells",
        "mass_g",
    ]
    prepared = []
    for r in rows:
        conf = readiness(r, required)
        if conf == "Discovery":
            continue
        cont = optional_float(r.get("continuous_current_a"))
        smax = optional_int(r.get("max_cells"))
        mass = optional_float(r.get("mass_g"))
        if not all(x is not None and x > 0 for x in (cont, smax, mass)):
            continue
        item = dict(r)
        item["_confidence"] = conf
        prepared.append(item)
    prepared = dedupe_best(prepared)

    out = []
    for r in prepared:
        out.append(
            EscCandidate(
                canonical_id=str(r.get("canonical_id") or ""),
                manufacturer=str(r.get("manufacturer") or ""),
                model=str(r.get("model") or ""),
                continuous_current_a=float(r["continuous_current_a"]),
                burst_current_a=optional_float(r.get("burst_current_a")),
                min_cells=optional_int(r.get("min_cells")),
                max_cells=int(float(r["max_cells"])),
                mass_each_kg=float(r["mass_g"]) / 1000.0,
                price_each_usd=optional_float(r.get("price_usd")),
                source_name=str(r.get("source_name") or ""),
                confidence=str(r.get("_confidence")),
            )
        )
    return out


def voltage_compatible_escs(escs, series_cells):
    """
    Return voltage-compatible ESCs ranked by confidence then mass.

    V0.2 incorrectly required the ESC to exceed the MOTOR DATASHEET maximum
    current before the motor/prop/battery physics had even been solved. That can
    eliminate perfectly valid combinations whose actual current is much lower
    than the motor's absolute rating.

    V0.2.1 instead chooses after an initial physics solve using the ACTUAL
    full-throttle motor current.
    """
    compatible = []
    for e in escs:
        if e.min_cells is not None and series_cells < e.min_cells:
            continue
        if series_cells > e.max_cells:
            continue
        confidence_penalty = 0 if e.confidence == "Ready" else 0.02
        compatible.append((confidence_penalty + e.mass_each_kg, e))
    compatible.sort(key=lambda x: x[0])
    return [e for _, e in compatible]


def choose_esc_from_actual_current(
    escs,
    series_cells,
    actual_max_motor_current,
    margin,
    price_resolver_obj=None,
    require_price=False,
):
    required_current = actual_max_motor_current * margin
    compatible = []
    for e in voltage_compatible_escs(escs, series_cells):
        if e.continuous_current_a < required_current:
            continue

        esc_price = e.price_each_usd
        if esc_price is None and price_resolver_obj is not None:
            hit = price_resolver_obj.resolve(
                "esc",
                canonical_id=e.canonical_id,
                manufacturer=e.manufacturer,
                model=e.model,
            )
            esc_price = hit["price_usd"] if hit else None

        if require_price and esc_price is None:
            continue

        priced_e = (
            replace(e, price_each_usd=esc_price)
            if esc_price is not None and e.price_each_usd is None
            else e
        )

        confidence_penalty = 0 if e.confidence == "Ready" else 0.02
        score = (
            e.mass_each_kg
            + 0.0003 * (e.continuous_current_a - required_current)
            + confidence_penalty
        )
        compatible.append((score, priced_e))
    if not compatible:
        return None
    compatible.sort(key=lambda x: x[0])
    return compatible[0][1]



def _tube_properties(od_mm, wall_mm):
    od = float(od_mm) / 1000.0
    wall = float(wall_mm) / 1000.0
    inner = od - 2.0 * wall
    if inner <= 0:
        return None
    area = math.pi / 4.0 * (od**2 - inner**2)
    inertia = math.pi / 64.0 * (od**4 - inner**4)
    section_modulus = inertia / (od / 2.0)
    linear_mass = FRAME_CF_DENSITY_KG_M3 * area
    return {
        "od_m": od,
        "wall_m": wall,
        "area_m2": area,
        "I_m4": inertia,
        "Z_m3": section_modulus,
        "linear_mass_kg_m": linear_mass,
    }


def _select_arm_tube(force_n, arm_length_m, structural_margin):
    design_force = float(force_n) * float(structural_margin)
    moment = design_force * float(arm_length_m)
    deflection_limit = (
        FRAME_MAX_ARM_DEFLECTION_RATIO * float(arm_length_m)
    )

    feasible = []
    for od_mm, wall_mm in FRAME_TUBE_CANDIDATES_MM:
        p = _tube_properties(od_mm, wall_mm)
        if p is None:
            continue
        stress = moment / max(p["Z_m3"], 1e-15)
        deflection = (
            design_force * arm_length_m**3
            / (3.0 * FRAME_CF_EFFECTIVE_E_PA * p["I_m4"])
        )
        if (
            stress <= FRAME_CF_ALLOWABLE_BENDING_PA
            and deflection <= deflection_limit
        ):
            mass_each = p["linear_mass_kg_m"] * arm_length_m
            feasible.append((
                mass_each,
                od_mm,
                wall_mm,
                stress,
                deflection,
                p,
            ))

    if not feasible:
        return None

    feasible.sort(key=lambda x: x[0])
    mass_each, od_mm, wall_mm, stress, deflection, p = feasible[0]
    return {
        "od_mm": od_mm,
        "wall_mm": wall_mm,
        "mass_each_kg": mass_each,
        "stress_pa": stress,
        "deflection_m": deflection,
        "stress_utilization": (
            stress / FRAME_CF_ALLOWABLE_BENDING_PA
        ),
        "deflection_utilization": (
            deflection / max(deflection_limit, 1e-12)
        ),
        "properties": p,
    }


def estimate_frame_mass(
    nonframe_mass_kg,
    prop_diameter_in,
    battery_mass_kg,
    min_twr,
    *,
    prop_clearance_m=DEFAULT_FRAME_PROP_CLEARANCE_M,
    structural_margin=DEFAULT_FRAME_STRUCTURAL_MARGIN,
    **_ignored,
):
    """
    Physically grounded concept-stage frame sizing.

    The frame is modeled as:
      - four discrete carbon-fiber tube arms,
      - two carbon-fiber center plates,
      - one carbon-fiber battery tray,
      - hardware/joint and landing-support allowances.

    Arms are selected from real geometric tube sizes using cantilever bending
    stress and deflection limits. Frame mass is iterated with AUW because a
    heavier frame raises the design rotor load, which can require a larger tube.

    This is still a conceptual sizing model, not a replacement for CAD/FEA.
    """
    prop_diameter_m = float(prop_diameter_in) * 0.0254
    motor_spacing_m = prop_diameter_m + float(prop_clearance_m)
    motor_radius_m = motor_spacing_m / math.sqrt(2.0)

    # Approximate battery packaging from physical volume rather than an arbitrary
    # percentage-of-battery-mass structural term.
    battery_volume_m3 = (
        float(battery_mass_kg)
        / FRAME_ASSUMED_PACK_BULK_DENSITY_KG_M3
    )
    battery_footprint_m2 = (
        battery_volume_m3 / FRAME_ASSUMED_PACK_THICKNESS_M
    )
    battery_side_m = math.sqrt(max(battery_footprint_m2, 0.0))
    center_side_m = min(
        0.32,
        max(0.14, battery_side_m + FRAME_CENTER_MARGIN_M),
    )

    # Arms begin near the center plate corner; using the full center-to-motor
    # radius would over-count tube length.
    center_corner_radius_m = center_side_m / math.sqrt(2.0)
    arm_tube_length_m = max(
        0.10,
        motor_radius_m - 0.70 * center_corner_radius_m,
    )

    center_plate_mass = (
        2.0
        * center_side_m**2
        * FRAME_CENTER_PLATE_THICKNESS_M
        * FRAME_CF_DENSITY_KG_M3
    )
    tray_side_m = max(0.10, battery_side_m + 0.020)
    battery_tray_mass = (
        tray_side_m**2
        * FRAME_BATTERY_TRAY_THICKNESS_M
        * FRAME_CF_DENSITY_KG_M3
    )

    # Joint/hardware allowance scales mildly with arm diameter once a tube is
    # selected. Start with a conservative small-frame estimate for iteration.
    frame_mass = (
        center_plate_mass
        + battery_tray_mass
        + FRAME_HARDWARE_BASE_MASS_KG
        + FRAME_LANDING_SUPPORT_MASS_KG
        + 0.20
    )

    selected = None
    for _ in range(60):
        auw = float(nonframe_mass_kg) + frame_mass
        target_force_each = (
            float(min_twr) * auw * G / N_ROTORS
        )
        selected = _select_arm_tube(
            target_force_each,
            arm_tube_length_m,
            structural_margin,
        )
        if selected is None:
            return {
                "structurally_feasible": False,
                "frame_mass_kg": float("inf"),
                "motor_spacing_m": motor_spacing_m,
                "arm_radius_m": motor_radius_m,
                "estimated_tip_to_tip_span_m": (
                    2.0 * motor_radius_m + prop_diameter_m
                ),
                "failure_reason": "no_candidate_arm_tube_passes",
            }

        arms_mass = N_ROTORS * selected["mass_each_kg"]

        # Approximate aluminum/carbon clamps, motor mounts and fasteners.
        # This is a hardware mass allowance, but unlike the old model it does
        # not stand in for the arm structure itself.
        clamp_mass = (
            FRAME_HARDWARE_BASE_MASS_KG
            + N_ROTORS * 0.0015 * selected["od_mm"]
        )
        new_mass = (
            arms_mass
            + center_plate_mass
            + battery_tray_mass
            + clamp_mass
            + FRAME_LANDING_SUPPORT_MASS_KG
        )
        if abs(new_mass - frame_mass) < 1e-6:
            frame_mass = new_mass
            break
        frame_mass = 0.5 * (frame_mass + new_mass)

    auw = float(nonframe_mass_kg) + frame_mass
    target_force_each = float(min_twr) * auw * G / N_ROTORS
    selected = _select_arm_tube(
        target_force_each,
        arm_tube_length_m,
        structural_margin,
    )
    if selected is None:
        return {
            "structurally_feasible": False,
            "frame_mass_kg": float("inf"),
            "failure_reason": "no_candidate_arm_tube_passes",
        }

    arms_mass = N_ROTORS * selected["mass_each_kg"]
    clamp_mass = (
        FRAME_HARDWARE_BASE_MASS_KG
        + N_ROTORS * 0.0015 * selected["od_mm"]
    )
    frame_mass = (
        arms_mass
        + center_plate_mass
        + battery_tray_mass
        + clamp_mass
        + FRAME_LANDING_SUPPORT_MASS_KG
    )

    return {
        "structurally_feasible": True,
        "frame_material": FRAME_MATERIAL_NAME,
        "frame_mass_kg": frame_mass,
        "motor_spacing_m": motor_spacing_m,
        "arm_radius_m": motor_radius_m,
        "arm_tube_length_m": arm_tube_length_m,
        "estimated_tip_to_tip_span_m": (
            2.0 * motor_radius_m + prop_diameter_m
        ),
        "arm_tube_od_mm": selected["od_mm"],
        "arm_tube_wall_mm": selected["wall_mm"],
        "arm_stress_mpa": selected["stress_pa"] / 1e6,
        "arm_tip_deflection_mm": selected["deflection_m"] * 1000.0,
        "arm_stress_utilization_pct": (
            100.0 * selected["stress_utilization"]
        ),
        "arm_deflection_utilization_pct": (
            100.0 * selected["deflection_utilization"]
        ),
        "arms_mass_kg": arms_mass,
        "center_plate_side_m": center_side_m,
        "center_plate_mass_kg": center_plate_mass,
        "battery_tray_mass_kg": battery_tray_mass,
        "hardware_joint_mass_kg": clamp_mass,
        "landing_support_mass_kg": FRAME_LANDING_SUPPORT_MASS_KG,
        # Compatibility aliases used by the current Results UI.
        "geometry_mass_kg": (
            arms_mass + center_plate_mass
        ),
        "battery_mount_mass_kg": battery_tray_mass,
        "reinforcement_mass_kg": clamp_mass,
        "design_arm_root_moment_nm": (
            target_force_each
            * structural_margin
            * arm_tube_length_m
        ),
    }


class RealAircraftPerfComp(om.ExplicitComponent):
    def initialize(self):
        self.options.declare("battery_energy_wh", types=float)
        self.options.declare("usable_fraction", types=float)
        self.options.declare("battery_voltage_v", types=float)

    def setup(self):
        self.add_input("auw_kg", val=4.0)
        self.add_input("hover_thrust_n", val=10.0)
        self.add_input("required_hover_thrust_n", val=10.0)
        self.add_input("hover_battery_power_w", val=150.0)
        self.add_input("max_thrust_n", val=20.0)
        self.add_input("max_battery_power_w", val=300.0)

        self.add_output("hover_residual_n", val=0.0)
        self.add_output("total_hover_power_w", val=600.0)
        self.add_output("endurance_min", val=30.0)
        self.add_output("max_thrust_to_weight", val=2.0)
        self.add_output("hover_pack_current_a", val=20.0)
        self.add_output("max_pack_current_a", val=40.0)

    def compute(self, inputs, outputs):
        auw = float(inputs["auw_kg"][0])
        hover_thrust = float(inputs["hover_thrust_n"][0])
        required = float(inputs["required_hover_thrust_n"][0])
        hover_each = float(inputs["hover_battery_power_w"][0])
        max_thrust_each = float(inputs["max_thrust_n"][0])
        max_power_each = float(inputs["max_battery_power_w"][0])

        total_hover_power = N_ROTORS * hover_each + ACCESSORY_POWER_W
        energy = self.options["battery_energy_wh"] * self.options["usable_fraction"]
        endurance = 60.0 * energy / max(total_hover_power, 1e-9)
        voltage = self.options["battery_voltage_v"]

        outputs["hover_residual_n"] = hover_thrust - required
        outputs["total_hover_power_w"] = total_hover_power
        outputs["endurance_min"] = endurance
        outputs["max_thrust_to_weight"] = (
            N_ROTORS * max_thrust_each / max(auw * G, 1e-9)
        )
        outputs["hover_pack_current_a"] = total_hover_power / max(voltage, 1e-9)
        outputs["max_pack_current_a"] = (
            N_ROTORS * max_power_each + ACCESSORY_POWER_W
        ) / max(voltage, 1e-9)


def scalar(prob, name):
    return float(np.asarray(prob.get_val(name)).ravel()[0])



def _cache_float(value, digits=8):
    return round(float(value), digits)


def propulsion_cache_key(prop_entry, motor, voltage_v):
    payload = {
        "schema": PROPULSION_CURVE_SCHEMA_VERSION,
        "prop_id": str(prop_entry.metadata.id),
        "prop_diameter_m": _cache_float(prop_entry.diameter_m),
        "motor_id": str(motor.id),
        "kv": _cache_float(motor.kv),
        "resistance": _cache_float(motor.resistance),
        "io": _cache_float(motor.io),
        "max_current": _cache_float(motor.max_current),
        "voltage": _cache_float(voltage_v, 5),
        "throttles": [
            _cache_float(x, 5)
            for x in PROPULSION_CURVE_THROTTLES.tolist()
        ],
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), payload


class PropulsionCurveCache:
    """
    Persistent PyThrust response-curve cache.

    Each unique motor + prop + voltage combination is evaluated across a
    fixed throttle grid once. Subsequent aircraft candidates use interpolation,
    while the most promising aircraft are still revalidated with exact PyThrust
    hover solves before final ranking.
    """

    def __init__(self, db_path=PROPULSION_CACHE_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = {}
        self.hits = 0
        self.misses = 0
        self.builds = 0
        con = sqlite3.connect(str(self.db_path))
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS propulsion_curves (
                    cache_key TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    motor_id TEXT,
                    prop_id TEXT,
                    voltage_v REAL,
                    created_unix_s REAL,
                    payload_json TEXT NOT NULL,
                    curve_json TEXT NOT NULL
                )
                """
            )
            con.commit()
        finally:
            con.close()

    def get(self, cache_key):
        if cache_key in self._memory:
            self.hits += 1
            return self._memory[cache_key]

        con = sqlite3.connect(str(self.db_path))
        try:
            row = con.execute(
                """
                SELECT curve_json
                FROM propulsion_curves
                WHERE cache_key=? AND schema_version=?
                """,
                (cache_key, PROPULSION_CURVE_SCHEMA_VERSION),
            ).fetchone()
        finally:
            con.close()

        if not row:
            self.misses += 1
            return None

        curve = json.loads(row[0])
        self._memory[cache_key] = curve
        self.hits += 1
        return curve

    def put(
        self,
        cache_key,
        payload,
        curve,
        motor_id,
        prop_id,
        voltage_v,
    ):
        con = sqlite3.connect(str(self.db_path))
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO propulsion_curves
                (
                    cache_key, schema_version, motor_id, prop_id,
                    voltage_v, created_unix_s, payload_json, curve_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    PROPULSION_CURVE_SCHEMA_VERSION,
                    str(motor_id),
                    str(prop_id),
                    float(voltage_v),
                    time.time(),
                    json.dumps(payload, sort_keys=True),
                    json.dumps(curve),
                ),
            )
            con.commit()
        finally:
            con.close()
        self._memory[cache_key] = curve
        self.builds += 1

    def get_or_build(
        self,
        legacy,
        prop_entry,
        motor,
        voltage_v,
    ):
        key, payload = propulsion_cache_key(
            prop_entry, motor, voltage_v
        )
        cached = self.get(key)
        if cached is not None:
            return cached, key, True

        curve = build_propulsion_curve(
            legacy, prop_entry, motor, voltage_v
        )
        self.put(
            key,
            payload,
            curve,
            motor.id,
            prop_entry.metadata.id,
            voltage_v,
        )
        return curve, key, False

    def stats(self):
        return {
            "hits": int(self.hits),
            "misses": int(self.misses),
            "new_curves_built": int(self.builds),
            "cache_path": str(self.db_path),
        }


def build_propulsion_curve(
    legacy,
    prop_entry,
    motor,
    voltage_v,
):
    """
    Build one PyThrust response curve with a single reusable OpenMDAO Problem.
    """
    prob = om.Problem(reports=False)
    model = prob.model

    ivc = om.IndepVarComp()
    ivc.add_output("kv_rpm_per_v", val=float(motor.kv))
    ivc.add_output("resistance_ohm", val=float(motor.resistance))
    ivc.add_output("no_load_current_a", val=float(motor.io))
    ivc.add_output("current_max_a", val=float(motor.max_current))
    ivc.add_output("voltage_v", val=float(voltage_v))
    ivc.add_output("diameter_m", val=float(prop_entry.diameter_m))
    ivc.add_output("throttle", val=0.40)
    model.add_subsystem("design", ivc, promotes_outputs=["*"])

    prop = legacy.PropulsionComponent(prop_entry=prop_entry)
    model.add_subsystem("prop", prop)
    for src, dst in [
        ("kv_rpm_per_v", "kv_rpm_per_v"),
        ("resistance_ohm", "resistance_ohm"),
        ("no_load_current_a", "no_load_current_a"),
        ("current_max_a", "current_max_a"),
        ("voltage_v", "voltage_v"),
        ("diameter_m", "diameter_m"),
        ("throttle", "throttle"),
    ]:
        model.connect(src, f"prop.{dst}")

    prob.setup()

    rows = []
    for throttle in PROPULSION_CURVE_THROTTLES:
        prob.set_val("throttle", float(throttle))
        with contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            prob.run_model()

        try:
            row = {
                "throttle": float(throttle),
                "thrust_n": scalar(prob, "prop.thrust_n"),
                "battery_power_w": scalar(
                    prob, "prop.battery_power_w"
                ),
                "motor_current_a": scalar(
                    prob, "prop.motor_current_a"
                ),
                "rpm": scalar(prob, "prop.rpm"),
                "is_feasible": scalar(
                    prob, "prop.is_feasible"
                ),
            }
        except Exception as exc:
            raise RuntimeError(
                "Could not read PyThrust propulsion curve point"
            ) from exc
        rows.append(row)

    # Thrust should normally be monotonic. Keep only a monotonic envelope for
    # safe inversion in the exploratory stage.
    monotonic = []
    last_thrust = -float("inf")
    for row in rows:
        thrust = row["thrust_n"]
        if (
            np.isfinite(thrust)
            and thrust > last_thrust + 1e-9
            and row["is_feasible"] >= 0.5
        ):
            monotonic.append(row)
            last_thrust = thrust

    if len(monotonic) < 4:
        raise RuntimeError(
            "Insufficient feasible monotonic PyThrust points for cached curve"
        )

    return {
        "motor_id": str(motor.id),
        "prop_id": str(prop_entry.metadata.id),
        "voltage_v": float(voltage_v),
        "points": monotonic,
    }


def interpolate_propulsion_curve(curve, required_thrust_n):
    points = curve["points"]
    thrust = np.asarray(
        [p["thrust_n"] for p in points], dtype=float
    )
    target = float(required_thrust_n)

    if target < thrust[0] or target > thrust[-1]:
        return None

    idx = int(np.searchsorted(thrust, target))
    if idx <= 0:
        lo = hi = points[0]
    elif idx >= len(points):
        lo = hi = points[-1]
    else:
        lo = points[idx - 1]
        hi = points[idx]

    if lo is hi or abs(
        hi["thrust_n"] - lo["thrust_n"]
    ) < 1e-12:
        alpha = 0.0
    else:
        alpha = (
            target - lo["thrust_n"]
        ) / (hi["thrust_n"] - lo["thrust_n"])

    def lerp(field):
        return (
            float(lo[field])
            + alpha * (float(hi[field]) - float(lo[field]))
        )

    return {
        "throttle": lerp("throttle"),
        "thrust_n": target,
        "battery_power_w": lerp("battery_power_w"),
        "motor_current_a": lerp("motor_current_a"),
        "rpm": lerp("rpm"),
        "is_feasible": 1.0,
    }


def max_propulsion_point(curve):
    points = curve["points"]
    return max(points, key=lambda p: p["throttle"])


def build_real_problem(
    legacy,
    prop_entry,
    prop_catalog,
    motor,
    battery,
    esc,
    fixed_mass,
    frame_mass_kg,
):
    """
    Pure evaluation model.

    Once motor, prop, battery, and ESC are fixed, hover throttle is the only
    scalar unknown. We therefore do not invoke an OpenMDAO optimization driver.
    """
    prob = om.Problem(reports=False)
    model = prob.model

    d_in = float(prop_entry.metadata.diameter_in)
    frame_mass = float(frame_mass_kg)
    props_mass = N_ROTORS * float(prop_catalog["weight_g"]) / 1000.0
    motors_mass = N_ROTORS * float(motor.weight_g) / 1000.0
    esc_mass = N_ROTORS * float(esc.mass_each_kg)
    battery_mass = float(battery.mass_kg)

    auw = (
        float(fixed_mass)
        + frame_mass
        + props_mass
        + motors_mass
        + esc_mass
        + battery_mass
    )
    required_hover_each_n = auw * G / N_ROTORS

    ivc = om.IndepVarComp()
    ivc.add_output("kv_rpm_per_v", val=float(motor.kv))
    ivc.add_output("resistance_ohm", val=float(motor.resistance))
    ivc.add_output("no_load_current_a", val=float(motor.io))
    ivc.add_output("current_max_a", val=float(motor.max_current))
    ivc.add_output("voltage_v", val=float(battery.nominal_voltage_v))
    ivc.add_output("diameter_m", val=float(prop_entry.diameter_m))
    ivc.add_output("hover_throttle", val=0.45)
    ivc.add_output("max_throttle", val=1.0)

    ivc.add_output("frame_mass_kg", val=frame_mass)
    ivc.add_output("battery_mass_kg", val=battery_mass)
    ivc.add_output("props_mass_kg", val=props_mass)
    ivc.add_output("motor_mass_total_kg", val=motors_mass)
    ivc.add_output("esc_mass_total_kg", val=esc_mass)
    ivc.add_output("auw_kg", val=auw)
    ivc.add_output("required_hover_thrust_n", val=required_hover_each_n)

    model.add_subsystem("design", ivc, promotes_outputs=["*"])

    hover = legacy.PropulsionComponent(prop_entry=prop_entry)
    max_prop = legacy.PropulsionComponent(prop_entry=prop_entry)
    model.add_subsystem("hover_prop", hover)
    model.add_subsystem("max_prop", max_prop)

    for target in ("hover_prop", "max_prop"):
        for src, dst in [
            ("kv_rpm_per_v", "kv_rpm_per_v"),
            ("resistance_ohm", "resistance_ohm"),
            ("no_load_current_a", "no_load_current_a"),
            ("current_max_a", "current_max_a"),
            ("voltage_v", "voltage_v"),
            ("diameter_m", "diameter_m"),
        ]:
            model.connect(src, f"{target}.{dst}")

    model.connect("hover_throttle", "hover_prop.throttle")
    model.connect("max_throttle", "max_prop.throttle")

    perf = RealAircraftPerfComp(
        battery_energy_wh=float(battery.energy_wh),
        usable_fraction=float(battery.usable_fraction),
        battery_voltage_v=float(battery.nominal_voltage_v),
    )
    model.add_subsystem("aircraft", perf)
    model.connect("auw_kg", "aircraft.auw_kg")
    model.connect("hover_prop.thrust_n", "aircraft.hover_thrust_n")
    model.connect("required_hover_thrust_n", "aircraft.required_hover_thrust_n")
    model.connect("hover_prop.battery_power_w", "aircraft.hover_battery_power_w")
    model.connect("max_prop.thrust_n", "aircraft.max_thrust_n")
    model.connect("max_prop.battery_power_w", "aircraft.max_battery_power_w")

    prob.setup()
    return prob


def solve_hover_throttle(
    prob,
    lower=0.10,
    upper=0.90,
    tol_n=0.01,
    max_iter=50,
):
    """Robustly solve hover thrust = required thrust using bisection."""

    def residual(throttle):
        prob.set_val("hover_throttle", float(throttle))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            prob.run_model()
        return scalar(prob, "hover_prop.thrust_n") - scalar(
            prob, "required_hover_thrust_n"
        )

    try:
        f_lo = residual(lower)
        f_hi = residual(upper)
    except Exception as exc:
        return False, ("openmdao_model_exception", exc)

    if not np.isfinite(f_lo) or not np.isfinite(f_hi):
        return False, ("nonfinite_hover_residual", None)

    if f_lo > 0:
        lower = 0.01
        try:
            f_lo = residual(lower)
        except Exception as exc:
            return False, ("openmdao_model_exception", exc)

    if f_lo > 0:
        return False, ("too_much_thrust_at_min_throttle", None)
    if f_hi < 0:
        return False, ("insufficient_hover_thrust", None)

    mid = 0.5 * (lower + upper)
    for _ in range(max_iter):
        mid = 0.5 * (lower + upper)
        try:
            f_mid = residual(mid)
        except Exception as exc:
            return False, ("openmdao_model_exception", exc)

        if not np.isfinite(f_mid):
            return False, ("nonfinite_hover_residual", None)

        if abs(f_mid) <= tol_n:
            return True, None

        if f_mid > 0:
            upper = mid
        else:
            lower = mid

    try:
        prob.set_val("hover_throttle", float(mid))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            prob.run_model()
    except Exception as exc:
        return False, ("openmdao_model_exception", exc)

    final_res = scalar(prob, "hover_prop.thrust_n") - scalar(
        prob, "required_hover_thrust_n"
    )
    if abs(final_res) <= max(0.05, 5.0 * tol_n):
        return True, None
    return False, ("hover_bisection_not_converged", None)


def _norm_name(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


class PriceResolver:
    """
    Resolve the best available unit price from both master component rows and
    prices_master. This does not invent a price when none is present.
    """

    TABLE_INFO = {
        "motor": ("motors_master", "price_usd"),
        "prop": ("props_master", "price_usd"),
        "battery_cell": ("battery_cells_master", "price_usd_each"),
        "battery_pack": ("battery_packs_master", "price_usd"),
        "esc": ("escs_master", "price_usd"),
    }

    def __init__(self):
        self.by_canonical = {}
        self.by_source_id = {}
        self.by_name = {}

        for component_type, (table, price_field) in self.TABLE_INFO.items():
            try:
                rows = warehouse_rows(table)
            except Exception:
                rows = []
            for r in rows:
                price = optional_float(r.get(price_field))
                if price is None or price <= 0:
                    continue
                item = {
                    "price_usd": price,
                    "source": r.get("source_name") or table,
                    "checked_date": r.get("checked_date"),
                    "in_stock": r.get("status"),
                }
                cid = str(r.get("canonical_id") or "")
                sid = str(r.get("source_record_id") or "")
                name_key = (
                    component_type,
                    _norm_name(r.get("manufacturer")),
                    _norm_name(r.get("model")),
                )
                if cid:
                    self.by_canonical[(component_type, cid)] = item
                if sid:
                    self.by_source_id[(component_type, sid)] = item
                if name_key[1] or name_key[2]:
                    self.by_name.setdefault(name_key, item)

        try:
            price_rows = warehouse_rows("prices_master")
        except Exception:
            price_rows = []

        for r in price_rows:
            raw_type = str(r.get("component_type") or "").strip().lower()
            type_map = {
                "motors": "motor",
                "motor": "motor",
                "props": "prop",
                "propeller": "prop",
                "prop": "prop",
                "cells": "battery_cell",
                "cell": "battery_cell",
                "battery_cell": "battery_cell",
                "battery cells": "battery_cell",
                "packs": "battery_pack",
                "battery": "battery_pack",
                "battery_pack": "battery_pack",
                "battery packs": "battery_pack",
                "esc": "esc",
                "escs": "esc",
            }
            component_type = type_map.get(raw_type, raw_type)
            price = optional_float(r.get("price_usd"))
            qty = optional_float(r.get("quantity")) or 1.0
            if price is None or price <= 0 or qty <= 0:
                continue

            item = {
                "price_usd": price / qty,
                "source": r.get("vendor") or r.get("source_name") or "prices_master",
                "checked_date": r.get("checked_date"),
                "in_stock": r.get("in_stock"),
            }
            cid = str(r.get("canonical_id") or "")
            name_key = (
                component_type,
                _norm_name(r.get("manufacturer")),
                _norm_name(r.get("model")),
            )
            if cid:
                self.by_canonical[(component_type, cid)] = item
            if name_key[1] or name_key[2]:
                self.by_name[name_key] = item

    def resolve(
        self,
        component_type,
        *,
        canonical_id=None,
        source_record_id=None,
        manufacturer=None,
        model=None,
    ):
        if source_record_id:
            found = self.by_source_id.get(
                (component_type, str(source_record_id))
            )
            if found:
                return found

        if canonical_id:
            found = self.by_canonical.get(
                (component_type, str(canonical_id))
            )
            if found:
                return found

        key = (
            component_type,
            _norm_name(manufacturer),
            _norm_name(model),
        )
        return self.by_name.get(key)


def price_resolver():
    return PriceResolver()



def _resolve_design_prices(
    price_resolver_obj,
    prop_entry,
    prop_catalog,
    motor,
    battery,
    esc=None,
):
    motor_hit = price_resolver_obj.resolve(
        "motor",
        source_record_id=motor.id,
        manufacturer=motor.manufacturer,
        model=motor.name,
    )
    motor_price = motor_hit["price_usd"] if motor_hit else None

    prop_price = optional_float(prop_catalog.get("price_usd"))
    if prop_price is None:
        prop_hit = price_resolver_obj.resolve(
            "prop",
            manufacturer=getattr(
                prop_entry.metadata, "manufacturer", "APC"
            ),
            model=prop_entry.metadata.model,
        )
        prop_price = prop_hit["price_usd"] if prop_hit else None

    battery_cost = battery.price_usd
    if battery_cost is None:
        if battery.battery_type == "Custom Li-ion":
            parts = str(battery.battery_id).split(":")
            cell_canonical = parts[1] if len(parts) >= 3 else None
            cell_hit = price_resolver_obj.resolve(
                "battery_cell",
                canonical_id=cell_canonical,
                manufacturer=battery.manufacturer,
                model=battery.model,
            )
            if cell_hit and battery.parallel_cells:
                battery_cost = (
                    cell_hit["price_usd"]
                    * battery.series_cells
                    * battery.parallel_cells
                )
        else:
            parts = str(battery.battery_id).split(":")
            pack_canonical = parts[1] if len(parts) >= 2 else None
            pack_hit = price_resolver_obj.resolve(
                "battery_pack",
                canonical_id=pack_canonical,
                manufacturer=battery.manufacturer,
                model=battery.model,
            )
            if pack_hit:
                battery_cost = pack_hit["price_usd"]

    esc_price_each = None
    if esc is not None:
        esc_price_each = esc.price_each_usd
        if esc_price_each is None:
            hit = price_resolver_obj.resolve(
                "esc",
                canonical_id=esc.canonical_id,
                manufacturer=esc.manufacturer,
                model=esc.model,
            )
            esc_price_each = hit["price_usd"] if hit else None

    return motor_price, prop_price, battery_cost, esc_price_each


def run_real_design_cached(
    legacy,
    curve_cache,
    prop_entry,
    prop_catalog,
    motor,
    battery,
    esc_pool,
    fixed_mass,
    max_auw,
    min_twr,
    rpm_tolerance,
    esc_margin,
    price_resolver_obj,
    frame_prop_clearance_m,
    frame_structural_margin,
):
    """
    Fast exploratory aircraft evaluation using a persistent PyThrust response
    curve. Finalists are re-run by run_real_design_exact before final output.
    """
    voltage_escs = voltage_compatible_escs(
        esc_pool, battery.series_cells
    )
    if not voltage_escs:
        return reject("no_voltage_compatible_esc")

    try:
        curve, curve_key, cache_hit = curve_cache.get_or_build(
            legacy,
            prop_entry,
            motor,
            battery.nominal_voltage_v,
        )
    except Exception as exc:
        return reject("propulsion_curve_exception", exc)

    max_pt = max_propulsion_point(curve)
    max_motor_current = float(max_pt["motor_current_a"])

    esc = choose_esc_from_actual_current(
        esc_pool,
        battery.series_cells,
        max_motor_current,
        esc_margin,
        price_resolver_obj=price_resolver_obj,
        require_price=False,
    )
    if esc is None:
        return reject("no_esc_meets_actual_current")

    d_in = float(prop_entry.metadata.diameter_in)
    prop_mass_total = (
        N_ROTORS * float(prop_catalog["weight_g"]) / 1000.0
    )
    motor_mass_total = (
        N_ROTORS * float(motor.weight_g) / 1000.0
    )
    nonframe = (
        float(fixed_mass)
        + prop_mass_total
        + motor_mass_total
        + N_ROTORS * esc.mass_each_kg
        + battery.mass_kg
    )

    frame_info = estimate_frame_mass(
        nonframe,
        d_in,
        battery.mass_kg,
        min_twr,
        prop_clearance_m=frame_prop_clearance_m,
        structural_margin=frame_structural_margin,
    )
    if not frame_info.get("structurally_feasible", True):
        return reject("frame_structural_infeasible")

    frame_mass = float(frame_info["frame_mass_kg"])
    auw = nonframe + frame_mass
    if auw > max_auw + 1e-6:
        return reject("auw_limit")

    required_hover_n = auw * G / N_ROTORS
    hover = interpolate_propulsion_curve(
        curve, required_hover_n
    )
    if hover is None:
        return reject("insufficient_hover_thrust")

    max_thrust = float(max_pt["thrust_n"])
    twr = N_ROTORS * max_thrust / max(auw * G, 1e-9)
    if twr < min_twr - 1e-3:
        return reject("twr_limit")

    hover_power_total = (
        N_ROTORS * float(hover["battery_power_w"])
        + ACCESSORY_POWER_W
    )
    max_power_total = (
        N_ROTORS * float(max_pt["battery_power_w"])
        + ACCESSORY_POWER_W
    )
    hover_pack_current = (
        hover_power_total / battery.nominal_voltage_v
    )
    max_pack_current = (
        max_power_total / battery.nominal_voltage_v
    )

    if max_motor_current > float(motor.max_current) + 1e-6:
        return reject("motor_current_limit")
    if (
        esc.continuous_current_a
        < max_motor_current * esc_margin
    ):
        return reject("esc_current_limit")
    if (
        hover_pack_current
        > battery.max_cont_current_a + 1e-6
    ):
        return reject("battery_hover_current_limit")

    full_current_limit = (
        battery.max_burst_current_a
        if battery.max_burst_current_a is not None
        else battery.max_cont_current_a
    )
    if max_pack_current > full_current_limit + 1e-6:
        return reject("battery_full_current_limit")

    hover_rpm = float(hover["rpm"])
    max_rpm = float(max_pt["rpm"])
    rpm_levels = list(
        getattr(prop_entry, "rpm_levels", []) or []
    )
    if rpm_levels:
        lo, hi = min(rpm_levels), max(rpm_levels)
        if hover_rpm < lo * (1.0 - rpm_tolerance):
            return reject("hover_rpm_below_data")
        if max_rpm > hi * (1.0 + rpm_tolerance):
            return reject("max_rpm_above_data")

    endurance_min = (
        battery.energy_wh
        * battery.usable_fraction
        / max(hover_power_total, 1e-9)
        * 60.0
    )

    (
        motor_price,
        prop_price,
        battery_cost,
        esc_price_each,
    ) = _resolve_design_prices(
        price_resolver_obj,
        prop_entry,
        prop_catalog,
        motor,
        battery,
        esc,
    )

    motor_cost = (
        N_ROTORS * motor_price
        if motor_price is not None else None
    )
    prop_cost = (
        N_ROTORS * prop_price
        if prop_price is not None else None
    )
    esc_cost = (
        N_ROTORS * esc_price_each
        if esc_price_each is not None else None
    )
    known_parts = [
        motor_cost, prop_cost, battery_cost, esc_cost
    ]
    propulsion_known_cost = sum(
        x for x in known_parts if x is not None
    )
    coverage = (
        100.0 * sum(x is not None for x in known_parts) / 4.0
    )
    whole_cost = (
        propulsion_known_cost
        + FRAME_PLANNING_COST_USD
        + CAMERA_AVIONICS_PLANNING_COST_USD
        if coverage >= 100.0 else None
    )

    row = {
        "motor_id": motor.id,
        "motor_manufacturer": motor.manufacturer,
        "motor_model": motor.name,
        "motor_kv": float(motor.kv),
        "motor_mass_each_g": float(motor.weight_g),
        "motor_max_current_a": float(motor.max_current),
        "prop_id": prop_entry.metadata.id,
        "prop_model": prop_entry.metadata.model,
        "prop_diameter_in": float(
            prop_entry.metadata.diameter_in
        ),
        "prop_pitch_in": float(prop_entry.metadata.pitch_in),
        "prop_mass_each_g": float(prop_catalog["weight_g"]),
        "prop_price_each_usd": prop_price,
        "battery_id": battery.battery_id,
        "battery_type": battery.battery_type,
        "battery_manufacturer": battery.manufacturer,
        "battery_model": battery.model,
        "battery_topology": battery.topology,
        "battery_voltage_v": battery.nominal_voltage_v,
        "battery_capacity_ah": battery.capacity_ah,
        "battery_energy_wh": battery.energy_wh,
        "battery_mass_kg": battery.mass_kg,
        "battery_max_cont_current_a":
            battery.max_cont_current_a,
        "battery_price_usd": battery_cost,
        "battery_confidence": battery.confidence,
        "esc_id": esc.canonical_id,
        "esc_manufacturer": esc.manufacturer,
        "esc_model": esc.model,
        "esc_cont_current_a": esc.continuous_current_a,
        "esc_mass_each_g": 1000.0 * esc.mass_each_kg,
        "esc_price_each_usd": esc_price_each,
        "esc_confidence": esc.confidence,
        "fixed_mass_kg": fixed_mass,
        "frame_mass_kg": frame_mass,
        "frame_motor_spacing_m":
            frame_info["motor_spacing_m"],
        "frame_arm_radius_m": frame_info["arm_radius_m"],
        "frame_tip_to_tip_span_m":
            frame_info["estimated_tip_to_tip_span_m"],
        "frame_geometry_mass_kg":
            frame_info["geometry_mass_kg"],
        "frame_battery_mount_mass_kg":
            frame_info["battery_mount_mass_kg"],
        "frame_reinforcement_mass_kg":
            frame_info["reinforcement_mass_kg"],
        "frame_design_arm_root_moment_nm":
            frame_info["design_arm_root_moment_nm"],
        "frame_material": frame_info.get("frame_material"),
        "frame_arm_tube_od_mm": frame_info.get("arm_tube_od_mm"),
        "frame_arm_tube_wall_mm":
            frame_info.get("arm_tube_wall_mm"),
        "frame_arm_tube_length_m":
            frame_info.get("arm_tube_length_m"),
        "frame_arm_stress_mpa":
            frame_info.get("arm_stress_mpa"),
        "frame_arm_tip_deflection_mm":
            frame_info.get("arm_tip_deflection_mm"),
        "frame_arm_stress_utilization_pct":
            frame_info.get("arm_stress_utilization_pct"),
        "frame_arm_deflection_utilization_pct":
            frame_info.get("arm_deflection_utilization_pct"),
        "frame_arms_mass_kg": frame_info.get("arms_mass_kg"),
        "frame_center_plate_mass_kg":
            frame_info.get("center_plate_mass_kg"),
        "frame_battery_tray_mass_kg":
            frame_info.get("battery_tray_mass_kg"),
        "frame_hardware_joint_mass_kg":
            frame_info.get("hardware_joint_mass_kg"),
        "auw_kg": auw,
        "hover_throttle_pct":
            100.0 * float(hover["throttle"]),
        "hover_rpm": hover_rpm,
        "max_rpm": max_rpm,
        "hover_power_total_w": hover_power_total,
        "hover_pack_current_a": hover_pack_current,
        "max_pack_current_a": max_pack_current,
        "max_motor_current_actual_a": max_motor_current,
        "max_twr": twr,
        "endurance_min": endurance_min,
        "known_component_cost_usd":
            propulsion_known_cost,
        "cost_coverage_pct": coverage,
        "propulsion_cost_usd": propulsion_known_cost,
        "propulsion_cost_coverage_pct": coverage,
        "frame_cost_estimate_usd":
            FRAME_PLANNING_COST_USD,
        "fixed_nonpropulsion_cost_usd":
            CAMERA_AVIONICS_PLANNING_COST_USD,
        "whole_aircraft_estimated_cost_usd":
            whole_cost,
        "budget_mode": "off",
        "budget_limit_usd": None,
        "motor_price_each_usd": motor_price,
        "motor_cost_total_usd": motor_cost,
        "prop_cost_total_usd": prop_cost,
        "battery_cost_usd": battery_cost,
        "esc_cost_total_usd": esc_cost,
        "evaluation_mode": "cached_explore",
        "propulsion_curve_cache_key": curve_key,
        "propulsion_curve_cache_hit": bool(cache_hit),
    }
    return row


def run_real_design_exact(
    legacy,
    prop_entry,
    prop_catalog,
    motor,
    battery,
    esc_pool,
    fixed_mass,
    max_auw,
    min_twr,
    rpm_tolerance,
    esc_margin,
    price_resolver_obj,
    budget_mode,
    max_budget_usd,
    frame_prop_clearance_m,
    frame_structural_margin,
):
    budget_mode = str(budget_mode or "off")
    strict_pricing = budget_mode in {
        "propulsion_strict", "whole_aircraft_strict"
    }

    voltage_escs = voltage_compatible_escs(
        esc_pool, battery.series_cells
    )
    if not voltage_escs:
        return reject("no_voltage_compatible_esc")

    # ---------------------------------------------------------------
    # Resolve prices that are independent of the eventual ESC choice.
    # ---------------------------------------------------------------
    motor_hit = price_resolver_obj.resolve(
        "motor",
        source_record_id=motor.id,
        manufacturer=motor.manufacturer,
        model=motor.name,
    )
    motor_price = motor_hit["price_usd"] if motor_hit else None

    prop_price = optional_float(prop_catalog.get("price_usd"))
    if prop_price is None:
        prop_hit = price_resolver_obj.resolve(
            "prop",
            manufacturer=getattr(
                prop_entry.metadata, "manufacturer", "APC"
            ),
            model=prop_entry.metadata.model,
        )
        prop_price = prop_hit["price_usd"] if prop_hit else None

    battery_cost = battery.price_usd
    if battery_cost is None:
        if battery.battery_type == "Custom Li-ion":
            parts = str(battery.battery_id).split(":")
            cell_canonical = parts[1] if len(parts) >= 3 else None
            cell_hit = price_resolver_obj.resolve(
                "battery_cell",
                canonical_id=cell_canonical,
                manufacturer=battery.manufacturer,
                model=battery.model,
            )
            if cell_hit and battery.parallel_cells:
                battery_cost = (
                    cell_hit["price_usd"]
                    * battery.series_cells
                    * battery.parallel_cells
                )
        else:
            parts = str(battery.battery_id).split(":")
            pack_canonical = parts[1] if len(parts) >= 2 else None
            pack_hit = price_resolver_obj.resolve(
                "battery_pack",
                canonical_id=pack_canonical,
                manufacturer=battery.manufacturer,
                model=battery.model,
            )
            if pack_hit:
                battery_cost = pack_hit["price_usd"]

    if strict_pricing and (
        motor_price is None
        or prop_price is None
        or battery_cost is None
    ):
        return reject("incomplete_propulsion_pricing")

    # Obvious lower-bound price check before any OpenMDAO work.
    partial_cost = (
        (N_ROTORS * motor_price if motor_price is not None else 0.0)
        + (N_ROTORS * prop_price if prop_price is not None else 0.0)
        + (battery_cost if battery_cost is not None else 0.0)
    )
    if (
        strict_pricing
        and max_budget_usd is not None
        and partial_cost > max_budget_usd
    ):
        return reject("budget_limit_precheck")

    # ---------------------------------------------------------------
    # Frame allowance.
    # ---------------------------------------------------------------
    d_in = float(prop_entry.metadata.diameter_in)
    prop_mass_total = (
        N_ROTORS * float(prop_catalog["weight_g"]) / 1000.0
    )
    motor_mass_total = (
        N_ROTORS * float(motor.weight_g) / 1000.0
    )

    frame_kwargs = {
        "prop_clearance_m": frame_prop_clearance_m,
        "structural_margin": frame_structural_margin,
    }

    # ---------------------------------------------------------------
    # Pass 1: use the lightest voltage-compatible ESC to estimate actual
    # full-throttle motor current.
    # ---------------------------------------------------------------
    provisional_esc = voltage_escs[0]
    provisional_nonframe = (
        float(fixed_mass)
        + prop_mass_total
        + motor_mass_total
        + N_ROTORS * provisional_esc.mass_each_kg
        + battery.mass_kg
    )
    provisional_frame = estimate_frame_mass(
        provisional_nonframe,
        d_in,
        battery.mass_kg,
        min_twr,
        **frame_kwargs,
    )
    if not provisional_frame.get("structurally_feasible", True):
        return reject("frame_structural_infeasible")
    provisional_frame_mass = provisional_frame["frame_mass_kg"]
    provisional_auw = (
        provisional_nonframe + provisional_frame_mass
    )
    if provisional_auw > max_auw + 1e-6:
        return reject("auw_limit")

    try:
        pre_prob = build_real_problem(
            legacy,
            prop_entry,
            prop_catalog,
            motor,
            battery,
            provisional_esc,
            fixed_mass,
            provisional_frame_mass,
        )
    except Exception as exc:
        return reject("openmdao_setup_exception", exc)

    ok, failure = solve_hover_throttle(pre_prob)
    if not ok:
        reason, detail = failure
        return reject(reason, detail)

    try:
        preliminary_max_motor_current = scalar(
            pre_prob, "max_prop.motor_current_a"
        )
    except Exception as exc:
        return reject("could_not_read_motor_current", exc)

    # ---------------------------------------------------------------
    # Select the real ESC using actual modeled motor current.
    # In strict budget mode it must also have a known price.
    # ---------------------------------------------------------------
    esc = choose_esc_from_actual_current(
        esc_pool,
        battery.series_cells,
        preliminary_max_motor_current,
        esc_margin,
        price_resolver_obj=price_resolver_obj,
        require_price=strict_pricing,
    )
    if esc is None:
        return reject(
            "no_priced_esc_meets_actual_current"
            if strict_pricing
            else "no_esc_meets_actual_current"
        )

    # ---------------------------------------------------------------
    # Pass 2: recompute frame and physics using the selected ESC mass.
    # ---------------------------------------------------------------
    final_nonframe = (
        float(fixed_mass)
        + prop_mass_total
        + motor_mass_total
        + N_ROTORS * esc.mass_each_kg
        + battery.mass_kg
    )
    frame_info = estimate_frame_mass(
        final_nonframe,
        d_in,
        battery.mass_kg,
        min_twr,
        **frame_kwargs,
    )
    if not frame_info.get("structurally_feasible", True):
        return reject("frame_structural_infeasible")
    frame_mass = frame_info["frame_mass_kg"]
    final_auw = final_nonframe + frame_mass
    if final_auw > max_auw + 1e-6:
        return reject("auw_limit")

    try:
        prob = build_real_problem(
            legacy,
            prop_entry,
            prop_catalog,
            motor,
            battery,
            esc,
            fixed_mass,
            frame_mass,
        )
    except Exception as exc:
        return reject("openmdao_setup_exception", exc)

    ok, failure = solve_hover_throttle(prob)
    if not ok:
        reason, detail = failure
        return reject(reason, detail)

    try:
        auw = scalar(prob, "auw_kg")
        residual = scalar(prob, "aircraft.hover_residual_n")
        twr = scalar(prob, "aircraft.max_thrust_to_weight")
        motor_current_max = scalar(
            prob, "max_prop.motor_current_a"
        )
        hover_rpm = scalar(prob, "hover_prop.rpm")
        max_rpm = scalar(prob, "max_prop.rpm")
        hover_pack_current = scalar(
            prob, "aircraft.hover_pack_current_a"
        )
        max_pack_current = scalar(
            prob, "aircraft.max_pack_current_a"
        )
    except Exception as exc:
        return reject("could_not_read_solution", exc)

    # ---------------------------------------------------------------
    # Engineering constraints.
    # ---------------------------------------------------------------
    if auw > max_auw + 1e-3:
        return reject("auw_limit")
    if abs(residual) > 0.05:
        return reject("hover_balance")
    if twr < min_twr - 1e-3:
        return reject("twr_limit")

    try:
        if scalar(prob, "hover_prop.is_feasible") < 0.5:
            return reject("pythrust_hover_infeasible")
        if scalar(prob, "max_prop.is_feasible") < 0.5:
            return reject("pythrust_max_infeasible")
    except Exception:
        pass

    if motor_current_max > float(motor.max_current) + 1e-6:
        return reject("motor_current_limit")

    if (
        esc.continuous_current_a
        < motor_current_max * esc_margin
    ):
        return reject("esc_current_limit")

    if (
        hover_pack_current
        > battery.max_cont_current_a + 1e-6
    ):
        return reject("battery_hover_current_limit")

    full_current_limit = (
        battery.max_burst_current_a
        if battery.max_burst_current_a is not None
        else battery.max_cont_current_a
    )
    if max_pack_current > full_current_limit + 1e-6:
        return reject("battery_full_current_limit")

    rpm_levels = list(
        getattr(prop_entry, "rpm_levels", []) or []
    )
    if rpm_levels:
        lo, hi = min(rpm_levels), max(rpm_levels)
        if hover_rpm < lo * (1.0 - rpm_tolerance):
            return reject("hover_rpm_below_data")
        if max_rpm > hi * (1.0 + rpm_tolerance):
            return reject("max_rpm_above_data")

    # ---------------------------------------------------------------
    # Complete pricing and enforce selected budget scope.
    # ---------------------------------------------------------------
    esc_price_each = esc.price_each_usd
    if esc_price_each is None:
        esc_hit = price_resolver_obj.resolve(
            "esc",
            canonical_id=esc.canonical_id,
            manufacturer=esc.manufacturer,
            model=esc.model,
        )
        esc_price_each = (
            esc_hit["price_usd"] if esc_hit else None
        )

    motor_cost = (
        N_ROTORS * motor_price
        if motor_price is not None else None
    )
    prop_cost = (
        N_ROTORS * prop_price
        if prop_price is not None else None
    )
    esc_cost = (
        N_ROTORS * esc_price_each
        if esc_price_each is not None else None
    )

    propulsion_cost_parts = {
        "motors": motor_cost,
        "props": prop_cost,
        "battery": battery_cost,
        "escs": esc_cost,
    }
    propulsion_known_cost = sum(
        x for x in propulsion_cost_parts.values()
        if x is not None
    )
    priced_categories = sum(
        x is not None
        for x in propulsion_cost_parts.values()
    )
    propulsion_cost_coverage = (
        100.0 * priced_categories / 4.0
    )

    frame_cost = FRAME_PLANNING_COST_USD
    camera_avionics_cost = CAMERA_AVIONICS_PLANNING_COST_USD
    whole_aircraft_estimated_cost = (
        propulsion_known_cost
        + frame_cost
        + camera_avionics_cost
        if priced_categories == 4
        else None
    )

    if budget_mode == "propulsion_strict":
        if priced_categories < 4:
            return reject("incomplete_propulsion_pricing")
        if (
            max_budget_usd is not None
            and propulsion_known_cost > max_budget_usd
        ):
            return reject("budget_limit")
    elif budget_mode == "whole_aircraft_strict":
        if priced_categories < 4:
            return reject("incomplete_propulsion_pricing")
        if (
            max_budget_usd is not None
            and whole_aircraft_estimated_cost is not None
            and whole_aircraft_estimated_cost > max_budget_usd
        ):
            return reject("budget_limit")
    elif budget_mode == "known_only":
        if (
            max_budget_usd is not None
            and propulsion_known_cost > max_budget_usd
        ):
            return reject("budget_limit_known_cost_only")

    return {
        "motor_id": motor.id,
        "motor_manufacturer": motor.manufacturer,
        "motor_model": motor.name,
        "motor_kv": float(motor.kv),
        "motor_mass_each_g": float(motor.weight_g),
        "motor_max_current_a": float(motor.max_current),
        "prop_id": prop_entry.metadata.id,
        "prop_model": prop_entry.metadata.model,
        "prop_diameter_in": float(
            prop_entry.metadata.diameter_in
        ),
        "prop_pitch_in": float(prop_entry.metadata.pitch_in),
        "prop_mass_each_g": float(prop_catalog["weight_g"]),
        "prop_price_each_usd": prop_price,
        "battery_id": battery.battery_id,
        "battery_type": battery.battery_type,
        "battery_manufacturer": battery.manufacturer,
        "battery_model": battery.model,
        "battery_topology": battery.topology,
        "battery_voltage_v": battery.nominal_voltage_v,
        "battery_capacity_ah": battery.capacity_ah,
        "battery_energy_wh": battery.energy_wh,
        "battery_mass_kg": battery.mass_kg,
        "battery_max_cont_current_a":
            battery.max_cont_current_a,
        "battery_price_usd": battery_cost,
        "battery_confidence": battery.confidence,
        "esc_id": esc.canonical_id,
        "esc_manufacturer": esc.manufacturer,
        "esc_model": esc.model,
        "esc_cont_current_a": esc.continuous_current_a,
        "esc_mass_each_g": 1000.0 * esc.mass_each_kg,
        "esc_price_each_usd": esc_price_each,
        "esc_confidence": esc.confidence,
        "fixed_mass_kg": fixed_mass,
        "frame_mass_kg": frame_mass,
        "frame_motor_spacing_m":
            frame_info["motor_spacing_m"],
        "frame_arm_radius_m": frame_info["arm_radius_m"],
        "frame_tip_to_tip_span_m":
            frame_info["estimated_tip_to_tip_span_m"],
        "frame_geometry_mass_kg":
            frame_info["geometry_mass_kg"],
        "frame_battery_mount_mass_kg":
            frame_info["battery_mount_mass_kg"],
        "frame_reinforcement_mass_kg":
            frame_info["reinforcement_mass_kg"],
        "frame_design_arm_root_moment_nm":
            frame_info["design_arm_root_moment_nm"],
        "frame_material": frame_info.get("frame_material"),
        "frame_arm_tube_od_mm": frame_info.get("arm_tube_od_mm"),
        "frame_arm_tube_wall_mm": frame_info.get("arm_tube_wall_mm"),
        "frame_arm_tube_length_m": frame_info.get("arm_tube_length_m"),
        "frame_arm_stress_mpa": frame_info.get("arm_stress_mpa"),
        "frame_arm_tip_deflection_mm":
            frame_info.get("arm_tip_deflection_mm"),
        "frame_arm_stress_utilization_pct":
            frame_info.get("arm_stress_utilization_pct"),
        "frame_arm_deflection_utilization_pct":
            frame_info.get("arm_deflection_utilization_pct"),
        "frame_arms_mass_kg": frame_info.get("arms_mass_kg"),
        "frame_center_plate_mass_kg":
            frame_info.get("center_plate_mass_kg"),
        "frame_battery_tray_mass_kg":
            frame_info.get("battery_tray_mass_kg"),
        "frame_hardware_joint_mass_kg":
            frame_info.get("hardware_joint_mass_kg"),
        "auw_kg": auw,
        "hover_throttle_pct":
            100.0 * scalar(prob, "hover_throttle"),
        "hover_rpm": hover_rpm,
        "max_rpm": max_rpm,
        "hover_power_total_w":
            scalar(prob, "aircraft.total_hover_power_w"),
        "hover_pack_current_a": hover_pack_current,
        "max_pack_current_a": max_pack_current,
        "max_motor_current_actual_a": motor_current_max,
        "max_twr": twr,
        "endurance_min":
            scalar(prob, "aircraft.endurance_min"),
        "known_component_cost_usd":
            propulsion_known_cost,
        "cost_coverage_pct":
            propulsion_cost_coverage,
        "propulsion_cost_usd":
            propulsion_known_cost,
        "propulsion_cost_coverage_pct":
            propulsion_cost_coverage,
        "frame_cost_estimate_usd": frame_cost,
        "fixed_nonpropulsion_cost_usd":
            camera_avionics_cost,
        "whole_aircraft_estimated_cost_usd":
            whole_aircraft_estimated_cost,
        "budget_mode": budget_mode,
        "budget_limit_usd": max_budget_usd,
        "motor_price_each_usd": motor_price,
        "motor_cost_total_usd": motor_cost,
        "prop_cost_total_usd": prop_cost,
        "battery_cost_usd": battery_cost,
        "esc_cost_total_usd": esc_cost,
        "evaluation_mode": "exact_validated",
        "propulsion_curve_cache_key": None,
        "propulsion_curve_cache_hit": None,
    }




DEFAULT_DECISION_WEIGHTS = {
    "cost": 35.0,
    "endurance": 25.0,
    "weight": 17.0,
    "quality": 13.0,
    "complexity": 10.0,
}


def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(value)))


def _confidence_numeric(value):
    text = str(value or "").strip().lower()
    if text == "ready":
        return 100.0
    if text == "screening":
        return 80.0
    if text == "discovery":
        return 60.0
    return 70.0


def _parse_topology_count(topology):
    m = re.search(
        r"(\d+)\s*S\s*[xX×]?\s*(\d+)\s*P",
        str(topology or ""),
        flags=re.I,
    )
    if not m:
        return None
    return int(m.group(1)) * int(m.group(2))


def _robust_scores(values, higher_is_better=True):
    arr = np.asarray(values, dtype=float)
    finite = np.isfinite(arr)
    out = np.full(arr.shape, 50.0, dtype=float)
    if not finite.any():
        return out.tolist()

    good = arr[finite]
    lo = float(np.percentile(good, 5))
    hi = float(np.percentile(good, 95))
    if abs(hi - lo) < 1e-12:
        out[finite] = 100.0
        return out.tolist()

    norm = (arr[finite] - lo) / (hi - lo)
    norm = np.clip(norm, 0.0, 1.0)
    if not higher_is_better:
        norm = 1.0 - norm
    out[finite] = 100.0 * norm
    return out.tolist()


def _quality_score(row):
    # Robustness/integration quality. All candidates share the same camera/
    # avionics mission functionality, so the differentiating quality factors
    # are propulsion margin, operating point, and data confidence.
    twr = float(row.get("max_twr") or 0.0)
    twr_score = _clamp(70.0 + 150.0 * (twr - 1.8))

    throttle = float(row.get("hover_throttle_pct") or 50.0)
    throttle_score = _clamp(
        100.0 - 2.0 * abs(throttle - 45.0),
        45.0,
        100.0,
    )

    motor_i = float(row.get("max_motor_current_actual_a") or 0.0)
    esc_i = float(row.get("esc_cont_current_a") or 0.0)
    esc_ratio = esc_i / max(motor_i, 1e-9)
    esc_score = _clamp(
        70.0 + 85.7 * (esc_ratio - 1.15),
        50.0,
        100.0,
    )

    pack_i = float(row.get("max_pack_current_a") or 0.0)
    batt_i = float(row.get("battery_max_cont_current_a") or 0.0)
    batt_ratio = batt_i / max(pack_i, 1e-9)
    battery_margin_score = _clamp(
        60.0 + 133.3 * (batt_ratio - 1.0),
        40.0,
        100.0,
    )

    confidence_score = (
        _confidence_numeric(row.get("battery_confidence"))
        + _confidence_numeric(row.get("esc_confidence"))
    ) / 2.0

    return (
        0.25 * twr_score
        + 0.20 * throttle_score
        + 0.20 * esc_score
        + 0.20 * battery_margin_score
        + 0.15 * confidence_score
    )


def _complexity_score(row):
    # 100 = easiest to build. Shared DIY camera/frame work is reported in the
    # part-count fields but does not distort comparative ranking because every
    # candidate needs those subsystems.
    score = 100.0

    battery_type = str(row.get("battery_type") or "")
    custom_battery = "custom" in battery_type.lower()
    cell_count = _parse_topology_count(row.get("battery_topology"))

    if custom_battery:
        score -= 15.0
        if cell_count:
            score -= min(25.0, max(0, cell_count - 8) * 0.55)

    span = float(row.get("frame_tip_to_tip_span_m") or 0.0)
    if span > 0.8:
        score -= min(15.0, (span - 0.8) * 25.0)

    prop = float(row.get("prop_diameter_in") or 0.0)
    if prop > 18.0:
        score -= min(8.0, (prop - 18.0) * 1.2)

    tube_od = float(row.get("frame_arm_tube_od_mm") or 0.0)
    if tube_od > 20.0:
        score -= min(8.0, (tube_od - 20.0) * 0.8)

    return _clamp(score, 25.0, 100.0)


def add_decision_matrix(rows, weights=None):
    """
    Add an auditable weighted-decision score to every valid design.

    Default priorities:
      cost 35%, endurance 25%, weight 17%, quality 13%, complexity 10%.

    Cost includes:
      $220 frame planning allowance
      $700 DIY camera + avionics planning allowance
      actual propulsion prices when known
      explicit fallback estimates for missing propulsion categories.
    """
    if not rows:
        return rows

    weights = dict(weights or DEFAULT_DECISION_WEIGHTS)
    total_w = sum(float(v) for v in weights.values())
    weights = {
        k: 100.0 * float(v) / max(total_w, 1e-9)
        for k, v in weights.items()
    }

    for row in rows:
        estimated_categories = []

        motor_cost = optional_float(row.get("motor_cost_total_usd"))
        prop_cost = optional_float(row.get("prop_cost_total_usd"))
        battery_cost = optional_float(row.get("battery_cost_usd"))
        esc_cost = optional_float(row.get("esc_cost_total_usd"))

        if motor_cost is None:
            motor_cost = FALLBACK_PROPULSION_COST_USD["motors"]
            estimated_categories.append("motors")
        if prop_cost is None:
            prop_cost = FALLBACK_PROPULSION_COST_USD["props"]
            estimated_categories.append("props")
        if battery_cost is None:
            battery_cost = FALLBACK_PROPULSION_COST_USD["battery"]
            estimated_categories.append("battery")
        if esc_cost is None:
            esc_cost = FALLBACK_PROPULSION_COST_USD["escs"]
            estimated_categories.append("escs")

        estimated_propulsion = (
            motor_cost + prop_cost + battery_cost + esc_cost
        )
        total_cost = (
            FIXED_PLANNING_COST_USD + estimated_propulsion
        )

        cell_count = _parse_topology_count(
            row.get("battery_topology")
        )
        custom_battery = (
            "custom" in str(row.get("battery_type") or "").lower()
        )
        # Approximate assembly-level part count; fasteners are grouped rather
        # than counted individually.
        part_count = (
            4 + 4 + 4        # motors, props, ESCs
            + 4 + 2 + 1      # arms, center plates, battery tray
            + 8              # grouped mounts/hardware/landing pieces
            + 8              # DIY camera/avionics modules and mounting
            + (cell_count if custom_battery and cell_count else 1)
        )
        diy_subsystems = 2 + (1 if custom_battery else 0)

        row["decision_frame_allowance_usd"] = FRAME_PLANNING_COST_USD
        row["decision_camera_avionics_allowance_usd"] = (
            CAMERA_AVIONICS_PLANNING_COST_USD
        )
        row["decision_propulsion_estimated_cost_usd"] = (
            estimated_propulsion
        )
        row["decision_total_estimated_cost_usd"] = total_cost
        row["decision_price_estimated_categories"] = (
            ", ".join(estimated_categories)
            if estimated_categories else "none"
        )
        row["decision_price_actual_categories"] = (
            4 - len(estimated_categories)
        )
        row["decision_estimated_part_count"] = part_count
        row["decision_diy_subsystems"] = diy_subsystems
        row["decision_quality_score"] = _quality_score(row)
        row["decision_complexity_score"] = _complexity_score(row)

    cost_scores = _robust_scores(
        [r["decision_total_estimated_cost_usd"] for r in rows],
        higher_is_better=False,
    )
    endurance_scores = _robust_scores(
        [r["endurance_min"] for r in rows],
        higher_is_better=True,
    )
    weight_scores = _robust_scores(
        [r["auw_kg"] for r in rows],
        higher_is_better=False,
    )

    for row, c, e, w in zip(
        rows, cost_scores, endurance_scores, weight_scores
    ):
        row["decision_cost_score"] = c
        row["decision_endurance_score"] = e
        row["decision_weight_score"] = w
        q = row["decision_quality_score"]
        x = row["decision_complexity_score"]

        row["decision_matrix_score"] = (
            weights["cost"] * c
            + weights["endurance"] * e
            + weights["weight"] * w
            + weights["quality"] * q
            + weights["complexity"] * x
        ) / 100.0

        row["decision_weight_cost_pct"] = weights["cost"]
        row["decision_weight_endurance_pct"] = weights["endurance"]
        row["decision_weight_aircraft_mass_pct"] = weights["weight"]
        row["decision_weight_quality_pct"] = weights["quality"]
        row["decision_weight_complexity_pct"] = weights["complexity"]

    rows.sort(
        key=lambda r: (
            -r["decision_matrix_score"],
            r["decision_total_estimated_cost_usd"],
            -r["endurance_min"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["decision_rank"] = rank

    return rows


def pareto_frontier(rows):
    """
    Mass/endurance Pareto frontier.

    A design is dominated when another valid design is at least as light and
    at least as long-endurance, with a strict improvement in one of those two
    metrics. Cost is intentionally excluded here because many records still
    have incomplete cost coverage.
    """
    frontier = []
    for a in rows:
        dominated = False
        for b in rows:
            if a is b:
                continue
            if (
                b["endurance_min"] >= a["endurance_min"]
                and b["auw_kg"] <= a["auw_kg"]
                and (
                    b["endurance_min"] > a["endurance_min"]
                    or b["auw_kg"] < a["auw_kg"]
                )
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(a)
    return frontier


def save_results(rows, settings):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()

    (RESULTS_DIR / "run_settings.json").write_text(
        json.dumps(settings, indent=2),
        encoding="utf-8",
    )

    # Remove stale artifacts from a prior run so the GUI never mixes runs.
    for stale_name in (
        "rejection_summary.csv",
        "pareto_frontier.csv",
        "integrated_real_component_results.csv",
        "RUN_SUMMARY.txt",
        "exception_details.csv",
        "decision_matrix_top25.csv",
        "decision_matrix_all.csv",
    ):
        stale = RESULTS_DIR / stale_name
        if stale.exists():
            try:
                stale.unlink()
            except Exception:
                pass

    if not rows:
        rejection_rows = [
            {"reason": reason, "count": count}
            for reason, count in sorted(
                REJECTION_COUNTS.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        diag_path = RESULTS_DIR / "rejection_summary.csv"
        with diag_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["reason", "count"])
            writer.writeheader()
            writer.writerows(rejection_rows)

        exception_rows = [
            {"detail": detail, "count": count}
            for detail, count in sorted(
                EXCEPTION_DETAILS.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        with (RESULTS_DIR / "exception_details.csv").open(
            "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=["detail", "count"])
            writer.writeheader()
            writer.writerows(exception_rows)

        # Always create the main result CSV so the GUI can distinguish
        # "run completed with zero valid designs" from "optimizer never ran".
        (RESULTS_DIR / "integrated_real_component_results.csv").write_text(
            "status\nNO_VALID_DESIGNS\n",
            encoding="utf-8",
        )

        lines = [
            "INTEGRATED REAL-COMPONENT OPTIMIZER",
            "",
            "NO VALID DESIGNS SURVIVED THE CURRENT CONSTRAINTS.",
            "",
            "This is a completed optimization run, not a Results-tab error.",
            "",
            "REJECTION COUNTS",
        ]
        if rejection_rows:
            lines.extend(
                f"  {r['reason']}: {r['count']}"
                for r in rejection_rows
            )
        else:
            lines.append(
                "  No detailed solves reached a rejection check. "
                "This usually means no real battery candidates were generated "
                "or the parametric finalists could not be matched."
            )

        lines += [
            "",
            "See rejection_summary.csv for the machine-readable breakdown.",
            "See exception_details.csv for exact exception text.",
        ]
        (RESULTS_DIR / "RUN_SUMMARY.txt").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )
        return

    rows = add_decision_matrix(
        rows,
        settings.get("decision_weights") or DEFAULT_DECISION_WEIGHTS,
    )
    decision_rows = list(rows)

    # Preserve the full valid result table ordered by decision score.
    decision_all_path = RESULTS_DIR / "decision_matrix_all.csv"
    with decision_all_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=list(decision_rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(decision_rows)

    decision_top25_path = RESULTS_DIR / "decision_matrix_top25.csv"
    with decision_top25_path.open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=list(decision_rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(decision_rows[:25])

    # Existing Pareto and raw result files remain useful for engineering review.
    endurance_sorted = sorted(
        decision_rows,
        key=lambda r: (-r["endurance_min"], r["auw_kg"]),
    )
    frontier = pareto_frontier(endurance_sorted)

    csv_path = RESULTS_DIR / "integrated_real_component_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=list(decision_rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(decision_rows[:TOP_RESULTS_TO_SAVE])

    frontier_path = RESULTS_DIR / "pareto_frontier.csv"
    with frontier_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(sorted(frontier, key=lambda r: r["auw_kg"]))

    best_decision = decision_rows[0]
    best = endurance_sorted[0]
    light_45 = [
        r for r in endurance_sorted if r["endurance_min"] >= 45.0
    ]
    best_light_45 = min(light_45, key=lambda r: r["auw_kg"]) if light_45 else None

    summary = [
        "INTEGRATED REAL-COMPONENT OPTIMIZER",
        "",
        f"Valid detailed designs: {len(rows)}",
        f"Pareto-front designs: {len(frontier)}",
        "",
        "TOP WEIGHTED DECISION-MATRIX DESIGN",
        f"  Matrix score: {best_decision['decision_matrix_score']:.1f}/100",
        f"  Estimated total cost: ${best_decision['decision_total_estimated_cost_usd']:.2f}",
        f"  Endurance: {best_decision['endurance_min']:.1f} min",
        f"  AUW: {best_decision['auw_kg']:.3f} kg",
        f"  Quality score: {best_decision['decision_quality_score']:.1f}/100",
        f"  Buildability score: {best_decision['decision_complexity_score']:.1f}/100",
        f"  Motor: {best_decision['motor_manufacturer']} {best_decision['motor_model']}",
        f"  Prop: {best_decision['prop_model']}",
        f"  Battery: {best_decision['battery_manufacturer']} {best_decision['battery_model']} {best_decision['battery_topology']}",
        f"  ESC: {best_decision['esc_manufacturer']} {best_decision['esc_model']}",
        "",
        "DEFAULT DECISION WEIGHTS",
        "  Cost: 35%",
        "  Endurance: 25%",
        "  Aircraft weight: 17%",
        "  Design quality / robustness: 13%",
        "  Complexity / buildability: 10%",
        "",
        "BEST ENDURANCE",
        f"  Endurance: {best['endurance_min']:.1f} min",
        f"  AUW: {best['auw_kg']:.3f} kg",
        f"  Motor: {best['motor_manufacturer']} {best['motor_model']}",
        f"  Prop: {best['prop_model']} ({best['prop_diameter_in']:.2f}x{best['prop_pitch_in']:.2f})",
        f"  Battery: {best['battery_manufacturer']} {best['battery_model']} {best['battery_topology']}",
        f"  ESC: {best['esc_manufacturer']} {best['esc_model']}",
        f"  T/W: {best['max_twr']:.2f}",
        f"  Known cost: ${best['known_component_cost_usd']:.2f} "
        f"({best['cost_coverage_pct']:.0f}% of propulsion categories costed)",
    ]
    if best_light_45:
        summary += [
            "",
            "LIGHTEST DESIGN >= 45 MIN",
            f"  Endurance: {best_light_45['endurance_min']:.1f} min",
            f"  AUW: {best_light_45['auw_kg']:.3f} kg",
            f"  Motor: {best_light_45['motor_manufacturer']} {best_light_45['motor_model']}",
            f"  Prop: {best_light_45['prop_model']}",
            f"  Battery: {best_light_45['battery_manufacturer']} "
            f"{best_light_45['battery_model']} {best_light_45['battery_topology']}",
            f"  ESC: {best_light_45['esc_manufacturer']} {best_light_45['esc_model']}",
        ]

    summary += [
        "",
        "MODEL LIMITATIONS",
        "  Battery voltage sag / SOC-dependent resistance are not yet modeled.",
        "  Frame arms are concept-sized from carbon-tube stress/deflection; center/joint details still require CAD/FEA.",
        "  Decision cost includes $220 frame + $700 DIY camera/avionics planning allowances.",
        "  Missing propulsion prices use explicit fallback estimates and are labeled in the matrix.",
        "  Prop structural RPM limits are not available for all props.",
        "  Cost is explicitly coverage-tracked; unknown costs are not silently treated as $0.",
    ]

    (RESULTS_DIR / "RUN_SUMMARY.txt").write_text("\n".join(summary), encoding="utf-8")



def _file_signature(path):
    p = Path(path)
    if not p.exists():
        return None
    st = p.stat()
    return {
        "path": str(p),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def physical_pool_key(settings):
    blob = json.dumps(
        settings, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_physical_pool(expected_key):
    if not PHYSICAL_POOL_CSV.exists() or not PHYSICAL_POOL_META.exists():
        return None
    try:
        meta = json.loads(
            PHYSICAL_POOL_META.read_text(encoding="utf-8")
        )
        if meta.get("physical_pool_key") != expected_key:
            return None
        with PHYSICAL_POOL_CSV.open(
            newline="", encoding="utf-8"
        ) as f:
            rows = list(csv.DictReader(f))
        # Convert numeric-looking CSV values back to floats where possible.
        converted = []
        for row in rows:
            out = {}
            for k, v in row.items():
                if v is None:
                    out[k] = None
                    continue
                s = str(v).strip()
                if s == "":
                    out[k] = None
                    continue
                if s.lower() == "true":
                    out[k] = True
                    continue
                if s.lower() == "false":
                    out[k] = False
                    continue
                try:
                    out[k] = float(s)
                except Exception:
                    out[k] = s
            converted.append(out)
        return converted
    except Exception:
        return None


def save_physical_pool(rows, key, settings):
    if not rows:
        return
    PHYSICAL_POOL_CSV.parent.mkdir(
        parents=True, exist_ok=True
    )
    with PHYSICAL_POOL_CSV.open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=list(rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(rows)
    PHYSICAL_POOL_META.write_text(
        json.dumps(
            {
                "physical_pool_key": key,
                "created_unix_s": time.time(),
                "validated_designs": len(rows),
                "settings": settings,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def validation_candidate_ids(scored_rows):
    """
    Select a broad exact-validation pool without validating every explored row.
    It includes the strongest overall matrix designs plus extremes in every
    decision criterion so reasonable future reweighting remains well covered.
    """
    if not scored_rows:
        return set()

    chosen = set()

    def add(rows, n):
        for r in rows[:n]:
            chosen.add(str(r["explore_candidate_id"]))

    add(
        sorted(
            scored_rows,
            key=lambda r: -r["decision_matrix_score"],
        ),
        90,
    )
    add(
        sorted(
            scored_rows,
            key=lambda r: -r["endurance_min"],
        ),
        25,
    )
    add(
        sorted(
            scored_rows,
            key=lambda r: r["auw_kg"],
        ),
        25,
    )
    add(
        sorted(
            scored_rows,
            key=lambda r: r["decision_total_estimated_cost_usd"],
        ),
        25,
    )
    add(
        sorted(
            scored_rows,
            key=lambda r: -r["decision_quality_score"],
        ),
        25,
    )
    add(
        sorted(
            scored_rows,
            key=lambda r: -r["decision_complexity_score"],
        ),
        25,
    )

    return chosen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-parametric", action="store_true")
    parser.add_argument("--force-physics", action="store_true")
    parser.add_argument("--motor-mode", choices=["optimize","lock"], default="optimize")
    parser.add_argument("--motor-id", default=None)
    parser.add_argument("--prop-mode", choices=["optimize","lock"], default="optimize")
    parser.add_argument("--prop-id", default=None)
    parser.add_argument("--battery-mode", choices=["optimize","commercial","cell"], default="optimize")
    parser.add_argument("--battery-id", default=None)
    parser.add_argument("--esc-mode", choices=["optimize","lock"], default="optimize")
    parser.add_argument("--esc-id", default=None)
    parser.add_argument("--fixed-mass", type=float, default=None)
    parser.add_argument("--max-auw", type=float, default=10.0)
    parser.add_argument("--min-twr", type=float, default=1.8)
    parser.add_argument("--pack-overhead", type=float, default=DEFAULT_PACK_OVERHEAD_FRACTION)
    parser.add_argument("--esc-margin", type=float, default=DEFAULT_ESC_CURRENT_MARGIN)
    parser.add_argument("--rpm-tolerance", type=float, default=DEFAULT_RPM_DATA_TOLERANCE)
    parser.add_argument("--search-depth", choices=["standard","thorough","deep"], default="thorough")
    parser.add_argument("--budget-mode", choices=["off","known_only","propulsion_strict","whole_aircraft_strict"], default="off")
    parser.add_argument("--max-budget", type=float, default=None)
    parser.add_argument("--fixed-nonpropulsion-cost", type=float, default=CAMERA_AVIONICS_PLANNING_COST_USD)
    parser.add_argument("--frame-cost-per-kg", type=float, default=0.0)
    parser.add_argument("--frame-prop-clearance-mm", type=float, default=1000.0 * DEFAULT_FRAME_PROP_CLEARANCE_M)
    parser.add_argument("--frame-structural-margin", type=float, default=DEFAULT_FRAME_STRUCTURAL_MARGIN)
    parser.add_argument("--weight-cost", type=float, default=DEFAULT_DECISION_WEIGHTS["cost"])
    parser.add_argument("--weight-endurance", type=float, default=DEFAULT_DECISION_WEIGHTS["endurance"])
    parser.add_argument("--weight-aircraft-mass", type=float, default=DEFAULT_DECISION_WEIGHTS["weight"])
    parser.add_argument("--weight-quality", type=float, default=DEFAULT_DECISION_WEIGHTS["quality"])
    parser.add_argument("--weight-complexity", type=float, default=DEFAULT_DECISION_WEIGHTS["complexity"])
    args = parser.parse_args()

    started = time.time()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    guide_started = time.time()

    guide_frame_model = {
        "material": FRAME_MATERIAL_NAME,
        "prop_clearance_m": args.frame_prop_clearance_mm / 1000.0,
        "structural_margin": args.frame_structural_margin,
        "allowable_bending_mpa": FRAME_CF_ALLOWABLE_BENDING_PA / 1e6,
        "effective_modulus_gpa": FRAME_CF_EFFECTIVE_E_PA / 1e9,
        "max_arm_deflection_ratio": FRAME_MAX_ARM_DEFLECTION_RATIO,
    }

    locked_motor_id = (
        args.motor_id if args.motor_mode == "lock" else None
    )
    locked_prop_id = (
        args.prop_id if args.prop_mode == "lock" else None
    )
    locked_battery_id = (
        args.battery_id
        if args.battery_mode in {"commercial", "cell"}
        else None
    )
    locked_esc_id = (
        args.esc_id if args.esc_mode == "lock" else None
    )

    voltage_override = None
    if args.battery_mode == "commercial":
        voltage_override = locked_commercial_pack_voltage_case(
            locked_battery_id
        )
        if not voltage_override:
            raise RuntimeError(
                "The selected commercial battery pack is not complete enough "
                "for optimization or could not be found in the warehouse."
            )

    legacy, parametric_csv, search_depth, search_profile = ensure_parametric_results(
        force=args.force_parametric,
        search_depth=args.search_depth,
        frame_model=guide_frame_model,
        min_twr=args.min_twr,
        max_auw=args.max_auw,
        fixed_mass_override=args.fixed_mass,
        locked_motor_id=locked_motor_id,
        locked_prop_id=locked_prop_id,
        voltage_cases_override=voltage_override,
    )
    parametric = read_csv_rows(parametric_csv)
    guide_elapsed_s = round(time.time() - guide_started, 2)

    physical_settings = {
        "warehouse": _file_signature(WAREHOUSE),
        "guide_csv": _file_signature(parametric_csv),
        "fixed_mass_override_kg": args.fixed_mass,
        "max_auw_kg": args.max_auw,
        "min_twr": args.min_twr,
        "pack_overhead_fraction": args.pack_overhead,
        "esc_current_margin": args.esc_margin,
        "rpm_data_tolerance": args.rpm_tolerance,
        "search_depth": search_depth,
        "search_profile": search_profile,
        "frame_model": guide_frame_model,
        "propulsion_curve_schema":
            PROPULSION_CURVE_SCHEMA_VERSION,
        "component_selection": {
            "motor_mode": args.motor_mode,
            "motor_id": locked_motor_id,
            "prop_mode": args.prop_mode,
            "prop_id": locked_prop_id,
            "battery_mode": args.battery_mode,
            "battery_id": locked_battery_id,
            "esc_mode": args.esc_mode,
            "esc_id": locked_esc_id,
        },
    }
    pool_key = physical_pool_key(physical_settings)

    cached_pool = (
        None
        if args.force_physics
        else load_physical_pool(pool_key)
    )

    curve_cache = PropulsionCurveCache()
    explore_count = 0
    exact_count = 0
    exact_valid_count = 0

    if cached_pool:
        print("=" * 78)
        print("REUSING VALIDATED PHYSICAL DESIGN POOL")
        print("=" * 78)
        print(
            f"Validated designs loaded from cache: "
            f"{len(cached_pool)}"
        )
        print(
            "No PyThrust aircraft re-solves are required because the "
            "physical inputs are unchanged."
        )
        real_rows = cached_pool
        exact_valid_count = len(cached_pool)
        physical_pool_reused = True

    else:
        physical_pool_reused = False

        prop_db, motor_db = legacy.load_databases()
        _, apc_catalog = legacy.load_apc_catalog()
        escs = load_esc_candidates()
        if locked_esc_id:
            escs = [
                e for e in escs
                if str(e.canonical_id) == str(locked_esc_id)
            ]
            if not escs:
                raise RuntimeError(
                    "The selected ESC could not be found as a usable "
                    "engineering record in the warehouse."
                )
        prices = price_resolver()

        motor_by_id = {
            m.id: m
            for m in motor_db.search(
                min_kv=1,
                max_kv=100000,
                min_max_current=0,
                min_weight=0,
                max_weight=100000,
            )
        }
        prop_by_id = {
            pid: prop_db.get(pid)
            for pid in prop_db.list_propellers()
        }

        print("=" * 78)
        print("FAST EXPLORE + EXACT VALIDATE")
        print("=" * 78)
        print(f"Guide finalists loaded: {len(parametric)}")
        print(f"Usable real ESC records: {len(escs)}")
        print(
            "Explore stage uses persistent PyThrust response curves; "
            "only a broad finalist pool receives exact hover solves."
        )
        print()

        explore_started = time.time()
        real_solve_limit = MAX_REAL_SOLVES
        parametric.sort(
            key=lambda r: -float(
                r.get("endurance_min") or 0.0
            )
        )

        explore_rows = []
        candidate_records = {}

        for idx, p in enumerate(parametric, start=1):
            if explore_count >= real_solve_limit:
                break

            motor_id = p.get("motor_id")
            prop_id = p.get("prop_id")
            motor = motor_by_id.get(motor_id)
            prop_entry = prop_by_id.get(prop_id)
            if motor is None or prop_entry is None:
                reject("parametric_component_not_found")
                continue

            prop_catalog = legacy.match_prop_to_catalog(
                prop_entry, apc_catalog
            )
            if prop_catalog is None:
                reject("prop_catalog_match_failed")
                continue

            label = str(p.get("battery_label") or "")
            series = optional_int(label.replace("S", ""))
            if not series:
                reject("battery_series_parse_failed")
                continue

            target_energy = float(
                p.get("battery_energy_wh") or 0.0
            )
            target_mass = float(
                p.get("battery_mass_kg") or 0.0
            )
            fixed_mass = (
                float(args.fixed_mass)
                if args.fixed_mass is not None
                else float(p.get("fixed_mass_kg") or 1.0)
            )

            if args.battery_mode == "commercial":
                locked_pack = build_locked_commercial_battery(
                    locked_battery_id
                )
                batteries = []
                if (
                    locked_pack is not None
                    and int(locked_pack.series_cells) == int(series)
                ):
                    batteries = [locked_pack]
            elif args.battery_mode == "cell":
                batteries = build_custom_battery_candidates(
                    series,
                    target_energy,
                    target_mass,
                    args.pack_overhead,
                    locked_cell_id=locked_battery_id,
                )
                batteries = rank_batteries(
                    batteries, target_energy, target_mass
                )
                batteries = batteries[
                    :MAX_BATTERY_CHOICES_PER_PARAMETRIC_RESULT
                ]
            else:
                batteries = build_custom_battery_candidates(
                    series,
                    target_energy,
                    target_mass,
                    args.pack_overhead,
                )
                batteries += build_commercial_battery_candidates(
                    series, target_energy, target_mass
                )
                batteries = rank_batteries(
                    batteries, target_energy, target_mass
                )
                batteries = batteries[
                    :MAX_BATTERY_CHOICES_PER_PARAMETRIC_RESULT
                ]

            if not batteries:
                reject("no_real_battery_candidates")
                continue

            for battery in batteries:
                if explore_count >= real_solve_limit:
                    break

                candidate_payload = {
                    "motor_id": str(motor.id),
                    "prop_id": str(prop_entry.metadata.id),
                    "battery_id": str(battery.battery_id),
                    "fixed_mass": round(float(fixed_mass), 6),
                    "max_auw": round(float(args.max_auw), 6),
                    "min_twr": round(float(args.min_twr), 6),
                }
                candidate_id = hashlib.sha1(
                    json.dumps(
                        candidate_payload,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:16]

                row = run_real_design_cached(
                    legacy,
                    curve_cache,
                    prop_entry,
                    prop_catalog,
                    motor,
                    battery,
                    escs,
                    fixed_mass,
                    args.max_auw,
                    args.min_twr,
                    args.rpm_tolerance,
                    args.esc_margin,
                    prices,
                    args.frame_prop_clearance_mm / 1000.0,
                    args.frame_structural_margin,
                )
                explore_count += 1
                if row is None:
                    continue

                row["explore_candidate_id"] = candidate_id
                explore_rows.append(row)
                candidate_records[candidate_id] = (
                    prop_entry,
                    prop_catalog,
                    motor,
                    battery,
                    fixed_mass,
                )

            if idx % 5 == 0:
                stats = curve_cache.stats()
                print(
                    f"Explore {explore_count}/{real_solve_limit} | "
                    f"valid {len(explore_rows)} | "
                    f"curve hits {stats['hits']} | "
                    f"new curves {stats['new_curves_built']}"
                )

        if not explore_rows:
            real_rows = []
        else:
            explore_elapsed_s = round(
                time.time() - explore_started, 2
            )

            # Score the broad cached exploration first, then exact-validate a
            # deliberately diverse finalist pool.
            preliminary_weights = {
                "cost": args.weight_cost,
                "endurance": args.weight_endurance,
                "weight": args.weight_aircraft_mass,
                "quality": args.weight_quality,
                "complexity": args.weight_complexity,
            }
            add_decision_matrix(
                explore_rows, preliminary_weights
            )
            validate_ids = validation_candidate_ids(
                explore_rows
            )
            print()
            print(
                f"Cached explore produced {len(explore_rows)} valid "
                f"designs; exact-validating "
                f"{len(validate_ids)} diverse finalists..."
            )

            validation_started = time.time()
            real_rows = []
            for n, candidate_id in enumerate(
                sorted(validate_ids), start=1
            ):
                record = candidate_records.get(candidate_id)
                if record is None:
                    continue
                (
                    prop_entry,
                    prop_catalog,
                    motor,
                    battery,
                    fixed_mass,
                ) = record

                row = run_real_design_exact(
                    legacy,
                    prop_entry,
                    prop_catalog,
                    motor,
                    battery,
                    escs,
                    fixed_mass,
                    args.max_auw,
                    args.min_twr,
                    args.rpm_tolerance,
                    args.esc_margin,
                    prices,
                    "off",
                    None,
                    args.frame_prop_clearance_mm / 1000.0,
                    args.frame_structural_margin,
                )
                exact_count += 1
                if row is not None:
                    row["explore_candidate_id"] = candidate_id
                    real_rows.append(row)
                    exact_valid_count += 1

                if n % 20 == 0:
                    print(
                        f"Exact validation {n}/{len(validate_ids)} | "
                        f"valid {exact_valid_count}"
                    )

            validation_elapsed_s = round(
                time.time() - validation_started, 2
            )

            if real_rows:
                save_physical_pool(
                    real_rows,
                    pool_key,
                    physical_settings,
                )

    if physical_pool_reused:
        explore_elapsed_s = 0.0
        validation_elapsed_s = 0.0
    else:
        explore_elapsed_s = locals().get(
            "explore_elapsed_s",
            round(time.time() - locals().get("explore_started", time.time()), 2),
        )
        validation_elapsed_s = locals().get(
            "validation_elapsed_s", 0.0
        )

    decision_weights = {
        "cost": args.weight_cost,
        "endurance": args.weight_endurance,
        "weight": args.weight_aircraft_mass,
        "quality": args.weight_quality,
        "complexity": args.weight_complexity,
    }

    settings = {
        "force_parametric": args.force_parametric,
        "fixed_mass_override_kg": args.fixed_mass,
        "max_auw_kg": args.max_auw,
        "min_twr": args.min_twr,
        "pack_overhead_fraction": args.pack_overhead,
        "esc_current_margin": args.esc_margin,
        "rpm_data_tolerance": args.rpm_tolerance,
        "search_depth": search_depth,
        "search_profile": search_profile,
        "budget_mode": args.budget_mode,
        "max_budget_usd": args.max_budget,
        "frame_model": {
            "material": FRAME_MATERIAL_NAME,
            "prop_clearance_mm": args.frame_prop_clearance_mm,
            "structural_margin": args.frame_structural_margin,
            "effective_modulus_gpa": FRAME_CF_EFFECTIVE_E_PA / 1e9,
            "allowable_bending_mpa": FRAME_CF_ALLOWABLE_BENDING_PA / 1e6,
            "max_arm_deflection_ratio": FRAME_MAX_ARM_DEFLECTION_RATIO,
            "tube_candidates_mm": FRAME_TUBE_CANDIDATES_MM,
            "center_plate_thickness_mm": 1000.0 * FRAME_CENTER_PLATE_THICKNESS_M,
            "battery_tray_thickness_mm": 1000.0 * FRAME_BATTERY_TRAY_THICKNESS_M,
        },
        "component_selection": physical_settings["component_selection"],
        "planning_costs": {
            "frame_usd": FRAME_PLANNING_COST_USD,
            "diy_camera_avionics_usd": CAMERA_AVIONICS_PLANNING_COST_USD,
            "fixed_total_usd": FIXED_PLANNING_COST_USD,
            "fallback_propulsion_usd": FALLBACK_PROPULSION_COST_USD,
        },
        "decision_weights": decision_weights,
        "data_snapshot": warehouse_run_snapshot(),
        "parametric_source_csv": str(parametric_csv),
        "physical_pool_reused": physical_pool_reused,
        "timing_s": {
            "guide": guide_elapsed_s,
            "cached_explore": explore_elapsed_s,
            "exact_validation": validation_elapsed_s,
        },
        "cached_explore_evaluations": explore_count,
        "exact_validation_attempts": exact_count,
        "exact_valid_designs": exact_valid_count,
        "propulsion_cache": curve_cache.stats(),
        "physical_pool_key": pool_key,
        "valid_real_designs": len(real_rows),
        "elapsed_s": round(time.time() - started, 1),
    }
    save_results(real_rows, settings)

    print()
    print("=" * 78)
    print("INTEGRATED RUN COMPLETE")
    print("=" * 78)
    print(
        f"Cached exploratory aircraft evaluations: {explore_count}"
    )
    print(
        f"Exact PyThrust finalist validations: {exact_count}"
    )
    print(f"Validated real-component designs: {len(real_rows)}")
    print(
        f"Physical design pool reused: {physical_pool_reused}"
    )
    print(
        f"Propulsion cache stats: {curve_cache.stats()}"
    )
    print(f"Results: {RESULTS_DIR}")
    if REJECTION_COUNTS:
        print("Rejection summary:")
        for reason, count in sorted(
            REJECTION_COUNTS.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(f"  {reason}: {count}")
    if real_rows:
        best = max(real_rows, key=lambda r: r["endurance_min"])
        print(
            f"Best: {best['endurance_min']:.1f} min | {best['auw_kg']:.3f} kg | "
            f"{best['motor_manufacturer']} {best['motor_model']} | "
            f"{best['prop_model']} | "
            f"{best['battery_manufacturer']} {best['battery_model']} "
            f"{best['battery_topology']} | "
            f"{best['esc_manufacturer']} {best['esc_model']}"
        )
    cleanup()


if __name__ == "__main__":
    main()
