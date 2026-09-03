"""Version 3.2 FAST: adaptive real-motor + APC prop mass/price screening.

What is real in this version
----------------------------
* Motor Kv, resistance, no-load current, max current, and motor mass come from
  PyThrust's real motor database.
* Motor data come from PyThrust's real motor database.
* Each propeller is now a discrete real APC database entry with its own
  diameter, pitch, blade count, and measured/tabulated Ct/Cp data.

What is still approximate
-------------------------
* Battery energy/mass is parametric rather than a real pack database.
* ESC mass is a placeholder.
* Frame arms are sized from carbon-tube bending stress and deflection limits.
* Motor, ESC, battery, and frame prices are not yet populated.

APC physical catalog data
-------------------------
This script reads apc_catalog_202602.csv, which was normalized from APC's
PROP-DATA-FILE_202602.xlsx. Matching propellers use APC's real catalog mass,
SKU, stock status, and price instead of an estimated prop mass.

Cost handling
-------------
Propeller cost is now real and included in the reported known-component cost.
The $1000 total-build budget switch is intentionally OFF until motor, ESC,
battery, and frame prices are also populated; otherwise a "total" cost limit
would be misleading.

Run from the PyThrust repository root:
    python .\\examples\\quadrotor_real_motor_prop_price_optimization.py
"""

from __future__ import annotations

import sys
import os
import io
import contextlib
import shutil
import atexit
from pathlib import Path

# Disable OpenMDAO's automatic reports. OpenMDAO normally creates an output
# directory for every Problem, so below we also redirect ALL temporary OpenMDAO
# files into one disposable work folder instead of cluttering the PyThrust root.
os.environ["OPENMDAO_REPORTS"] = "0"

import numpy as np
import openmdao.api as om

from optimizer.paths import DATA_ROOT as PROJECT_ROOT

# Only useful final outputs are kept here.
RESULTS_DIR = PROJECT_ROOT / "optimizer_results" / "fast_real_motor_prop_price_screening"

# All per-optimization OpenMDAO folders are redirected here and automatically
# deleted when the script finishes (including most Ctrl+C exits).
OPENMDAO_WORK_DIR = RESULTS_DIR / "_openmdao_temp"
os.environ["OPENMDAO_WORKDIR"] = str(OPENMDAO_WORK_DIR)


def cleanup_openmdao_workdir():
    """Delete temporary per-problem OpenMDAO output folders."""
    shutil.rmtree(OPENMDAO_WORK_DIR, ignore_errors=True)


atexit.register(cleanup_openmdao_workdir)

from pythrust.motors import MotorDatabase
from pythrust.openmdao import PropulsionComponent
from pythrust.propellers import PropellerDatabase


# ===========================================================================
# USER-EDITABLE DESIGN ASSUMPTIONS
# ===========================================================================

N_ROTORS = 4
MAX_AUW_KG = 10.0

# We still do not know fixed payload/avionics mass, so keep sensitivity cases.
FIXED_MASS_CASES_KG = [0.50, 1.00, 1.50, 2.00]

# Battery voltage is discrete.
BATTERY_VOLTAGE_CASES_V = {
    "4S": 14.8,
    "6S": 22.2,
    "8S": 29.6,
}

# Temporary battery model.
BATTERY_SPECIFIC_ENERGY_WH_KG = 200.0
BATTERY_USABLE_FRACTION = 0.80
BATTERY_ENERGY_MIN_WH = 75.0
BATTERY_ENERGY_MAX_WH = 1500.0

# Accessory load from cameras, flight computer, telemetry, etc.
ACCESSORY_POWER_W = 15.0

# Temporary ESC mass per motor.
ESC_MASS_EACH_KG = 0.035

# Concept-stage physical carbon-frame sizing. The integrated optimizer can
# override prop clearance / structural margin from the GUI.
FRAME_CF_DENSITY_KG_M3 = 1600.0
FRAME_CF_EFFECTIVE_E_PA = 60.0e9
FRAME_CF_ALLOWABLE_BENDING_PA = 300.0e6
FRAME_MAX_ARM_DEFLECTION_RATIO = 0.010
FRAME_PROP_CLEARANCE_M = 0.050
FRAME_STRUCTURAL_MARGIN = 1.25
FRAME_CENTER_PLATE_THICKNESS_M = 0.0020
FRAME_BATTERY_TRAY_THICKNESS_M = 0.0015
FRAME_ASSUMED_PACK_BULK_DENSITY_KG_M3 = 900.0
FRAME_ASSUMED_PACK_THICKNESS_M = 0.050
FRAME_CENTER_MARGIN_M = 0.040
FRAME_HARDWARE_BASE_MASS_KG = 0.090
FRAME_LANDING_SUPPORT_MASS_KG = 0.050
FRAME_TUBE_CANDIDATES_MM = [
    (12, 1.0), (14, 1.0), (16, 1.0), (16, 1.5),
    (18, 1.0), (18, 1.5), (20, 1.0), (20, 1.5),
    (22, 1.0), (22, 1.5), (22, 2.0), (25, 1.0),
    (25, 1.5), (25, 2.0), (28, 1.5), (28, 2.0),
    (30, 1.5), (30, 2.0),
]

# APC manufacturer catalog normalized from PROP-DATA-FILE_202602.xlsx.
# Put the supplied CSV in either the PyThrust root or pythrust/data/propellers/.
APC_CATALOG_FILENAMES = [
    PROJECT_ROOT / "apc_catalog_202602.csv",
    PROJECT_ROOT / "pythrust" / "data" / "propellers" / "apc_catalog_202602.csv",
    PROJECT_ROOT / "data_seed" / "apc_catalog_202602.csv",
]

# Use only currently listed in-stock props by default.
REQUIRE_PROP_IN_STOCK = True

# Cost framework. Real APC prop prices are active now, but a true total-build
# budget must wait until motor/ESC/battery/frame prices are available too.
TOTAL_BUILD_BUDGET_USD = 1000.0
ENFORCE_TOTAL_BUILD_BUDGET = False

# Real propeller catalog bounds. Only real APC entries inside this envelope
# are considered. The optimizer NEVER invents an in-between diameter or pitch.
PROP_MIN_IN = 14.0
PROP_MAX_IN = 20.0
PROP_BLADE_COUNT = 2

