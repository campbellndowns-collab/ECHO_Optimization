"""Lock / substitution / price-fallback / ranking / cache-key regression tests."""

from __future__ import annotations

import copy

import pytest

from .conftest import LEGACY_ROOT, load_integrated, nearly


def test_locked_cell_does_not_substitute_other_cells():
    integ = load_integrated()
    batteries = integ.build_custom_battery_candidates(
        target_series=6,
        target_energy_wh=500,
        target_mass_kg=2.0,
        pack_overhead_fraction=0.10,
        locked_cell_id="fcde6dc9431046231f6f",
    )
    assert batteries
    for b in batteries:
        assert "fcde6dc9431046231f6f" in b.battery_id
        assert b.manufacturer == "Molicel"
        assert "M65A" in b.model


def test_locked_esc_pool_filters_to_exact_id():
    integ = load_integrated()
    target = "4420b0e1b6c93b81ca45"
    pool = [
        e for e in integ.load_esc_candidates() if str(e.canonical_id) == target
    ]
    assert len(pool) == 1
    assert pool[0].canonical_id == target


def test_missing_prices_use_fallback_not_zero():
    integ = load_integrated()
    # Minimal synthetic row with no propulsion prices.
    row = {
        "motor_cost_total_usd": None,
        "prop_cost_total_usd": None,
        "battery_cost_usd": None,
        "esc_cost_total_usd": None,
        "endurance_min": 60.0,
        "auw_kg": 4.0,
        "battery_type": "Custom Li-ion",
        "battery_topology": "6S4P",
        "battery_confidence": "Ready",
        "esc_confidence": "Ready",
        "prop_id": "APC_19x10E",
        "motor_id": "NeuMotors_4610MC-207",
        "esc_id": "4420b0e1b6c93b81ca45",
        "battery_id": "custom:fcde6dc9431046231f6f:6S4P",
        "max_twr": 2.0,
        "hover_throttle_pct": 50.0,
        "frame_mass_kg": 0.8,
        "frame_arm_tube_od_mm": 14,
        "frame_arm_tube_wall_mm": 1.0,
    }
    scored = integ.add_decision_matrix([dict(row)])
    out = scored[0]
    assert out["decision_total_estimated_cost_usd"] > 900  # 220+700 + fallbacks
    cats = str(out["decision_price_estimated_categories"])
    for part in ("motors", "props", "battery", "escs"):
        assert part in cats
    # Fallbacks must not collapse to zero propulsion estimate.
    assert out["decision_propulsion_estimated_cost_usd"] == pytest.approx(
        180 + 70 + 250 + 160
    )


def test_weight_only_rerank_does_not_call_pythrust(monkeypatch):
    integ = load_integrated()
    pool_path = (
        LEGACY_ROOT
        / "optimizer_results"
        / "drone_optimizer_integrated"
        / "physical_design_pool.csv"
    )
    if not pool_path.exists():
        pytest.skip("physical design pool not present")

    import pandas as pd

    df = pd.read_csv(pool_path)
    rows = df.to_dict("records")
    assert len(rows) >= 10

    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("PyThrust/exact path should not run during rerank")

    monkeypatch.setattr(integ, "run_real_design_exact", boom)
    monkeypatch.setattr(integ, "run_real_design_cached", boom)
    monkeypatch.setattr(integ, "build_propulsion_curve", boom)

    w1 = {
        "cost": 35.0,
        "endurance": 25.0,
        "weight": 17.0,
        "quality": 13.0,
        "complexity": 10.0,
    }
    w2 = {
        "cost": 10.0,
        "endurance": 50.0,
        "weight": 10.0,
        "quality": 20.0,
        "complexity": 10.0,
    }
    a = integ.add_decision_matrix(copy.deepcopy(rows), weights=w1)
    b = integ.add_decision_matrix(copy.deepcopy(rows), weights=w2)
    assert calls["n"] == 0
    assert len(a) == len(b) == len(rows)
    # Physical metrics must be untouched by rerank.
    by_id_a = {str(r.get("explore_candidate_id")): r for r in a}
    by_id_b = {str(r.get("explore_candidate_id")): r for r in b}
    assert set(by_id_a) == set(by_id_b)
    sample_id = next(iter(by_id_a))
    assert nearly(by_id_a[sample_id]["auw_kg"], by_id_b[sample_id]["auw_kg"])
    assert nearly(
        by_id_a[sample_id]["endurance_min"], by_id_b[sample_id]["endurance_min"]
    )
    # Matrix scores should respond to weight changes.
    assert by_id_a[sample_id]["decision_matrix_score"] != by_id_b[sample_id][
        "decision_matrix_score"
    ]


def test_physical_pool_key_changes_when_physical_input_changes():
    integ = load_integrated()
    base = {
        "warehouse": ("x", 1, "a"),
        "guide_csv": ("y", 1, "b"),
        "fixed_mass_override_kg": 0.5,
        "max_auw_kg": 10.0,
        "min_twr": 1.8,
        "pack_overhead_fraction": 0.1,
        "esc_current_margin": 1.15,
        "rpm_data_tolerance": 0.1,
        "search_depth": "thorough",
        "search_profile": {"max_real_solves": 500},
        "frame_model": {"prop_clearance_m": 0.05, "structural_margin": 1.25},
        "propulsion_curve_schema": 2,
        "component_selection": {
            "motor_mode": "optimize",
            "motor_id": None,
            "prop_mode": "optimize",
            "prop_id": None,
            "battery_mode": "optimize",
            "battery_id": None,
            "esc_mode": "optimize",
            "esc_id": None,
        },
    }
    k1 = integ.physical_pool_key(base)
    changed = copy.deepcopy(base)
    changed["min_twr"] = 2.0
    k2 = integ.physical_pool_key(changed)
    assert k1 != k2

    locked = copy.deepcopy(base)
    locked["component_selection"]["motor_mode"] = "lock"
    locked["component_selection"]["motor_id"] = "NeuMotors_4610MC-207"
    k3 = integ.physical_pool_key(locked)
    assert k3 != k1


def test_top25_come_from_exact_validated_pool():
    integ = load_integrated()
    pool_path = (
        LEGACY_ROOT
        / "optimizer_results"
        / "drone_optimizer_integrated"
        / "physical_design_pool.csv"
    )
    results_path = (
        LEGACY_ROOT
        / "optimizer_results"
        / "drone_optimizer_integrated"
        / "decision_matrix_top25.csv"
    )
    if not pool_path.exists() or not results_path.exists():
        pytest.skip("reference result CSVs not present")

    import pandas as pd

    pool = pd.read_csv(pool_path)
    top = pd.read_csv(results_path)
    assert (pool["evaluation_mode"] == "exact_validated").all()
    assert (top["evaluation_mode"] == "exact_validated").all()
    pool_ids = set(pool["explore_candidate_id"].astype(str))
    top_ids = set(top["explore_candidate_id"].astype(str))
    assert top_ids.issubset(pool_ids)
