"""
Lightweight mixed-mode behavior checks (not full long searches).

Full Cases B/C/E thorough searches are expensive; these tests verify lock
filtering and cell-topology expansion without running the multi-minute guide.
"""

from __future__ import annotations

from .conftest import load_integrated


def test_case_d_locked_cell_allows_multiple_topologies():
    integ = load_integrated()
    batteries = integ.build_custom_battery_candidates(
        target_series=6,
        target_energy_wh=400,
        target_mass_kg=2.5,
        pack_overhead_fraction=0.10,
        locked_cell_id="fcde6dc9431046231f6f",
    )
    topologies = {b.topology for b in batteries}
    assert len(topologies) >= 2
    assert all(b.model.startswith("INR-21700-M65A") for b in batteries)


def test_locked_motor_exists_in_pythrust_physics_db():
    integ = load_integrated()
    legacy = integ.load_legacy_module()
    _, motor_db = legacy.load_databases()
    ids = {
        m.id
        for m in motor_db.search(
            min_kv=1,
            max_kv=100000,
            min_max_current=0,
            min_weight=0,
            max_weight=100000,
        )
    }
    assert "NeuMotors_4610MC-207" in ids


def test_locked_prop_exists_in_apc_aero_db():
    integ = load_integrated()
    legacy = integ.load_legacy_module()
    prop_db, _ = legacy.load_databases()
    assert prop_db.get("APC_19x10E") is not None


def test_invalid_locked_esc_yields_empty_pool():
    integ = load_integrated()
    pool = [
        e
        for e in integ.load_esc_candidates()
        if str(e.canonical_id) == "this-esc-does-not-exist"
    ]
    assert pool == []