# To keep the first discrete search practical, retain a diverse subset of real
# APC propellers across diameter and pitch. Increase this later if desired.
MAX_PROP_CANDIDATES = 10

# Optional component locks set by the integrated optimizer.
# Empty lists preserve normal optimization behavior.
LOCKED_MOTOR_IDS = []
LOCKED_PROP_IDS = []


# Hover-throttle search bounds.
HOVER_THROTTLE_MIN = 0.15
HOVER_THROTTLE_MAX = 0.78

# Aircraft constraint.
MIN_MAX_THRUST_TO_WEIGHT = 1.8

# Motor database pre-filter.
# These are deliberately broad for a medium/large endurance quad.
MOTOR_KV_MIN = 150.0
MOTOR_KV_MAX = 900.0
MOTOR_WEIGHT_MIN_G = 40.0
MOTOR_WEIGHT_MAX_G = 500.0
MOTOR_MIN_MAX_CURRENT_A = 15.0

# Running thousands of nonlinear optimizations would be slow.
# We first retain the most promising broad set using a lightweight screen.
MAX_MOTOR_CANDIDATES = 18

# Number of results to print for each fixed-mass case.
TOP_RESULTS_PER_CASE = 8

# Number of stage-2 guide designs retained for the integrated real-component stage.
GUIDE_RESULTS_TO_SAVE = 25

# Fast adaptive-search settings.
# Stage 1 explores the catalog only at one representative fixed mass. Stage 2
# re-optimizes only the best discrete motor/prop/voltage configurations across
# all requested fixed-mass sensitivity cases.
SCREENING_FIXED_MASS_KG = 1.00
MAX_FINALIST_CONFIGS = 24
MAX_FINALISTS_PER_MOTOR = 4
MAX_FINALISTS_PER_PROP = 8

# Very high pitch/diameter props are normally poor hover candidates and waste a
# lot of nonlinear solves. Keep this adjustable rather than hard-coding a tiny
# hand-picked prop list.
MAX_HOVER_PITCH_DIAMETER_RATIO = 0.80

G = 9.80665
INCH_TO_M = 0.0254


# ===========================================================================
# AIRCRAFT-LEVEL OPENMDAO COMPONENTS
# ===========================================================================


def _guide_tube_properties(od_mm, wall_mm):
    od = od_mm / 1000.0
    wall = wall_mm / 1000.0
    inner = od - 2.0 * wall
    if inner <= 0:
        return None
    area = np.pi / 4.0 * (od**2 - inner**2)
    inertia = np.pi / 64.0 * (od**4 - inner**4)
    section_modulus = inertia / (od / 2.0)
    return area, inertia, section_modulus


def _guide_frame_mass(nonframe_mass, battery_mass, prop_diameter_m):
    motor_spacing = prop_diameter_m + FRAME_PROP_CLEARANCE_M
    motor_radius = motor_spacing / np.sqrt(2.0)

    battery_volume = (
        battery_mass / FRAME_ASSUMED_PACK_BULK_DENSITY_KG_M3
    )
    footprint = (
        battery_volume / FRAME_ASSUMED_PACK_THICKNESS_M
    )
    battery_side = np.sqrt(max(footprint, 0.0))
    center_side = min(
        0.32,
        max(0.14, battery_side + FRAME_CENTER_MARGIN_M),
    )
    center_corner_radius = center_side / np.sqrt(2.0)
    arm_length = max(
        0.10,
        motor_radius - 0.70 * center_corner_radius,
    )

    center_mass = (
        2.0 * center_side**2
        * FRAME_CENTER_PLATE_THICKNESS_M
        * FRAME_CF_DENSITY_KG_M3
    )
    tray_side = max(0.10, battery_side + 0.020)
    tray_mass = (
        tray_side**2
        * FRAME_BATTERY_TRAY_THICKNESS_M
        * FRAME_CF_DENSITY_KG_M3
    )

    frame_mass = center_mass + tray_mass + 0.25
    for _ in range(40):
        auw = nonframe_mass + frame_mass
        force = MIN_MAX_THRUST_TO_WEIGHT * auw * G / N_ROTORS
        design_force = force * FRAME_STRUCTURAL_MARGIN
        moment = design_force * arm_length
        deflection_limit = FRAME_MAX_ARM_DEFLECTION_RATIO * arm_length

        choices = []
        for od_mm, wall_mm in FRAME_TUBE_CANDIDATES_MM:
            props = _guide_tube_properties(od_mm, wall_mm)
            if props is None:
                continue
            area, inertia, section_modulus = props
            stress = moment / max(section_modulus, 1e-15)
            deflection = (
                design_force * arm_length**3
                / (3.0 * FRAME_CF_EFFECTIVE_E_PA * inertia)
            )
            if (
                stress <= FRAME_CF_ALLOWABLE_BENDING_PA
                and deflection <= deflection_limit
            ):
                linear_mass = FRAME_CF_DENSITY_KG_M3 * area
                arm_mass_each = linear_mass * arm_length
                choices.append((arm_mass_each, od_mm))

        if not choices:
            return 99.0  # deliberately impossible; AUW constraint rejects it

        choices.sort(key=lambda x: x[0])
        arm_mass_each, od_mm = choices[0]
        clamp_mass = (
            FRAME_HARDWARE_BASE_MASS_KG
            + N_ROTORS * 0.0015 * od_mm
        )
        new_mass = (
            N_ROTORS * arm_mass_each
            + center_mass
            + tray_mass
            + clamp_mass
            + FRAME_LANDING_SUPPORT_MASS_KG
        )
        if abs(new_mass - frame_mass) < 1e-6:
            frame_mass = new_mass
            break
        frame_mass = 0.5 * (frame_mass + new_mass)

    return frame_mass


