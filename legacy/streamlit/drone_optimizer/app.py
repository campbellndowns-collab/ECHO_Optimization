from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import sqlite3
import sys

import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DRONE_OPTIMIZER_DATA_ROOT", str(PROJECT_ROOT))

from drone_optimizer.db import (
    canonical_counts,
    source_counts,
    component_rows,
    searchable_counts,
    pricing_coverage_counts,
    warehouse_modified_iso,
)
from drone_optimizer.quality import add_scores, readiness_summary
from drone_optimizer.screening import fast_component_prescreen
from drone_optimizer.jobs import refresh_database, run_detailed_optimizer
from drone_optimizer.diagnostics import run_diagnostics


def load_config():
    return json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))


CFG = load_config()
WAREHOUSE = PROJECT_ROOT / CFG["warehouse_path"]
BUILDER = PROJECT_ROOT / CFG["builder_path"]
DETAILED_OPT = PROJECT_ROOT / CFG["detailed_optimizer_path"]
LEGACY_OPT = PROJECT_ROOT / CFG["legacy_optimizer_path"]
RESULTS_CSV = PROJECT_ROOT / CFG["results_path"]
PARETO_CSV = PROJECT_ROOT / CFG["pareto_path"]
SUMMARY_TXT = PROJECT_ROOT / CFG["summary_path"]
REJECTION_CSV = PROJECT_ROOT / CFG["rejection_path"]
EXCEPTION_CSV = PROJECT_ROOT / CFG["exception_path"]
RUN_SETTINGS = RESULTS_CSV.parent / "run_settings.json"
DECISION_TOP25_CSV = RESULTS_CSV.parent / "decision_matrix_top25.csv"
DECISION_ALL_CSV = RESULTS_CSV.parent / "decision_matrix_all.csv"
PROPULSION_CACHE_DB = (
    PROJECT_ROOT / "component_data" / "propulsion_performance_cache.sqlite"
)
PHYSICAL_POOL_CSV = RESULTS_CSV.parent / "physical_design_pool.csv"
PHYSICAL_POOL_META = RESULTS_CSV.parent / "physical_design_pool_metadata.json"


SEARCH_DEPTHS = {
    "Standard — fastest": {
        "key": "standard",
        "description": (
            "18 motor candidates × 10 prop candidates in the guide stage, "
            "24 guide finalists, up to 140 detailed real-component solves."
        ),
    },
    "Thorough — recommended": {
        "key": "thorough",
        "description": (
            "36 motor candidates × 18 prop candidates, 60 guide finalists, "
            "up to 500 detailed real-component solves."
        ),
    },
    "Deep — broadest / slowest": {
        "key": "deep",
        "description": (
            "64 motor candidates × 28 prop candidates, 110 guide finalists, "
            "up to 1,000 detailed real-component solves. This can take much longer."
        ),
    },
}



def performance_cache_status():
    curves = 0
    if PROPULSION_CACHE_DB.exists():
        try:
            con = sqlite3.connect(str(PROPULSION_CACHE_DB))
            try:
                curves = int(
                    con.execute(
                        "SELECT COUNT(*) FROM propulsion_curves"
                    ).fetchone()[0]
                )
            finally:
                con.close()
        except Exception:
            curves = 0

    validated = 0
    if PHYSICAL_POOL_META.exists():
        try:
            meta = json.loads(
                PHYSICAL_POOL_META.read_text(encoding="utf-8")
            )
            validated = int(meta.get("validated_designs", 0))
        except Exception:
            validated = 0

    return curves, validated




@st.cache_data(show_spinner=False, ttl=600)
def selectable_local_motors(project_root_str):
    """
    Load the actual local PyThrust motor physics database so a locked motor ID
    is guaranteed to be meaningful to the propulsion solver.
    """
    from pythrust.motors import MotorDatabase

    project_root = Path(project_root_str)
    db = MotorDatabase()
    motor_dir = project_root / "pythrust" / "data" / "motors"
    if not db.load(motor_dir):
        return []

    motors = db.search(
        min_kv=1,
        max_kv=100000,
        min_max_current=0,
        min_weight=0,
        max_weight=100000,
    )
    out = []
    for m in motors:
        try:
            if not (
                float(m.kv) > 0
                and float(m.resistance) > 0
                and float(m.max_current) > 0
                and float(m.weight_g) > 0
            ):
                continue
        except Exception:
            continue

        label = (
            f"{m.manufacturer} {m.name} · "
            f"{float(m.kv):.0f} KV · "
            f"{float(m.weight_g):.0f} g · "
            f"{float(m.max_current):.1f} A max"
        )
        out.append({
            "id": str(m.id),
            "label": label,
            "manufacturer": str(m.manufacturer),
            "model": str(m.name),
        })

    out.sort(
        key=lambda r: (
            r["manufacturer"].lower(),
            r["model"].lower(),
        )
    )
    return out


@st.cache_data(show_spinner=False, ttl=600)
def selectable_local_props(project_root_str):
    """
    Load local APC aerodynamic records and retain only entries that also match
    the bundled physical/price APC catalog used by the optimizer.
    """
    from pythrust.propellers import PropellerDatabase
    import csv

    project_root = Path(project_root_str)
    db = PropellerDatabase()
    prop_dir = (
        project_root / "pythrust" / "data"
        / "propellers" / "apc_202602"
    )
    if not db.load(prop_dir, strict=False):
        return []

    catalog_paths = [
        project_root / "data_seed" / "apc_catalog_202602.csv",
        project_root / "apc_catalog_202602.csv",
    ]
    catalog_path = next(
        (p for p in catalog_paths if p.exists()), None
    )
    catalog = {}
    if catalog_path:
        with catalog_path.open(
            "r", newline="", encoding="utf-8-sig"
        ) as f:
            for row in csv.DictReader(f):
                key = (
                    str(row.get("product_name") or "")
                    .strip().upper()
                    .replace("APC_", "")
                    .replace("×", "X")
                    .replace(" ", "")
                )
                catalog[key] = row

    out = []
    for pid in db.list_propellers():
        p = db.get(pid)
        if p is None or not getattr(p, "data_by_rpm", None):
            continue
        md = p.metadata
        key = (
            str(md.model or "")
            .strip().upper()
            .replace("APC_", "")
            .replace("×", "X")
            .replace(" ", "")
        )
        row = catalog.get(key)
        if catalog and row is None:
            continue

        price = None
        mass = None
        if row:
            try:
                price = float(row.get("price_usd"))
            except Exception:
                pass
            try:
                mass = float(row.get("weight_g"))
            except Exception:
                pass

        extras = []
        if mass is not None:
            extras.append(f"{mass:.0f} g")
        if price is not None:
            extras.append(f"${price:.2f}")
        suffix = " · " + " · ".join(extras) if extras else ""
        label = (
            f"{md.manufacturer} {md.model} · "
            f"{float(md.diameter_in):.1f}×{float(md.pitch_in):.1f} in · "
            f"{int(md.blade_count)} blade"
            f"{suffix}"
        )
        out.append({
            "id": str(md.id),
            "label": label,
            "diameter_in": float(md.diameter_in),
            "pitch_in": float(md.pitch_in),
        })

    out.sort(
        key=lambda r: (
            r["diameter_in"], r["pitch_in"], r["label"]
        )
    )
    return out


def _warehouse_selector_rows(dataset, label_builder):
    if not WAREHOUSE.exists():
        return []
    df = component_rows(WAREHOUSE, dataset)
    if df.empty:
        return []

    rows = []
    seen = set()
    for _, r in df.iterrows():
        cid = str(r.get("canonical_id", "") or "")
        if not cid or cid in seen:
            continue
        try:
            label = label_builder(r)
        except Exception:
            continue
        if not label:
            continue
        seen.add(cid)
        rows.append({"id": cid, "label": label})
    rows.sort(key=lambda x: x["label"].lower())
    return rows


@st.cache_data(show_spinner=False, ttl=300)
def selectable_battery_packs(warehouse_str, warehouse_mtime):
    return _warehouse_selector_rows(
        "battery_packs",
        lambda r: (
            f"{str(r.get('manufacturer') or '').strip()} "
            f"{str(r.get('model') or '').strip()} · "
            f"{int(float(r.get('series_cells')))}S · "
            f"{float(r.get('capacity_ah')):.2f} Ah · "
            f"{float(r.get('mass_g')):.0f} g"
            if pd.notna(r.get("series_cells"))
            and pd.notna(r.get("capacity_ah"))
            and pd.notna(r.get("mass_g"))
            else None
        ),
    )


