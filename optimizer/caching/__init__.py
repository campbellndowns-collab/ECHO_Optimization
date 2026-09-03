"""Propulsion curve and physical-pool caches — re-exported from v0.7."""

from optimizer.propulsion.integrated import (
    PROPULSION_CACHE_DB,
    PROPULSION_CURVE_SCHEMA_VERSION,
    PropulsionCurveCache,
    load_physical_pool,
    physical_pool_key,
    save_physical_pool,
)

__all__ = [
    "PROPULSION_CACHE_DB",
    "PROPULSION_CURVE_SCHEMA_VERSION",
    "PropulsionCurveCache",
    "load_physical_pool",
    "physical_pool_key",
    "save_physical_pool",
]