class AircraftMassComp(om.ExplicitComponent):
    """Compute aircraft mass and hover thrust using a REAL motor mass."""

    def initialize(self):
        self.options.declare("fixed_mass_kg", types=float)
        self.options.declare("motor_mass_each_kg", types=float)
        self.options.declare("prop_diameter_in", types=float)
        self.options.declare("prop_mass_each_kg", types=float)

    def setup(self):
        self.add_input("battery_energy_wh", val=300.0)

        self.add_output("frame_mass_kg", val=0.4)
        self.add_output("battery_mass_kg", val=1.5)
        self.add_output("props_mass_kg", val=0.1)
        self.add_output("motor_mass_total_kg", val=0.4)
        self.add_output("esc_mass_total_kg", val=N_ROTORS * ESC_MASS_EACH_KG)
        self.add_output("auw_kg", val=3.0)
        self.add_output("required_hover_thrust_n", val=7.0)

        self.declare_partials(of="*", wrt="*", method="fd")

    def compute(self, inputs, outputs):
        d_in = self.options["prop_diameter_in"]
        battery_energy_wh = float(inputs["battery_energy_wh"][0])

        fixed_mass = self.options["fixed_mass_kg"]
        motor_mass_each = self.options["motor_mass_each_kg"]
        prop_mass_each = self.options["prop_mass_each_kg"]

        battery_mass = battery_energy_wh / BATTERY_SPECIFIC_ENERGY_WH_KG
        props_mass = N_ROTORS * prop_mass_each
        motor_mass_total = N_ROTORS * motor_mass_each
        esc_mass_total = N_ROTORS * ESC_MASS_EACH_KG

        prop_diameter_m = d_in * INCH_TO_M
        nonframe_mass = (
            fixed_mass
            + battery_mass
            + props_mass
            + motor_mass_total
            + esc_mass_total
        )
        frame_mass = _guide_frame_mass(
            nonframe_mass,
            battery_mass,
            prop_diameter_m,
        )
        auw = nonframe_mass + frame_mass

        outputs["frame_mass_kg"] = frame_mass
        outputs["battery_mass_kg"] = battery_mass
        outputs["props_mass_kg"] = props_mass
        outputs["motor_mass_total_kg"] = motor_mass_total
        outputs["esc_mass_total_kg"] = esc_mass_total
        outputs["auw_kg"] = auw
        outputs["required_hover_thrust_n"] = auw * G / N_ROTORS


class AircraftPerformanceComp(om.ExplicitComponent):
    """Compute hover endurance and full-throttle thrust margin."""

    def setup(self):
        self.add_input("auw_kg", val=3.0)
        self.add_input("battery_energy_wh", val=300.0)
        self.add_input("hover_thrust_n", val=7.0)
        self.add_input("required_hover_thrust_n", val=7.0)
        self.add_input("hover_battery_power_w", val=100.0)
        self.add_input("max_thrust_n", val=15.0)

        self.add_output("hover_residual_n", val=0.0)
        self.add_output("total_hover_power_w", val=400.0)
        self.add_output("endurance_min", val=30.0)
        self.add_output("negative_endurance_min", val=-30.0)
        self.add_output("max_thrust_to_weight", val=2.0)

        self.declare_partials(of="*", wrt="*", method="fd")

    def compute(self, inputs, outputs):
        auw = float(inputs["auw_kg"][0])
        energy_wh = float(inputs["battery_energy_wh"][0])
        hover_thrust = float(inputs["hover_thrust_n"][0])
        required_hover_thrust = float(inputs["required_hover_thrust_n"][0])
        hover_power_each = float(inputs["hover_battery_power_w"][0])
        max_thrust_each = float(inputs["max_thrust_n"][0])

        total_hover_power = N_ROTORS * hover_power_each + ACCESSORY_POWER_W
        usable_energy_wh = BATTERY_USABLE_FRACTION * energy_wh
        endurance_min = 60.0 * usable_energy_wh / max(total_hover_power, 1e-9)

        max_twr = (N_ROTORS * max_thrust_each) / max(auw * G, 1e-9)

        outputs["hover_residual_n"] = hover_thrust - required_hover_thrust
        outputs["total_hover_power_w"] = total_hover_power
        outputs["endurance_min"] = endurance_min
        outputs["negative_endurance_min"] = -endurance_min
        outputs["max_thrust_to_weight"] = max_twr


# ===========================================================================
# DATABASE LOADING / SCREENING
# ===========================================================================

def load_databases():
    prop_db = PropellerDatabase()
    prop_dir = PROJECT_ROOT / "pythrust" / "data" / "propellers" / "apc_202602"
    if not prop_db.load(prop_dir, strict=False):
        raise RuntimeError(f"Could not load propeller database: {prop_dir}")

    motor_db = MotorDatabase()
    motor_dir = PROJECT_ROOT / "pythrust" / "data" / "motors"
    if not motor_db.load(motor_dir):
        raise RuntimeError(f"Could not load motor database: {motor_dir}")

    return prop_db, motor_db


def select_motor_candidates(motor_db):
    """Broadly filter the catalog, or return an explicitly locked motor."""
    if LOCKED_MOTOR_IDS:
        wanted = {str(x) for x in LOCKED_MOTOR_IDS}
        broad = motor_db.search(
            min_kv=1,
            max_kv=100000,
            min_max_current=0,
            min_weight=0,
            max_weight=100000,
        )
        locked = [
            m for m in broad
            if str(m.id) in wanted
            and m.kv > 0
            and m.resistance > 0
            and m.io >= 0
            and m.max_current > 0
            and m.weight_g > 0
        ]
        missing = wanted - {str(m.id) for m in locked}
        if missing:
            raise RuntimeError(
                "Locked motor ID(s) not found as complete PyThrust physics "
                f"records: {sorted(missing)}"
            )
        return locked

    motors = motor_db.search(
        min_kv=MOTOR_KV_MIN,
        max_kv=MOTOR_KV_MAX,
        min_max_current=MOTOR_MIN_MAX_CURRENT_A,
        min_weight=MOTOR_WEIGHT_MIN_G,
        max_weight=MOTOR_WEIGHT_MAX_G,
    )

    motors = [
        m for m in motors
        if m.kv > 0
        and m.resistance > 0
        and m.io >= 0
        and m.max_current > 0
        and m.weight_g > 0
    ]

    if len(motors) <= MAX_MOTOR_CANDIDATES:
        return motors

    kv_edges = np.linspace(MOTOR_KV_MIN, MOTOR_KV_MAX, 13)
    selected = []
    per_bin = max(
        1, MAX_MOTOR_CANDIDATES // (len(kv_edges) - 1)
    )

    for lo, hi in zip(kv_edges[:-1], kv_edges[1:]):
        bucket = [m for m in motors if lo <= m.kv < hi]
        bucket.sort(
            key=lambda m: (
                m.weight_g,
                m.resistance,
                -m.max_current,
            )
        )
        selected.extend(bucket[:per_bin])

    used = {m.id for m in selected}
    remainder = [m for m in motors if m.id not in used]
    remainder.sort(key=lambda m: (m.weight_g, m.resistance))
    selected.extend(
        remainder[
            :max(0, MAX_MOTOR_CANDIDATES - len(selected))
        ]
    )
    return selected[:MAX_MOTOR_CANDIDATES]


