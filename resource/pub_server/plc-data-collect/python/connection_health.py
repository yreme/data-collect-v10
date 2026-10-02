"""UDP data path health tracking for 727R PLC monitor."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class LinkState(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class HealthSnapshot:
    state: LinkState
    mock_mode: bool
    running: bool
    last_packet_at: float | None
    seconds_since_last_packet: float | None
    timeout_seconds: float
    message: str
    can_collect: bool
    can_record_mcap: bool

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "mock_mode": self.mock_mode,
            "running": self.running,
            "last_packet_at": self.last_packet_at,
            "seconds_since_last_packet": self.seconds_since_last_packet,
            "timeout_seconds": self.timeout_seconds,
            "message": self.message,
            "can_collect": self.can_collect,
            "can_record_mcap": self.can_record_mcap,
        }


class ConnectionHealth:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.running = False
        self.mock_mode = True
        self.last_packet_at: float | None = None
        self.error_message = ""

    def reset(self, mock_mode: bool) -> None:
        self.running = True
        self.mock_mode = mock_mode
        self.last_packet_at = None
        self.error_message = ""

    def stop(self) -> None:
        self.running = False
        self.last_packet_at = None

    def set_error(self, message: str) -> None:
        self.error_message = message
        self.running = False

    def on_packet(self) -> None:
        self.last_packet_at = time.time()
        self.error_message = ""

    def snapshot(self, mcap_enabled: bool) -> HealthSnapshot:
        now = time.time()

        if not self.running:
            return HealthSnapshot(
                state=LinkState.IDLE,
                mock_mode=self.mock_mode,
                running=False,
                last_packet_at=self.last_packet_at,
                seconds_since_last_packet=None,
                timeout_seconds=self.timeout_seconds,
                message="监控未启动",
                can_collect=False,
                can_record_mcap=False,
            )

        if self.error_message:
            return HealthSnapshot(
                state=LinkState.ERROR,
                mock_mode=self.mock_mode,
                running=False,
                last_packet_at=self.last_packet_at,
                seconds_since_last_packet=(
                    now - self.last_packet_at if self.last_packet_at else None
                ),
                timeout_seconds=self.timeout_seconds,
                message=self.error_message,
                can_collect=False,
                can_record_mcap=False,
            )

        if self.mock_mode:
            return HealthSnapshot(
                state=LinkState.CONNECTED,
                mock_mode=True,
                running=True,
                last_packet_at=self.last_packet_at,
                seconds_since_last_packet=(
                    now - self.last_packet_at if self.last_packet_at else None
                ),
                timeout_seconds=self.timeout_seconds,
                message="Mock 模拟数据通路正常",
                can_collect=True,
                can_record_mcap=mcap_enabled,
            )

        if self.last_packet_at is None:
            return HealthSnapshot(
                state=LinkState.WAITING,
                mock_mode=False,
                running=True,
                last_packet_at=None,
                seconds_since_last_packet=None,
                timeout_seconds=self.timeout_seconds,
                message=f"等待 UDP 数据…（{self.timeout_seconds:.0f}s 内未收到将报错）",
                can_collect=False,
                can_record_mcap=False,
            )

        elapsed = now - self.last_packet_at
        if elapsed > self.timeout_seconds:
            return HealthSnapshot(
                state=LinkState.DISCONNECTED,
                mock_mode=False,
                running=True,
                last_packet_at=self.last_packet_at,
                seconds_since_last_packet=elapsed,
                timeout_seconds=self.timeout_seconds,
                message=(
                    f"UDP 数据通路中断：已超过 {self.timeout_seconds:.0f}s 未收到数据包"
                ),
                can_collect=False,
                can_record_mcap=False,
            )

        return HealthSnapshot(
            state=LinkState.CONNECTED,
            mock_mode=False,
            running=True,
            last_packet_at=self.last_packet_at,
            seconds_since_last_packet=elapsed,
            timeout_seconds=self.timeout_seconds,
            message="UDP 数据通路正常",
            can_collect=True,
            can_record_mcap=mcap_enabled,
        )
