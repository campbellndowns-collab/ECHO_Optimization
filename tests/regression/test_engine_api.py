"""Public engine.evaluate_fixed_design must match Case A golden values."""

from __future__ import annotations

import json

from optimizer.engine import evaluate_fixed_design

from .conftest import FIXTURES, nearly, require_runtime_data


def test_engine_evaluate_fixed_design_matches_case_a_golden():
    require_runtime_data()
    golden = json.loads((FIXTURES / "case_a_fixed_design.json").read_text())
    row = evaluate_fixed_design(
        motor_id="NeuMotors_4610MC-207",
        propeller_id="APC_19x10E",
        battery_cell_id="fcde6dc9431046231f6f",
        battery_topology="6S4P",
        esc_id="4420b0e1b6c93b81ca45",
        fixed_mass_kg=0.5,
        max_auw_kg=10.0,
        min_twr=1.8,
        pack_overhead_fraction=0.10,
        esc_current_margin=1.15,
        rpm_data_tolerance=0.10,
        frame_prop_clearance_m=0.050,
        frame_structural_margin=1.25,
    )
    assert row["motor_id"] == golden["motor_id"]
    assert row["prop_id"] == golden["prop_id"]
    assert row["battery_id"] == golden["battery_id"]
    assert row["esc_id"] == golden["esc_id"]
    for key in [
        "auw_kg",
        "endurance_min",
        "max_twr",
        "hover_throttle_pct",
        "hover_power_total_w",
        "frame_mass_kg",
        "decision_total_estimated_cost_usd",
    ]:
        assert nearly(row[key], golden[key], rel=1e-6, abs_tol=1e-6), (
            f"{key}: got {row[key]} expected {golden[key]}"
        )
