"""Foxglove WebSocket IMU 广播（默认端口 28765）。"""

from __future__ import annotations

import threading
from typing import Dict

from ..config import AppConfig, ImuConfig, SceneModelConfig
from ..logging_setup import get_logger
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

LOG = get_logger("client.foxglove")


class FoxgloveClient(ShmClient):
    client_name = "foxglove"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._server = None
        self._ctx = None
        self._imu_channels: Dict[str, object] = {}
        self._scene_channels: Dict[str, object] = {}
        self._scene_model_channels: Dict[str, object] = {}
        self._world_pose_channels: Dict[str, object] = {}
        self._tf_channels: Dict[str, object] = {}
        self._scene_model_cfgs: Dict[str, SceneModelConfig] = {}
        self._lock = threading.Lock()
        self._fc = cfg.clients.foxglove
        self._pose_registry = PoseTrackerRegistry(self._fc.pose)

    def setup(self) -> None:
        try:
            import foxglove
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("缺少 foxglove-sdk：pip install foxglove-sdk") from exc
        self._foxglove = foxglove
        self._ctx = foxglove.Context()
        self._server = foxglove.start_server(
            name="imu-sync",
            host=self._fc.host,
            port=self._fc.port,
            context=self._ctx,
        )
        self.log.info(
            "Foxglove ws://%s:%d（Imu + world_pose + scene + scene_model + TF）",
            self._fc.host,
            self._fc.port,
        )

    def _scene_model_cfg(self, imu_cfg: ImuConfig) -> SceneModelConfig:
        cfg = self._scene_model_cfgs.get(imu_cfg.name)
        if cfg is None:
            cfg = scene_model_config_for_imu(self._fc.scene_model, imu_cfg)
            self._scene_model_cfgs[imu_cfg.name] = cfg
        return cfg

    def _imu_channel(self, imu_cfg: ImuConfig):
        ch = self._imu_channels.get(imu_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._imu_channels.get(imu_cfg.name)
                if ch is None:
                    from foxglove import Channel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{imu_cfg.name}/imu"
                    ch = Channel(
                        topic,
                        schema=IMU_JSON_SCHEMA,
                        context=self._ctx,
                        metadata={"schema_name": "sensor_msgs/Imu"},
                    )
                    self._imu_channels[imu_cfg.name] = ch
                    self.log.info("通道: %s (sensor_msgs/Imu)", topic)
        return ch

    def _world_pose_channel(self, imu_cfg: ImuConfig):
        ch = self._world_pose_channels.get(imu_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._world_pose_channels.get(imu_cfg.name)
                if ch is None:
                    from foxglove.channels import PoseInFrameChannel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{imu_cfg.name}/world_pose"
                    ch = PoseInFrameChannel(topic, context=self._ctx)
                    self._world_pose_channels[imu_cfg.name] = ch
                    self.log.info("通道: %s (PoseInFrame 累计 world 位姿)", topic)
        return ch

    def _scene_channel(self, imu_cfg: ImuConfig):
        ch = self._scene_channels.get(imu_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._scene_channels.get(imu_cfg.name)
                if ch is None:
                    from foxglove.channels import SceneUpdateChannel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{imu_cfg.name}/scene"
                    ch = SceneUpdateChannel(topic, context=self._ctx)
                    self._scene_channels[imu_cfg.name] = ch
                    self.log.info("通道: %s (SceneUpdate 立方体)", topic)
        return ch

    def _scene_model_channel(self, imu_cfg: ImuConfig):
        ch = self._scene_model_channels.get(imu_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._scene_model_channels.get(imu_cfg.name)
                if ch is None:
                    from foxglove.channels import SceneUpdateChannel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{imu_cfg.name}/scene_model"
                    ch = SceneUpdateChannel(topic, context=self._ctx)
                    self._scene_model_channels[imu_cfg.name] = ch
                    model_cfg = self._scene_model_cfg(imu_cfg)
                    self.log.info(
                        "通道: %s (SceneUpdate GLB: %s)",
                        topic,
                        model_cfg.url,
                    )
        return ch

    def _tf_channel(self, imu_cfg: ImuConfig):
        ch = self._tf_channels.get(imu_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._tf_channels.get(imu_cfg.name)
                if ch is None:
                    from foxglove.channels import FrameTransformChannel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{imu_cfg.name}/tf"
                    ch = FrameTransformChannel(topic, context=self._ctx)
                    self._tf_channels[imu_cfg.name] = ch
                    self.log.info("通道: %s (FrameTransform)", topic)
        return ch

    def handle(self, imu_cfg: ImuConfig, view: ImuView) -> None:
        sample = sample_from_view(view)
        log_ns = log_ns_from_view(view)
        pose = self._pose_registry.update(imu_cfg, sample, log_ns)
        if self._fc.publish_imu:
            msg = build_imu_message(imu_cfg, sample, log_ns)
            self._imu_channel(imu_cfg).log(msg, log_time=log_ns)
        if self._fc.publish_world_pose:
            world_pose = build_world_pose(pose, log_ns)
            self._world_pose_channel(imu_cfg).log(world_pose, log_time=log_ns)
        if self._fc.publish_scene:
            scene = build_scene_update(imu_cfg, pose, log_ns)
            self._scene_channel(imu_cfg).log(scene, log_time=log_ns)
        if self._fc.publish_scene_model:
            model_scene = build_scene_model_update(
                imu_cfg, pose, log_ns, self._scene_model_cfg(imu_cfg)
            )
            self._scene_model_channel(imu_cfg).log(model_scene, log_time=log_ns)
        if self._fc.pose.publish_tf:
            tf = build_frame_transform(pose, log_ns)
            self._tf_channel(imu_cfg).log(tf, log_time=log_ns)

    def teardown(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:  # noqa: BLE001
                pass
