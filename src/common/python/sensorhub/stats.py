"""轻量统计：频率、带宽、延迟分位数（线程安全，O(1) 记录）。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class StreamStats:
    """在固定时间窗口内统计消息频率/字节/延迟。"""

    def __init__(self, window_s: float = 5.0, max_samples: int = 4096) -> None:
        self._window_ns = int(window_s * 1e9)
        self._buf: Deque[Tuple[int, int, int, int]] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self.total_count = 0
        self.total_bytes = 0
        self.gaps = 0
        self._last_seq: Optional[int] = None
        self.last_ns = 0

    def record(self, nbytes: int, *, latency_ns: int = 0, sync_err_ns: int = 0,
               seq: Optional[int] = None, t_ns: Optional[int] = None) -> None:
        t = t_ns if t_ns is not None else time.time_ns()
        with self._lock:
            self._buf.append((t, nbytes, latency_ns, sync_err_ns))
            self.total_count += 1
            self.total_bytes += nbytes
            self.last_ns = t
            if seq is not None:
                if self._last_seq is not None and seq > self._last_seq + 1:
                    self.gaps += seq - self._last_seq - 1
                self._last_seq = seq

    def snapshot(self, now_ns: Optional[int] = None) -> Dict[str, object]:
        now = now_ns if now_ns is not None else time.time_ns()
        with self._lock:
            lo = now - self._window_ns
            while self._buf and self._buf[0][0] < lo:
                self._buf.popleft()
            items = list(self._buf)
            out: Dict[str, object] = {
                "count": self.total_count,
                "gaps": self.gaps,
                "last_ns": self.last_ns,
                "age_ms": round((now - self.last_ns) / 1e6, 1) if self.last_ns else None,
            }
        n = len(items)
        if n >= 2:
            span = (items[-1][0] - items[0][0]) / 1e9
            rate = (n - 1) / span if span > 0 else 0.0
        else:
            rate = 0.0
        # 若最后一条已经超过窗口一半没有更新，频率按 0 处理（避免“僵尸”频率）
        if items and (now - items[-1][0]) > self._window_ns / 2:
            rate = 0.0
        sizes = [i[1] for i in items]
        out["rate_hz"] = round(rate, 2)
        out["msg_bytes"] = int(sum(sizes) / n) if n else 0
        out["bytes_per_s"] = int(rate * out["msg_bytes"])
        out["latency_ms"] = _pct([i[2] for i in items if i[2]])
        out["sync_err_ms"] = _pct([abs(i[3]) for i in items if i[3]])
        return out


def _pct(vals) -> Optional[Dict[str, float]]:
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)

    def q(p: float) -> float:
        return round(vals[min(n - 1, int(p * n))] / 1e6, 3)

    return {"p50": q(0.5), "p95": q(0.95), "max": round(vals[-1] / 1e6, 3)}
