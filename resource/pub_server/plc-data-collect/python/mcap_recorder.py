"""Dual-tier rotating MCAP recorder with Beijing-time file naming."""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxglove_schemas_protobuf.SceneUpdate_pb2 import SceneUpdate
from mcap.writer import Writer as McapWriter
from mcap.well_known import MessageEncoding
from mcap_protobuf.schema import register_schema

from foxglove_messages import (
    TOPIC_CONNECTION,
    TOPIC_HEARTBEAT,
    TOPIC_OICR,
    TOPIC_POSITION,
    TOPIC_RAW,
    TOPIC_SCENE,
    TOPIC_SMH,
    TOPIC_VELOCITY,
    build_connection_json,
    build_heartbeat_json,
    build_position_json,
    build_raw_json,
    build_scene_update,
    build_signals_json,
    build_velocity_json,
    schema_bytes,
)
from udp_parser import PlcPacket, packet_value_signature

BEIJING = ZoneInfo("Asia/Shanghai")
FILE_NAME_RE = re.compile(
    r"^(\d{8}_\d{6})-(\d{8}_\d{6})(?:-empty)?\.mcap$"
)


def beijing_stamp(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), tz=BEIJING)
    return dt.strftime("%Y%m%d_%H%M%S")


def parse_beijing_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=BEIJING)
    except ValueError:
        return None


@dataclass
class McapCarryover:
    packet: PlcPacket
    velocities: tuple[float, float, float, float]
    health: dict[str, Any]


@dataclass
class McapTierConfig:
    name: str
    label: str
    output_dir: str
    rotation_seconds: int
    retention_days: int | None = 30
    list_limit: int = 10
    empty_max_unique_timestamps: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "output_dir": self.output_dir,
            "rotation_seconds": self.rotation_seconds,
            "retention_days": self.retention_days,
            "list_limit": self.list_limit,
            "empty_max_unique_timestamps": self.empty_max_unique_timestamps,
        }


@dataclass
class McapTierStatus:
    name: str
    label: str
    enabled: bool = True
    recording: bool = False
    current_file: str = ""
    bytes_written: int = 0
    messages_written: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "recording": self.recording,
            "current_file": self.current_file,
            "bytes_written": self.bytes_written,
            "messages_written": self.messages_written,
            "files": self.files,
            "last_error": self.last_error,
        }


