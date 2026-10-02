"""订阅侧工具：帧解析、降频、只保留最新帧、多传感器同步组装。

性能要点：
- zenoh 回调运行在 zenoh 内部线程，**回调里禁止做重活**（推理、编码、写盘）。
  用 ``LatestSlot``/队列把帧交给自己的工作线程。
- ``Frame`` 持有 sample 期间 SHM 缓冲不会被回收；需要长期保存数据请 ``frame.payload()`` 拷贝出来后释放 Frame。
- 只读 ``frame.header`` 不会触碰 payload（零开销），适合统计/监控。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

import zenoh

from .header import FrameHeader
from .sync_grid import nearest_slot, period_ns, slot_index


@dataclass
class Frame:
    key: str
    header: Optional[FrameHeader]
    sample: zenoh.Sample
    recv_ns: int = field(default_factory=time.time_ns)
    _payload: Optional[bytes] = None

    @classmethod
    def from_sample(cls, sample: zenoh.Sample) -> "Frame":
        hdr = None
        att = sample.attachment
        if att is not None:
            try:
                hdr = FrameHeader.unpack(att.to_bytes())
            except ValueError:
                hdr = None
        return cls(str(sample.key_expr), hdr, sample)

    def payload(self) -> bytes:
        if self._payload is None:
            self._payload = self.sample.payload.to_bytes()
        return self._payload

    @property
    def is_shm(self) -> bool:
        return self.sample.payload.as_shm() is not None

    @property
    def time_ns(self) -> int:
        """用于对齐的时间：优先网格时刻，其次传感器时刻，最后接收时刻。"""
        h = self.header
        if h is not None:
            return h.trigger_ns or h.stamp_ns or self.recv_ns
        return self.recv_ns

    @property
    def transport_latency_ns(self) -> int:
        return self.recv_ns - self.header.pub_ns if self.header and self.header.pub_ns else 0


class Decimator:
    """按全局网格降频：hz=1 时每个整秒只放行一帧，所有订阅者选中的是“同一拍”。"""

    def __init__(self, hz: Optional[float]) -> None:
        self.period = period_ns(hz) if hz else 0
        self._last: Dict[str, int] = {}

    def accept(self, key: str, t_ns: int) -> bool:
        if not self.period:
            return True
        s = slot_index(t_ns, self.period)
        if self._last.get(key) == s:
            return False
        self._last[key] = s
        return True


class LatestSlot:
    """容量为 1 的“只保留最新”槽：慢消费者总是拿到最新一帧，旧帧自动丢弃。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._item = None
        self.dropped = 0

    def put(self, item) -> None:
        with self._cv:
            if self._item is not None:
                self.dropped += 1
            self._item = item
            self._cv.notify()

    def get(self, timeout: Optional[float] = None):
        with self._cv:
            if self._item is None:
                self._cv.wait(timeout)
            item, self._item = self._item, None
            return item


def subscribe_frames(
    session: zenoh.Session,
    key_expr: str,
    on_frame: Callable[[Frame], None],
    *,
    max_hz: Optional[float] = None,
):
    """订阅数据帧；max_hz 不为空时按网格降频（预览/可视化用）。返回 zenoh Subscriber。"""
    dec = Decimator(max_hz)

    def _cb(sample: zenoh.Sample) -> None:
        fr = Frame.from_sample(sample)
        if dec.accept(fr.key, fr.time_ns):
            on_frame(fr)

    return session.declare_subscriber(key_expr, _cb)


class SyncAssembler:
    """把多个 key 的帧按网格时刻组装成同步组。

    - ``required``: 必须到齐的 key 列表（如 6 路相机 + 2 路雷达）。
    - ``base_hz``  : 组装网格频率（取各传感器频率的最小公共频率，如相机 20Hz、雷达 10Hz 时取 10）。
    - ``timeout_s``: 组最长等待时间，超时后若 ``emit_partial`` 则输出不完整组，否则丢弃。

    回调参数：``(slot_ns, {key: Frame})``。
    """

    def __init__(
        self, required: Iterable[str], on_group: Callable[[int, Dict[str, Frame]], None], *,
        base_hz: float = 10, timeout_s: float = 0.3, emit_partial: bool = False, max_pending: int = 32,
    ) -> None:
        self.required = list(required)
        self.on_group = on_group
        self.period = period_ns(base_hz)
        self.timeout_ns = int(timeout_s * 1e9)
        self.emit_partial = emit_partial
        self.max_pending = max_pending
        self._pending: "OrderedDict[int, Dict[str, Frame]]" = OrderedDict()
        self._first_seen: Dict[int, int] = {}
        self._lock = threading.Lock()
        self.complete = 0
        self.partial = 0
        self.dropped = 0

    def push(self, fr: Frame) -> None:
        if fr.key not in self.required:
            return
        slot = nearest_slot(fr.time_ns, self.period)
        ready: List = []
        with self._lock:
            grp = self._pending.setdefault(slot, {})
            self._first_seen.setdefault(slot, time.time_ns())
            grp[fr.key] = fr
            if len(grp) == len(self.required):
                ready.append((slot, self._pending.pop(slot)))
                self._first_seen.pop(slot, None)
                self.complete += 1
            ready.extend(self._expire_locked())
        for slot_ns, g in ready:
            self.on_group(slot_ns, g)

    def flush(self) -> None:
        with self._lock:
            ready = self._expire_locked()
        for slot_ns, g in ready:
            self.on_group(slot_ns, g)

    def _expire_locked(self):
        now = time.time_ns()
        out = []
        for slot in list(self._pending):
            too_old = now - self._first_seen.get(slot, now) > self.timeout_ns
            overflow = len(self._pending) > self.max_pending
            if not (too_old or overflow):
                break
            grp = self._pending.pop(slot)
            self._first_seen.pop(slot, None)
            if self.emit_partial:
                self.partial += 1
                out.append((slot, grp))
            else:
                self.dropped += 1
        return out