def normalize_apc_model(value):
    """Normalize APC model strings for conservative exact matching."""
    text = str(value or "").strip().upper()
    if text.startswith("APC_"):
        text = text[4:]
    text = text.replace("×", "X")
    text = text.replace(" ", "")
    return text


def load_apc_catalog():
    """Load normalized APC manufacturer catalog mass/price data."""
    import csv

    catalog_path = next((p for p in APC_CATALOG_FILENAMES if p.exists()), None)
    if catalog_path is None:
        locations = "\n".join(f"  - {p}" for p in APC_CATALOG_FILENAMES)
        raise FileNotFoundError(
            "Could not find apc_catalog_202602.csv.\n"
            "Place the supplied CSV in one of these locations:\n"
            f"{locations}"
        )

    lookup = {}
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {
            "product_name",
            "sku",
            "price_usd",
            "status",
            "pitch_in",
            "diameter_in",
            "weight_g",
            "categories",
        }
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise RuntimeError(
                f"APC catalog CSV has unexpected columns: {reader.fieldnames}"
            )

        for row in reader:
            try:
                price = float(row["price_usd"])
                diameter = float(row["diameter_in"])
                pitch = float(row["pitch_in"])
                weight_g = float(row["weight_g"])
            except (TypeError, ValueError):
                continue

            if price <= 0 or diameter <= 0 or pitch < 0 or weight_g <= 0:
                continue

            record = {
                "product_name": row["product_name"].strip(),
                "sku": row["sku"].strip(),
                "price_usd": price,
                "status": row["status"].strip(),
                "pitch_in": pitch,
                "diameter_in": diameter,
                "weight_g": weight_g,
                "categories": row["categories"].strip(),
                "source_file": catalog_path.name,
            }

            key = normalize_apc_model(record["product_name"])
            # Prefer in-stock record if duplicate normalized names occur.
            old = lookup.get(key)
            if old is None or (
                old["status"].lower() != "in stock"
                and record["status"].lower() == "in stock"
            ):
                lookup[key] = record

    return catalog_path, lookup


def match_prop_to_catalog(prop_entry, catalog_lookup):
    """Match a PyThrust APC entry to APC's catalog without fuzzy guessing."""
    candidates = [
        normalize_apc_model(prop_entry.metadata.model),
        normalize_apc_model(prop_entry.metadata.id),
    ]

    for key in candidates:
        record = catalog_lookup.get(key)
        if record is None:
            continue

        # Guard against an accidental string collision.
        if abs(record["diameter_in"] - prop_entry.metadata.diameter_in) > 0.02:
            continue
        if abs(record["pitch_in"] - prop_entry.metadata.pitch_in) > 0.02:
            continue

        return record

    return None


def select_prop_candidates(prop_db, catalog_lookup):
    """Return real APC propellers, or one explicitly locked propeller."""
    if LOCKED_PROP_IDS:
        wanted = {str(x) for x in LOCKED_PROP_IDS}
        locked = []
        for prop_id in wanted:
            entry = prop_db.get(prop_id)
            if entry is None:
                continue
            md = entry.metadata
            if not entry.data_by_rpm:
                continue
            catalog = match_prop_to_catalog(
                entry, catalog_lookup
            )
            if catalog is None:
                continue
            if (
                REQUIRE_PROP_IN_STOCK
                and catalog["status"].lower() != "in stock"
            ):
                continue
            locked.append((entry, catalog))

        missing = wanted - {
            str(p[0].metadata.id) for p in locked
        }
        if missing:
            raise RuntimeError(
                "Locked propeller ID(s) are not available with both APC "
                f"aerodynamic and physical catalog data: {sorted(missing)}"
            )
        return locked

    props = []
    for prop_id in prop_db.list_propellers():
        entry = prop_db.get(prop_id)
        if entry is None:
            continue
        md = entry.metadata
        if md.blade_count != PROP_BLADE_COUNT:
            continue
        if not (PROP_MIN_IN <= md.diameter_in <= PROP_MAX_IN):
            continue
        if (
            md.pitch_in / md.diameter_in
            > MAX_HOVER_PITCH_DIAMETER_RATIO
        ):
            continue
        if not entry.data_by_rpm:
            continue

        catalog = match_prop_to_catalog(entry, catalog_lookup)
        if catalog is None:
            continue
        if (
            REQUIRE_PROP_IN_STOCK
            and catalog["status"].lower() != "in stock"
        ):
            continue

        props.append((entry, catalog))

    if len(props) <= MAX_PROP_CANDIDATES:
        return sorted(
            props,
            key=lambda p: (
                p[0].metadata.diameter_in,
                p[0].metadata.pitch_in,
            ),
        )

    edges = np.linspace(PROP_MIN_IN, PROP_MAX_IN, 7)
    selected = []
    per_bin = max(
        1, MAX_PROP_CANDIDATES // (len(edges) - 1)
    )

    for i, (lo, hi) in enumerate(
        zip(edges[:-1], edges[1:])
    ):
        if i == len(edges) - 2:
            bucket = [
                p for p in props
                if lo <= p[0].metadata.diameter_in <= hi
            ]
        else:
            bucket = [
                p for p in props
                if lo <= p[0].metadata.diameter_in < hi
            ]

        bucket.sort(
            key=lambda p: (
                p[0].metadata.pitch_in
                / p[0].metadata.diameter_in
            )
        )
        if not bucket:
            continue
        if len(bucket) <= per_bin:
            selected.extend(bucket)
        else:
            idxs = np.linspace(
                0, len(bucket) - 1, per_bin
            ).round().astype(int)
            selected.extend(
                bucket[int(j)] for j in idxs
            )

    used = {p[0].metadata.id for p in selected}
    remainder = [
        p for p in props
        if p[0].metadata.id not in used
    ]
    remainder.sort(
        key=lambda p: (
            p[0].metadata.diameter_in,
            p[0].metadata.pitch_in,
        )
    )
    selected.extend(
        remainder[
            :max(0, MAX_PROP_CANDIDATES - len(selected))
        ]
    )
    return selected[:MAX_PROP_CANDIDATES]


