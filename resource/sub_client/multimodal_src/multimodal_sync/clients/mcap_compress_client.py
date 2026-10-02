"""MCAP recorder with H.264 CompressedVideo for cameras + IMU/lidar as JPEG/raw."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from gige_sync.clients.topics import image_topic
from imu_sync.clients.messages import (
    IMU_JSON_SCHEMA,
    build_frame_transform,
    build_imu_message,
    build_world_pose,
    sample_from_view,
)
from imu_sync.clients.pose_tracker import PoseTrackerRegistry
from imu_sync.config import ImuConfig, PoseConfig
from lidar_sync.pointcloud_codec import unpack_points

from ..callback import notify_mcap_saved
from ..config import AppConfig, SensorRef
from ..timestamps import log_ns_from_fields
from .base import MultimodalClient
from .mcap_client import _mcap_compression, _to_timestamp
from .video_encoder import H264Encoder, decode_jpeg_to_rgb


class _CamEnc:
    def __init__(self) -> None:
        self.encoder: Optional[H264Encoder] = None
        self.channel = None


class McapCompressMultimodalClient(MultimodalClient):
    client_name = "mcap-compress"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._mc = cfg.mcap_compress
        out = Path(self._mc.output_dir).expanduser()
        if not out.is_absolute():
            out = (Path.cwd() / out).resolve()
        self._out_dir = out
        self._ctx = None
        self._writer = None
        self._current_path: Optional[Path] = None
        self._window = -1
        self._lock = threading.Lock()
        self._cams: Dict[str, _CamEnc] = {}
        self._imu_channels: Dict[str, object] = {}
        self._world_pose_channels: Dict[str, object] = {}
        self._tf_channels: Dict[str, object] = {}
        self._lidar_channels: Dict[str, object] = {}
        pose_cfg = PoseConfig(translation_mode="auto", publish_tf=self._mc.publish_tf)
        self._pose_registry = PoseTrackerRegistry(pose_cfg)
        self._fps = 25.0

    def setup(self) -> None:
        import foxglove

        try:
            import av  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("mcap-compress 需要 PyAV：pip install av") from exc

        self._foxglove = foxglove
        self._ctx = foxglove.Context()
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self.log.info(
            "MCAP 视频输出: %s（codec=%s，每 %.0fs 切分）",
            self._out_dir,
            self._mc.codec,
            self._mc.rotate_sec,
        )
        # Open the first MCAP immediately so stop/save always has a file path.
        with self._lock:
            self._ensure_window()

    def teardown(self) -> None:
        with self._lock:
            for name, ce in self._cams.items():
                if ce.encoder is not None:
                    ts = int(time.time() * 1e9)
                    for pkt in ce.encoder.flush():
                        self._log_packet(name, ce, pkt, ts)
                    ce.encoder.close()
                    ce.encoder = None
            self._close_writer(notify=True)

    def _writer_options(self):
        from foxglove.mcap import MCAPWriteOptions

        comp = _mcap_compression(self._mc.compression)
        if comp is None:
            return None
        return MCAPWriteOptions(compression=comp)

    def _close_writer(self, *, notify: bool) -> None:
        path = self._current_path
        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                writer.flush()
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        if notify and path is not None and path.exists():
            notify_mcap_saved(
                path,
                callback_url=self._mc.callback_url,
                extra={"client": "multimodal-sync", "mode": "mcap-compress"},
            )

    def _rotate(self, window: int) -> None:
        for name, ce in self._cams.items():
            if ce.encoder is not None:
                ts = int(time.time() * 1e9)
                for pkt in ce.encoder.flush():
                    self._log_packet(name, ce, pkt, ts)
                ce.encoder.close()
                ce.encoder = None
        self._close_writer(notify=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = self._out_dir / f"{self._mc.filename_prefix}_{ts}.mcap"
        opts = self._writer_options()
        kwargs = {"allow_overwrite": self._mc.allow_overwrite, "context": self._ctx}
        if opts is not None:
            kwargs["writer_options"] = opts
        self._writer = self._foxglove.open_mcap(str(path), **kwargs)
        self._current_path = path
        self._window = window
        self.log.info("新 MCAP 视频: %s", path)

    def _ensure_window(self) -> None:
        if self._mc.rotate_sec <= 0:
            if self._writer is None:
                self._rotate(0)
            return
        window = int(time.time() // self._mc.rotate_sec)
        if window != self._window:
            self._rotate(window)

    def _cam(self, cam_name: str) -> _CamEnc:
        ce = self._cams.get(cam_name)
        if ce is None:
            ce = _CamEnc()
            self._cams[cam_name] = ce
        return ce

    def _video_channel(self, sensor: SensorRef):
        ce = self._cam(sensor.name)
        if ce.channel is None:
            from foxglove.channels import CompressedVideoChannel

            topic = image_topic(sensor.name, self._mc.camera_topic_prefix, sensor.params)
            ce.channel = CompressedVideoChannel(topic, context=self._ctx)
            self.log.info("MCAP 视频通道: %s", topic)
        return ce.channel

    def _log_packet(self, cam_name: str, ce: _CamEnc, pkt, log_ns: int) -> None:
        from foxglove.messages import CompressedVideo

        ch = ce.channel
        if ch is None or self._writer is None:
            return
        fmt = ce.encoder.video_format if ce.encoder is not None else "h264"
        msg = CompressedVideo(
            timestamp=_to_timestamp(log_ns),
            frame_id=cam_name,
            format=fmt,
            data=pkt.data,
        )
        ch.log(msg, log_time=log_ns)

    def _imu_cfg(self, sensor: SensorRef) -> ImuConfig:
        return ImuConfig(name=sensor.name, frame_id=sensor.frame_id, params=sensor.params)

    def _imu_channel(self, sensor: SensorRef):
        ch = self._imu_channels.get(sensor.name)
        if ch is None:
            from foxglove import Channel

            topic = f"{self._mc.imu_topic_prefix.rstrip('/')}/{sensor.name}/imu"
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

            topic = f"{self._mc.imu_topic_prefix.rstrip('/')}/{sensor.name}/world_pose"
            ch = PoseInFrameChannel(topic, context=self._ctx)
            self._world_pose_channels[sensor.name] = ch
        return ch

    def _tf_channel(self, sensor: SensorRef):
        ch = self._tf_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import FrameTransformChannel

            topic = f"{self._mc.imu_topic_prefix.rstrip('/')}/{sensor.name}/tf"
            ch = FrameTransformChannel(topic, context=self._ctx)
            self._tf_channels[sensor.name] = ch
        return ch

    def _lidar_channel(self, sensor: SensorRef):
        ch = self._lidar_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import PointCloudChannel

            topic = f"{self._mc.lidar_topic_prefix.rstrip('/')}/{sensor.name}/points"
            ch = PointCloudChannel(topic, context=self._ctx)
            self._lidar_channels[sensor.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def on_camera(self, sensor: SensorRef, fv) -> None:
        m = fv.meta
        log_ns = log_ns_from_fields(m.trigger_ms, m.recv_ms, m.host_ms)
        ts_ms = log_ns // 1_000_000
        with self._lock:
            self._ensure_window()
            rgb = decode_jpeg_to_rgb(fv.data)
            if rgb is None:
                self.log.warning(
                    "相机 %s JPEG 解码失败（%d 字节），跳过 H.264 编码",
                    sensor.name,
                    len(fv.data),
                )
                return
            ce = self._cam(sensor.name)
            if ce.encoder is None:
                h, w = rgb.shape[:2]
                gop = self._mc.gop or int(round(self._fps))
                try:
                    ce.encoder = H264Encoder(
                        w,
                        h,
                        fps=self._fps,
                        gop=gop,
                        preset=self._mc.preset,
                        codec=self._mc.codec,
                        bitrate=self._mc.bitrate,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.error("相机 %s H.264 编码器初始化失败: %s", sensor.name, exc)
                    raise
                self._video_channel(sensor)
            pts = int(ts_ms // 40)  # ~25Hz grid
            for pkt in ce.encoder.encode_at_index(rgb, pts):
                self._log_packet(sensor.name, ce, pkt, log_ns)

    def on_imu(self, sensor: SensorRef, view) -> None:
        imu_cfg = self._imu_cfg(sensor)
        sample = sample_from_view(view)
        m = view.meta
        log_ns = log_ns_from_fields(m.trigger_ms, m.recv_ms, m.host_ms)
        pose = self._pose_registry.update(imu_cfg, sample, log_ns)
        with self._lock:
            self._ensure_window()
            if self._mc.publish_imu:
                self._imu_channel(sensor).log(
                    build_imu_message(imu_cfg, sample, log_ns), log_time=log_ns
                )
            if self._mc.publish_world_pose:
                self._world_pose_channel(sensor).log(
                    build_world_pose(pose, log_ns), log_time=log_ns
                )
            if self._mc.publish_tf:
                self._tf_channel(sensor).log(
                    build_frame_transform(pose, log_ns), log_time=log_ns
                )

    def on_lidar(self, sensor: SensorRef, pc) -> None:
        from foxglove.messages import (
            PackedElementField,
            PackedElementFieldNumericType,
            PointCloud,
            Pose,
            Quaternion,
            Vector3,
        )

        pts = unpack_points(pc.data)
        m = pc.meta
        log_ns = log_ns_from_fields(m.trigger_ms, m.recv_ms, getattr(m, "host_ms", 0))
        f32 = PackedElementFieldNumericType.Float32
        fields = [
            PackedElementField(name="x", offset=0, type=f32),
            PackedElementField(name="y", offset=4, type=f32),
            PackedElementField(name="z", offset=8, type=f32),
            PackedElementField(name="intensity", offset=12, type=f32),
        ]
        msg = PointCloud(
            timestamp=_to_timestamp(log_ns),
            frame_id=sensor.frame_id or sensor.name,
            pose=Pose(
                position=Vector3(x=0, y=0, z=0),
                orientation=Quaternion(x=0, y=0, z=0, w=1),
            ),
            point_stride=16,
            fields=fields,
            data=pts.astype("float32").tobytes(),
        )
        with self._lock:
            self._ensure_window()
            self._lidar_channel(sensor).log(msg, log_time=log_ns)