class McapTierRecorder:
    """Single-tier MCAP writer with Beijing-time start-end file names."""

    def __init__(self, config: McapTierConfig, enabled: bool = True) -> None:
        self.config = config
        self.enabled = enabled
        self.status = McapTierStatus(name=config.name, label=config.label, enabled=enabled)
        self._lock = threading.Lock()
        self._writer: McapWriter | None = None
        self._file_handle = None
        self._file_started_at = 0.0
        self._file_start_stamp = ""
        self._current_path: Path | None = None
        self._channels: dict[str, int] = {}
        self._schema_ids: dict[str, int] = {}
        self._scene_schema_id: int | None = None
        self._file_needs_baseline = False
        self._unique_log_times: set[int] = set()
        self._cleanup_orphan_writing_files()
        self._prune_old_files()

    def _writing_dir(self) -> Path:
        path = Path(self.config.output_dir) / ".writing"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_orphan_writing_files(self) -> None:
        writing_dir = Path(self.config.output_dir) / ".writing"
        if not writing_dir.exists():
            return
        for path in writing_dir.glob("*.tmp"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _refresh_file_list(self) -> None:
        root = Path(self.config.output_dir)
        if not root.exists():
            self.status.files = []
            return
        files = sorted(root.glob("*.mcap"), key=lambda p: p.stat().st_mtime, reverse=True)
        self.status.files = [
            {
                "name": f.name,
                "path": str(f.resolve()),
                "size": f.stat().st_size,
                "modified_at": f.stat().st_mtime,
                "start_time": self._file_start_from_name(f.name),
                "end_time": self._file_end_from_name(f.name),
                "is_empty": "-empty.mcap" in f.name,
            }
            for f in files[: self.config.list_limit]
        ]

    @staticmethod
    def _file_start_from_name(name: str) -> str:
        match = FILE_NAME_RE.match(name)
        return match.group(1) if match else ""

    @staticmethod
    def _file_end_from_name(name: str) -> str:
        match = FILE_NAME_RE.match(name)
        return match.group(2) if match else ""

    def _close_writer(self, finalize_name: bool = True) -> None:
        end_stamp = beijing_stamp()
        path = self._current_path

        if self._writer is not None:
            try:
                self._writer.finish()
            except Exception:
                pass
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except Exception:
                pass

        self._writer = None
        self._file_handle = None
        self._channels.clear()
        self._schema_ids.clear()
        self._scene_schema_id = None
        self.status.recording = False

        if finalize_name and path is not None and path.exists() and self._file_start_stamp:
            suffix = ""
            if len(self._unique_log_times) <= self.config.empty_max_unique_timestamps:
                suffix = "-empty"
            final_name = f"{self._file_start_stamp}-{end_stamp}{suffix}.mcap"
            final_path = Path(self.config.output_dir) / final_name
            try:
                if final_path.exists():
                    final_path.unlink()
                path.rename(final_path)
                self.status.current_file = str(final_path)
            except OSError as exc:
                self.status.last_error = f"重命名 MCAP 失败: {exc}"
        elif path is not None:
            self.status.current_file = str(path)

        self._current_path = None
        self._file_start_stamp = ""
        self._unique_log_times.clear()

    def _new_temp_path(self, start_stamp: str) -> Path:
        return self._writing_dir() / f"{self.config.name}_{start_stamp}.mcap.tmp"

    def _maybe_rotate(self) -> None:
        if self._writer is None:
            return
        if time.time() - self._file_started_at < self.config.rotation_seconds:
            return
        self._close_writer(finalize_name=True)
        self._prune_old_files()

    def _open_writer(self) -> None:
        now = time.time()
        start_stamp = beijing_stamp(now)
        path = self._new_temp_path(start_stamp)
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)
        self._file_handle = open(path, "wb")
        self._writer = McapWriter(self._file_handle)
        self._writer.start(profile="foxglove", library="727r-plc-monitor")
        self._file_started_at = now
        self._file_start_stamp = start_stamp
        self._current_path = path
        self.status.current_file = str(path)
        self.status.recording = True
        self.status.bytes_written = 0
        self._file_needs_baseline = True
        self._unique_log_times.clear()
        self._register_channels()

    def _register_json_channel(self, topic: str, schema_name: str) -> int:
        assert self._writer is not None
        if topic in self._channels:
            return self._channels[topic]
        schema_id = self._writer.register_schema(
            name=schema_name,
            encoding="jsonschema",
            data=schema_bytes(schema_name),
        )
        channel_id = self._writer.register_channel(
            topic=topic,
            message_encoding=MessageEncoding.JSON,
            schema_id=schema_id,
        )
        self._channels[topic] = channel_id
        self._schema_ids[topic] = schema_id
        return channel_id

    def _register_channels(self) -> None:
        assert self._writer is not None
        self._register_json_channel(TOPIC_RAW, "crane727r.PlcRaw")
        self._register_json_channel(TOPIC_POSITION, "crane727r.Position")
        self._register_json_channel(TOPIC_VELOCITY, "crane727r.Velocity")
        self._register_json_channel(TOPIC_SMH, "crane727r.Signals")
        self._register_json_channel(TOPIC_OICR, "crane727r.Signals")
        self._register_json_channel(TOPIC_HEARTBEAT, "crane727r.Heartbeat")
        self._register_json_channel(TOPIC_CONNECTION, "crane727r.Connection")

        if self._scene_schema_id is None:
            self._scene_schema_id = register_schema(self._writer, SceneUpdate)
        self._channels[TOPIC_SCENE] = self._writer.register_channel(
            topic=TOPIC_SCENE,
            message_encoding=MessageEncoding.Protobuf,
            schema_id=self._scene_schema_id,
        )

    def _file_end_dt(self, path: Path) -> datetime:
        match = FILE_NAME_RE.match(path.name)
        if match:
            end_dt = parse_beijing_stamp(match.group(2))
            if end_dt is not None:
                return end_dt
        return datetime.fromtimestamp(path.stat().st_mtime, tz=BEIJING)

    def _prune_old_files(self) -> None:
        if self.config.retention_days is None:
            return
        cutoff = datetime.now(tz=BEIJING) - timedelta(days=self.config.retention_days)
        root = Path(self.config.output_dir)
        if not root.exists():
            return
        for path in root.glob("*.mcap"):
            try:
                if self._file_end_dt(path) < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def advance(self, carryover: McapCarryover | None) -> None:
        """Rotate files when needed and write baseline state into a new file."""
        if not self.enabled:
            return

        with self._lock:
            try:
                self._maybe_rotate()
                if self._writer is None:
                    self._open_writer()
                if self._file_needs_baseline and carryover is not None:
                    self._write_messages(
                        carryover.packet,
                        carryover.velocities,
                        carryover.health,
                    )
                    self._file_needs_baseline = False
            except Exception as exc:
                self.status.last_error = str(exc)
                self._close_writer(finalize_name=False)

    def record(self, carryover: McapCarryover) -> None:
        """Write a changed PLC snapshot to the current MCAP file."""
        if not self.enabled:
            return

        with self._lock:
            try:
                if self._writer is None:
                    self._open_writer()
                self._write_messages(
                    carryover.packet,
                    carryover.velocities,
                    carryover.health,
                )
                self._file_needs_baseline = False
            except Exception as exc:
                self.status.last_error = str(exc)
                self._close_writer(finalize_name=False)

    def _write_messages(
        self,
        packet: PlcPacket,
        velocities: tuple[float, float, float, float],
        health: dict[str, Any],
    ) -> None:
        assert self._writer is not None
        stamp_ns = int(packet.received_at * 1_000_000_000)
        self._unique_log_times.add(stamp_ns)

        writes = [
            (TOPIC_RAW, build_raw_json(packet)),
            (TOPIC_POSITION, build_position_json(packet)),
            (TOPIC_VELOCITY, build_velocity_json(*velocities)),
            (TOPIC_SMH, build_signals_json(packet.smh.__dict__)),
            (TOPIC_OICR, build_signals_json(packet.oicr.__dict__)),
            (TOPIC_HEARTBEAT, build_heartbeat_json(packet)),
            (TOPIC_CONNECTION, build_connection_json(health)),
        ]
        for topic, payload in writes:
            self._writer.add_message(
                channel_id=self._channels[topic],
                log_time=stamp_ns,
                data=payload,
                publish_time=stamp_ns,
            )
            self.status.messages_written += 1

        scene = build_scene_update(packet, stamp_ns)
        self._writer.add_message(
            channel_id=self._channels[TOPIC_SCENE],
            log_time=stamp_ns,
            data=scene.SerializeToString(),
            publish_time=stamp_ns,
        )
        self.status.messages_written += 1

        if self._file_handle is not None:
            self.status.bytes_written = self._file_handle.tell()
        self.status.last_error = ""

    def stop(self) -> None:
        with self._lock:
            self._close_writer(finalize_name=True)
            self._refresh_file_list()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_file_list()
            return {
                **self.status.to_dict(),
                "config": self.config.to_dict(),
            }