@st.cache_data(show_spinner=False, ttl=300)
def selectable_battery_cells(warehouse_str, warehouse_mtime):
    return _warehouse_selector_rows(
        "battery_cells",
        lambda r: (
            f"{str(r.get('manufacturer') or '').strip()} "
            f"{str(r.get('model') or '').strip()} · "
            f"{float(r.get('nominal_voltage_v')):.2f} V · "
            f"{float(r.get('capacity_ah')):.2f} Ah · "
            f"{float(r.get('mass_g')):.1f} g"
            if pd.notna(r.get("nominal_voltage_v"))
            and pd.notna(r.get("capacity_ah"))
            and pd.notna(r.get("mass_g"))
            else None
        ),
    )


@st.cache_data(show_spinner=False, ttl=300)
def selectable_escs(warehouse_str, warehouse_mtime):
    return _warehouse_selector_rows(
        "escs",
        lambda r: (
            f"{str(r.get('manufacturer') or '').strip()} "
            f"{str(r.get('model') or '').strip()} · "
            f"{float(r.get('continuous_current_a')):.0f} A · "
            f"up to {int(float(r.get('max_cells')))}S · "
            f"{float(r.get('mass_g')):.0f} g"
            if pd.notna(r.get("continuous_current_a"))
            and pd.notna(r.get("max_cells"))
            and pd.notna(r.get("mass_g"))
            else None
        ),
    )


def part_selectbox(label, options, key):
    if not options:
        st.warning(f"No selectable {label.lower()} records are available.")
        return None
    ids = [x["id"] for x in options]
    labels = {x["id"]: x["label"] for x in options}
    return st.selectbox(
        label,
        ids,
        format_func=lambda x: labels.get(x, x),
        key=key,
    )



def format_utc_iso(value):
    if not value:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def local_physics_library_counts():
    motor_dir = PROJECT_ROOT / "pythrust" / "data" / "motors"
    prop_dir = PROJECT_ROOT / "pythrust" / "data" / "propellers" / "apc_202602"
    motors = len(list(motor_dir.rglob("*.json"))) if motor_dir.exists() else 0
    props = len(list(prop_dir.rglob("*.json"))) if prop_dir.exists() else 0
    return motors, props


def friendly_design_table(df: pd.DataFrame, limit=None):
    if df.empty:
        return df

    view = df.copy()
    if limit is not None:
        view = view.head(limit).copy()

    def s(col, default=""):
        if col in view.columns:
            return view[col].fillna(default).astype(str)
        return pd.Series(default, index=view.index)

    def n(col):
        if col in view.columns:
            return pd.to_numeric(view[col], errors="coerce")
        return pd.Series(float("nan"), index=view.index)

    motor = (s("motor_manufacturer") + " " + s("motor_model")).str.strip()
    prop = s("prop_model")
    battery = (
        s("battery_manufacturer")
        + " "
        + s("battery_model")
        + " "
        + s("battery_topology")
    ).str.replace(r"\s+", " ", regex=True).str.strip()
    esc = (s("esc_manufacturer") + " " + s("esc_model")).str.strip()

    out = pd.DataFrame({
        "Rank": range(1, len(view) + 1),
        "Endurance (min)": n("endurance_min").round(1),
        "AUW (kg)": n("auw_kg").round(3),
        "Frame (kg)": n("frame_mass_kg").round(3),
        "Motor": motor,
        "Propeller": prop,
        "Battery": battery,
        "ESC": esc,
        "Max T/W": n("max_twr").round(2),
        "Hover power (W)": n("hover_power_total_w").round(0),
        "Hover throttle (%)": n("hover_throttle_pct").round(1),
        "Propulsion cost ($)": n("propulsion_cost_usd").round(2),
        "Price coverage (%)": n("propulsion_cost_coverage_pct").round(0),
        "Est. whole-aircraft cost ($)": n("whole_aircraft_estimated_cost_usd").round(2),
    })
    return out




def rerank_existing_designs(df, weights):
    """
    Re-rank an already exact-validated physical design pool without rerunning
    PyThrust. Criterion scores are physical outputs; only their weighted sum
    changes.
    """
    if df.empty:
        return df

    out = df.copy()
    total = sum(float(v) for v in weights.values())
    if total <= 0:
        total = 1.0
    w = {
        k: 100.0 * float(v) / total
        for k, v in weights.items()
    }

    required = {
        "cost": "decision_cost_score",
        "endurance": "decision_endurance_score",
        "weight": "decision_weight_score",
        "quality": "decision_quality_score",
        "complexity": "decision_complexity_score",
    }
    for col in required.values():
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    out["decision_matrix_score"] = (
        w["cost"] * out[required["cost"]]
        + w["endurance"] * out[required["endurance"]]
        + w["weight"] * out[required["weight"]]
        + w["quality"] * out[required["quality"]]
        + w["complexity"] * out[required["complexity"]]
    ) / 100.0

    out = out.sort_values(
        [
            "decision_matrix_score",
            "decision_total_estimated_cost_usd",
            "endurance_min",
        ],
        ascending=[False, True, False],
    ).reset_index(drop=True)
    out["decision_rank"] = range(1, len(out) + 1)
    return out


def decision_matrix_table(df: pd.DataFrame, limit=25):
    if df.empty:
        return df

    view = df.head(limit).copy()

    def s(col, default=""):
        if col in view.columns:
            return view[col].fillna(default).astype(str)
        return pd.Series(default, index=view.index)

    def n(col):
        if col in view.columns:
            return pd.to_numeric(view[col], errors="coerce")
        return pd.Series(float("nan"), index=view.index)

    motor = (s("motor_manufacturer") + " " + s("motor_model")).str.strip()
    battery = (
        s("battery_manufacturer") + " " + s("battery_model")
        + " " + s("battery_topology")
    ).str.replace(r"\s+", " ", regex=True).str.strip()

    rank = (
        n("decision_rank").fillna(pd.Series(range(1, len(view)+1), index=view.index))
        .astype(int)
    )

    return pd.DataFrame({
        "Rank": rank,
        "Matrix score": n("decision_matrix_score").round(1),
        "Est. total cost ($)": n("decision_total_estimated_cost_usd").round(0),
        "Endurance (min)": n("endurance_min").round(1),
        "AUW (kg)": n("auw_kg").round(3),
        "Quality": n("decision_quality_score").round(1),
        "Buildability": n("decision_complexity_score").round(1),
        "Frame (kg)": n("frame_mass_kg").round(3),
        "Motor": motor,
        "Prop": s("prop_model"),
        "Battery": battery,
        "ESC": (s("esc_manufacturer") + " " + s("esc_model")).str.strip(),
        "Price estimates used": s("decision_price_estimated_categories"),
        "Approx. parts": n("decision_estimated_part_count").round(0),
        "DIY subsystems": n("decision_diy_subsystems").round(0),
    })



def design_label(row, rank):
    return (
        f"#{rank} · {float(row['endurance_min']):.1f} min · "
        f"{float(row['auw_kg']):.2f} kg · "
        f"{row.get('motor_model','')} + {row.get('prop_model','')}"
    )


