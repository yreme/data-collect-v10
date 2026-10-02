"""雷达事件日志（供 Web 异常页查询）。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


@dataclass
class LidarEvent:
    ts_ms: int
    lidar: str
    level: str
    category: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "lidar": self.lidar,
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "hint": self.hint,
        }


class EventLog:
    _MAX = 500

    def __init__(self) -> None:
        self._events: Deque[LidarEvent] = deque(maxlen=self._MAX)
        self._lock = threading.Lock()

    def add(
        self,
        lidar: str,
        level: str,
        category: str,
        message: str,
        hint: str = "",
    ) -> None:
        ev = LidarEvent(
            ts_ms=int(time.time() * 1000),
            lidar=lidar,
            level=level,
            category=category,
            message=message,
            hint=hint,
        )
        with self._lock:
            self._events.appendleft(ev)

    def list_events(self, lidar: Optional[str] = None, limit: int = 100) -> List[dict]:
        with self._lock:
            items = list(self._events)
        if lidar:
            items = [e for e in items if e.lidar == lidar]
        return [e.to_dict() for e in items[:limit]]


GLOBAL_EVENT_LOG = EventLog()
