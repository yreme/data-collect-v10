"""Unified MCAP recorder for camera + IMU + lidar."""

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
    build_scene_model_update,
    build_scene_update,
    build_world_pose,
    log_ns_from_view,
    sample_from_view,
)
from imu_sync.clients.pose_tracker import PoseTrackerRegistry, scene_model_config_for_imu
from imu_sync.config import ImuConfig, PoseConfig, SceneModelConfig
from lidar_sync.pointcloud_codec import unpack_points

from ..callback import notify_mcap_saved
from ..config import AppConfig, SensorRef
from ..logging_setup import get_logger
from .base import MultimodalClient

LOG = get_logger("client.mcap")


def _mcap_compression(kind: str):
    from foxglove.mcap import MCAPCompression

    k = (kind or "zstd").strip().lower()
    if k in ("none", "off", ""):
        return None
    if k == "lz4":
        return MCAPCompression.Lz4
    return MCAPCompression.Zstd


def _to_timestamp(ns: int):
    from foxglove.messages import Timestamp

    return Timestamp(sec=int(ns // 1_000_000_000), nsec=int(ns % 1_000_000_000))


class McapMultimodalClient(MultimodalClient):
    client_name = "mcap"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._mc = cfg.mcap
        out = Path(self._mc.output_dir).expanduser()
        if not out.is_absolute():
            out = (Path.cwd() / out).resolve()
        self._out_dir = out
        self._ctx = None
        self._writer = None
        self._current_path: Optional[Path] = None
        self._window = -1
        self._lock = threading.Lock()
        self._cam_channels: Dict[str, object] = {}
        self._imu_channels: Dict[str, object] = {}
        self._world_pose_channels: Dict[str, object] = {}
        self._scene_channels: Dict[str, object] = {}
        self._tf_channels: Dict[str, object] = {}
        self._lidar_channels: Dict[str, object] = {}
        self._default_scene_model = SceneModelConfig()
        pose_cfg = PoseConfig(
            translation_mode="auto",
            publish_tf=self._mc.publish_tf,
        )
        self._pose_registry = PoseTrackerRegistry(pose_cfg)

    def setup(self) -> None:
        import foxglove

        self._foxglove = foxglove
        self._ctx = foxglove.Context()
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self.log.info(
            "MCAP 输出: %s（每 %.0fs 切分，压缩=%s，回调=%s）",
            self._out_dir,
            self._mc.rotate_sec,
            self._mc.compression,
            self._mc.callback_url or "(无)",
        )

    def teardown(self) -> None:
        with self._lock:
            self._close_writer(notify=True)

    def _writer_options(self):
        from foxglove.mcap import MCAPWriteOptions

        comp = _mcap_compression(self._mc.compression)
        if comp is None:
            return None
        return MCAPWriteOptions(compression=comp)

    def _close_writer(self, *, notify: bool) -> None:
        path = self._current_path
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        if notify and path is not None and path.exists():
            notify_mcap_saved(
                path,
                callback_url=self._mc.callback_url,
                extra={"client": "multimodal-sync"},
            )

    def _rotate(self, window: int) -> None:
        self._close_writer(notify=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._out_dir / f"{self._mc.filename_prefix}_{ts}.mcap"
        opts = self._writer_options()
        kwargs = {"allow_overwrite": self._mc.allow_overwrite, "context": self._ctx}
        if opts is not None:
            kwargs["writer_options"] = opts
        self._writer = self._foxglove.open_mcap(str(path), **kwargs)
        self._current_path = path
        self._window = window
        self.log.info("新 MCAP: %s", path)

    def _ensure_window(self) -> None:
        window = int(time.time() // self._mc.rotate_sec)
        if window != self._window:
            self._rotate(window)

    def _imu_cfg(self, sensor: SensorRef) -> ImuConfig:
        return ImuConfig(name=sensor.name, frame_id=sensor.frame_id, params=sensor.params)

    def _cam_channel(self, sensor: SensorRef):
        ch = self._cam_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import CompressedImageChannel

            topic = image_topic(sensor.name, self._mc.camera_topic_prefix, sensor.params)
            ch = CompressedImageChannel(topic, context=self._ctx)
            self._cam_channels[sensor.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

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

    def _scene_channel(self, sensor: SensorRef):
        ch = self._scene_channels.get(sensor.name)
        if ch is None:
            from foxglove.channels import SceneUpdateChannel

            topic = f"{self._mc.imu_topic_prefix.rstrip('/')}/{sensor.name}/scene"
            ch = SceneUpdateChannel(topic, context=self._ctx)
            self._scene_channels[sensor.name] = ch
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
        from foxglove.messages import CompressedImage

        m = fv.meta
        ts_ms = m.trigger_ms or m.recv_ms or m.host_ms
        log_ns = int(ts_ms) * 1_000_000 if ts_ms else int(time.time() * 1e9)
        with self._lock:
            self._ensure_window()
            msg = CompressedImage(
                timestamp=_to_timestamp(log_ns),
                frame_id=sensor.frame_id or sensor.name,
                format=m.encoding,
                data=fv.data,
            )
            self._cam_channel(sensor).log(msg, log_time=log_ns)

    def on_imu(self, sensor: SensorRef, view) -> None:
        imu_cfg = self._imu_cfg(sensor)
        sample = sample_from_view(view)
        log_ns = log_ns_from_view(view)
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
            if self._mc.publish_scene:
                self._scene_channel(sensor).log(
                    build_scene_update(imu_cfg, pose, log_ns), log_time=log_ns
                )
            if self._mc.publish_scene_model:
                model_cfg = scene_model_config_for_imu(self._default_scene_model, imu_cfg)
                from foxglove.channels import SceneUpdateChannel

                topic = f"{self._mc.imu_topic_prefix.rstrip('/')}/{sensor.name}/scene_model"
                ch = SceneUpdateChannel(topic, context=self._ctx)
                ch.log(
                    build_scene_model_update(imu_cfg, pose, log_ns, model_cfg),
                    log_time=log_ns,
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
        ts_ms = m.trigger_ms or m.recv_ms or int(time.time() * 1000)
        log_ns = int(ts_ms) * 1_000_000
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
