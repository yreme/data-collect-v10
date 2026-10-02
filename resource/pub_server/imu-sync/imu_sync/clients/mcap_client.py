"""从共享内存录制 Imu + SceneUpdate 到 MCAP。"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from ..config import AppConfig, ImuConfig, SceneModelConfig
from ..shm import ImuView
from .base import ShmClient
from .messages import (
    IMU_JSON_SCHEMA,
    build_frame_transform,
    build_imu_message,
    build_scene_model_update,
    build_scene_update,
    build_world_pose,
    log_ns_from_view,
    sample_from_view,
)
from .pose_tracker import PoseTrackerRegistry, scene_model_config_for_imu


def _mcap_compression(kind: str):
    from foxglove.mcap import MCAPCompression

    k = (kind or "zstd").strip().lower()
    if k in ("none", "off", ""):
        return None
    if k == "lz4":
        return MCAPCompression.Lz4
    return MCAPCompression.Zstd


class McapClient(ShmClient):
    client_name = "mcap"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._mc = cfg.clients.mcap
        out = Path(self._mc.output_dir).expanduser()
        if not out.is_absolute():
            out = (Path.cwd() / out).resolve()
        self._out_dir = out
        self._ctx = None
        self._writer = None
        self._imu_channels: Dict[str, object] = {}
        self._scene_channels: Dict[str, object] = {}
        self._scene_model_channels: Dict[str, object] = {}
        self._world_pose_channels: Dict[str, object] = {}
        self._tf_channels: Dict[str, object] = {}
        self._scene_model_cfgs: Dict[str, SceneModelConfig] = {}
        self._window = -1
        self._lock = threading.Lock()
        self._foxglove = None
        self._pose_registry = PoseTrackerRegistry(self._mc.pose)

    def setup(self) -> None:
        try:
            import foxglove
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("缺少 foxglove-sdk：pip install foxglove-sdk") from exc
        self._foxglove = foxglove
        self._ctx = foxglove.Context()
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self.log.info(
            "MCAP 输出: %s（每 %.0fs 切分，压缩=%s）",
            self._out_dir, self._mc.rotate_sec, self._mc.compression,
        )

    def _scene_model_cfg(self, imu_cfg: ImuConfig) -> SceneModelConfig:
        cfg = self._scene_model_cfgs.get(imu_cfg.name)
        if cfg is None:
            cfg = scene_model_config_for_imu(self._mc.scene_model, imu_cfg)
            self._scene_model_cfgs[imu_cfg.name] = cfg
        return cfg

    def _writer_options(self):
        from foxglove.mcap import MCAPWriteOptions
        comp = _mcap_compression(self._mc.compression)
        if comp is None:
            return None
        return MCAPWriteOptions(compression=comp)

    def _rotate(self, window: int) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._out_dir / f"{self._mc.filename_prefix}_{ts}.mcap"
        opts = self._writer_options()
        kwargs = {"allow_overwrite": self._mc.allow_overwrite, "context": self._ctx}
        if opts is not None:
            kwargs["writer_options"] = opts
        self._writer = self._foxglove.open_mcap(str(path), **kwargs)
        self._window = window
        self.log.info("新 MCAP: %s", path)

    def _imu_channel(self, imu_cfg: ImuConfig):
        ch = self._imu_channels.get(imu_cfg.name)
        if ch is None:
            from foxglove import Channel
            topic = f"{self._mc.topic_prefix.rstrip('/')}/{imu_cfg.name}/imu"
            ch = Channel(
                topic,
                schema=IMU_JSON_SCHEMA,
                context=self._ctx,
                metadata={"schema_name": "sensor_msgs/Imu"},
            )
            self._imu_channels[imu_cfg.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def _world_pose_channel(self, imu_cfg: ImuConfig):
        ch = self._world_pose_channels.get(imu_cfg.name)
        if ch is None:
            from foxglove.channels import PoseInFrameChannel
            topic = f"{self._mc.topic_prefix.rstrip('/')}/{imu_cfg.name}/world_pose"
            ch = PoseInFrameChannel(topic, context=self._ctx)
            self._world_pose_channels[imu_cfg.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def _scene_channel(self, imu_cfg: ImuConfig):
        ch = self._scene_channels.get(imu_cfg.name)
        if ch is None:
            from foxglove.channels import SceneUpdateChannel
            topic = f"{self._mc.topic_prefix.rstrip('/')}/{imu_cfg.name}/scene"
            ch = SceneUpdateChannel(topic, context=self._ctx)
            self._scene_channels[imu_cfg.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def _scene_model_channel(self, imu_cfg: ImuConfig):
        ch = self._scene_model_channels.get(imu_cfg.name)
        if ch is None:
            from foxglove.channels import SceneUpdateChannel
            topic = f"{self._mc.topic_prefix.rstrip('/')}/{imu_cfg.name}/scene_model"
            ch = SceneUpdateChannel(topic, context=self._ctx)
            self._scene_model_channels[imu_cfg.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def _tf_channel(self, imu_cfg: ImuConfig):
        ch = self._tf_channels.get(imu_cfg.name)
        if ch is None:
            from foxglove.channels import FrameTransformChannel
            topic = f"{self._mc.topic_prefix.rstrip('/')}/{imu_cfg.name}/tf"
            ch = FrameTransformChannel(topic, context=self._ctx)
            self._tf_channels[imu_cfg.name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def handle(self, imu_cfg: ImuConfig, view: ImuView) -> None:
        sample = sample_from_view(view)
        log_ns = log_ns_from_view(view)
        pose = self._pose_registry.update(imu_cfg, sample, log_ns)
        window = int(time.time() // self._mc.rotate_sec)
        with self._lock:
            if window != self._window:
                self._rotate(window)
            if self._mc.publish_imu:
                self._imu_channel(imu_cfg).log(
                    build_imu_message(imu_cfg, sample, log_ns), log_time=log_ns
                )
            if self._mc.publish_world_pose:
                self._world_pose_channel(imu_cfg).log(
                    build_world_pose(pose, log_ns), log_time=log_ns
                )
            if self._mc.publish_scene:
                self._scene_channel(imu_cfg).log(
                    build_scene_update(imu_cfg, pose, log_ns), log_time=log_ns
                )
            if self._mc.publish_scene_model:
                self._scene_model_channel(imu_cfg).log(
                    build_scene_model_update(
                        imu_cfg, pose, log_ns, self._scene_model_cfg(imu_cfg)
                    ),
                    log_time=log_ns,
                )
            if self._mc.pose.publish_tf:
                self._tf_channel(imu_cfg).log(
                    build_frame_transform(pose, log_ns), log_time=log_ns
                )

    def teardown(self) -> None:
        with self._lock:
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception:  # noqa: BLE001
                    pass
                self._writer = None
