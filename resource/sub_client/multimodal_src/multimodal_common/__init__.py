"""Multimodal sensor capture shared utilities."""

from .sync_grid import (
    SUPPORTED_HZ,
    advance_sync_trigger_ms,
    align_up_sync_ms,
    hz_to_period_ms,
    next_sync_trigger_ms,
    slots_in_second,
    validate_hz,
    wait_until_sync_ms,
)
from .unified_config import UnifiedConfig, load_unified_config

__all__ = [
    "SUPPORTED_HZ",
    "advance_sync_trigger_ms",
    "align_up_sync_ms",
    "hz_to_period_ms",
    "next_sync_trigger_ms",
    "slots_in_second",
    "validate_hz",
    "wait_until_sync_ms",
    "UnifiedConfig",
    "load_unified_config",
]
