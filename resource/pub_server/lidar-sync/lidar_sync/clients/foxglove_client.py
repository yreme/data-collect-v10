"""Foxglove WebSocket 点云广播（默认端口 38765）。"""

from __future__ import annotations

import threading
import time
from typing import Dict

from ..config import AppConfig, LidarConfig
from ..logging_setup import get_logger
from ..pointcloud_codec import unpack_points
from ..shm import PointCloudView
from .base import ShmClient

LOG = get_logger("client.foxglove")


def _to_timestamp(ns: int):
    from foxglove.messages import Timestamp
    return Timestamp(sec=int(ns // 1_000_000_000), nsec=int(ns % 1_000_000_000))


class FoxgloveClient(ShmClient):
    client_name = "foxglove"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._server = None
        self._ctx = None
        self._channels: Dict[str, object] = {}
        self._lock = threading.Lock()
        self._fc = cfg.clients.foxglove

    def setup(self) -> None:
        try:
            import foxglove
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "缺少 foxglove-sdk：pip install foxglove-sdk"
            ) from exc
        self._foxglove = foxglove
        self._ctx = foxglove.Context()
        self._server = foxglove.start_server(
            name="lidar-sync",
            host=self._fc.host,
            port=self._fc.port,
            context=self._ctx,
        )
        self.log.info(
            "Foxglove ws://%s:%d（3D 面板订阅 PointCloud）",
            self._fc.host, self._fc.port,
        )

    def _channel(self, lidar_cfg: LidarConfig):
        ch = self._channels.get(lidar_cfg.name)
        if ch is None:
            with self._lock:
                ch = self._channels.get(lidar_cfg.name)
                if ch is None:
                    from foxglove.channels import PointCloudChannel
                    topic = f"{self._fc.topic_prefix.rstrip('/')}/{lidar_cfg.name}/points"
                    ch = PointCloudChannel(topic, context=self._ctx)
                    self._channels[lidar_cfg.name] = ch
                    self.log.info("通道: %s", topic)
        return ch

    def handle(self, lidar_cfg: LidarConfig, pc: PointCloudView) -> None:
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
            frame_id=lidar_cfg.frame_id or lidar_cfg.name,
            pose=Pose(
                position=Vector3(x=0, y=0, z=0),
                orientation=Quaternion(x=0, y=0, z=0, w=1),
            ),
            point_stride=16,
            fields=fields,
            data=pts.astype("float32").tobytes(),
        )
        self._channel(lidar_cfg).log(msg, log_time=log_ns)

    def teardown(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:  # noqa: BLE001
                pass