def component_summary(row):
    def val(name, default=None):
        v = row.get(name, default)
        return v

    motor_name = " ".join(
        x for x in [
            str(val("motor_manufacturer", "") or "").strip(),
            str(val("motor_model", "") or "").strip(),
        ] if x
    )
    esc_name = " ".join(
        x for x in [
            str(val("esc_manufacturer", "") or "").strip(),
            str(val("esc_model", "") or "").strip(),
        ] if x
    )
    battery_name = " ".join(
        x for x in [
            str(val("battery_manufacturer", "") or "").strip(),
            str(val("battery_model", "") or "").strip(),
            str(val("battery_topology", "") or "").strip(),
        ] if x
    )

    return pd.DataFrame([
        {
            "System": "Motors",
            "Selected component": motor_name,
            "Qty": 4,
            "Key data": (
                f"{float(val('motor_kv',0)):.0f} KV · "
                f"{float(val('motor_mass_each_g',0)):.0f} g each · "
                f"{float(val('motor_max_current_a',0)):.1f} A motor limit"
            ),
        },
        {
            "System": "Propellers",
            "Selected component": str(val("prop_model", "")),
            "Qty": 4,
            "Key data": (
                f"{float(val('prop_diameter_in',0)):.1f} × "
                f"{float(val('prop_pitch_in',0)):.1f} in · "
                f"{float(val('prop_mass_each_g',0)):.0f} g each"
            ),
        },
        {
            "System": "Battery",
            "Selected component": battery_name,
            "Qty": 1,
            "Key data": (
                f"{float(val('battery_voltage_v',0)):.1f} V · "
                f"{float(val('battery_energy_wh',0)):.0f} Wh · "
                f"{float(val('battery_mass_kg',0)):.2f} kg · "
                f"{float(val('battery_max_cont_current_a',0)):.0f} A continuous"
            ),
        },
        {
            "System": "ESCs",
            "Selected component": esc_name,
            "Qty": 4,
            "Key data": (
                f"{float(val('esc_cont_current_a',0)):.0f} A continuous · "
                f"{float(val('esc_mass_each_g',0)):.0f} g each"
            ),
        },
        {
            "System": "Frame",
            "Selected component": (
                f"{str(val('frame_material','Carbon fiber'))} · "
                f"{float(val('frame_arm_tube_od_mm',0)):.0f} × "
                f"{float(val('frame_arm_tube_wall_mm',0)):.1f} mm tube arms"
            ),
            "Qty": 1,
            "Key data": (
                f"{float(val('frame_mass_kg',0)):.3f} kg · "
                f"{float(val('frame_motor_spacing_m',0))*1000:.0f} mm motor spacing · "
                f"{float(val('frame_arm_tip_deflection_mm',0)):.1f} mm design-load tip deflection"
            ),
        },
    ])


def performance_summary(row):
    items = [
        (
            "Estimated hover endurance",
            f"{float(row.get('endurance_min',0)):.1f} min",
            "Idealized steady-hover endurance using the modeled usable battery energy.",
        ),
        (
            "All-up weight (AUW)",
            f"{float(row.get('auw_kg',0)):.3f} kg",
            "Estimated total aircraft mass represented by the current model.",
        ),
        (
            "Maximum thrust-to-weight",
            f"{float(row.get('max_twr',0)):.2f}",
            "Total full-throttle thrust divided by aircraft weight. Higher means more control/thrust margin.",
        ),
        (
            "Hover electrical power",
            f"{float(row.get('hover_power_total_w',0)):.0f} W",
            "Electrical power required by all four motors plus the accessory load while hovering.",
        ),
        (
            "Hover throttle",
            f"{float(row.get('hover_throttle_pct',0)):.1f}%",
            "Throttle required for the modeled propulsors to exactly balance aircraft weight.",
        ),
        (
            "Hover battery current",
            f"{float(row.get('hover_pack_current_a',0)):.1f} A",
            "Estimated pack current during hover.",
        ),
        (
            "Full-power battery current",
            f"{float(row.get('max_pack_current_a',0)):.1f} A",
            "Estimated pack current at the modeled full-throttle condition.",
        ),
        (
            "Frame structural allowance",
            f"{float(row.get('frame_mass_kg',0)):.3f} kg",
            (
                "Concept-stage carbon-frame mass from discrete tube sizing, "
                "bending stress, arm deflection, carbon plates, battery tray, "
                "joints/hardware, and landing-support allowance."
            ),
        ),
        (
            "Decision-matrix total cost",
            f"${float(row.get('decision_total_estimated_cost_usd',0)):.0f}",
            (
                "Motor + prop + battery + ESC cost, plus the fixed $220 frame "
                "allowance and $700 DIY camera/avionics allowance. Any missing "
                "propulsion categories use visible fallback estimates."
            ),
        ),
        (
            "Price estimates used",
            str(row.get("decision_price_estimated_categories", "none")),
            "Lists propulsion categories whose price was estimated rather than sourced.",
        ),
    ]
    return pd.DataFrame(items, columns=["Metric", "Value", "What it means"])


def show_data_snapshot_from_settings(settings):
    snapshot = settings.get("data_snapshot", {}) if isinstance(settings, dict) else {}
    tables = snapshot.get("tables", {}) if isinstance(snapshot, dict) else {}
    if not tables:
        return

    rows = []
    friendly = {
        "motors_master": "Motors",
        "props_master": "Props",
        "battery_cells_master": "Battery cells",
        "battery_packs_master": "Battery packs",
        "escs_master": "ESCs",
    }
    for table, vals in tables.items():
        if not isinstance(vals, dict) or "error" in vals:
            continue
        rows.append({
            "Dataset": friendly.get(table, table),
            "Source rows": vals.get("source_rows", 0),
            "Unique/canonical": vals.get("canonical_parts", 0),
            "Warehouse optimizer-ready": vals.get(
                "warehouse_optimizer_eligible_rows", 0
            ),
        })

    st.caption(
        "Warehouse snapshot used by this optimization: "
        f"{format_utc_iso(snapshot.get('modified_utc'))}"
    )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


st.set_page_config(
    page_title="Drone Optimizer",
    page_icon="🛩️",
    layout="wide",
)

st.title("Drone Optimizer v0.7")
st.caption(
    "Data-first component search → broad screening → real-component physics → "
    "clear mass/endurance/cost trade studies"
)

tabs = st.tabs([
    "Home",
    "Data Library",
    "Fast Screen",
    "Components",
    "Integrated Run",
    "Results",
    "Logs",
])
tab_home, tab_data, tab_screen, tab_components, tab_run, tab_results, tab_logs = tabs


# ---------------------------------------------------------------------------
# HOME
# ---------------------------------------------------------------------------
with tab_home:
    st.subheader("System readiness")
    checks = run_diagnostics(PROJECT_ROOT, CFG)
    check_df = pd.DataFrame(checks)
    if not check_df.empty:
        check_df["Status"] = check_df["ok"].map(
            {True: "✓ Ready", False: "✗ Needs attention"}
        )
        st.dataframe(
            check_df[["Status", "check", "detail"]],
            use_container_width=True,
            hide_index=True,
        )
        failed = check_df[~check_df["ok"]]
        if failed.empty:
            st.success("All required application pieces are present.")
        else:
            st.warning(
                "One or more prerequisites are missing. Fix the items shown above "
                "before starting a full optimization."
            )

    if WAREHOUSE.exists():
        counts = canonical_counts(WAREHOUSE)
        if not counts.empty:
            st.markdown("#### Current component library")
            cols = st.columns(len(counts))
            for col, (_, row) in zip(cols, counts.iterrows()):
                col.metric(
                    row["dataset"].replace("_", " ").title(),
                    f'{int(row["canonical_parts"]):,}',
                    f'{int(row["optimizer_eligible_rows"]):,} strict-ready',
                )
            st.caption(
                "These are warehouse counts, not the number receiving expensive "
                "physics solves. Open Data Library for the important distinction."
            )

    st.markdown("#### Performance caches")
    cached_curves, validated_pool = performance_cache_status()
    pc1, pc2 = st.columns(2)
    pc1.metric(
        "Cached propulsion curves",
        f"{cached_curves:,}",
        help=(
            "Reusable PyThrust motor + prop + voltage response curves. "
            "This cache grows as you explore new combinations."
        ),
    )
    pc2.metric(
        "Exact-validated aircraft pool",
        f"{validated_pool:,}",
        help=(
            "Physical aircraft designs that can be re-ranked without rerunning "
            "PyThrust when only decision weights change."
        ),
    )

    st.markdown("#### Recommended workflow")
    st.write(
        "**1. Update Data Library → 2. choose Thorough or Deep search → "
        "3. run Integrated Run → 4. inspect the clean shortlist and Pareto trade space.**"
    )


