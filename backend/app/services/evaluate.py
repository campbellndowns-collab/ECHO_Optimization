"""Fixed-configuration evaluation service."""

from __future__ import annotations

from typing import Any

from backend.app.models.evaluate import (
    CostBreakdown,
    FixedEvaluateRequest,
    FixedEvaluateResponse,
    FrameResult,
)
from optimizer.engine import evaluate_fixed_design
from optimizer.propulsion import integrated as integ


def _success_from_row(row: dict[str, Any]) -> FixedEvaluateResponse:
    return FixedEvaluateResponse(
        valid=True,
        motor_id=row.get("motor_id"),
        propeller_id=row.get("prop_id"),
        battery_id=row.get("battery_id"),
        esc_id=row.get("esc_id"),
        auw_kg=row.get("auw_kg"),
        endurance_min=row.get("endurance_min"),
        hover_throttle_pct=row.get("hover_throttle_pct"),
        hover_rpm=row.get("hover_rpm"),
        max_rpm=row.get("max_rpm"),
        hover_power_w=row.get("hover_power_total_w"),
        hover_pack_current_a=row.get("hover_pack_current_a"),
        max_pack_current_a=row.get("max_pack_current_a"),
        max_motor_current_a=row.get("max_motor_current_actual_a"),
        max_twr=row.get("max_twr"),
        frame=FrameResult(
            material=str(row.get("frame_material") or "Carbon fiber"),
            arm_tube_od_mm=float(row["frame_arm_tube_od_mm"]),
            arm_tube_wall_mm=float(row["frame_arm_tube_wall_mm"]),
            arm_tube_length_m=float(row["frame_arm_tube_length_m"]),
            arm_stress_mpa=float(row["frame_arm_stress_mpa"]),
            arm_tip_deflection_mm=float(row["frame_arm_tip_deflection_mm"]),
            stress_utilization_pct=float(
                row["frame_arm_stress_utilization_pct"]
            ),
            deflection_utilization_pct=float(
                row["frame_arm_deflection_utilization_pct"]
            ),
            tip_to_tip_span_m=float(row["frame_tip_to_tip_span_m"]),
            frame_mass_kg=float(row["frame_mass_kg"]),
        ),
        cost=CostBreakdown(
            motors_usd=row.get("motor_cost_total_usd"),
            props_usd=row.get("prop_cost_total_usd"),
            battery_usd=row.get("battery_cost_usd"),
            escs_usd=row.get("esc_cost_total_usd"),
            frame_allowance_usd=float(
                row.get("decision_frame_allowance_usd") or 220.0
            ),
            camera_avionics_allowance_usd=float(
                row.get("decision_camera_avionics_allowance_usd") or 700.0
            ),
            propulsion_estimated_usd=float(
                row["decision_propulsion_estimated_cost_usd"]
            ),
            total_estimated_usd=float(
                row["decision_total_estimated_cost_usd"]
            ),
            price_estimated_categories=str(
                row.get("decision_price_estimated_categories") or ""
            ),
            price_actual_category_count=int(
                row.get("decision_price_actual_categories") or 0
            ),
        ),
        quality_score=row.get("decision_quality_score"),
        buildability_score=row.get("decision_complexity_score"),
        decision_matrix_score=row.get("decision_matrix_score"),
        evaluation_mode=row.get("evaluation_mode"),
    )


def evaluate_fixed(req: FixedEvaluateRequest) -> FixedEvaluateResponse:
    clearance_m = req.frame_prop_clearance_mm / 1000.0

    if req.battery_cell_id:
        try:
            row = evaluate_fixed_design(
                motor_id=req.motor_id,
                propeller_id=req.propeller_id,
                battery_cell_id=req.battery_cell_id,
                battery_topology=str(req.battery_topology),
                esc_id=req.esc_id,
                fixed_mass_kg=req.fixed_mass_kg,
                max_auw_kg=req.max_auw_kg,
                min_twr=req.min_twr,
                pack_overhead_fraction=req.pack_overhead_fraction,
                esc_current_margin=req.esc_current_margin,
                rpm_data_tolerance=req.rpm_data_tolerance,
                frame_prop_clearance_m=clearance_m,
                frame_structural_margin=req.frame_structural_margin,
            )
        except ValueError as exc:
            return FixedEvaluateResponse(
                valid=False,
                rejection_reason="invalid_component_selection",
                rejection_details={"message": str(exc)},
            )
        except RuntimeError as exc:
            return FixedEvaluateResponse(
                valid=False,
                rejection_reason="engineering_rejection",
                rejection_details={
                    "message": str(exc),
                    "counts": dict(integ.REJECTION_COUNTS),
                },
            )
        return _success_from_row(row)

    # Commercial pack path
    pack = integ.build_locked_commercial_battery(req.battery_pack_id)
    if pack is None:
        return FixedEvaluateResponse(
            valid=False,
            rejection_reason="commercial_pack_incomplete",
            rejection_details={
                "battery_pack_id": req.battery_pack_id,
                "message": (
                    "Commercial pack is missing engineering fields required "
                    "by the solver (typically mass_g and/or continuous current)."
                ),
            },
        )

    # Reuse exact path by temporarily constructing via engine internals.
    try:
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
                if m.id == req.motor_id
            ),
            None,
        )
        if motor is None:
            raise ValueError(f"Motor not found: {req.motor_id}")
        prop_entry = prop_db.get(req.propeller_id)
        if prop_entry is None:
            raise ValueError(f"Propeller not found: {req.propeller_id}")
        prop_catalog = legacy.match_prop_to_catalog(prop_entry, apc_catalog)
        if prop_catalog is None:
            raise ValueError(
                f"Propeller lacks APC catalog match: {req.propeller_id}"
            )
        esc_pool = [
            e
            for e in integ.load_esc_candidates()
            if str(e.canonical_id) == str(req.esc_id)
        ]
        if not esc_pool:
            raise ValueError(f"ESC not found: {req.esc_id}")

        integ.REJECTION_COUNTS.clear()
        row = integ.run_real_design_exact(
            legacy,
            prop_entry,
            prop_catalog,
            motor,
            pack,
            esc_pool,
            fixed_mass=req.fixed_mass_kg,
            max_auw=req.max_auw_kg,
            min_twr=req.min_twr,
            rpm_tolerance=req.rpm_data_tolerance,
            esc_margin=req.esc_current_margin,
            price_resolver_obj=integ.price_resolver(),
            budget_mode="off",
            max_budget_usd=None,
            frame_prop_clearance_m=clearance_m,
            frame_structural_margin=req.frame_structural_margin,
        )
        if row is None:
            return FixedEvaluateResponse(
                valid=False,
                rejection_reason="engineering_rejection",
                rejection_details={"counts": dict(integ.REJECTION_COUNTS)},
            )
        scored = integ.add_decision_matrix([row])
        return _success_from_row(scored[0])
    except ValueError as exc:
        return FixedEvaluateResponse(
            valid=False,
            rejection_reason="invalid_component_selection",
            rejection_details={"message": str(exc)},
        )
