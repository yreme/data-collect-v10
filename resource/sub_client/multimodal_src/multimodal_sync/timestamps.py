"""Normalize sensor wall-clock timestamps for Foxglove log_time / MCAP."""

from __future__ import annotations

import time
from typing import Optional

# u64::MAX — uninitialized SHM or corrupt metadata may surface this value.
UINT64_MAX = 18446744073709551615

# Reasonable epoch-ms window (2020-01-01 .. 2038-01-01).
_MIN_EPOCH_MS = 1_577_836_800_000
_MAX_EPOCH_MS = 2_208_988_800_000


def _is_sane_epoch_ms(ts_ms: int) -> bool:
    return _MIN_EPOCH_MS <= ts_ms <= _MAX_EPOCH_MS


def normalize_epoch_ms(
    trigger_ms: int = 0,
    recv_ms: int = 0,
    host_ms: int = 0,
    *,
    fallback_ms: Optional[int] = None,
) -> int:
    """Pick the first plausible epoch-millisecond timestamp from SHM metadata.

    Falls back to wall clock when all candidates are missing or invalid
    (0, UINT64_MAX, out-of-range, or seconds mistaken for milliseconds).
    """
    if fallback_ms is None:
        fallback_ms = int(time.time() * 1000)

    for raw in (trigger_ms, recv_ms, host_ms):
        if not raw:
            continue
        ts = int(raw)
        if ts <= 0 or ts >= UINT64_MAX:
            continue
        if _is_sane_epoch_ms(ts):
            return ts
        # Some firmware reports epoch seconds in a u64 field.
        as_ms = ts * 1000
        if _is_sane_epoch_ms(as_ms):
            return as_ms

    return int(fallback_ms)


def log_ns_from_epoch_ms(ts_ms: int, *, fallback_ms: Optional[int] = None) -> int:
    """Convert validated epoch milliseconds to nanoseconds for MCAP log_time."""
    sane_ms = normalize_epoch_ms(ts_ms, fallback_ms=fallback_ms)
    log_ns = int(sane_ms) * 1_000_000
    if log_ns < 0 or log_ns > UINT64_MAX:
        return int(time.time() * 1e9)
    return log_ns


def log_ns_from_fields(
    trigger_ms: int = 0,
    recv_ms: int = 0,
    host_ms: int = 0,
    *,
    fallback_ms: Optional[int] = None,
) -> int:
    """Combine :func:`normalize_epoch_ms` and :func:`log_ns_from_epoch_ms`."""
    ts_ms = normalize_epoch_ms(trigger_ms, recv_ms, host_ms, fallback_ms=fallback_ms)
    return log_ns_from_epoch_ms(ts_ms, fallback_ms=fallback_ms)