def build_problem(prop_entry, prop_catalog, motor, fixed_mass_kg, battery_voltage_v):
    prob = om.Problem(reports=False)
    model = prob.model

    ivc = om.IndepVarComp()

    # REAL motor parameters from PyThrust database.
    ivc.add_output("kv_rpm_per_v", val=float(motor.kv))
    ivc.add_output("resistance_ohm", val=float(motor.resistance))
    ivc.add_output("no_load_current_a", val=float(motor.io))
    ivc.add_output("current_max_a", val=float(motor.max_current))

    # Fixed battery voltage for this discrete voltage case.
    ivc.add_output("voltage_v", val=float(battery_voltage_v))
    ivc.add_output("diameter_m", val=float(prop_entry.diameter_m))

    # Continuous variables still optimized.
    ivc.add_output("hover_throttle", val=0.45)
    ivc.add_output("battery_energy_wh", val=350.0)

    # Full-throttle point.
    ivc.add_output("max_throttle", val=1.0)

    model.add_subsystem("design", ivc, promotes_outputs=["*"])

    # Hover propulsion point.
    hover = PropulsionComponent(prop_entry=prop_entry)
    model.add_subsystem("hover_prop", hover)
    for src, dst in [
        ("kv_rpm_per_v", "kv_rpm_per_v"),
        ("resistance_ohm", "resistance_ohm"),
        ("no_load_current_a", "no_load_current_a"),
        ("current_max_a", "current_max_a"),
        ("voltage_v", "voltage_v"),
        ("diameter_m", "diameter_m"),
        ("hover_throttle", "throttle"),
    ]:
        model.connect(src, f"hover_prop.{dst}")

    # Max-throttle propulsion point.
    max_prop = PropulsionComponent(prop_entry=prop_entry)
    model.add_subsystem("max_prop", max_prop)
    for src, dst in [
        ("kv_rpm_per_v", "kv_rpm_per_v"),
        ("resistance_ohm", "resistance_ohm"),
        ("no_load_current_a", "no_load_current_a"),
        ("current_max_a", "current_max_a"),
        ("voltage_v", "voltage_v"),
        ("diameter_m", "diameter_m"),
        ("max_throttle", "throttle"),
    ]:
        model.connect(src, f"max_prop.{dst}")

    # Use the REAL catalog motor mass.
    mass = AircraftMassComp(
        fixed_mass_kg=float(fixed_mass_kg),
        motor_mass_each_kg=float(motor.weight_g) / 1000.0,
        prop_diameter_in=float(prop_entry.metadata.diameter_in),
        prop_mass_each_kg=float(prop_catalog["weight_g"]) / 1000.0,
    )
    model.add_subsystem("mass", mass)
    model.connect("battery_energy_wh", "mass.battery_energy_wh")

    perf = AircraftPerformanceComp()
    model.add_subsystem("aircraft", perf)
    model.connect("mass.auw_kg", "aircraft.auw_kg")
    model.connect("battery_energy_wh", "aircraft.battery_energy_wh")
    model.connect("hover_prop.thrust_n", "aircraft.hover_thrust_n")
    model.connect("mass.required_hover_thrust_n", "aircraft.required_hover_thrust_n")
    model.connect("hover_prop.battery_power_w", "aircraft.hover_battery_power_w")
    model.connect("max_prop.thrust_n", "aircraft.max_thrust_n")

    # Only these variables remain continuous.
    model.add_design_var(
        "hover_throttle",
        lower=HOVER_THROTTLE_MIN,
        upper=HOVER_THROTTLE_MAX,
        ref=0.45,
    )
    model.add_design_var(
        "battery_energy_wh",
        lower=BATTERY_ENERGY_MIN_WH,
        upper=BATTERY_ENERGY_MAX_WH,
        ref=400.0,
    )

    # Hover exactly balances aircraft weight.
    model.add_constraint("aircraft.hover_residual_n", equals=0.0, ref=5.0)

    # Aircraft-level constraints.
    model.add_constraint("mass.auw_kg", upper=MAX_AUW_KG, ref=5.0)
    model.add_constraint(
        "aircraft.max_thrust_to_weight",
        lower=MIN_MAX_THRUST_TO_WEIGHT,
        ref=2.0,
    )

    # Respect THIS motor's actual catalog max-current rating.
    model.add_constraint(
        "max_prop.motor_current_a",
        upper=float(motor.max_current),
        ref=max(float(motor.max_current), 1.0),
    )

    # Maximize endurance.
    model.add_objective("aircraft.negative_endurance_min", ref=30.0)

    prob.driver = om.ScipyOptimizeDriver(optimizer="SLSQP")
    prob.driver.options["disp"] = False
    prob.driver.options["maxiter"] = 180
    prob.driver.options["tol"] = 1e-6

    prob.setup()
    return prob


def scalar(prob, name):
    return float(np.asarray(prob.get_val(name)).ravel()[0])


