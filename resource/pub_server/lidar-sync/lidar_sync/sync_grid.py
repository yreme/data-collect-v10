"""秒级网格同步触发时刻（1000ms 整除频率）。

支持频率：5 / 10 / 20 / 25 / 40 Hz（period = 1000/hz ms）。
每秒钟在 0、period、2*period … ms 处触发，便于与其它传感器对齐。
"""

from __future__ import annotations

from typing import List, Tuple

SUPPORTED_HZ: Tuple[int, ...] = (5, 10, 20, 25, 40)

MSOP_PACKET_SIZE = 1200
DIFOP_PACKET_SIZE = 256


def validate_hz(hz: int) -> int:
    if hz not in SUPPORTED_HZ:
        raise ValueError(f"capture.hz 必须是 {SUPPORTED_HZ} 之一，收到 {hz}")
    return hz


def hz_to_period_ms(hz: int) -> int:
    validate_hz(hz)
    return 1000 // hz


def period_ms_to_hz(period_ms: int) -> int:
    hz = 1000 // period_ms
    if 1000 % period_ms != 0 or hz not in SUPPORTED_HZ:
        raise ValueError(f"period_ms={period_ms} 无法映射到支持频率 {SUPPORTED_HZ}")
    return hz


def slots_in_second(period_ms: int) -> List[int]:
    return list(range(0, 1000, period_ms))


def next_sync_trigger_ms(now_ms: int, period_ms: int) -> int:
    if period_ms < 1:
        period_ms = 1
    sec = now_ms // 1000
    offset = now_ms - sec * 1000
    for slot in slots_in_second(period_ms):
        if slot > offset:
            return sec * 1000 + slot
    return (sec + 1) * 1000


def advance_sync_trigger_ms(prev_ms: int, period_ms: int) -> int:
    if period_ms < 1:
        period_ms = 1
    sec = prev_ms // 1000
    offset = prev_ms % 1000
    nxt = offset + period_ms
    if nxt < 1000:
        return sec * 1000 + nxt
    return (sec + 1) * 1000


def align_up_sync_ms(now_ms: int, period_ms: int) -> int:
    return next_sync_trigger_ms(now_ms - 1, period_ms)
