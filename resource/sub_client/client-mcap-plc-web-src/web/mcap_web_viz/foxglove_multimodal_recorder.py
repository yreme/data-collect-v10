"""Dual-tier Foxglove MCAP recorder: PLC-triggered multimodal snapshots."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from gige_sync.clients.topics import image_topic
from multimodal_sync.clients.video_encoder import H264Encoder, decode_camera_to_rgb

from .topic_sidecar import TopicSidecarBuilder, sidecar_path_for_mcap, write_topic_sidecar
from imu_sync.clients.messages import (
    IMU_JSON_SCHEMA,
    build_frame_transform,
    build_imu_message,
    build_world_pose,
    log_ns_from_view,
    sample_from_view,
)
from imu_sync.clients.pose_tracker import PoseTrackerRegistry, scene_model_config_for_imu
from imu_sync.config import ImuConfig, PoseConfig, SceneModelConfig
from lidar_sync.pointcloud_codec import unpack_points
from multimodal_sync.config import AppConfig, SensorRef

LOG = logging.getLogger("mcap_web.foxglove_recorder")
BEIJING = ZoneInfo("Asia/Shanghai")
FILE_NAME_RE = re.compile(r"^(\d{8}_\d{6})-(\d{8}_\d{6})(?:-empty)?\.mcap$")


def beijing_stamp(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), tz=BEIJING)
    return dt.strftime("%Y%m%d_%H%M%S")


def parse_beijing_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=BEIJING)
    except ValueError:
        return None


def period_start_ts(ts: float, period_seconds: int) -> float:
    """Align file window start to wall-clock period (10 min / 1 hour)."""
    dt = datetime.fromtimestamp(ts, tz=BEIJING)
    if period_seconds >= 3600:
        aligned = dt.replace(minute=0, second=0, microsecond=0)
    elif period_seconds >= 600:
        aligned = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
    else:
        aligned = dt.replace(second=0, microsecond=0)
    return aligned.timestamp()


def _to_timestamp(ns: int):
    from foxglove.messages import Timestamp

    return Timestamp(sec=int(ns // 1_000_000_000), nsec=int(ns % 1_000_000_000))


def _mcap_compression(kind: str):
    from foxglove.mcap import MCAPCompression

    k = (kind or "zstd").strip().lower()
    if k in ("none", "off", ""):
        return None
    if k == "lz4":
        return MCAPCompression.Lz4
    return MCAPCompression.Zstd


@dataclass
class PlcSnapshot:
    packet: Any
    velocities: tuple[float, float, float, float]
    health: dict[str, Any]
    log_ns: int


@dataclass
class CameraSample:
    name: str
    sensor: SensorRef
    encoding: str
    data: bytes
    log_ns: int
    seq: int
    width: int = 0
    height: int = 0


@dataclass
class ImuSample:
    name: str
    sensor: SensorRef
    view: Any
    log_ns: int
    seq: int


@dataclass
class LidarSample:
    name: str
    sensor: SensorRef
    pc: Any
    log_ns: int
    seq: int


@dataclass
class MultimodalCarryover:
    plc: PlcSnapshot | None = None
    cameras: dict[str, CameraSample] = field(default_factory=dict)
    imus: dict[str, ImuSample] = field(default_factory=dict)
    lidars: dict[str, LidarSample] = field(default_factory=dict)


@dataclass
class McapTierConfig:
    name: str
    label: str
    output_dir: str
    rotation_seconds: int
    retention_days: int | None = 30
    list_limit: int = 10
    empty_max_unique_timestamps: int = 2


@dataclass
class McapConfig:
    enabled: bool = True
    dedup_enabled: bool = True
    hourly_dir: str = "/data/mcap/hourly"
    short_dir: str = "/data/mcap/short"
    hourly_rotation_seconds: int = 3600
    short_rotation_seconds: int = 600
    hourly_retention_days: int | None = 30
    short_retention_days: int | None = 1
    list_limit: int = 10
    empty_max_unique_timestamps: int = 2
    compression: str = "zstd"
    camera_encoding: str = "video"
    video_codec: str = "libx264"
    video_preset: str = "ultrafast"
    video_fps: float = 20.0
    video_gop: int = 20
    video_bitrate: int | None = None


def mcap_config_from_env() -> McapConfig:
    hourly_retention = os.getenv("MCAP_HOURLY_RETENTION_DAYS", "30")
    short_retention = os.getenv("MCAP_SHORT_RETENTION_DAYS", "1")
    bitrate_raw = os.getenv("MCAP_VIDEO_BITRATE", "").strip()
    return McapConfig(
        enabled=os.getenv("MCAP_ENABLED", "true").lower() in {"1", "true", "yes"},
        dedup_enabled=os.getenv("MCAP_DEDUP_ENABLED", "true").lower() in {"1", "true", "yes"},
        hourly_dir=os.getenv("MCAP_HOURLY_DIR", "/data/mcap/hourly"),
        short_dir=os.getenv("MCAP_SHORT_DIR", "/data/mcap/short"),
        hourly_rotation_seconds=int(os.getenv("MCAP_HOURLY_ROTATION_SECONDS", "3600")),
        short_rotation_seconds=int(os.getenv("MCAP_SHORT_ROTATION_SECONDS", "600")),
        hourly_retention_days=None
        if hourly_retention in {"", "0", "none", "unlimited"}
        else int(hourly_retention),
        short_retention_days=None
        if short_retention in {"", "0", "none", "unlimited"}
        else int(short_retention),
        list_limit=int(os.getenv("MCAP_LIST_LIMIT", "10")),
        empty_max_unique_timestamps=int(os.getenv("MCAP_EMPTY_MAX_UNIQUE_TIMESTAMPS", "2")),
        compression=os.getenv("MCAP_COMPRESSION", "zstd"),
        camera_encoding=os.getenv("MCAP_CAMERA_ENCODING", "video").strip().lower(),
        video_codec=os.getenv("MCAP_VIDEO_CODEC", "libx264"),
        video_preset=os.getenv("MCAP_VIDEO_PRESET", "ultrafast"),
        video_fps=float(os.getenv("MCAP_VIDEO_FPS", "20")),
        video_gop=int(os.getenv("MCAP_VIDEO_GOP", "20")),
        video_bitrate=int(bitrate_raw) if bitrate_raw else None,
    )


@dataclass
class _CamEncState:
    encoder: H264Encoder | None = None
    video_channel: Any | None = None
    image_channel: Any | None = None
    use_video: bool = True
    force_keyframe: bool = True
    last_pts_index: int = -1
    last_rgb: np.ndarray | None = None
    pts_time_origin_ns: int = 0
    pts_time_origin_set: bool = False


class FoxgloveTierWriter:
    """Single-tier writer with Beijing-time naming and empty suffix."""

    RETENTION_CHECK_SECONDS = 300

    def __init__(
        self,
        tier: McapTierConfig,
        app_cfg: AppConfig,
        mcap_cfg: McapConfig,
        *,
        enabled: bool = True,
    ) -> None:
        self.tier = tier
        self.app_cfg = app_cfg
        self.mcap_cfg = mcap_cfg
        self.enabled = enabled
        self._lock = threading.Lock()
        self._ctx = None
        self._writer = None
        self._current_path: Path | None = None
        self._file_started_at = 0.0
        self._file_start_stamp = ""
        self._file_end_boundary = 0.0
        self._file_start_log_ns = 0
        self._file_needs_baseline = False
        self._unique_log_times: set[int] = set()
        self._messages_written = 0
        self._bytes_written = 0
        self._last_error = ""
        self._recording = False
        self._files: list[dict[str, Any]] = []
        self._pose_registry = PoseTrackerRegistry(
            PoseConfig(translation_mode="auto", publish_tf=self.app_cfg.mcap.publish_tf)
        )
        self._default_scene_model = SceneModelConfig()
        self._plc_channels: dict[str, Any] = {}
        self._cam_enc: dict[str, _CamEncState] = {}
        self._imu_channels: dict[str, Any] = {}
        self._world_pose_channels: dict[str, Any] = {}
        self._tf_channels: dict[str, Any] = {}
        self._lidar_channels: dict[str, Any] = {}
        self._scene_channel: Any | None = None
        self._topic_sidecar = TopicSidecarBuilder(
            tier_name=tier.name,
            file_name="",
            start_stamp="",
            end_stamp="",
        )
        self._camera_encoding = (
            "video" if mcap_cfg.camera_encoding in {"video", "compressvideo", "h264"} else "image"
        )
        self._video_available = self._probe_video_stack()
        self._cleanup_orphan_writing_files()

    def _writing_dir(self) -> Path:
        path = Path(self.tier.output_dir) / ".writing"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _probe_video_stack(self) -> bool:
        if self._camera_encoding != "video":
            return False
        try:
            import av  # noqa: F401
        except Exception:  # noqa: BLE001
            LOG.warning("MCAP [%s] PyAV 不可用，相机将回退 CompressedImage", self.tier.name)
            return False
        return True

    def _track_topic(
        self,
        topic: str,
        log_ns: int,
        *,
        schema: str,
        message_encoding: str,
    ) -> None:
        self._topic_sidecar.track(
            topic,
            log_ns,
            schema=schema,
            message_encoding=message_encoding,
        )

    def _reset_camera_encoders(self) -> None:
        for state in self._cam_enc.values():
            if state.encoder is not None:
                try:
                    state.encoder.close()
                except Exception:  # noqa: BLE001
                    pass
        self._cam_enc.clear()

    def _cleanup_orphan_writing_files(self) -> None:
        writing_dir = Path(self.tier.output_dir) / ".writing"
        if not writing_dir.exists():
            return
        for path in writing_dir.glob("*.tmp"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _writer_options(self):
        from foxglove.mcap import MCAPWriteOptions

        comp = _mcap_compression(self.app_cfg.mcap.compression)
        if comp is None:
            return None
        return MCAPWriteOptions(compression=comp)

    def _open_writer(self) -> None:
        import foxglove

        self._ctx = foxglove.Context()
        now = time.time()
        start = period_start_ts(now, self.tier.rotation_seconds)
        start_stamp = beijing_stamp(start)
        path = self._writing_dir() / f"{self.tier.name}_{start_stamp}.mcap.tmp"
        Path(self.tier.output_dir).mkdir(parents=True, exist_ok=True)
        opts = self._writer_options()
        kwargs: dict[str, Any] = {"allow_overwrite": True, "context": self._ctx}
        if opts is not None:
            kwargs["writer_options"] = opts
        self._writer = foxglove.open_mcap(str(path), **kwargs)
        self._current_path = path
        self._file_started_at = start
        self._file_start_stamp = start_stamp
        self._file_end_boundary = start + self.tier.rotation_seconds
        self._file_start_log_ns = int(start * 1_000_000_000)
        self._file_needs_baseline = True
        self._unique_log_times.clear()
        self._messages_written = 0
        self._bytes_written = 0
        self._recording = True
        self._plc_channels.clear()
        self._reset_camera_encoders()
        self._imu_channels.clear()
        self._world_pose_channels.clear()
        self._tf_channels.clear()
        self._lidar_channels.clear()
        self._scene_channel = None
        self._topic_sidecar = TopicSidecarBuilder(
            tier_name=self.tier.name,
            file_name="",
            start_stamp=start_stamp,
            end_stamp="",
        )
        LOG.info(
            "新 MCAP [%s]: %s (camera_encoding=%s video_fps=%.0f gop=%d)",
            self.tier.name,
            path,
            self._camera_encoding if self._video_available else "image",
            self.mcap_cfg.video_fps,
            self.mcap_cfg.video_gop,
        )

    def _flush_camera_encoders(self) -> None:
        for name, state in self._cam_enc.items():
            if state.encoder is None or state.video_channel is None:
                continue
            ts = int(time.time() * 1_000_000_000)
            for pkt in state.encoder.flush():
                self._log_video_packet(SensorRef(name=name), state, pkt, ts)

    def _close_writer(self, *, finalize_name: bool = True, end_ts: float | None = None) -> None:
        path = self._current_path
        end_stamp = beijing_stamp(end_ts if end_ts is not None else time.time())
        self._flush_camera_encoders()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._recording = False
        final_path: Path | None = None

        if finalize_name and path is not None and path.exists() and self._file_start_stamp:
            suffix = ""
            if self._is_empty_file():
                suffix = "-empty"
            final_name = f"{self._file_start_stamp}-{end_stamp}{suffix}.mcap"
            final_path = Path(self.tier.output_dir) / final_name
            try:
                if final_path.exists():
                    final_path.unlink()
                path.rename(final_path)
                self._topic_sidecar.file_name = final_name
                self._topic_sidecar.end_stamp = end_stamp
                sidecar = write_topic_sidecar(final_path, self._topic_sidecar)
                if sidecar is not None:
                    LOG.info("归档 topic 摘要 [%s]: %s", self.tier.name, sidecar.name)
                LOG.info("归档 MCAP [%s]: %s", self.tier.name, final_name)
            except OSError as exc:
                self._last_error = f"重命名 MCAP 失败: {exc}"
        elif path is not None and path.exists():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        self._reset_camera_encoders()
        self._current_path = None
        self._file_start_stamp = ""
        self._file_end_boundary = 0.0
        self._file_start_log_ns = 0
        self._unique_log_times.clear()
        self._scene_channel = None

    def _maybe_rotate(self) -> bool:
        if self._writer is None:
            return False
        if time.time() < self._file_end_boundary:
            return False
        self._close_writer(finalize_name=True, end_ts=self._file_end_boundary)
        self._prune_old_files()
        return True

    def tick_rotation(self, carryover: MultimodalCarryover | None = None) -> None:
        """Background rotation check (wall-clock aligned windows)."""
        if not self.enabled:
            return
        with self._lock:
            self._maybe_rotate()
            if self._writer is None and carryover is not None and carryover.plc is not None:
                self._open_writer()
                self._write_snapshot(carryover)
                self._file_needs_baseline = False

    def _file_end_dt(self, path: Path) -> datetime:
        match = FILE_NAME_RE.match(path.name)
        if match:
            end_dt = parse_beijing_stamp(match.group(2))
            if end_dt is not None:
                return end_dt
        return datetime.fromtimestamp(path.stat().st_mtime, tz=BEIJING)

    def _prune_old_files(self) -> None:
        if self.tier.retention_days is None:
            return
        cutoff = datetime.now(tz=BEIJING) - timedelta(days=self.tier.retention_days)
        root = Path(self.tier.output_dir)
        if not root.exists():
            return
        for path in root.glob("*.mcap"):
            try:
                if self._file_end_dt(path) < cutoff:
                    path.unlink(missing_ok=True)
                    sidecar_path_for_mcap(path).unlink(missing_ok=True)
            except OSError:
                pass

    def _refresh_file_list(self) -> None:
        root = Path(self.tier.output_dir)
        if not root.exists():
            self._files = []
            return
        files = sorted(root.glob("*.mcap"), key=lambda p: p.stat().st_mtime, reverse=True)
        self._files = [
            {
                "name": f.name,
                "path": str(f.resolve()),
                "size": f.stat().st_size,
                "modified_at": f.stat().st_mtime,
                "is_empty": "-empty.mcap" in f.name,
            }
            for f in files[: self.tier.list_limit]
        ]

    def _is_empty_file(self) -> bool:
        if len(self._unique_log_times) > self.tier.empty_max_unique_timestamps:
            return False
        for stat in self._topic_sidecar.topics.values():
            topic = stat.topic
            if topic.startswith("/gige/") or topic.startswith("/imu/") or topic.startswith("/lidar/"):
                return False
            if topic == "/crane727r/spreader/scene":
                return False
        return True

    def _track_time(self, log_ns: int) -> None:
        self._unique_log_times.add(log_ns)

    def _plc_json_channel(self, topic: str, schema_name: str):
        from foxglove import Channel

        from plc.foxglove_messages import JSON_SCHEMAS

        if topic in self._plc_channels:
            return self._plc_channels[topic]
        ch = Channel(
            topic,
            schema=JSON_SCHEMAS[schema_name],
            context=self._ctx,
            metadata={"schema_name": schema_name},
        )
        self._plc_channels[topic] = ch
        return ch

    def _write_plc(self, snap: PlcSnapshot) -> None:
        from plc.foxglove_messages import (
            TOPIC_CONNECTION,
            TOPIC_HEARTBEAT,
            TOPIC_OICR,
            TOPIC_POSITION,
            TOPIC_RAW,
            TOPIC_SMH,
            TOPIC_VELOCITY,
        )

        packet = snap.packet
        stamp_ns = snap.log_ns
        self._track_time(stamp_ns)

        self._plc_json_channel(TOPIC_RAW, "crane727r.PlcRaw").log(
            packet.to_dict(), log_time=stamp_ns
        )
        self._plc_json_channel(TOPIC_POSITION, "crane727r.Position").log(
            {
                "mh_pos": packet.mh_pos,
                "mt_pos": packet.mt_pos,
                "mc_pos": packet.mc_pos,
                "cntrh_pos": packet.cntrh_pos,
            },
            log_time=stamp_ns,
        )
        self._plc_json_channel(TOPIC_VELOCITY, "crane727r.Velocity").log(
            {
                "mh_vel": round(snap.velocities[0], 2),
                "mt_vel": round(snap.velocities[1], 2),
                "mc_vel": round(snap.velocities[2], 2),
                "cntrh_vel": round(snap.velocities[3], 2),
                "unit": "mm/s",
            },
            log_time=stamp_ns,
        )
        self._plc_json_channel(TOPIC_SMH, "crane727r.Signals").log(
            packet.smh.__dict__, log_time=stamp_ns
        )
        self._plc_json_channel(TOPIC_OICR, "crane727r.Signals").log(
            packet.oicr.__dict__, log_time=stamp_ns
        )
        self._plc_json_channel(TOPIC_HEARTBEAT, "crane727r.Heartbeat").log(
            {"heart_beat": packet.heart_beat, "source_ip": packet.source_ip},
            log_time=stamp_ns,
        )
        self._plc_json_channel(TOPIC_CONNECTION, "crane727r.Connection").log(
            snap.health, log_time=stamp_ns
        )
        plc_topics = [
            (TOPIC_RAW, "crane727r.PlcRaw"),
            (TOPIC_POSITION, "crane727r.Position"),
            (TOPIC_VELOCITY, "crane727r.Velocity"),
            (TOPIC_SMH, "crane727r.Signals"),
            (TOPIC_OICR, "crane727r.Signals"),
            (TOPIC_HEARTBEAT, "crane727r.Heartbeat"),
            (TOPIC_CONNECTION, "crane727r.Connection"),
        ]
        for topic, schema_name in plc_topics:
            self._track_topic(topic, stamp_ns, schema=schema_name, message_encoding="json")

        from plc.foxglove_messages import TOPIC_SCENE, build_scene_update

        scene = build_scene_update(packet, stamp_ns)
        self._scene_protobuf_channel().log(scene.SerializeToString(), log_time=stamp_ns)
        self._track_topic(
            TOPIC_SCENE,
            stamp_ns,
            schema="foxglove.SceneUpdate",
            message_encoding="protobuf",
        )
        self._messages_written += 8

    def _scene_protobuf_channel(self):
        if self._scene_channel is not None:
            return self._scene_channel
        from foxglove import Channel, Schema
        from foxglove_schemas_protobuf.SceneUpdate_pb2 import SceneUpdate
        from mcap_protobuf.schema import build_file_descriptor_set

        from plc.foxglove_messages import TOPIC_SCENE

        fds = build_file_descriptor_set(message_class=SceneUpdate)
        schema = Schema(
            name=SceneUpdate.DESCRIPTOR.full_name,
            encoding="protobuf",
            data=fds.SerializeToString(),
        )
        self._scene_channel = Channel(
            TOPIC_SCENE,
            schema=schema,
            message_encoding="protobuf",
            context=self._ctx,
        )
        return self._scene_channel

    def _cam_enc_state(self, sensor: SensorRef) -> _CamEncState:
        state = self._cam_enc.get(sensor.name)
        if state is None:
            state = _CamEncState(use_video=self._video_available and self._camera_encoding == "video")
            self._cam_enc[sensor.name] = state
        return state

    def _image_channel(self, sensor: SensorRef):
        state = self._cam_enc_state(sensor)
        if state.image_channel is None:
            from foxglove.channels import CompressedImageChannel

            topic = image_topic(
                sensor.name,
                self.app_cfg.mcap.camera_topic_prefix,
                sensor.params,
            )
            state.image_channel = CompressedImageChannel(topic, context=self._ctx)
        return state.image_channel

    def _video_channel(self, sensor: SensorRef):
        state = self._cam_enc_state(sensor)
        if state.video_channel is None:
            from foxglove.channels import CompressedVideoChannel

            topic = image_topic(
                sensor.name,
                self.app_cfg.mcap.camera_topic_prefix,
                sensor.params,
            )
            state.video_channel = CompressedVideoChannel(topic, context=self._ctx)
        return state.video_channel

    def _video_interval_ns(self) -> int:
        fps = max(1.0, float(self.mcap_cfg.video_fps))
        return max(1, int(1_000_000_000 / fps))

    def _video_grid_pts(self, log_ns: int) -> int:
        fps = max(1.0, float(self.mcap_cfg.video_fps))
        interval_ms = max(1, int(round(1000.0 / fps)))
        return int((log_ns // 1_000_000) // interval_ms)

    def _ensure_pts_time_origin(self, state: _CamEncState, pts: int, log_ns: int) -> None:
        if state.pts_time_origin_set:
            return
        state.pts_time_origin_ns = log_ns - pts * self._video_interval_ns()
        state.pts_time_origin_set = True

    def _video_log_ns_for_pts(self, state: _CamEncState, pts: int, fallback_log_ns: int) -> int:
        if not state.pts_time_origin_set:
            return fallback_log_ns
        return state.pts_time_origin_ns + pts * self._video_interval_ns()

    def _video_target_pts(self, log_ns: int, state: _CamEncState) -> int:
        """Map log_time to fixed-FPS grid PTS, keeping stream monotonic."""
        grid_pts = self._video_grid_pts(log_ns)
        if state.last_pts_index < 0:
            return grid_pts
        return max(state.last_pts_index + 1, grid_pts)

    def _init_video_encoder(self, sensor: SensorRef, state: _CamEncState, rgb: np.ndarray) -> None:
        h, w = rgb.shape[:2]
        gop = max(1, int(self.mcap_cfg.video_gop))
        fps = max(1.0, float(self.mcap_cfg.video_fps))
        state.encoder = H264Encoder(
            w,
            h,
            fps=fps,
            gop=gop,
            preset=self.mcap_cfg.video_preset,
            codec=self.mcap_cfg.video_codec,
            bitrate=self.mcap_cfg.video_bitrate,
        )
        self._video_channel(sensor)

    def _encode_video_rgb(
        self,
        sensor: SensorRef,
        state: _CamEncState,
        rgb: np.ndarray,
        *,
        start_pts: int,
        end_pts: int,
        anchor_log_ns: int,
        force_keyframe: bool,
    ) -> None:
        if state.encoder is None:
            self._init_video_encoder(sensor, state, rgb)
        self._ensure_pts_time_origin(state, end_pts, anchor_log_ns)
        for pts in range(start_pts, end_pts + 1):
            pkt_log_ns = self._video_log_ns_for_pts(state, pts, anchor_log_ns)
            force_i = force_keyframe and pts == start_pts
            pkts = state.encoder.encode_at_index(rgb, pts, force_keyframe=force_i)
            if not pkts:
                continue
            for pkt in pkts:
                self._log_video_packet(sensor, state, pkt, pkt_log_ns)
            state.last_pts_index = pts

    def _log_video_packet(self, sensor: SensorRef, state: _CamEncState, pkt, log_ns: int) -> None:
        from foxglove.messages import CompressedVideo

        fmt = state.encoder.video_format if state.encoder is not None else "h264"
        topic = image_topic(
            sensor.name,
            self.app_cfg.mcap.camera_topic_prefix,
            sensor.params,
        )
        msg = CompressedVideo(
            timestamp=_to_timestamp(log_ns),
            frame_id=sensor.frame_id or sensor.name,
            format=fmt,
            data=pkt.data,
        )
        self._video_channel(sensor).log(msg, log_time=log_ns)
        self._track_topic(
            topic,
            log_ns,
            schema="foxglove.CompressedVideo",
            message_encoding="protobuf",
        )
        self._messages_written += 1

    def _write_camera_image(self, sample: CameraSample) -> None:
        from foxglove.messages import CompressedImage

        topic = image_topic(
            sample.sensor.name,
            self.app_cfg.mcap.camera_topic_prefix,
            sample.sensor.params,
        )
        msg = CompressedImage(
            timestamp=_to_timestamp(sample.log_ns),
            frame_id=sample.sensor.frame_id or sample.name,
            format=sample.encoding,
            data=sample.data,
        )
        self._image_channel(sample.sensor).log(msg, log_time=sample.log_ns)
        self._track_topic(
            topic,
            sample.log_ns,
            schema="foxglove.CompressedImage",
            message_encoding="protobuf",
        )
        self._messages_written += 1

    def _write_camera_video(self, sample: CameraSample) -> None:
        state = self._cam_enc_state(sample.sensor)
        rgb = decode_camera_to_rgb(
            sample.data,
            sample.encoding,
            width=sample.width,
            height=sample.height,
        )
        if rgb is None:
            if state.last_rgb is None:
                raise RuntimeError(
                    f"Camera decode failed for {sample.name} ({sample.encoding}) and no prior frame"
                )
            rgb = state.last_rgb
        else:
            state.last_rgb = rgb if rgb.flags["C_CONTIGUOUS"] else np.ascontiguousarray(rgb)

        target_pts = self._video_target_pts(sample.log_ns, state)
        start_pts = 0 if state.last_pts_index < 0 else state.last_pts_index + 1
        max_fill = max(1, int(os.getenv("MCAP_VIDEO_MAX_PTS_FILL", "120")))
        if target_pts >= start_pts and target_pts - start_pts + 1 > max_fill:
            start_pts = target_pts - max_fill + 1

        force_key = state.force_keyframe
        state.force_keyframe = False
        self._encode_video_rgb(
            sample.sensor,
            state,
            rgb,
            start_pts=start_pts,
            end_pts=target_pts,
            anchor_log_ns=sample.log_ns,
            force_keyframe=force_key,
        )
        if state.encoder is None:
            raise RuntimeError(f"H.264 encoder produced no packets for {sample.name}")

    def _write_camera(self, sample: CameraSample) -> None:
        self._track_time(sample.log_ns)
        state = self._cam_enc_state(sample.sensor)
        if state.use_video and self._video_available and state.image_channel is None:
            try:
                self._write_camera_video(sample)
                return
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "相机 %s H.264 编码失败，回退 CompressedImage: %s",
                    sample.name,
                    exc,
                )
                state.use_video = False
                if state.encoder is not None:
                    try:
                        state.encoder.close()
                    except Exception:  # noqa: BLE001
                        pass
                    state.encoder = None
                state.video_channel = None
        self._write_camera_image(sample)

    def _imu_channel(self, sensor: SensorRef):
        ch = self._imu_channels.get(sensor.name)
        if ch is None:
            from foxglove import Channel

            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sensor.name}/imu"
            ch = Channel(
                topic,
                schema=IMU_JSON_SCHEMA,
                context=self._ctx,
                metadata={"schema_name": "sensor_msgs/Imu"},
            )
            self._imu_channels[sensor.name] = ch
        return ch

    def _world_pose_channel(self, sensor: SensorRef):
        ch = self._world_pose_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import PoseInFrameChannel

            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sensor.name}/world_pose"
            ch = PoseInFrameChannel(topic, context=self._ctx)
            self._world_pose_channels[sensor.name] = ch
        return ch

    def _tf_channel(self, sensor: SensorRef):
        ch = self._tf_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import FrameTransformChannel

            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sensor.name}/tf"
            ch = FrameTransformChannel(topic, context=self._ctx)
            self._tf_channels[sensor.name] = ch
        return ch

    def _lidar_channel(self, sensor: SensorRef):
        ch = self._lidar_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import PointCloudChannel

            topic = f"{self.app_cfg.mcap.lidar_topic_prefix.rstrip('/')}/{sensor.name}/points"
            ch = PointCloudChannel(topic, context=self._ctx)
            self._lidar_channels[sensor.name] = ch
        return ch

    def _write_imu(self, sample: ImuSample) -> None:
        imu_cfg = ImuConfig(
            name=sample.sensor.name,
            frame_id=sample.sensor.frame_id,
            params=sample.sensor.params,
        )
        imu_sample = sample_from_view(sample.view)
        log_ns = sample.log_ns
        pose = self._pose_registry.update(imu_cfg, imu_sample, log_ns)
        self._track_time(log_ns)
        if self.app_cfg.mcap.publish_imu:
            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sample.sensor.name}/imu"
            self._imu_channel(sample.sensor).log(
                build_imu_message(imu_cfg, imu_sample, log_ns), log_time=log_ns
            )
            self._track_topic(topic, log_ns, schema="sensor_msgs/Imu", message_encoding="json")
            self._messages_written += 1
        if self.app_cfg.mcap.publish_world_pose:
            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sample.sensor.name}/world_pose"
            self._world_pose_channel(sample.sensor).log(
                build_world_pose(pose, log_ns), log_time=log_ns
            )
            self._track_topic(
                topic, log_ns, schema="foxglove.PoseInFrame", message_encoding="protobuf"
            )
            self._messages_written += 1
        if self.app_cfg.mcap.publish_tf:
            topic = f"{self.app_cfg.mcap.imu_topic_prefix.rstrip('/')}/{sample.sensor.name}/tf"
            self._tf_channel(sample.sensor).log(
                build_frame_transform(pose, log_ns), log_time=log_ns
            )
            self._track_topic(
                topic, log_ns, schema="foxglove.FrameTransform", message_encoding="protobuf"
            )
            self._messages_written += 1

    def _write_lidar(self, sample: LidarSample) -> None:
        from foxglove.messages import (
            PackedElementField,
            PackedElementFieldNumericType,
            PointCloud,
            Pose,
            Quaternion,
            Vector3,
        )

        pts = unpack_points(sample.pc.data)
        log_ns = sample.log_ns
        f32 = PackedElementFieldNumericType.Float32
        fields = [
            PackedElementField(name="x", offset=0, type=f32),
            PackedElementField(name="y", offset=4, type=f32),
            PackedElementField(name="z", offset=8, type=f32),
            PackedElementField(name="intensity", offset=12, type=f32),
        ]
        msg = PointCloud(
            timestamp=_to_timestamp(log_ns),
            frame_id=sample.sensor.frame_id or sample.name,
            pose=Pose(
                position=Vector3(x=0, y=0, z=0),
                orientation=Quaternion(x=0, y=0, z=0, w=1),
            ),
            point_stride=16,
            fields=fields,
            data=pts.astype("float32").tobytes(),
        )
        self._track_time(log_ns)
        topic = f"{self.app_cfg.mcap.lidar_topic_prefix.rstrip('/')}/{sample.sensor.name}/points"
        self._lidar_channel(sample.sensor).log(msg, log_time=log_ns)
        self._track_topic(topic, log_ns, schema="foxglove.PointCloud", message_encoding="protobuf")
        self._messages_written += 1

    def _write_snapshot(
        self,
        carryover: MultimodalCarryover,
        *,
        cameras: dict[str, CameraSample] | None = None,
        imus: dict[str, ImuSample] | None = None,
        lidars: dict[str, LidarSample] | None = None,
    ) -> None:
        if carryover.plc is not None:
            self._write_plc(carryover.plc)
        cams = cameras if cameras is not None else carryover.cameras
        imu_map = imus if imus is not None else carryover.imus
        lidar_map = lidars if lidars is not None else carryover.lidars
        for sample in cams.values():
            self._write_camera(sample)
        for sample in imu_map.values():
            self._write_imu(sample)
        for sample in lidar_map.values():
            self._write_lidar(sample)

    def advance(self, carryover: MultimodalCarryover | None) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                self._maybe_rotate()
                if self._writer is None:
                    self._open_writer()
                if self._file_needs_baseline and carryover is not None:
                    self._write_snapshot(carryover)
                    self._file_needs_baseline = False
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                LOG.error("MCAP advance [%s] 失败: %s", self.tier.name, exc, exc_info=True)
                self._close_writer(finalize_name=False)

    def record(
        self,
        carryover: MultimodalCarryover,
        *,
        cameras: dict[str, CameraSample] | None = None,
        imus: dict[str, ImuSample] | None = None,
        lidars: dict[str, LidarSample] | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                if self._writer is None:
                    self._open_writer()
                self._write_snapshot(
                    carryover,
                    cameras=cameras,
                    imus=imus,
                    lidars=lidars,
                )
                self._file_needs_baseline = False
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                LOG.error("MCAP record [%s] 失败: %s", self.tier.name, exc, exc_info=True)
                self._close_writer(finalize_name=False)

    def stop(self) -> None:
        with self._lock:
            self._close_writer(finalize_name=True)
            self._refresh_file_list()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_file_list()
            return {
                "name": self.tier.name,
                "label": self.tier.label,
                "enabled": self.enabled,
                "recording": self._recording,
                "current_file": str(self._current_path or ""),
                "bytes_written": self._bytes_written,
                "messages_written": self._messages_written,
                "files": self._files,
                "last_error": self._last_error,
                "config": {
                    "output_dir": self.tier.output_dir,
                    "rotation_seconds": self.tier.rotation_seconds,
                    "retention_days": self.tier.retention_days,
                },
            }


class FoxgloveMultimodalRecorder:
    """Dual-tier facade triggered by PLC changes."""

    RETENTION_CHECK_SECONDS = 300

    def __init__(self, app_cfg: AppConfig, mcap_cfg: McapConfig | None = None) -> None:
        self.app_cfg = app_cfg
        self.config = mcap_cfg or mcap_config_from_env()
        self._last_signature: tuple[Any, ...] | None = None
        self._carryover = MultimodalCarryover()
        self._packets_written = 0
        self._packets_skipped = 0
        self._stop_event = threading.Event()
        self._hourly = FoxgloveTierWriter(
            McapTierConfig(
                name="hourly",
                label="1小时归档",
                output_dir=self.config.hourly_dir,
                rotation_seconds=self.config.hourly_rotation_seconds,
                retention_days=self.config.hourly_retention_days,
                list_limit=self.config.list_limit,
                empty_max_unique_timestamps=self.config.empty_max_unique_timestamps,
            ),
            app_cfg,
            self.config,
            enabled=self.config.enabled,
        )
        self._short = FoxgloveTierWriter(
            McapTierConfig(
                name="short",
                label="10分钟归档",
                output_dir=self.config.short_dir,
                rotation_seconds=self.config.short_rotation_seconds,
                retention_days=self.config.short_retention_days,
                list_limit=self.config.list_limit,
                empty_max_unique_timestamps=self.config.empty_max_unique_timestamps,
            ),
            app_cfg,
            self.config,
            enabled=self.config.enabled,
        )
        self._rotation_thread = threading.Thread(
            target=self._rotation_loop, name="mcap-rotation", daemon=True
        )
        self._rotation_thread.start()
        self._retention_thread = threading.Thread(
            target=self._retention_loop, name="mcap-retention", daemon=True
        )
        self._retention_thread.start()

    @property
    def tiers(self) -> list[FoxgloveTierWriter]:
        return [self._hourly, self._short]

    def _rotation_loop(self) -> None:
        while not self._stop_event.wait(5):
            carryover = self._carryover if self._carryover.plc is not None else None
            for tier in self.tiers:
                tier.tick_rotation(carryover)

    def _retention_loop(self) -> None:
        while not self._stop_event.wait(self.RETENTION_CHECK_SECONDS):
            for tier in self.tiers:
                with tier._lock:
                    tier._prune_old_files()
                    tier._refresh_file_list()

    def update_carryover(self, carryover: MultimodalCarryover) -> None:
        if carryover.plc is not None:
            self._carryover.plc = carryover.plc
        self._carryover.cameras.update(carryover.cameras)
        self._carryover.imus.update(carryover.imus)
        self._carryover.lidars.update(carryover.lidars)

    def append_sensor_snapshot(
        self,
        carryover: MultimodalCarryover,
        *,
        cameras: dict[str, CameraSample] | None = None,
        imus: dict[str, ImuSample] | None = None,
        lidars: dict[str, LidarSample] | None = None,
    ) -> None:
        """Write latest sensor cache without requiring a PLC signature change."""
        if not self.config.enabled:
            return

        merged = MultimodalCarryover(
            plc=carryover.plc or self._carryover.plc,
            cameras=dict(cameras or carryover.cameras or self._carryover.cameras),
            imus=dict(imus or carryover.imus or self._carryover.imus),
            lidars=dict(lidars or carryover.lidars or self._carryover.lidars),
        )
        if carryover.plc is not None:
            merged.plc = carryover.plc
        self.update_carryover(merged)

        for tier in self.tiers:
            tier.advance(self._carryover)
            tier.record(
                self._carryover,
                cameras=merged.cameras,
                imus=merged.imus,
                lidars=merged.lidars,
            )

    def write_snapshot(
        self,
        carryover: MultimodalCarryover,
        signature: tuple[Any, ...],
        *,
        cameras: dict[str, CameraSample] | None = None,
        imus: dict[str, ImuSample] | None = None,
        lidars: dict[str, LidarSample] | None = None,
    ) -> bool:
        """Return True if a new snapshot was written (PLC value changed)."""
        if not self.config.enabled:
            return False

        merged = MultimodalCarryover(
            plc=carryover.plc or self._carryover.plc,
            cameras=dict(cameras or carryover.cameras or self._carryover.cameras),
            imus=dict(imus or carryover.imus or self._carryover.imus),
            lidars=dict(lidars or carryover.lidars or self._carryover.lidars),
        )
        if carryover.plc is not None:
            merged.plc = carryover.plc
        self.update_carryover(merged)

        for tier in self.tiers:
            tier.advance(self._carryover)

        if self.config.dedup_enabled and self._last_signature == signature:
            self._packets_skipped += 1
            return False

        self._last_signature = signature
        self._packets_written += 1

        for tier in self.tiers:
            tier.record(
                self._carryover,
                cameras=merged.cameras,
                imus=merged.imus,
                lidars=merged.lidars,
            )
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._hourly.stop()
        self._short.stop()

    def get_status(self) -> dict[str, Any]:
        hourly = self._hourly.get_status()
        short = self._short.get_status()
        return {
            "enabled": self.config.enabled,
            "dedup_enabled": self.config.dedup_enabled,
            "packets_written": self._packets_written,
            "packets_skipped": self._packets_skipped,
            "config": {
                "hourly_dir": self.config.hourly_dir,
                "short_dir": self.config.short_dir,
                "hourly_rotation_seconds": self.config.hourly_rotation_seconds,
                "short_rotation_seconds": self.config.short_rotation_seconds,
                "camera_encoding": self.config.camera_encoding,
                "video_codec": self.config.video_codec,
                "video_fps": self.config.video_fps,
                "video_gop": self.config.video_gop,
            },
            "tiers": {"hourly": hourly, "short": short},
            "recording": hourly["recording"] or short["recording"],
            "files": {"hourly": hourly["files"], "short": short["files"]},
            "messages_written": hourly["messages_written"] + short["messages_written"],
            "last_error": hourly["last_error"] or short["last_error"],
        }