def run_one_design(
    prop_entry,
    prop_catalog,
    motor,
    fixed_mass,
    battery_label,
    voltage,
):
    prob = build_problem(
        prop_entry,
        prop_catalog,
        motor,
        fixed_mass,
        voltage,
    )

    try:
        # Many catalog combinations are physically infeasible. OpenMDAO/SLSQP
        # prints a multi-line "Optimization FAILED" message for each one, which
        # makes a healthy database screen look broken. Silence those expected
        # per-case messages and simply discard non-converged combinations.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            driver_result = prob.run_driver()
        driver_success = bool(driver_result.success)
    except Exception:
        return None

    auw = scalar(prob, "mass.auw_kg")
    residual = scalar(prob, "aircraft.hover_residual_n")
    twr = scalar(prob, "aircraft.max_thrust_to_weight")
    max_current = scalar(prob, "max_prop.motor_current_a")

    # Sanity / feasibility checks.
    if not driver_success:
        return None
    if auw > MAX_AUW_KG + 1e-3:
        return None
    if abs(residual) > 0.05:
        return None
    if twr < MIN_MAX_THRUST_TO_WEIGHT - 1e-3:
        return None
    if max_current > motor.max_current + 0.1:
        return None
    if scalar(prob, "hover_prop.is_feasible") < 0.5:
        return None
    if scalar(prob, "max_prop.is_feasible") < 0.5:
        return None

    prop_cost_total_usd = N_ROTORS * float(prop_catalog["price_usd"])

    # IMPORTANT: this is currently only the known/real propeller portion of cost.
    # Do not claim it is the full build cost until motor/ESC/battery/frame price
    # data are added.
    known_component_cost_usd = prop_cost_total_usd

    if ENFORCE_TOTAL_BUILD_BUDGET:
        # Safety guard: enabling this now would undercount the total build.
        raise RuntimeError(
            "ENFORCE_TOTAL_BUILD_BUDGET cannot be enabled yet because "
            "motor/ESC/battery/frame prices are not populated."
        )

    return {
        "motor_id": motor.id,
        "manufacturer": motor.manufacturer,
        "motor_name": motor.name,
        "motor_kv": float(motor.kv),
        "motor_mass_g": float(motor.weight_g),
        "motor_resistance_mohm": 1000.0 * float(motor.resistance),
        "motor_io_a": float(motor.io),
        "motor_max_current_a": float(motor.max_current),
        "motor_max_power_w": float(motor.max_power),
        "battery_label": battery_label,
        "voltage_v": voltage,
        "fixed_mass_kg": fixed_mass,
        "prop_id": prop_entry.metadata.id,
        "prop_manufacturer": prop_entry.metadata.manufacturer,
        "prop_model": prop_entry.metadata.model,
        "prop_in": float(prop_entry.metadata.diameter_in),
        "prop_pitch_in": float(prop_entry.metadata.pitch_in),
        "prop_blades": int(prop_entry.metadata.blade_count),
        "prop_sku": prop_catalog["sku"],
        "prop_status": prop_catalog["status"],
        "prop_mass_each_g": float(prop_catalog["weight_g"]),
        "prop_price_each_usd": float(prop_catalog["price_usd"]),
        "prop_cost_total_usd": prop_cost_total_usd,
        "known_component_cost_usd": known_component_cost_usd,
        "hover_throttle_pct": 100.0 * scalar(prob, "hover_throttle"),
        "battery_energy_wh": scalar(prob, "battery_energy_wh"),
        "battery_mass_kg": scalar(prob, "mass.battery_mass_kg"),
        "frame_mass_kg": scalar(prob, "mass.frame_mass_kg"),
        "props_mass_kg": scalar(prob, "mass.props_mass_kg"),
        "auw_kg": auw,
        "hover_thrust_each_n": scalar(prob, "mass.required_hover_thrust_n"),
        "hover_power_total_w": scalar(prob, "aircraft.total_hover_power_w"),
        "hover_motor_current_a": scalar(prob, "hover_prop.motor_current_a"),
        "max_motor_current_actual_a": max_current,
        "max_twr": twr,
        "hover_rpm": scalar(prob, "hover_prop.rpm"),
        "endurance_min": scalar(prob, "aircraft.endurance_min"),
    }



def discrete_config_key(result):
    return (
        result["motor_id"],
        result["prop_id"],
        result["battery_label"],
    )


def choose_finalist_configs(screen_results):
    """Pick a diverse high-performing set for full sensitivity re-optimization."""
    ordered = sorted(
        screen_results,
        key=lambda r: (-r["endurance_min"], r["known_component_cost_usd"]),
    )

    finalists = []
    seen = set()
    per_motor = {}
    per_prop = {}

    for r in ordered:
        key = discrete_config_key(r)
        if key in seen:
            continue

        motor_id = r["motor_id"]
        prop_id = r["prop_id"]

        if per_motor.get(motor_id, 0) >= MAX_FINALISTS_PER_MOTOR:
            continue
        if per_prop.get(prop_id, 0) >= MAX_FINALISTS_PER_PROP:
            continue

        finalists.append(r)
        seen.add(key)
        per_motor[motor_id] = per_motor.get(motor_id, 0) + 1
        per_prop[prop_id] = per_prop.get(prop_id, 0) + 1

        if len(finalists) >= MAX_FINALIST_CONFIGS:
            break

    return finalists


# ===========================================================================
# REPORTING
# ===========================================================================

def print_design(r, rank=None):
    prefix = f"{rank}. " if rank is not None else ""
    print(f"{prefix}{r['manufacturer']} {r['motor_name']}")
    print(
        f"   Motor       : {r['motor_kv']:.0f} Kv | "
        f"{r['motor_mass_g']:.0f} g | "
        f"{r['motor_resistance_mohm']:.1f} mOhm | "
        f"Imax {r['motor_max_current_a']:.1f} A"
    )
    print(
        f"   Electrical  : {r['battery_label']} ({r['voltage_v']:.1f} V) | "
        f"battery {r['battery_energy_wh']:.0f} Wh"
    )
    print(
        f"   Prop        : {r['prop_manufacturer']} {r['prop_model']} | "
        f"{r['prop_in']:.2f}x{r['prop_pitch_in']:.2f} in | "
        f"{r['prop_blades']} blades | SKU {r['prop_sku']}"
    )
    print(
        f"   Prop data   : {r['prop_mass_each_g']:.1f} g each | "
        f"${r['prop_price_each_usd']:.2f} each | "
        f"${r['prop_cost_total_usd']:.2f} for {N_ROTORS} | "
        f"{r['prop_status']}"
    )
    print(
        f"   Hover       : {r['hover_throttle_pct']:.1f}% throttle | "
        f"{r['hover_rpm']:.0f} RPM"
    )
    print(
        f"   Aircraft    : AUW {r['auw_kg']:.3f} kg | "
        f"battery mass {r['battery_mass_kg']:.3f} kg | "
        f"frame est. {r['frame_mass_kg']:.3f} kg"
    )
    print(
        f"   Performance : {r['endurance_min']:.1f} min | "
        f"hover {r['hover_power_total_w']:.0f} W | "
        f"T/W {r['max_twr']:.2f}"
    )
    print(
        f"   Current     : hover {r['hover_motor_current_a']:.1f} A/motor | "
        f"max {r['max_motor_current_actual_a']:.1f} A/motor"
    )


