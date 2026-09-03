"""Concept-stage carbon-frame sizing — re-exported from the v0.7 integrated engine."""

from optimizer.propulsion.integrated import (
    DEFAULT_FRAME_PROP_CLEARANCE_M,
    DEFAULT_FRAME_STRUCTURAL_MARGIN,
    FRAME_CF_ALLOWABLE_BENDING_PA,
    FRAME_CF_DENSITY_KG_M3,
    FRAME_CF_EFFECTIVE_E_PA,
    FRAME_MATERIAL_NAME,
    FRAME_MAX_ARM_DEFLECTION_RATIO,
    FRAME_TUBE_CANDIDATES_MM,
    estimate_frame_mass,
)

__all__ = [
    "DEFAULT_FRAME_PROP_CLEARANCE_M",
    "DEFAULT_FRAME_STRUCTURAL_MARGIN",
    "FRAME_CF_ALLOWABLE_BENDING_PA",
    "FRAME_CF_DENSITY_KG_M3",
    "FRAME_CF_EFFECTIVE_E_PA",
    "FRAME_MATERIAL_NAME",
    "FRAME_MAX_ARM_DEFLECTION_RATIO",
    "FRAME_TUBE_CANDIDATES_MM",
    "estimate_frame_mass",
]
