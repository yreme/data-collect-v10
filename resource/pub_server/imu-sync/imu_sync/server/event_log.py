"""Server 事件日志（Web UI 展示）。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ImuEvent:
    timestamp_ms: int
    imu_name: str
    level: str
    category: str
    message: str
    hint: str = ""


class EventLog:
    def __init__(self, capacity: int = 500) -> None:
        self._capacity = capacity
        self._events: List[ImuEvent] = []
        self._lock = threading.Lock()

    def add(
        self,
        imu_name: str,
        level: str,
        category: str,
        message: str,
        *,
        hint: str = "",
    ) -> None:
        ev = ImuEvent(
            timestamp_ms=int(time.time() * 1000),
            imu_name=imu_name,
            level=level,
            category=category,
            message=message,
            hint=hint,
        )
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self._capacity:
                self._events = self._events[-self._capacity :]

    def list_events(self, *, imu: Optional[str] = None, limit: int = 200) -> List[dict]:
        with self._lock:
            events = list(self._events)
        if imu:
            events = [e for e in events if e.imu_name == imu]
        events = events[-limit:]
        return [
            {
                "timestamp_ms": e.timestamp_ms,
                "imu": e.imu_name,
                "level": e.level,
                "category": e.category,
                "message": e.message,
                "hint": e.hint,
            }
            for e in reversed(events)
        ]


GLOBAL_EVENT_LOG = EventLog()
