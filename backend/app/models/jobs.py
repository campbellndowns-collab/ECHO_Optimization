"""Optimization job request/response models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class OptimizationJobRequest(BaseModel):
    motor_mode: Literal["optimize", "lock"] = "optimize"
    motor_id: Optional[str] = None
    prop_mode: Literal["optimize", "lock"] = "optimize"
    prop_id: Optional[str] = None
    battery_mode: Literal["optimize", "commercial", "cell"] = "optimize"
    battery_id: Optional[str] = None
    esc_mode: Literal["optimize", "lock"] = "optimize"
    esc_id: Optional[str] = None

    fixed_mass_kg: Optional[float] = None
    max_auw_kg: float = 10.0
    min_twr: float = 1.8
    pack_overhead_fraction: float = 0.10
    esc_current_margin: float = 1.15
    rpm_data_tolerance: float = 0.10
    search_depth: Literal["standard", "thorough", "deep"] = "standard"
    budget_mode: Literal[
        "off", "known_only", "propulsion_strict", "whole_aircraft_strict"
    ] = "off"
    frame_prop_clearance_mm: float = 50.0
    frame_structural_margin: float = 1.25
    fixed_nonpropulsion_cost: float = 700.0

    weight_cost: float = 35.0
    weight_endurance: float = 25.0
    weight_aircraft_mass: float = 17.0
    weight_quality: float = 13.0
    weight_complexity: float = 10.0

    force_physics: bool = False
    force_parametric: bool = False


class OptimizationJobCreated(BaseModel):
    job_id: str


class OptimizationJobStatus(BaseModel):
    job_id: str
    status: str
    stage: Optional[str] = None
    progress: float = 0.0
    message: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    estimated_remaining_seconds: Optional[float] = None
    error: Optional[str] = None


class OptimizationJobResults(BaseModel):
    job_id: str
    status: str
    top_designs: list[dict[str, Any]] = Field(default_factory=list)
    pool: list[dict[str, Any]] = Field(default_factory=list)
    run_settings: Optional[dict[str, Any]] = None
    timing: Optional[dict[str, Any]] = None
    cache_statistics: Optional[dict[str, Any]] = None


class RerankRequest(BaseModel):
    designs: list[dict[str, Any]]
    weight_cost: float = 35.0
    weight_endurance: float = 25.0
    weight_aircraft_mass: float = 17.0
    weight_quality: float = 13.0
    weight_complexity: float = 10.0


class RerankResponse(BaseModel):
    designs: list[dict[str, Any]]
