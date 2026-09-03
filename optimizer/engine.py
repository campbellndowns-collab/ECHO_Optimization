"""Public numerical API for Drone Optimizer (Phase 2 extraction).

Wrappers around the v0.7 integrated engine. No formula changes.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Optional, Sequence

from optimizer.propulsion import integrated as integ


def evaluate_fixed_design(
    *,
    motor_id: str,
    propeller_id: str,
    battery_cell_id: str,
    battery_topology: str,
    esc_id: str,
    fixed_mass_kg: float = 0.5,
    max_auw_kg: float = 10.0,
    min_twr: float = 1.8,
    pack_overhead_fraction: float = 0.10,
    esc_current_margin: float = 1.15,
    rpm_data_tolerance: float = 0.10,
    frame_prop_clearance_m: float = 0.050,
    frame_structural_margin: float = 1.25,
    decision_weights: Optional[Mapping[str, float]] = None,
) -> dict[str, Any]:
    """
    Exact fixed-configuration evaluation (Case A path).

    Commercial-pack locking is not used here when warehouse packs lack mass;
    pass a cell id + explicit topology instead.
    """
    legacy = integ.load_legacy_module()
    prop_db, motor_db = legacy.load_databases()
    _, apc_catalog = legacy.load_apc_catalog()

    motor = next(
        (
            m
            for m in motor_db.search(
                min_kv=1,
                max_kv=100000,
                min_max_current=0,
                min_weight=0,
                max_weight=100000,
            )
            if m.id == motor_id
        ),
        None,
    )
    if motor is None:
        raise ValueError(f"Motor not found in PyThrust physics DB: {motor_id}")

    prop_entry = prop_db.get(propeller_id)
    if prop_entry is None:
        raise ValueError(f"Propeller not found in APC aero DB: {propeller_id}")

    prop_catalog = legacy.match_prop_to_catalog(prop_entry, apc_catalog)
    if prop_catalog is None:
        raise ValueError(
            f"Propeller {propeller_id} has no matching APC physical/price catalog row"
        )

    # Infer target series from topology like "6S4P"
    series = int(str(battery_topology).split("S")[0])
    batteries = integ.build_custom_battery_candidates(
        target_series=series,
        target_energy_wh=500.0,
        target_mass_kg=2.0,
        pack_overhead_fraction=pack_overhead_fraction,
        locked_cell_id=battery_cell_id,
    )
    battery = next((b for b in batteries if b.topology == battery_topology), None)
    if battery is None:
        raise ValueError(
            f"Could not build battery {battery_cell_id} topology {battery_topology}"
        )

    esc_pool = [
        e for e in integ.load_esc_candidates() if str(e.canonical_id) == str(esc_id)
    ]
    if not esc_pool:
        raise ValueError(f"ESC not found as usable engineering record: {esc_id}")

    integ.REJECTION_COUNTS.clear()
    integ.EXCEPTION_DETAILS.clear()
    row = integ.run_real_design_exact(
        legacy,
        prop_entry,
        prop_catalog,
        motor,
        battery,
        esc_pool,
        fixed_mass=fixed_mass_kg,
        max_auw=max_auw_kg,
        min_twr=min_twr,
        rpm_tolerance=rpm_data_tolerance,
        esc_margin=esc_current_margin,
        price_resolver_obj=integ.price_resolver(),
        budget_mode="off",
        max_budget_usd=None,
        frame_prop_clearance_m=frame_prop_clearance_m,
        frame_structural_margin=frame_structural_margin,
    )
    if row is None:
        reasons = dict(integ.REJECTION_COUNTS)
        raise RuntimeError(
            f"Fixed design rejected by engineering checks: {reasons or 'unknown'}"
        )

    scored = integ.add_decision_matrix(
        [row],
        weights=dict(decision_weights) if decision_weights else None,
    )
    return scored[0]


def rerank_designs(
    rows: Sequence[MutableMapping[str, Any]],
    weights: Optional[Mapping[str, float]] = None,
) -> list[dict[str, Any]]:
    """Weight-only rerank of an already exact-validated pool."""
    return integ.add_decision_matrix(
        [dict(r) for r in rows],
        weights=dict(weights) if weights else None,
    )


def physical_pool_key(settings: Mapping[str, Any]) -> str:
    return integ.physical_pool_key(dict(settings))