@dataclass
class McapConfig:
    enabled: bool = True
    dedup_enabled: bool = True
    hourly_dir: str = "data/mcap/hourly"
    short_dir: str = "data/mcap/short"
    hourly_rotation_seconds: int = 3600
    short_rotation_seconds: int = 600
    hourly_retention_days: int | None = 30
    short_retention_days: int | None = 1
    list_limit: int = 10
    empty_max_unique_timestamps: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dedup_enabled": self.dedup_enabled,
            "hourly_dir": self.hourly_dir,
            "short_dir": self.short_dir,
            "hourly_rotation_seconds": self.hourly_rotation_seconds,
            "short_rotation_seconds": self.short_rotation_seconds,
            "hourly_retention_days": self.hourly_retention_days,
            "short_retention_days": self.short_retention_days,
            "list_limit": self.list_limit,
            "empty_max_unique_timestamps": self.empty_max_unique_timestamps,
        }


class McapRecorder:
    """Dual-tier facade: hourly (30d) + short (10min, 1d)."""

    RETENTION_CHECK_SECONDS = 300

    def __init__(self, config: McapConfig | None = None) -> None:
        self.config = config or McapConfig()
        self._stop_event = threading.Event()
        self._retention_thread: threading.Thread | None = None
        self._last_signature: tuple[Any, ...] | None = None
        self._carryover: McapCarryover | None = None
        self._packets_written = 0
        self._packets_skipped = 0
        self._hourly = McapTierRecorder(
            McapTierConfig(
                name="hourly",
                label="1小时归档",
                output_dir=self.config.hourly_dir,
                rotation_seconds=self.config.hourly_rotation_seconds,
                retention_days=self.config.hourly_retention_days,
                list_limit=self.config.list_limit,
                empty_max_unique_timestamps=self.config.empty_max_unique_timestamps,
            ),
            enabled=self.config.enabled,
        )
        self._short = McapTierRecorder(
            McapTierConfig(
                name="short",
                label="10分钟归档",
                output_dir=self.config.short_dir,
                rotation_seconds=self.config.short_rotation_seconds,
                retention_days=self.config.short_retention_days,
                list_limit=self.config.list_limit,
                empty_max_unique_timestamps=self.config.empty_max_unique_timestamps,
            ),
            enabled=self.config.enabled,
        )
        self._start_retention_thread()

    def _start_retention_thread(self) -> None:
        if self._retention_thread and self._retention_thread.is_alive():
            return
        self._stop_event.clear()
        self._retention_thread = threading.Thread(
            target=self._retention_loop,
            name="mcap-retention",
            daemon=True,
        )
        self._retention_thread.start()

    def _retention_loop(self) -> None:
        while not self._stop_event.wait(self.RETENTION_CHECK_SECONDS):
            for tier in self.tiers:
                with tier._lock:
                    tier._prune_old_files()
                    tier._refresh_file_list()

    @property
    def tiers(self) -> list[McapTierRecorder]:
        return [self._hourly, self._short]

    def update_config(self, **kwargs: Any) -> McapConfig:
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)

        if "dedup_enabled" in kwargs and kwargs["dedup_enabled"]:
            self._last_signature = None

        self._hourly.enabled = self.config.enabled
        self._short.enabled = self.config.enabled
        self._hourly.config.output_dir = self.config.hourly_dir
        self._short.config.output_dir = self.config.short_dir
        self._hourly.config.rotation_seconds = self.config.hourly_rotation_seconds
        self._short.config.rotation_seconds = self.config.short_rotation_seconds
        self._hourly.config.retention_days = self.config.hourly_retention_days
        self._short.config.retention_days = self.config.short_retention_days
        self._hourly.config.list_limit = self.config.list_limit
        self._short.config.list_limit = self.config.list_limit
        self._hourly.config.empty_max_unique_timestamps = self.config.empty_max_unique_timestamps
        self._short.config.empty_max_unique_timestamps = self.config.empty_max_unique_timestamps

        for tier in self.tiers:
            Path(tier.config.output_dir).mkdir(parents=True, exist_ok=True)
            tier._refresh_file_list()
        return self.config

    def write_packet(
        self,
        packet: PlcPacket,
        velocities: tuple[float, float, float, float],
        health: dict[str, Any],
    ) -> None:
        if not self.config.enabled:
            return

        for tier in self.tiers:
            tier.advance(self._carryover)

        signature = packet_value_signature(packet)
        if self.config.dedup_enabled and self._last_signature == signature:
            self._packets_skipped += 1
            return

        self._last_signature = signature
        self._carryover = McapCarryover(packet, velocities, health)
        self._packets_written += 1

        for tier in self.tiers:
            tier.record(self._carryover)

    def stop(self) -> None:
        self._stop_event.set()
        self._hourly.stop()
        self._short.stop()

    def resolve_download_path(self, tier: str, filename: str) -> Path | None:
        safe_name = Path(filename).name
        if tier == "hourly":
            root = Path(self.config.hourly_dir)
        elif tier == "short":
            root = Path(self.config.short_dir)
        else:
            return None
        path = (root / safe_name).resolve()
        if not str(path).startswith(str(root.resolve())):
            return None
        if path.exists() and safe_name.endswith(".mcap"):
            return path
        return None

    def get_status(self) -> dict[str, Any]:
        hourly = self._hourly.get_status()
        short = self._short.get_status()
        return {
            "enabled": self.config.enabled,
            "dedup_enabled": self.config.dedup_enabled,
            "packets_written": self._packets_written,
            "packets_skipped": self._packets_skipped,
            "config": self.config.to_dict(),
            "tiers": {
                "hourly": hourly,
                "short": short,
            },
            "recording": hourly["recording"] or short["recording"],
            "current_files": {
                "hourly": hourly["current_file"],
                "short": short["current_file"],
            },
            "files": {
                "hourly": hourly["files"],
                "short": short["files"],
            },
            "messages_written": hourly["messages_written"] + short["messages_written"],
            "last_error": hourly["last_error"] or short["last_error"],
        }


