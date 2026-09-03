from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class FixedEvaluateRequest(BaseModel):
    """Fixed-configuration calculator request.

    Prefer ``battery_pack_id`` for commercial packs, or
    ``battery_cell_id`` + ``battery_topology`` for a locked custom pack.
    """

    motor_id: str
    propeller_id: str
    esc_id: str

    battery_pack_id: Optional[str] = None
    battery_cell_id: Optional[str] = None
    battery_topology: Optional[str] = Field(
        default=None,
        description='Custom pack topology such as "6S4P". Required with battery_cell_id.',
    )

    fixed_mass_kg: float = 0.50
    max_auw_kg: float = 5.0
    min_twr: float = 1.8
    pack_overhead_fraction: float = 0.10
    esc_current_margin: float = 1.15
    rpm_data_tolerance: float = 0.10
    frame_prop_clearance_mm: float = 50.0
    frame_structural_margin: float = 1.25

    @model_validator(mode="after")
    def _battery_mode(self):
        has_pack = bool(self.battery_pack_id)
        has_cell = bool(self.battery_cell_id)
        if has_pack == has_cell:
            raise ValueError(
                "Provide exactly one of battery_pack_id or battery_cell_id"
            )
        if has_cell and not self.battery_topology:
            raise ValueError(
                "battery_topology is required when battery_cell_id is set"
            )
        return self


class CostBreakdown(BaseModel):
    motors_usd: Optional[float] = None
    props_usd: Optional[float] = None
    battery_usd: Optional[float] = None
    escs_usd: Optional[float] = None
    frame_allowance_usd: float
    camera_avionics_allowance_usd: float
    propulsion_estimated_usd: float
    total_estimated_usd: float
    price_estimated_categories: str
    price_actual_category_count: int


class FrameResult(BaseModel):
    material: str
    arm_tube_od_mm: float
    arm_tube_wall_mm: float
    arm_tube_length_m: float
    arm_stress_mpa: float
    arm_tip_deflection_mm: float
    stress_utilization_pct: float
    deflection_utilization_pct: float
    tip_to_tip_span_m: float
    frame_mass_kg: float


class FixedEvaluateResponse(BaseModel):
    valid: bool
    rejection_reason: Optional[str] = None
    rejection_details: Optional[dict[str, Any]] = None

    motor_id: Optional[str] = None
    propeller_id: Optional[str] = None
    battery_id: Optional[str] = None
    esc_id: Optional[str] = None

    auw_kg: Optional[float] = None
    endurance_min: Optional[float] = None
    hover_throttle_pct: Optional[float] = None
    hover_rpm: Optional[float] = None
    max_rpm: Optional[float] = None
    hover_power_w: Optional[float] = None
    hover_pack_current_a: Optional[float] = None
    max_pack_current_a: Optional[float] = None
    max_motor_current_a: Optional[float] = None
    max_twr: Optional[float] = None

    frame: Optional[FrameResult] = None
    cost: Optional[CostBreakdown] = None

    quality_score: Optional[float] = None
    buildability_score: Optional[float] = None
    decision_matrix_score: Optional[float] = None
    evaluation_mode: Optional[str] = None
