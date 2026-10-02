"""秒级网格同步触发时刻（1000ms 整除频率）。

所有传感器（GigE 相机、雷达、IMU）共用同一套网格算法：
- 支持频率：5 / 10 / 20 / 25 / 40 Hz（period = 1000/hz ms）
- 每秒钟在 0、period、2*period … ms 处触发
- 例：10Hz → 0ms, 100ms, 200ms, …, 900ms
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

SUPPORTED_HZ: Tuple[int, ...] = (5, 10, 20, 25, 40)


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
    """返回严格大于 now_ms 的下一个网格触发时刻（epoch ms）。"""
    if period_ms < 1:
        period_ms = 1
    sec = now_ms // 1000
    offset = now_ms - sec * 1000
    for slot in slots_in_second(period_ms):
        if slot > offset:
            return sec * 1000 + slot
    return (sec + 1) * 1000


def advance_sync_trigger_ms(prev_ms: int, period_ms: int) -> int:
    """从上一触发时刻推进到下一个网格点。"""
    if period_ms < 1:
        period_ms = 1
    sec = prev_ms // 1000
    offset = prev_ms % 1000
    nxt = offset + period_ms
    if nxt < 1000:
        return sec * 1000 + nxt
    return (sec + 1) * 1000


def align_up_sync_ms(now_ms: int, period_ms: int) -> int:
    """对齐到 >= now_ms 的最近网格点（含当前槽）。"""
    return next_sync_trigger_ms(now_ms - 1, period_ms)


def wait_until_sync_ms(
    target_ms: int,
    *,
    stop: Optional[Callable[[], bool]] = None,
    max_early_ms: float = 2.0,
) -> int:
    """阻塞直到到达 target_ms（允许 max_early_ms 提前唤醒以预留处理时间）。

    返回实际唤醒时的 epoch ms。
    """
    while True:
        now_ms = int(time.time() * 1000)
        if now_ms >= target_ms - max_early_ms:
            return now_ms
        if stop is not None and stop():
            return now_ms
        remain = (target_ms - max_early_ms - now_ms) / 1000.0
        time.sleep(min(max(remain, 0.0005), 0.05))


def sync_lag_ms(trigger_ms: int, publish_ms: int) -> int:
    """发布相对触发时刻的延迟（ms），用于客户端质量监控。"""
    return int(publish_ms) - int(trigger_ms)
