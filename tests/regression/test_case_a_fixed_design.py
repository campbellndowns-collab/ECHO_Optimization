"""
Case A — all propulsion components fixed (exact calculator path).

Uses the reference top design IDs:
  motor NeuMotors_4610MC-207
  prop  APC_19x10E
  cell  fcde6dc9431046231f6f @ 6S4P
  ESC   4420b0e1b6c93b81ca45

Commercial-pack lock is not used here because the current warehouse packs lack
mass_g (build_locked_commercial_battery returns None). Fixed custom topology is
the numerically complete fixed-configuration path available in v0.7 data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import FIXTURES, load_integrated, nearly


@pytest.fixture(scope="module")
def case_a_result():
    integ = load_integrated()
    legacy = integ.load_legacy_module()
    prop_db, motor_db = legacy.load_databases()
    _, apc_catalog = legacy.load_apc_catalog()

    motor = next(
        m
        for m in motor_db.search(
            min_kv=1,
            max_kv=100000,
            min_max_current=0,
            min_weight=0,
            max_weight=100000,
        )
        if m.id == "NeuMotors_4610MC-207"
    )
    prop_entry = prop_db.get("APC_19x10E")
    prop_catalog = legacy.match_prop_to_catalog(prop_entry, apc_catalog)
    assert prop_catalog is not None

    batteries = integ.build_custom_battery_candidates(
        target_series=6,
        target_energy_wh=500,
        target_mass_kg=2.0,
        pack_overhead_fraction=0.10,
        locked_cell_id="fcde6dc9431046231f6f",
    )
    battery = next(b for b in batteries if b.topology == "6S4P")
    esc_pool = [
        e
        for e in integ.load_esc_candidates()
        if str(e.canonical_id) == "4420b0e1b6c93b81ca45"
    ]
    assert esc_pool

    integ.REJECTION_COUNTS.clear()
    row = integ.run_real_design_exact(
        legacy,
        prop_entry,
        prop_catalog,
        motor,
        battery,
        esc_pool,
        fixed_mass=0.5,
        max_auw=10.0,
        min_twr=1.8,
        rpm_tolerance=0.10,
        esc_margin=1.15,
        price_resolver_obj=integ.price_resolver(),
        budget_mode="off",
        max_budget_usd=None,
        frame_prop_clearance_m=0.050,
        frame_structural_margin=1.25,
    )
    assert row is not None, f"rejected: {integ.REJECTION_COUNTS}"
    scored = integ.add_decision_matrix([row])
    return scored[0]


def test_case_a_matches_golden_physics_and_cost(case_a_result):
    golden = json.loads(
        (FIXTURES / "case_a_fixed_design.json").read_text()
    )
    row = case_a_result

    assert row["motor_id"] == golden["motor_id"]
    assert row["prop_id"] == golden["prop_id"]
    assert row["battery_id"] == golden["battery_id"]
    assert row["esc_id"] == golden["esc_id"]
    assert row["evaluation_mode"] == "exact_validated"

    for key in [
        "auw_kg",
        "endurance_min",
        "max_twr",
        "hover_throttle_pct",
        "hover_power_total_w",
        "hover_rpm",
        "max_rpm",
        "hover_pack_current_a",
        "max_pack_current_a",
        "max_motor_current_actual_a",
        "frame_mass_kg",
        "frame_tip_to_tip_span_m",
        "frame_arm_stress_mpa",
        "frame_arm_tip_deflection_mm",
        "decision_total_estimated_cost_usd",
    ]:
        assert nearly(row[key], golden[key], rel=1e-6, abs_tol=1e-6), (
            f"{key}: got {row[key]} expected {golden[key]}"
        )

    assert int(row["frame_arm_tube_od_mm"]) == int(golden["frame_arm_tube_od_mm"])
    assert nearly(row["frame_arm_tube_wall_mm"], golden["frame_arm_tube_wall_mm"])
    assert row["decision_price_estimated_categories"] == golden[
        "decision_price_estimated_categories"
    ]


def test_case_a_matches_reference_run_summary_top_design(case_a_result):
    """Cross-check against the saved Windows run summary (same aircraft)."""
    row = case_a_result
    assert nearly(row["auw_kg"], 4.144, rel=0, abs_tol=5e-3)
    assert nearly(row["endurance_min"], 79.1, rel=0, abs_tol=5e-2)
    assert nearly(row["decision_total_estimated_cost_usd"], 1572.36, rel=0, abs_tol=5e-2)
    assert int(row["frame_arm_tube_od_mm"]) == 14