def mcap_config_from_env() -> McapConfig:
    hourly_retention = os.getenv("MCAP_HOURLY_RETENTION_DAYS", "30")
    short_retention = os.getenv("MCAP_SHORT_RETENTION_DAYS", "1")
    return McapConfig(
        enabled=os.getenv("MCAP_ENABLED", "true").lower() in {"1", "true", "yes"},
        dedup_enabled=os.getenv("MCAP_DEDUP_ENABLED", "true").lower() in {"1", "true", "yes"},
        hourly_dir=os.getenv("MCAP_HOURLY_DIR", "/data/mcap/hourly"),
        short_dir=os.getenv("MCAP_SHORT_DIR", "/data/mcap/short"),
        hourly_rotation_seconds=int(os.getenv("MCAP_HOURLY_ROTATION_SECONDS", "3600")),
        short_rotation_seconds=int(os.getenv("MCAP_SHORT_ROTATION_SECONDS", "600")),
        hourly_retention_days=None if hourly_retention in {"", "0", "none", "unlimited"} else int(hourly_retention),
        short_retention_days=None if short_retention in {"", "0", "none", "unlimited"} else int(short_retention),
        list_limit=int(os.getenv("MCAP_LIST_LIMIT", "10")),
        empty_max_unique_timestamps=int(os.getenv("MCAP_EMPTY_MAX_UNIQUE_TIMESTAMPS", "2")),
    )