# ---------------------------------------------------------------------------
# DATA LIBRARY
# ---------------------------------------------------------------------------
with tab_data:
    st.subheader("Component data library")
    st.write(
        "The optimizer keeps a broad discovery warehouse, but a component only "
        "becomes useful for physics when the fields needed by the model are present. "
        "A huge raw row count is therefore not the same thing as a huge usable design space."
    )

    if not WAREHOUSE.exists():
        st.error("Component warehouse does not exist yet.")
    else:
        modified = warehouse_modified_iso(WAREHOUSE)
        st.info(f"Warehouse last rebuilt: **{format_utc_iso(modified)}**")

        raw = canonical_counts(WAREHOUSE)
        usable = searchable_counts(WAREHOUSE)
        merged = raw.merge(usable, on="dataset", how="left")
        merged = merged.rename(columns={
            "dataset": "Dataset",
            "source_rows": "Source rows",
            "canonical_parts": "Unique / canonical",
            "optimizer_eligible_rows": "Strict optimizer-ready",
            "searchable_rows": "Engineering-field complete",
            "definition": "Minimum useful fields",
        })
        merged["Dataset"] = (
            merged["Dataset"]
            .str.replace("_", " ", regex=False)
            .str.title()
        )
        st.dataframe(merged, use_container_width=True, hide_index=True)

        motor_files, prop_files = local_physics_library_counts()
        a, b = st.columns(2)
        a.metric(
            "Local PyThrust motor records",
            f"{motor_files:,}",
            help=(
                "The propulsion guide can directly model motors that have a "
                "PyThrust electrical model."
            ),
        )
        b.metric(
            "Local APC aero prop records",
            f"{prop_files:,}",
            help=(
                "These have aerodynamic coefficient data rather than only "
                "diameter/pitch/catalog metadata."
            ),
        )

        st.markdown("#### Pricing coverage")
        price_cov = pricing_coverage_counts(WAREHOUSE)
        if not price_cov.empty:
            price_view = price_cov.rename(columns={
                "dataset": "Dataset",
                "canonical_parts": "Unique components",
                "priced_canonical_parts": "Components with price",
                "price_coverage_pct": "Price coverage (%)",
            })
            price_view["Dataset"] = (
                price_view["Dataset"]
                .str.replace("_", " ", regex=False)
                .str.title()
            )
            st.dataframe(
                price_view,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Strict budget optimization only accepts a design when the "
                "required propulsion-component prices are actually known. "
                "This table shows where price collection still needs work."
            )

        st.warning(
            "Important: the integrated motor/prop guide currently uses the local "
            "PyThrust motor models and APC aerodynamic prop records. Extra warehouse "
            "rows without motor electrical parameters or propeller Ct/Cp data are "
            "valuable for discovery, but they are **not** silently treated as physics-ready."
        )

        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "Update all component sources now",
                type="primary",
                use_container_width=True,
            ):
                with st.spinner(
                    "Refreshing remote sources, rebuilding normalized tables, "
                    "and updating SQLite..."
                ):
                    result = refresh_database(
                        PROJECT_ROOT, BUILDER, refresh=True
                    )
                st.session_state["last_refresh_job"] = result
                st.session_state["last_job"] = result
                if result["returncode"] == 0:
                    st.success(
                        f"Full data refresh finished in {result['elapsed_s']} s."
                    )
                    st.rerun()
                else:
                    st.error("Data refresh failed. See Logs.")

        with c2:
            if st.button(
                "Rebuild from cached/local data",
                use_container_width=True,
            ):
                with st.spinner("Rebuilding warehouse from existing caches..."):
                    result = refresh_database(
                        PROJECT_ROOT, BUILDER, refresh=False
                    )
                st.session_state["last_refresh_job"] = result
                st.session_state["last_job"] = result
                if result["returncode"] == 0:
                    st.success(
                        f"Warehouse rebuild finished in {result['elapsed_s']} s."
                    )
                    st.rerun()
                else:
                    st.error("Warehouse rebuild failed. See Logs.")

        with st.expander("Source-by-source coverage"):
            sc = source_counts(WAREHOUSE)
            if not sc.empty:
                st.dataframe(sc, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# FAST SCREEN
# ---------------------------------------------------------------------------
with tab_screen:
    st.subheader("Fast component screen")
    st.caption(
        "A cheap exploratory filter. This does not replace the Integrated Run; "
        "it lets you inspect which cells, packs, ESCs, motors, and props look "
        "plausible before detailed physics."
    )

    defaults = CFG["default_constraints"]
    c1, c2, c3, c4 = st.columns(4)
    fixed_mass = c1.number_input(
        "Fixed payload / avionics mass (kg)",
        0.1, 8.0, float(defaults["fixed_mass_kg"]), 0.1,
        key="screen_fixed_mass",
    )
    max_auw = c2.number_input(
        "Max AUW (kg)",
        0.5, 25.0, float(defaults["max_auw_kg"]), 0.5,
        key="screen_max_auw",
    )
    min_tw = c3.number_input(
        "Minimum T/W",
        1.0, 4.0, float(defaults["min_thrust_weight"]), 0.1,
        key="screen_min_tw",
    )
    budget = c4.number_input(
        "Budget ($)",
        100.0, 10000.0, float(defaults["budget_usd"]), 50.0,
        key="screen_budget",
    )

    c5, c6, c7 = st.columns(3)
    min_prop = c5.number_input(
        "Minimum prop diameter (in)",
        5.0, 30.0, float(defaults["min_prop_in"]), 0.5,
    )
    max_prop = c6.number_input(
        "Maximum prop diameter (in)",
        6.0, 40.0, float(defaults["max_prop_in"]), 0.5,
    )
    overhead = c7.slider(
        "Custom battery pack mass overhead",
        0.00, 0.30, float(defaults["pack_mass_overhead_fraction"]), 0.01,
        key="screen_pack_overhead",
    )

    if st.button("Run fast database screen", type="primary"):
        if not WAREHOUSE.exists():
            st.error("Build the component warehouse first.")
        else:
            constraints = {
                "fixed_mass_kg": fixed_mass,
                "max_auw_kg": max_auw,
                "min_thrust_weight": min_tw,
                "budget_usd": budget,
                "min_prop_in": min_prop,
                "max_prop_in": max_prop,
                "pack_mass_overhead_fraction": overhead,
            }
            result = fast_component_prescreen(
                component_rows(WAREHOUSE, "motors"),
                component_rows(WAREHOUSE, "props"),
                component_rows(WAREHOUSE, "battery_cells"),
                component_rows(WAREHOUSE, "battery_packs"),
                component_rows(WAREHOUSE, "escs"),
                constraints,
            )
            st.session_state["screen_result"] = result
            st.session_state["screen_constraints"] = constraints

    result = st.session_state.get("screen_result")
    if result:
        labels = [
            ("Motors", "motors"),
            ("Props", "props"),
            ("Eligible cells", "cells"),
            ("Custom pack topologies", "custom_packs"),
            ("Commercial packs", "commercial_packs"),
            ("ESCs", "escs"),
        ]
        metric_cols = st.columns(6)
        for col, (label, key) in zip(metric_cols, labels):
            col.metric(label, f"{len(result[key]):,}")

        for label, key in labels:
            with st.expander(label):
                df = result[key]
                if df.empty:
                    st.warning("No candidates passed this exploratory filter.")
                else:
                    st.dataframe(
                        df.head(150),
                        use_container_width=True,
                        hide_index=True,
                    )


# ---------------------------------------------------------------------------
# COMPONENT EXPLORER
# ---------------------------------------------------------------------------
with tab_components:
    st.subheader("Component explorer")

    if WAREHOUSE.exists():
        dataset = st.selectbox(
            "Dataset",
            ["motors", "props", "battery_cells", "battery_packs", "escs"],
        )
        df = component_rows(WAREHOUSE, dataset)
        scored = add_scores(df, dataset)
        summary = readiness_summary(df, dataset)

        a, b, c = st.columns(3)
        a.metric("Ready", summary["Ready"])
        b.metric("Screening", summary["Screening"])
        c.metric("Discovery", summary["Discovery"])

        bands = st.multiselect(
            "Confidence bands",
            ["Ready", "Screening", "Discovery"],
            default=["Ready", "Screening"],
        )
        view = (
            scored[scored["confidence_band"].isin(bands)]
            if bands
            else scored.iloc[0:0]
        )

        search = st.text_input("Search manufacturer/model")
        if search.strip() and not view.empty:
            man = (
                view["manufacturer"].astype(str)
                if "manufacturer" in view
                else pd.Series("", index=view.index)
            )
            model = (
                view["model"].astype(str)
                if "model" in view
                else pd.Series("", index=view.index)
            )
            view = view[
                man.str.contains(search, case=False, na=False)
                | model.str.contains(search, case=False, na=False)
            ]

        if not view.empty:
            st.dataframe(
                view.sort_values(
                    "confidence_score", ascending=False
                ).head(1000),
                use_container_width=True,
                hide_index=True,
            )


# ---------------------------------------------------------------------------
# INTEGRATED RUN
# ---------------------------------------------------------------------------
with tab_run:
    st.subheader("Integrated real-component optimization")
    st.write(
        "Search the component library, solve real propulsion combinations, "
        "physically size a concept carbon-fiber frame, then rank the valid "
        "aircraft using the weighted decision matrix."
    )

    if not DETAILED_OPT.exists():
        st.error(f"Integrated optimizer missing: `{DETAILED_OPT}`")
    elif not WAREHOUSE.exists():
        st.error("Component warehouse is missing. Update Data Library first.")
    else:
        st.caption(
            f"Current warehouse: **{format_utc_iso(warehouse_modified_iso(WAREHOUSE))}**"
        )

        data_mode = st.radio(
            "Data used for this run",
            [
                "Refresh all sources first — recommended for official runs",
                "Use the current warehouse snapshot — faster for repeat studies",
            ],
            index=1,
        )

        depth_label = st.selectbox(
            "Search depth",
            list(SEARCH_DEPTHS.keys()),
            index=1,
        )
        depth = SEARCH_DEPTHS[depth_label]["key"]
        st.info(SEARCH_DEPTHS[depth_label]["description"])

        st.markdown("### Component configuration")
        st.write(
            "Set any component to **Optimize** to let the search choose it, or "
            "lock a specific part to evaluate/optimize around hardware you "
            "already own or definitely want to use."
        )

        warehouse_stamp = (
            WAREHOUSE.stat().st_mtime_ns if WAREHOUSE.exists() else 0
        )
        try:
            motor_options = selectable_local_motors(str(PROJECT_ROOT))
        except Exception as exc:
            motor_options = []
            st.warning(f"Could not load motor selector: {exc}")
        try:
            prop_options = selectable_local_props(str(PROJECT_ROOT))
        except Exception as exc:
            prop_options = []
            st.warning(f"Could not load propeller selector: {exc}")

        pack_options = selectable_battery_packs(
            str(WAREHOUSE), warehouse_stamp
        )
        cell_options = selectable_battery_cells(
            str(WAREHOUSE), warehouse_stamp
        )
        esc_options = selectable_escs(
            str(WAREHOUSE), warehouse_stamp
        )

        s1, s2 = st.columns(2)
        with s1:
            motor_choice = st.radio(
                "Motor",
                ["Optimize", "Lock specific motor"],
                horizontal=True,
                key="component_motor_mode",
            )
            motor_mode = (
                "lock" if motor_choice == "Lock specific motor"
                else "optimize"
            )
            motor_id = None
            if motor_mode == "lock":
                motor_id = part_selectbox(
                    "Selected motor",
                    motor_options,
                    "component_motor_id",
                )

            prop_choice = st.radio(
                "Propeller",
                ["Optimize", "Lock specific propeller"],
                horizontal=True,
                key="component_prop_mode",
            )
            prop_mode = (
                "lock" if prop_choice == "Lock specific propeller"
                else "optimize"
            )
            prop_id = None
            if prop_mode == "lock":
                prop_id = part_selectbox(
                    "Selected propeller",
                    prop_options,
                    "component_prop_id",
                )

        with s2:
            battery_choice = st.radio(
                "Battery",
                [
                    "Optimize battery",
                    "Lock commercial pack",
                    "Lock cell, optimize pack topology",
                ],
                horizontal=False,
                key="component_battery_mode",
            )
            if battery_choice == "Lock commercial pack":
                battery_mode = "commercial"
                battery_id = part_selectbox(
                    "Selected commercial battery pack",
                    pack_options,
                    "component_battery_pack_id",
                )
            elif battery_choice == "Lock cell, optimize pack topology":
                battery_mode = "cell"
                battery_id = part_selectbox(
                    "Selected battery cell",
                    cell_options,
                    "component_battery_cell_id",
                )
            else:
                battery_mode = "optimize"
                battery_id = None

            esc_choice = st.radio(
                "ESC",
                ["Optimize", "Lock specific ESC"],
                horizontal=True,
                key="component_esc_mode",
            )
            esc_mode = (
                "lock" if esc_choice == "Lock specific ESC"
                else "optimize"
            )
            esc_id = None
            if esc_mode == "lock":
                esc_id = part_selectbox(
                    "Selected ESC",
                    esc_options,
                    "component_esc_id",
                )

        fixed_count = sum([
            motor_mode == "lock",
            prop_mode == "lock",
            battery_mode in {"commercial", "cell"},
            esc_mode == "lock",
        ])
        if fixed_count == 0:
            st.caption(
                "Current mode: **Full optimization** — motor, propeller, "
                "battery, and ESC are all design variables."
            )
        elif fixed_count == 4 and battery_mode == "commercial":
            st.caption(
                "Current mode: **Fixed configuration analysis** — all four "
                "propulsion components are locked. The program will size the "
                "frame, solve performance, and score the resulting aircraft."
            )
        else:
            st.caption(
                f"Current mode: **Mixed optimization** — {fixed_count} of 4 "
                "propulsion component categories are constrained."
            )

        with st.expander("How fixed/optimized parts interact"):
            st.write(
                "A locked motor or propeller is forced into the propulsion guide "
                "instead of relying on the normal candidate down-selection. A "
                "locked commercial battery uses its exact voltage, capacity, "
                "mass, and current limits. A locked cell still lets the optimizer "
                "choose series/parallel topology. A locked ESC must satisfy the "
                "selected battery voltage and modeled motor current."
            )
            st.write(
                "Any category left on **Optimize** still participates in the "
                "normal broad search, cached Explore stage, exact finalist "
                "validation, and weighted decision ranking."
            )

        st.markdown("### Aircraft constraints")
        r1, r2, r3 = st.columns(3)
        run_max_auw = r1.number_input(
            "Maximum AUW (kg)",
            1.0, 25.0, 10.0, 0.25,
            key="run_max_auw",
        )
        run_min_twr = r2.number_input(
            "Minimum T/W",
            1.0, 4.0, 1.8, 0.1,
            key="run_min_twr",
        )
        fixed_override = r3.checkbox(
            "Override fixed camera/avionics mass",
            value=False,
            help=(
                "Frame mass is calculated separately. Use this when you have a "
                "better mass estimate for the DIY camera, flight controller, GPS, "
                "telemetry, wiring, mounts, and other non-frame fixed equipment."
            ),
        )

        run_fixed_mass = None
        if fixed_override:
            run_fixed_mass = st.number_input(
                "Camera + avionics + other fixed non-frame mass (kg)",
                0.1, 12.0, 0.50, 0.05,
            )

        r4, r5, r6 = st.columns(3)
        pack_overhead = r4.slider(
            "Custom-pack structural mass overhead",
            0.0, 0.30, 0.10, 0.01,
        )
        esc_margin = r5.slider(
            "ESC current margin",
            1.00, 1.50, 1.15, 0.01,
        )
        rpm_tolerance = r6.slider(
            "Allowed prop-data RPM extrapolation",
            0.0, 0.25, 0.10, 0.01,
        )

        st.markdown("### Planning cost model")
        c1, c2, c3 = st.columns(3)
        c1.metric("Frame allowance", "$220")
        c2.metric("DIY camera + avionics", "$700")
        c3.metric("Fixed planning cost", "$920")
        st.caption(
            "These fixed planning allowances are added to the motor, propeller, "
            "battery, and ESC cost for every design. Missing propulsion prices "
            "use explicit fallback estimates and are marked as estimates in the "
            "decision matrix; they are never treated as $0."
        )

        st.markdown("### Weighted decision matrix")
        st.write(
            "The matrix ranks valid designs rather than imposing a hard dollar "
            "ceiling. Higher matrix score is better."
        )
        w1, w2, w3, w4, w5 = st.columns(5)
        weight_cost = w1.number_input(
            "Cost weight", 0.0, 100.0, 35.0, 1.0
        )
        weight_endurance = w2.number_input(
            "Endurance weight", 0.0, 100.0, 25.0, 1.0
        )
        weight_mass = w3.number_input(
            "Weight weight", 0.0, 100.0, 17.0, 1.0
        )
        weight_quality = w4.number_input(
            "Design quality weight", 0.0, 100.0, 13.0, 1.0
        )
        weight_complexity = w5.number_input(
            "Buildability weight", 0.0, 100.0, 10.0, 1.0
        )
        total_weight = (
            weight_cost + weight_endurance + weight_mass
            + weight_quality + weight_complexity
        )
        st.caption(
            f"Entered weights total {total_weight:.0f}. The optimizer normalizes "
            "them automatically to 100%, so only their relative sizes matter."
        )

        with st.expander("How quality and complexity are scored"):
            st.write(
                "**Design quality / robustness** uses thrust-to-weight margin, "
                "hover operating point, ESC current margin, battery current "
                "margin, and component-data confidence. The DIY camera/mission "
                "functionality is shared by every design, so it does not create "
                "an artificial ranking difference."
            )
            st.write(
                "**Buildability** rewards simpler aircraft. Custom Li-ion packs "
                "receive a complexity penalty, and that penalty grows with cell "
                "count. Very large frames/props and larger structural tube sizes "
                "also reduce buildability. The output includes estimated assembly "
                "part count and number of DIY subsystems."
            )

        st.markdown("### Physical frame sizing")
        f1, f2 = st.columns(2)
        frame_prop_clearance_mm = f1.number_input(
            "Minimum prop-tip clearance (mm)",
            20.0, 200.0, 50.0, 5.0,
        )
        frame_structural_margin = f2.number_input(
            "Structural load margin",
            1.0, 2.0, 1.25, 0.05,
        )
        st.info(
            "Frame material is currently standardized as carbon fiber. The "
            "optimizer chooses the lightest discrete carbon tube that passes "
            "both bending-stress and arm-deflection limits, then adds carbon "
            "center plates, a battery tray, joints/hardware, and landing-support "
            "allowance. Frame mass feeds back into AUW and the sizing iterates."
        )

        with st.expander("Frame engineering assumptions"):
            st.dataframe(
                pd.DataFrame([
                    ["Arm material", "Carbon fiber"],
                    ["Effective elastic modulus", "60 GPa"],
                    ["Allowable bending stress", "300 MPa"],
                    ["Maximum arm tip deflection", "1% of arm length"],
                    ["Center plates", "2 × 2.0 mm carbon plate"],
                    ["Battery tray", "1.5 mm carbon plate"],
                    ["Tube search", "12–30 mm OD, 1.0–2.0 mm walls"],
                    ["Frame procurement allowance", "$220"],
                ], columns=["Assumption", "Current value"]),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "This is a physical concept-sizing model, not final certification. "
                "When the CAD frame exists, replace these generic assumptions with "
                "actual laminate/tube properties and validate in FEA."
            )

        force_guide = False
        force_physics = False
        if "Use the current" in data_mode:
            force_guide = st.checkbox(
                "Rebuild the motor/prop guide search",
                value=False,
                help=(
                    "Changing search depth, AUW/TW constraints, fixed mass, or "
                    "frame sizing assumptions automatically invalidates an "
                    "incompatible cached guide."
                ),
            )
            force_physics = st.checkbox(
                "Force exact aircraft physics to rebuild",
                value=False,
                help=(
                    "Leave this off normally. If all physical inputs are unchanged, "
                    "v0.7 reuses the previously exact-validated aircraft pool and "
                    "only recomputes the decision ranking."
                ),
            )
        else:
            st.caption(
                "A full source refresh forces a fresh guide search so the result "
                "is tied to the refreshed data snapshot."
            )

        if st.button(
            "Run integrated optimizer",
            type="primary",
            use_container_width=True,
        ):
            can_run = True
            missing_locked = []
            if motor_mode == "lock" and not motor_id:
                missing_locked.append("motor")
            if prop_mode == "lock" and not prop_id:
                missing_locked.append("propeller")
            if battery_mode in {"commercial", "cell"} and not battery_id:
                missing_locked.append("battery")
            if esc_mode == "lock" and not esc_id:
                missing_locked.append("ESC")
            if missing_locked:
                st.error(
                    "Choose a specific "
                    + ", ".join(missing_locked)
                    + " before starting the run."
                )
                can_run = False
            refreshed = "Refresh all sources" in data_mode

            if refreshed:
                with st.spinner(
                    "Step 1 of 2 — refreshing all configured component sources..."
                ):
                    refresh_result = refresh_database(
                        PROJECT_ROOT, BUILDER, refresh=True
                    )
                st.session_state["last_refresh_job"] = refresh_result
                if refresh_result["returncode"] != 0:
                    st.session_state["last_job"] = refresh_result
                    st.error(
                        "The data refresh failed, so optimization was not started. "
                        "See Logs."
                    )
                    can_run = False
                else:
                    st.success(
                        f"Data library refreshed in "
                        f"{refresh_result['elapsed_s']} s."
                    )
                    force_guide = True
                    force_physics = True

            if can_run:
                with st.spinner(
                    "Step 2 of 2 — solving components, structurally sizing frames, "
                    "and building the decision matrix..."
                ):
                    result = run_detailed_optimizer(
                        PROJECT_ROOT,
                        DETAILED_OPT,
                        force_parametric=force_guide,
                        force_physics=force_physics,
                        motor_mode=motor_mode,
                        motor_id=motor_id,
                        prop_mode=prop_mode,
                        prop_id=prop_id,
                        battery_mode=battery_mode,
                        battery_id=battery_id,
                        esc_mode=esc_mode,
                        esc_id=esc_id,
                        fixed_mass=run_fixed_mass,
                        max_auw=run_max_auw,
                        min_twr=run_min_twr,
                        pack_overhead=pack_overhead,
                        esc_margin=esc_margin,
                        rpm_tolerance=rpm_tolerance,
                        search_depth=depth,
                        budget_mode="off",
                        fixed_nonpropulsion_cost=700.0,
                        frame_cost_per_kg=0.0,
                        frame_prop_clearance_mm=frame_prop_clearance_mm,
                        frame_structural_margin=frame_structural_margin,
                        weight_cost=weight_cost,
                        weight_endurance=weight_endurance,
                        weight_aircraft_mass=weight_mass,
                        weight_quality=weight_quality,
                        weight_complexity=weight_complexity,
                    )
                st.session_state["last_job"] = result

                if result["returncode"] == 0:
                    st.success(
                        f"Integrated optimization finished in "
                        f"{result['elapsed_s']} s. Open Results for the top 25 "
                        "weighted designs."
                    )
                else:
                    st.error(
                        "Integrated optimizer returned an error. See Logs."
                    )

        with st.expander("How v0.7 avoids repeated long runs"):
            st.write(
                "The integrated stage now has three reusable layers. "
                "**Propulsion curves** cache each motor + prop + voltage PyThrust "
                "response once. **Explore** uses those cached curves to evaluate "
                "many aircraft cheaply. A deliberately diverse finalist pool is "
                "then **exactly validated** with PyThrust. Finally, the exact "
                "validated physical design pool is saved. If you later change only "
                "the decision weights, the physics pool is reused instead of "
                "re-solving the aircraft."
            )
            st.write(
                "Changing a physical assumption such as AUW, fixed mass, T/W, "
                "battery overhead, frame clearance, structural margin, the data "
                "warehouse, or search depth automatically produces a different "
                "cache key and triggers the necessary physics again."
            )

        with st.expander("Why there is no hard price ceiling"):
            st.write(
                "Cost now competes directly with endurance, aircraft mass, design "
                "quality, and buildability. That lets a slightly more expensive "
                "design win when its endurance or engineering quality is worth the "
                "incremental cost, instead of discarding it at an arbitrary cutoff."
            )

        with st.expander("Model limitations"):
            st.write(
                "Frame-arm sizing is now physics-based, but center-joint details "
                "still need CAD/FEA. Battery voltage sag and thermal behavior remain "
                "first-order. Missing component prices use visible fallback planning "
                "estimates, so those designs should be verified before purchase."
            )


# ---------------------------------------------------------------------------
# RESULTS
# ---------------------------------------------------------------------------
with tab_results:
    st.subheader("Optimization results")

    settings = {}
    if RUN_SETTINGS.exists():
        try:
            settings = json.loads(RUN_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if RESULTS_CSV.exists():
        try:
            results = pd.read_csv(RESULTS_CSV)
        except Exception as exc:
            st.error(f"Could not read results: {exc}")
            results = pd.DataFrame()
    else:
        results = pd.DataFrame()

    if DECISION_TOP25_CSV.exists():
        try:
            decision_top25 = pd.read_csv(DECISION_TOP25_CSV)
        except Exception:
            decision_top25 = pd.DataFrame()
    else:
        decision_top25 = pd.DataFrame()

    if DECISION_ALL_CSV.exists():
        try:
            decision_all = pd.read_csv(DECISION_ALL_CSV)
        except Exception:
            decision_all = pd.DataFrame()
    else:
        decision_all = pd.DataFrame()

    no_valid = (
        not results.empty
        and "status" in results.columns
        and str(results.iloc[0]["status"]) == "NO_VALID_DESIGNS"
    )

    if no_valid:
        st.error(
            "The run completed, but no designs survived all current constraints."
        )
        if SUMMARY_TXT.exists():
            st.text(SUMMARY_TXT.read_text(encoding="utf-8"))
        if REJECTION_CSV.exists():
            rejection_df = pd.read_csv(REJECTION_CSV)
            if not rejection_df.empty:
                st.markdown("#### Why candidates were rejected")
                st.dataframe(
                    rejection_df,
                    use_container_width=True,
                    hide_index=True,
                )
        if EXCEPTION_CSV.exists():
            exception_df = pd.read_csv(EXCEPTION_CSV)
            if not exception_df.empty:
                st.markdown("#### Exact solver exception details")
                st.dataframe(
                    exception_df,
                    use_container_width=True,
                    hide_index=True,
                )

    elif not decision_top25.empty:
        decision_top25 = decision_top25.sort_values(
            "decision_rank"
        ).reset_index(drop=True)
        if decision_all.empty:
            decision_all = decision_top25.copy()

        st.markdown("### Instant matrix re-ranking")
        st.caption(
            "Changing these weights does **not** rerun PyThrust. It re-ranks the "
            "existing exact-validated physical design pool immediately."
        )
        saved_weights = settings.get("decision_weights", {}) if settings else {}
        rw1, rw2, rw3, rw4, rw5 = st.columns(5)
        rerank_weights = {
            "cost": rw1.number_input(
                "Cost", 0.0, 100.0,
                float(saved_weights.get("cost", 35.0)), 1.0,
                key="results_weight_cost",
            ),
            "endurance": rw2.number_input(
                "Endurance", 0.0, 100.0,
                float(saved_weights.get("endurance", 25.0)), 1.0,
                key="results_weight_endurance",
            ),
            "weight": rw3.number_input(
                "Aircraft weight", 0.0, 100.0,
                float(saved_weights.get("weight", 17.0)), 1.0,
                key="results_weight_mass",
            ),
            "quality": rw4.number_input(
                "Quality", 0.0, 100.0,
                float(saved_weights.get("quality", 13.0)), 1.0,
                key="results_weight_quality",
            ),
            "complexity": rw5.number_input(
                "Buildability", 0.0, 100.0,
                float(saved_weights.get("complexity", 10.0)), 1.0,
                key="results_weight_complexity",
            ),
        }
        decision_all = rerank_existing_designs(
            decision_all, rerank_weights
        )
        decision_top25 = decision_all.head(25).copy()

        best = decision_top25.iloc[0]

        st.markdown("### Recommended design by weighted decision matrix")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(
            "Matrix score",
            f"{best['decision_matrix_score']:.1f}/100",
        )
        m2.metric(
            "Estimated total cost",
            f"${best['decision_total_estimated_cost_usd']:.0f}",
        )
        m3.metric("Endurance", f"{best['endurance_min']:.1f} min")
        m4.metric("AUW", f"{best['auw_kg']:.3f} kg")
        m5.metric("Frame mass", f"{best['frame_mass_kg']:.3f} kg")

        weights = settings.get("decision_weights", {}) if settings else {}
        if weights:
            weight_df = pd.DataFrame([
                ["Cost", weights.get("cost", 35)],
                ["Endurance", weights.get("endurance", 25)],
                ["Aircraft weight", weights.get("weight", 17)],
                ["Design quality / robustness", weights.get("quality", 13)],
                ["Complexity / buildability", weights.get("complexity", 10)],
            ], columns=["Criterion", "Weight"])
            total_w = weight_df["Weight"].sum()
            if total_w > 0:
                weight_df["Normalized weight (%)"] = (
                    100.0 * weight_df["Weight"] / total_w
                ).round(1)
            with st.expander("Decision-matrix weights and scoring meaning"):
                st.dataframe(
                    weight_df,
                    use_container_width=True,
                    hide_index=True,
                )
                st.write(
                    "**Cost:** lower estimated total aircraft cost is better. "
                    "**Endurance:** longer hover endurance is better. "
                    "**Weight:** lower AUW is better. "
                    "**Quality:** combines T/W margin, hover operating point, "
                    "ESC/battery current headroom, and data confidence. "
                    "**Buildability:** penalizes custom high-cell-count battery "
                    "packs, very large frames/props, and heavier structural tubes."
                )

        st.markdown("### Top 25 designs")
        st.caption(
            "These are ranked by the weighted matrix—not simply by maximum "
            "endurance. Cost includes $220 for the frame and $700 for the DIY "
            "camera/avionics system."
        )
        st.dataframe(
            decision_matrix_table(decision_top25, 25),
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("How to interpret estimated prices"):
            st.write(
                "The optimizer uses sourced motor/prop/battery/ESC prices when "
                "available. If a category has no usable price, it substitutes a "
                "visible planning estimate rather than $0. The **Price estimates "
                "used** column tells you exactly which categories were filled this "
                "way. Current fallback propulsion allowances are four motors $180, "
                "four props $70, battery $250, and four ESCs $160."
            )

        labels = [
            (
                f"#{int(row['decision_rank'])} · "
                f"{float(row['decision_matrix_score']):.1f}/100 · "
                f"${float(row['decision_total_estimated_cost_usd']):.0f} · "
                f"{float(row['endurance_min']):.1f} min · "
                f"{row.get('motor_model','')} + {row.get('prop_model','')}"
            )
            for _, row in decision_top25.iterrows()
        ]
        chosen_label = st.selectbox("Inspect a top-25 design", labels)
        chosen = decision_top25.iloc[labels.index(chosen_label)]

        st.markdown("### Selected design")
        st.dataframe(
            component_summary(chosen),
            use_container_width=True,
            hide_index=True,
        )

        s1, s2 = st.columns(2)
        with s1:
            st.markdown("#### Decision score breakdown")
            score_df = pd.DataFrame([
                [
                    "Cost",
                    float(chosen.get("decision_cost_score", 0)),
                    "Lower total estimated cost is better.",
                ],
                [
                    "Endurance",
                    float(chosen.get("decision_endurance_score", 0)),
                    "Longer modeled hover endurance is better.",
                ],
                [
                    "Aircraft weight",
                    float(chosen.get("decision_weight_score", 0)),
                    "Lower AUW is better.",
                ],
                [
                    "Design quality",
                    float(chosen.get("decision_quality_score", 0)),
                    "Electrical/thrust margins, operating point, and data confidence.",
                ],
                [
                    "Buildability",
                    float(chosen.get("decision_complexity_score", 0)),
                    "Higher means fewer/custom simpler parts and easier fabrication.",
                ],
            ], columns=["Criterion", "Score / 100", "Meaning"])
            score_df["Score / 100"] = score_df["Score / 100"].round(1)
            st.dataframe(
                score_df,
                use_container_width=True,
                hide_index=True,
            )

        with s2:
            st.markdown("#### Cost breakdown")
            def money(v):
                try:
                    return f"${float(v):.0f}"
                except Exception:
                    return "Unknown"

            cost_df = pd.DataFrame([
                ["4 motors", money(chosen.get("motor_cost_total_usd"))],
                ["4 propellers", money(chosen.get("prop_cost_total_usd"))],
                ["Battery", money(chosen.get("battery_cost_usd"))],
                ["4 ESCs", money(chosen.get("esc_cost_total_usd"))],
                ["Frame planning allowance", "$220"],
                ["DIY camera + avionics allowance", "$700"],
                [
                    "Decision-matrix total",
                    money(chosen.get("decision_total_estimated_cost_usd")),
                ],
            ], columns=["Cost item", "Amount"])
            st.dataframe(
                cost_df,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"Estimated propulsion categories: "
                f"{chosen.get('decision_price_estimated_categories','none')}."
            )

        st.markdown("#### Performance")
        st.dataframe(
            performance_summary(chosen),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("#### Frame structural sizing")
        frame_df = pd.DataFrame([
            ["Material", str(chosen.get("frame_material", "Carbon fiber"))],
            [
                "Selected arm tube",
                f"{float(chosen.get('frame_arm_tube_od_mm',0)):.0f} mm OD × "
                f"{float(chosen.get('frame_arm_tube_wall_mm',0)):.1f} mm wall",
            ],
            [
                "Tube length per arm",
                f"{float(chosen.get('frame_arm_tube_length_m',0))*1000:.0f} mm",
            ],
            [
                "Design bending stress",
                f"{float(chosen.get('frame_arm_stress_mpa',0)):.1f} MPa",
            ],
            [
                "Stress utilization",
                f"{float(chosen.get('frame_arm_stress_utilization_pct',0)):.0f}%",
            ],
            [
                "Design-load tip deflection",
                f"{float(chosen.get('frame_arm_tip_deflection_mm',0)):.1f} mm",
            ],
            [
                "Deflection-limit utilization",
                f"{float(chosen.get('frame_arm_deflection_utilization_pct',0)):.0f}%",
            ],
            [
                "Approx. prop-tip span",
                f"{float(chosen.get('frame_tip_to_tip_span_m',0)):.2f} m",
            ],
        ], columns=["Frame check", "Result"])
        st.dataframe(frame_df, use_container_width=True, hide_index=True)
        st.caption(
            "The frame arm is selected from discrete carbon tube sizes and must "
            "pass both bending-stress and stiffness checks at the design thrust "
            "load. Center plates, tray, joints and landing support are then added "
            "to obtain the frame mass. Final CAD/FEA is still required."
        )

        st.markdown("### Trade-space graphs")
        g1, g2 = st.columns(2)

        with g1:
            st.markdown("#### Endurance vs total estimated cost")
            chart = decision_all[
                ["decision_total_estimated_cost_usd", "endurance_min"]
            ].dropna().copy()
            if not chart.empty:
                st.scatter_chart(
                    chart,
                    x="decision_total_estimated_cost_usd",
                    y="endurance_min",
                )
            st.caption(
                "**How to read it:** left is cheaper and up is longer endurance. "
                "The weighted matrix heavily prefers designs toward the upper-left, "
                "but can accept extra cost when the endurance/quality gain is large "
                "enough to justify it."
            )

        with g2:
            st.markdown("#### Endurance vs aircraft mass")
            chart = decision_all[
                ["auw_kg", "endurance_min"]
            ].dropna().copy()
            if not chart.empty:
                st.scatter_chart(
                    chart,
                    x="auw_kg",
                    y="endurance_min",
                )
            st.caption(
                "**How to read it:** left is lighter and up is longer endurance. "
                "The upper-left boundary is the strongest mass/endurance region."
            )

        g3, g4 = st.columns(2)
        with g3:
            st.markdown("#### Matrix score vs estimated cost")
            chart = decision_all[
                ["decision_total_estimated_cost_usd", "decision_matrix_score"]
            ].dropna().copy()
            if not chart.empty:
                st.scatter_chart(
                    chart,
                    x="decision_total_estimated_cost_usd",
                    y="decision_matrix_score",
                )
            st.caption(
                "**How to read it:** higher is a better overall weighted decision. "
                "This shows whether spending more actually improves the total "
                "decision score or whether cheaper designs dominate."
            )

        with g4:
            st.markdown("#### Prop size vs physically sized frame mass")
            chart = decision_all[
                ["prop_diameter_in", "frame_mass_kg"]
            ].dropna().copy()
            if not chart.empty:
                st.scatter_chart(
                    chart,
                    x="prop_diameter_in",
                    y="frame_mass_kg",
                )
            st.caption(
                "**How to read it:** larger props usually require longer arms. "
                "The optimizer then selects a tube that passes stress and deflection "
                "limits, so structural mass can jump when a larger tube size becomes "
                "necessary rather than following an arbitrary fixed percentage."
            )

        if PARETO_CSV.exists():
            pareto = pd.read_csv(PARETO_CSV)
            if not pareto.empty:
                st.markdown("### Mass/endurance Pareto frontier")
                st.write(
                    "These designs cannot be beaten simultaneously on both lower "
                    "aircraft mass and higher endurance. The decision matrix then "
                    "adds cost, quality, and buildability to choose among them and "
                    "other attractive trade options."
                )
                st.dataframe(
                    friendly_design_table(
                        pareto.sort_values("auw_kg").reset_index(drop=True)
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "Download top-25 decision matrix CSV",
                decision_top25.to_csv(index=False).encode("utf-8"),
                file_name="decision_matrix_top25.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with d2:
            st.download_button(
                "Download all ranked valid designs CSV",
                decision_all.to_csv(index=False).encode("utf-8"),
                file_name="decision_matrix_all.csv",
                mime="text/csv",
                use_container_width=True,
            )

        if settings:
            selection = settings.get("component_selection", {})
            if selection:
                st.markdown("### Component configuration used")
                def _mode_text(mode, part_id):
                    if mode in {"lock", "commercial", "cell"}:
                        return f"Locked · {part_id or 'unknown ID'}"
                    return "Optimize"

                selection_df = pd.DataFrame([
                    [
                        "Motor",
                        _mode_text(
                            selection.get("motor_mode"),
                            selection.get("motor_id"),
                        ),
                    ],
                    [
                        "Propeller",
                        _mode_text(
                            selection.get("prop_mode"),
                            selection.get("prop_id"),
                        ),
                    ],
                    [
                        "Battery",
                        (
                            "Optimize"
                            if selection.get("battery_mode") == "optimize"
                            else (
                                "Locked commercial pack · "
                                + str(selection.get("battery_id"))
                                if selection.get("battery_mode") == "commercial"
                                else (
                                    "Locked cell / optimized topology · "
                                    + str(selection.get("battery_id"))
                                )
                            )
                        ),
                    ],
                    [
                        "ESC",
                        _mode_text(
                            selection.get("esc_mode"),
                            selection.get("esc_id"),
                        ),
                    ],
                ], columns=["Component", "Mode"])
                st.dataframe(
                    selection_df,
                    use_container_width=True,
                    hide_index=True,
                )

            with st.expander("Data snapshot and run settings"):
                st.write(
                    f"**Search depth:** "
                    f"{str(settings.get('search_depth','unknown')).title()}"
                )
                show_data_snapshot_from_settings(settings)
                st.json({
                    "frame_model": settings.get("frame_model"),
                    "planning_costs": settings.get("planning_costs"),
                    "decision_weights": settings.get("decision_weights"),
                })

        with st.expander("Full engineering result table"):
            st.write(
                "This is the complete solver output for audit/debugging. The top "
                "25 matrix above is the intended decision interface."
            )
            st.dataframe(
                decision_all,
                use_container_width=True,
                hide_index=True,
            )

        if SUMMARY_TXT.exists():
            with st.expander("Raw run summary"):
                st.text(SUMMARY_TXT.read_text(encoding="utf-8"))

    elif not results.empty:
        st.warning(
            "Results exist, but no v0.6-compatible decision-matrix file was found. Run the "
            "Integrated Optimizer again with v0.6."
        )
    else:
        st.info(
            "No integrated result file exists yet. Run Integrated Run first."
        )


# ---------------------------------------------------------------------------
# LOGS
# ---------------------------------------------------------------------------
with tab_logs:
    st.subheader("Latest jobs")

    refresh_job = st.session_state.get("last_refresh_job")
    if refresh_job:
        with st.expander("Latest data refresh log"):
            st.write(
                f"Return code: `{refresh_job['returncode']}` · "
                f"elapsed `{refresh_job['elapsed_s']} s`"
            )
            if refresh_job["stdout"]:
                st.code(refresh_job["stdout"][-50000:])
            if refresh_job["stderr"]:
                st.code(refresh_job["stderr"][-25000:])

    job = st.session_state.get("last_job")
    if not job:
        st.info("No job has been run in this browser session yet.")
    else:
        st.write(f"Command: `{job['command']}`")
        st.write(
            f"Return code: `{job['returncode']}` · "
            f"elapsed `{job['elapsed_s']} s`"
        )
        if job["stdout"]:
            st.markdown("#### Output")
            st.code(job["stdout"][-50000:])
        if job["stderr"]:
            st.markdown("#### Errors / warnings")
            st.code(job["stderr"][-25000:])
