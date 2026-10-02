"""全局同步网格（纳秒）。

所有传感器共享一个确定性的时间网格，不需要互相通信即可对齐：

    slot_k = k * period_ns + phase_ns        (Unix 纳秒，主机时钟需 PTP/chrony 同步)

约束：``1e9 % hz == 0``（或 hz<1 时 ``1/hz`` 为整数秒），保证每个整秒都是网格点，
因此 20Hz 的网格点集合 ⊇ 10Hz ⊇ 5Hz……，不同频率的传感器天然在整秒和公共节拍上对齐：

    20Hz: 0 50 100 150 200 ... ms
    10Hz: 0    100     200 ... ms
     1Hz: 0                ... ms（每整秒）

频率变更统一在 **下一个整秒** 生效（``next_second_boundary``），避免某个容器先切换、
另一个后切换导致的短暂错拍。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Optional, Union

NS_PER_S = 1_000_000_000
Number = Union[int, float, str, Fraction]


class GridError(ValueError):
    pass


def period_ns(hz: Number) -> int:
    """频率 -> 周期（ns）。不能整除时抛 GridError。"""
    f = Fraction(str(hz)) if not isinstance(hz, Fraction) else hz
    if f <= 0:
        raise GridError(f"频率必须 > 0，收到 {hz}")
    p = Fraction(NS_PER_S) / f
    if p.denominator != 1:
        raise GridError(f"{hz}Hz 的周期不是整数纳秒")
    p_int = int(p)
    if p_int < NS_PER_S and NS_PER_S % p_int != 0:
        raise GridError(f"{hz}Hz 无法整除 1 秒（允许 1,2,4,5,8,10,20,25,40,50… 或 0.5/0.2/0.1）")
    if p_int >= NS_PER_S and p_int % NS_PER_S != 0:
        raise GridError(f"{hz}Hz 的周期必须为整数秒")
    return p_int


def is_valid_hz(hz: Number) -> bool:
    try:
        period_ns(hz)
        return True
    except GridError:
        return False


def align_up(t_ns: int, p_ns: int, phase_ns: int = 0) -> int:
    """>= t_ns 的第一个网格点。"""
    k = -((-(t_ns - phase_ns)) // p_ns)
    return k * p_ns + phase_ns


def align_down(t_ns: int, p_ns: int, phase_ns: int = 0) -> int:
    return ((t_ns - phase_ns) // p_ns) * p_ns + phase_ns


def nearest_slot(t_ns: int, p_ns: int, phase_ns: int = 0) -> int:
    lo = align_down(t_ns, p_ns, phase_ns)
    return lo if (t_ns - lo) * 2 < p_ns else lo + p_ns


def slot_index(t_ns: int, p_ns: int, phase_ns: int = 0) -> int:
    return (t_ns - phase_ns) // p_ns


def next_second_boundary(t_ns: Optional[int] = None, margin_ns: int = 200_000_000) -> int:
    """距 t_ns 至少 margin_ns 之后的整秒（用于“下一个整秒生效”）。"""
    t = time.time_ns() if t_ns is None else t_ns
    return align_up(t + margin_ns, NS_PER_S)


@dataclass
class GridTimer:
    """按网格节拍阻塞等待。

    用法::

        timer = GridTimer(hz=20)
        while running:
            slot_ns = timer.wait_next()   # 返回本次网格时刻
            trigger(slot_ns)

    - 若处理超时错过若干节拍，``wait_next`` 直接跳到下一个未来节拍，并在 ``missed`` 计数。
    - ``set_hz`` 在下一个整秒生效。
    """

    hz: Number
    phase_ns: int = 0
    spin_ns: int = 300_000  # 最后 0.3ms 忙等，降低唤醒抖动
    clock: Callable[[], int] = time.time_ns

    def __post_init__(self) -> None:
        self._p = period_ns(self.hz)
        self._next: Optional[int] = None
        self._pending: Optional[tuple] = None
        self.missed = 0

    @property
    def period_ns(self) -> int:
        return self._p

    def set_hz(self, hz: Number, effective_ns: Optional[int] = None) -> int:
        p = period_ns(hz)
        eff = effective_ns if effective_ns is not None else next_second_boundary(self.clock())
        self._pending = (hz, p, eff)
        return eff

    def _maybe_apply_pending(self, slot: int) -> int:
        if self._pending and slot >= self._pending[2]:
            hz, p, eff = self._pending
            self.hz, self._p, self._pending = hz, p, None
            return align_up(eff, p, self.phase_ns)
        return slot

    def peek_next(self) -> int:
        now = self.clock()
        if self._next is None or self._next < now:
            nxt = align_up(now, self._p, self.phase_ns)
            if self._next is not None:
                self.missed += max(0, (nxt - self._next) // self._p - 1)
            self._next = nxt
        if self._pending and self._next >= self._pending[2]:
            self._next = self._maybe_apply_pending(self._next)
        return self._next

    def wait_next(self, stop: Optional[Callable[[], bool]] = None) -> Optional[int]:
        target = self.peek_next()
        while True:
            remain = target - self.clock()
            if remain <= 0:
                break
            if stop is not None and stop():
                return None
            if remain > self.spin_ns:
                time.sleep(min(remain - self.spin_ns, 50_000_000) / NS_PER_S)
        self._next = target + self._p
        if self._pending and self._next >= self._pending[2]:
            self._next = self._maybe_apply_pending(self._next)
        return target
