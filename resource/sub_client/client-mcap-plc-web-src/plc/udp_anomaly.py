"""UDP parse anomaly tracking and PLC variable change detection."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from plc.udp_parser import PlcPacket

POSITION_FIELDS = ("mh_pos", "mt_pos", "mc_pos", "cntrh_pos")
POSITION_LABELS = {
    "mh_pos": "吊具位置 MH_POS",
    "mt_pos": "小车位置 MT_POS",
    "mc_pos": "大车位置 MC_POS",
    "cntrh_pos": "集装箱高度 CNTRH_POS",
}
SMH_LABELS = {
    "spr_sp20": "20尺吊具",
    "spr_sp40": "40尺吊具",
    "spr_sp45": "45尺吊具",
    "mh_spr_lcked": "吊具闭锁",
    "mh_spr_unlcked": "吊具开锁",
    "mh_spr_landed": "吊具着箱",
    "hoist_up": "吊具上升",
    "hoist_down": "吊具下降",
}
OICR_LABELS = {
    "outside": "外集卡车道",
    "inside": "内集卡车道",
    "crane_maintain": "维修模式",
    "crane_left": "大车向左",
    "crane_right": "大车向右",
    "crane_street": "过街模式",
}


@dataclass
class UdpAnomalyTracker:
    max_recent_errors: int = 20
    rate_window_seconds: float = 10.0
    heartbeat_stall_threshold: int = 0

    packets_received: int = 0
    packets_parsed: int = 0
    parse_errors: int = 0
    invalid_header: int = 0
    invalid_footer: int = 0
    invalid_length: int = 0
    last_parse_error: str = ""
    last_source_ip: str = ""
    last_source_port: int = 0
    last_heart_beat: int | None = None
    heart_beat_stalls: int = 0
    _recent_errors: deque[dict[str, Any]] = field(default_factory=deque, init=False)
    _packet_times: deque[float] = field(default_factory=deque, init=False)

    def reset(self) -> None:
        self.packets_received = 0
        self.packets_parsed = 0
        self.parse_errors = 0
        self.invalid_header = 0
        self.invalid_footer = 0
        self.invalid_length = 0
        self.last_parse_error = ""
        self.last_source_ip = ""
        self.last_source_port = 0
        self.last_heart_beat = None
        self.heart_beat_stalls = 0
        self._recent_errors.clear()
        self._packet_times.clear()

    def on_raw_packet(
        self,
        data: bytes,
        source_ip: str,
        source_port: int,
    ) -> None:
        self.packets_received += 1
        self.last_source_ip = source_ip
        self.last_source_port = source_port
        now = time.time()
        self._packet_times.append(now)
        cutoff = now - self.rate_window_seconds
        while self._packet_times and self._packet_times[0] < cutoff:
            self._packet_times.popleft()

        if len(data) == 20:
            return

        if len(data) < 5:
            self._record_error("数据长度过短", source_ip, source_port)
            return

        if data[0:2] == b"\xaa\xbb" and data[-2:] == b"\xcc\xdd":
            payload_len = data[2]
            if len(data) < 3 + payload_len + 2:
                self.invalid_length += 1
                self._record_error(f"长度字段异常: {payload_len}", source_ip, source_port)
            return

        if data[0:2] != b"\xaa\xbb":
            self.invalid_header += 1
            self._record_error("帧头无效 (期望 AA BB 或 20 字节裸载荷)", source_ip, source_port)
            return
        if data[-2:] != b"\xcc\xdd":
            self.invalid_footer += 1
            self._record_error("帧尾无效 (期望 CC DD)", source_ip, source_port)
            return

    def on_parsed_packet(self, packet: PlcPacket) -> None:
        self.packets_parsed += 1
        if self.last_heart_beat is not None:
            expected = (self.last_heart_beat + 1) & 0xFFFF
            if packet.heart_beat not in {self.last_heart_beat, expected}:
                self.heart_beat_stalls += 1
        self.last_heart_beat = packet.heart_beat

    def on_parse_failed(self, message: str, source_ip: str, source_port: int) -> None:
        self._record_error(message, source_ip, source_port)

    def _record_error(self, message: str, source_ip: str, source_port: int) -> None:
        self.parse_errors += 1
        self.last_parse_error = message
        entry = {
            "time": time.time(),
            "message": message,
            "source_ip": source_ip,
            "source_port": source_port,
        }
        self._recent_errors.appendleft(entry)
        while len(self._recent_errors) > self.max_recent_errors:
            self._recent_errors.pop()

    @property
    def packet_rate_hz(self) -> float:
        if not self._packet_times:
            return 0.0
        span = max(time.time() - self._packet_times[0], 0.001)
        return len(self._packet_times) / span

    @property
    def is_healthy(self) -> bool:
        if self.packets_received == 0:
            return True
        error_rate = self.parse_errors / max(self.packets_received, 1)
        return error_rate < 0.05 and self.heart_beat_stalls < 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "packets_received": self.packets_received,
            "packets_parsed": self.packets_parsed,
            "parse_errors": self.parse_errors,
            "invalid_header": self.invalid_header,
            "invalid_footer": self.invalid_footer,
            "invalid_length": self.invalid_length,
            "last_parse_error": self.last_parse_error,
            "last_source_ip": self.last_source_ip,
            "last_source_port": self.last_source_port,
            "last_heart_beat": self.last_heart_beat,
            "heart_beat_stalls": self.heart_beat_stalls,
            "packet_rate_hz": round(self.packet_rate_hz, 2),
            "is_healthy": self.is_healthy,
            "recent_errors": list(self._recent_errors),
        }


@dataclass
class VariableChangeTracker:
    max_changes: int = 30
    _last: dict[str, Any] | None = field(default=None, init=False)
    changes: deque[dict[str, Any]] = field(default_factory=deque, init=False)

    def reset(self) -> None:
        self._last = None
        self.changes.clear()

    def update(self, packet: PlcPacket) -> list[dict[str, Any]]:
        current = packet.to_dict()
        new_changes: list[dict[str, Any]] = []
        if self._last is None:
            self._last = current
            return new_changes

        now = packet.received_at or time.time()
        for field_name in POSITION_FIELDS:
            old_val = self._last.get(field_name)
            new_val = current.get(field_name)
            if old_val != new_val:
                change = {
                    "time": now,
                    "field": field_name,
                    "label": POSITION_LABELS[field_name],
                    "old_value": old_val,
                    "new_value": new_val,
                    "kind": "position",
                }
                new_changes.append(change)

        if self._last.get("heart_beat") != current.get("heart_beat"):
            new_changes.append(
                {
                    "time": now,
                    "field": "heart_beat",
                    "label": "心跳 HEART_BEAT",
                    "old_value": self._last.get("heart_beat"),
                    "new_value": current.get("heart_beat"),
                    "kind": "counter",
                }
            )

        for group, labels in (("smh", SMH_LABELS), ("oicr", OICR_LABELS)):
            old_group = self._last.get(group) or {}
            new_group = current.get(group) or {}
            for key, label in labels.items():
                old_val = old_group.get(key)
                new_val = new_group.get(key)
                if old_val != new_val:
                    new_changes.append(
                        {
                            "time": now,
                            "field": f"{group}.{key}",
                            "label": label,
                            "old_value": old_val,
                            "new_value": new_val,
                            "kind": "signal",
                        }
                    )

        for change in new_changes:
            self.changes.appendleft(change)
        while len(self.changes) > self.max_changes:
            self.changes.pop()

        self._last = current
        return new_changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent_changes": list(self.changes),
            "change_count": len(self.changes),
        }