def main():
    print("=== PyThrust Real-Motor + Real-Propeller Quadrotor Screening ===")
    prop_db, motor_db = load_databases()
    catalog_path, apc_catalog = load_apc_catalog()

    print(f"Loaded {motor_db.motor_count} real motor records from PyThrust.")
    print(f"Loaded {prop_db.propeller_count} real APC aerodynamic records from PyThrust.")
    print(f"Loaded {len(apc_catalog)} APC physical/price catalog records from {catalog_path.name}.")

    motors = select_motor_candidates(motor_db)
    props = select_prop_candidates(prop_db, apc_catalog)
    print(
        f"Fast broad filter retained {len(motors)} motors "
        f"(runtime cap = {MAX_MOTOR_CANDIDATES})."
    )
    print(
        f"Hover-oriented prop screen retained {len(props)} REAL APC props "
        f"(runtime cap = {MAX_PROP_CANDIDATES}; "
        f"pitch/diameter <= {MAX_HOVER_PITCH_DIAMETER_RATIO:.2f})."
    )
    print("Prop candidates:")
    for prop_entry, prop_catalog in props:
        md = prop_entry.metadata
        print(
            f"  - {md.id}: {md.diameter_in:.2f}x{md.pitch_in:.2f}, "
            f"{md.blade_count}-blade | "
            f"{prop_catalog['weight_g']:.1f} g | "
            f"${prop_catalog['price_usd']:.2f} | "
            f"{prop_catalog['status']}"
        )
    print(
        "\nNOTE: APC prop mass and price are now real catalog values. "
        "Battery, ESC, frame and non-prop prices are still simplified/missing.\n"
    )
    print(
        "Infeasible motor/voltage combinations are expected and will now be "
        "silently discarded instead of printing Optimization FAILED.\n"
    )

    # ------------------------------------------------------------------
    # STAGE 1: broad discrete catalog screen at one representative mass.
    # ------------------------------------------------------------------
    print("=" * 78)
    print(
        f"STAGE 1 — FAST CATALOG SCREEN AT FIXED MASS "
        f"{SCREENING_FIXED_MASS_KG:.2f} kg"
    )
    print("=" * 78)

    screen_results = []
    screen_total = len(motors) * len(props) * len(BATTERY_VOLTAGE_CASES_V)
    screen_run = 0

    # Keep object lookups so finalist result IDs can be mapped back to the
    # corresponding PyThrust entries without database rescans.
    motor_by_id = {m.id: m for m in motors}
    prop_by_id = {p.metadata.id: (p, c) for p, c in props}

    for motor in motors:
        for prop_entry, prop_catalog in props:
            for battery_label, voltage in BATTERY_VOLTAGE_CASES_V.items():
                screen_run += 1
                if screen_run % 50 == 0 or screen_run == screen_total:
                    print(
                        f"  Stage 1: {screen_run}/{screen_total} "
                        f"(valid: {len(screen_results)})...",
                        flush=True,
                    )

                result = run_one_design(
                    prop_entry,
                    prop_catalog,
                    motor,
                    SCREENING_FIXED_MASS_KG,
                    battery_label,
                    voltage,
                )
                if result is not None:
                    screen_results.append(result)

    if not screen_results:
        print("No valid designs survived Stage 1.")
        return

    finalists = choose_finalist_configs(screen_results)
    print(
        f"\nStage 1 found {len(screen_results)} valid designs. "
        f"Promoting {len(finalists)} diverse configurations to Stage 2."
    )

    print("\nTop Stage-1 finalists:")
    for idx, r in enumerate(finalists[:10], start=1):
        print(
            f"  {idx:2d}. {r['manufacturer']} {r['motor_name']} | "
            f"{r['prop_model']} | {r['battery_label']} | "
            f"{r['endurance_min']:.1f} min"
        )

    # Cache Stage-1 solutions so the 1.0-kg case is not solved twice.
    cache = {
        (SCREENING_FIXED_MASS_KG,) + discrete_config_key(r): r
        for r in screen_results
    }

    # ------------------------------------------------------------------
    # STAGE 2: sensitivity study only on the finalist discrete configs.
    # ------------------------------------------------------------------
    all_results = []

    for fixed_mass in FIXED_MASS_CASES_KG:
        print("\n" + "=" * 78)
        print(
            f"STAGE 2 — FINALISTS AT FIXED PAYLOAD / AVIONICS MASS: "
            f"{fixed_mass:.2f} kg"
        )
        print("=" * 78)

        case_results = []
        total_runs = len(finalists)

        for run_num, finalist in enumerate(finalists, start=1):
            key = (fixed_mass,) + discrete_config_key(finalist)

            if key in cache:
                result = cache[key]
            else:
                motor = motor_by_id[finalist["motor_id"]]
                prop_entry, prop_catalog = prop_by_id[finalist["prop_id"]]
                battery_label = finalist["battery_label"]
                voltage = BATTERY_VOLTAGE_CASES_V[battery_label]

                result = run_one_design(
                    prop_entry,
                    prop_catalog,
                    motor,
                    fixed_mass,
                    battery_label,
                    voltage,
                )
                if result is not None:
                    cache[key] = result

            if result is not None:
                case_results.append(result)
                all_results.append(result)

            if run_num % 8 == 0 or run_num == total_runs:
                print(
                    f"  Stage 2: {run_num}/{total_runs} "
                    f"(valid: {len(case_results)})...",
                    flush=True,
                )

        case_results.sort(
            key=lambda r: (-r["endurance_min"], r["known_component_cost_usd"])
        )

        if not case_results:
            print("No valid finalist designs found for this fixed-mass case.\n")
            continue

        print(
            f"\nValid finalist designs: {len(case_results)} | "
            f"showing top {min(TOP_RESULTS_PER_CASE, len(case_results))}\n"
        )

        for idx, result in enumerate(case_results[:TOP_RESULTS_PER_CASE], start=1):
            print_design(result, idx)
            print()

    if not all_results:
        print("No valid design converged across all cases.")
        return

    all_results.sort(key=lambda r: (-r["endurance_min"], r["known_component_cost_usd"]))

    print("\n" + "#" * 78)
    print("TOP OVERALL CURRENT SCREENING RESULT")
    print("#" * 78)
    print_design(all_results[0])

    # Save only useful summary outputs. Individual OpenMDAO run folders are
    # temporary and are deleted automatically.
    try:
        import csv

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Keep only the top designs, not thousands of intermediate files.
        top_n = min(GUIDE_RESULTS_TO_SAVE, len(all_results))
        top_results = all_results[:top_n]

        csv_path = RESULTS_DIR / "guide_search_designs.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(top_results[0].keys()))
            writer.writeheader()
            writer.writerows(top_results)

        # Human-readable best-result summary.
        best = all_results[0]
        summary_path = RESULTS_DIR / "best_fast_adaptive_design_summary.txt"
        with summary_path.open("w", encoding="utf-8") as f:
            f.write("BEST CURRENT REAL-MOTOR SCREENING RESULT\n")
            f.write("=" * 52 + "\n")
            f.write(f"Motor: {best['manufacturer']} {best['motor_name']}\n")
            f.write(f"Motor Kv: {best['motor_kv']:.0f} RPM/V\n")
            f.write(f"Motor mass: {best['motor_mass_g']:.1f} g each\n")
            f.write(f"Battery voltage: {best['battery_label']} ({best['voltage_v']:.1f} V)\n")
            f.write(f"Battery energy: {best['battery_energy_wh']:.1f} Wh\n")
            f.write(f"Estimated battery mass: {best['battery_mass_kg']:.3f} kg\n")
            f.write(f"Propeller: {best['prop_manufacturer']} {best['prop_model']}\n")
            f.write(f"Prop size: {best['prop_in']:.2f} x {best['prop_pitch_in']:.2f} in, {best['prop_blades']}-blade\n")
            f.write(f"Prop SKU: {best['prop_sku']}\n")
            f.write(f"Prop mass: {best['prop_mass_each_g']:.1f} g each ({best['props_mass_kg']:.3f} kg total)\n")
            f.write(f"Prop price: ${best['prop_price_each_usd']:.2f} each (${best['prop_cost_total_usd']:.2f} total)\n")
            f.write(f"Prop status: {best['prop_status']}\n")
            f.write(f"AUW: {best['auw_kg']:.3f} kg\n")
            f.write(f"Estimated hover endurance: {best['endurance_min']:.1f} min\n")
            f.write(f"Total hover power: {best['hover_power_total_w']:.1f} W\n")
            f.write(f"Max thrust/weight: {best['max_twr']:.2f}\n")
            f.write("\n")
            f.write(
                "Concept screening only: APC prop mass/price are real catalog "
                "values. Battery, ESC, frame, and non-prop prices are still "
                "simplified or missing.\n"
            )

        # Save one useful overview plot.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9.6, 5.4))
        x = [r["auw_kg"] for r in all_results]
        y = [r["endurance_min"] for r in all_results]
        ax.scatter(x, y, s=18, alpha=0.50)
        ax.scatter(
            [best["auw_kg"]],
            [best["endurance_min"]],
            s=90,
            marker="*",
            label="Best endurance",
        )
        ax.set_xlabel("All-up weight (kg)")
        ax.set_ylabel("Estimated hover endurance (min)")
        ax.set_title("Real motor + APC mass/price prop screening: endurance vs aircraft mass")
        ax.grid(True)
        ax.legend()
        fig.tight_layout()

        plot_path = RESULTS_DIR / "fast_adaptive_endurance_vs_mass.png"
        fig.savefig(plot_path, dpi=170)
        plt.close(fig)

        # Second plot uses only the currently known real propeller cost. It is
        # intentionally labeled as partial cost so it cannot be mistaken for
        # full vehicle cost.
        fig2, ax2 = plt.subplots(figsize=(9.6, 5.4))
        x2 = [r["prop_cost_total_usd"] for r in all_results]
        y2 = [r["endurance_min"] for r in all_results]
        ax2.scatter(x2, y2, s=18, alpha=0.50)
        ax2.scatter(
            [best["prop_cost_total_usd"]],
            [best["endurance_min"]],
            s=90,
            marker="*",
            label="Best endurance",
        )
        ax2.set_xlabel(f"APC propeller set cost for {N_ROTORS} props (USD)")
        ax2.set_ylabel("Estimated hover endurance (min)")
        ax2.set_title("Endurance vs real APC propeller cost (partial build cost only)")
        ax2.grid(True)
        ax2.legend()
        fig2.tight_layout()
        cost_plot_path = RESULTS_DIR / "endurance_vs_apc_prop_cost.png"
        fig2.savefig(cost_plot_path, dpi=170)
        plt.close(fig2)

        print(f"\nSaved only final summary outputs in:")
        print(f"  {RESULTS_DIR}")
        print(f"    - {csv_path.name}")
        print(f"    - {summary_path.name}")
        print(f"    - {plot_path.name}")
        print(f"    - {cost_plot_path.name}")

    except Exception as exc:
        print(f"\nCould not save final summary files: {exc}")

    print("\nINTERPRETATION")
    print(
        "The selected motor and propeller are discrete PyThrust entries, and "
        "the APC propeller mass, SKU, stock status, and price now come from the "
        "manufacturer catalog. The reported cost is still only the known real "
        "propeller portion of build cost. Battery pack, ESC, frame, and the "
        "remaining component prices still need real component data before the "
        "$1000 total-build cap can be enforced honestly."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        # Remove all temporary per-run OpenMDAO output directories.
        cleanup_openmdao_workdir()
