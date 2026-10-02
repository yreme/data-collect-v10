"""统一 Foxglove WebSocket 桥接：从所有传感器 SHM 读取并广播。"""

from __future__ import annotations

import signal
import threading
import time
from typing import Dict, Optional

from multimodal_common.logging_setup import get_logger
from multimodal_common.shm import (
    ImageView, ImuView, PointCloudView,
    SharedImageReader, SharedImuReader, SharedPointCloudReader,
)
from multimodal_common.sync_grid import sync_lag_ms
from multimodal_common.unified_config import SensorDevice, UnifiedConfig

LOG = get_logger("client.foxglove")


class FoxgloveBridge:
    def __init__(self, cfg: UnifiedConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._server = None
        self._ctx = None
        self._channels: Dict[str, object] = {}
        self._counts: Dict[str, int] = {}
        self._lag_warned: set = set()

    def run(self) -> int:
        def on_sig(signum, _frame):
            LOG.info("信号 %s", signum)
            self._stop.set()
        signal.signal(signal.SIGINT, on_sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_sig)

        try:
            import foxglove
            from foxglove.channels import CompressedImageChannel, PointCloudChannel
        except ImportError:
            LOG.error("缺少 foxglove-sdk：pip install foxglove-sdk")
            return 1

        self._ctx = foxglove.Context()
        cl = self.cfg.clients
        self._server = foxglove.start_server(
            name="multimodal-sync",
            host=cl.foxglove_host,
            port=cl.foxglove_port,
            context=self._ctx,
        )
        LOG.info("Foxglove ws://%s:%d", cl.foxglove_host, cl.foxglove_port)

        threads = []
        for dev in self.cfg.all_enabled():
            t = threading.Thread(target=self._reader_loop, args=(dev,), daemon=True)
            t.start()
            threads.append(t)

        while not self._stop.is_set():
            self._stop.wait(timeout=1.0)
        for t in threads:
            t.join(timeout=5.0)
        if self._server:
            self._server.stop()
        return 0

    def _check_lag(self, dev: SensorDevice, trigger_ms: int, publish_ms: int) -> None:
        lag = sync_lag_ms(trigger_ms, publish_ms)
        cl = self.cfg.clients
        if lag > cl.max_lag_ms or lag < cl.min_lag_ms:
            key = f"{dev.name}:{lag // 10}"
            if key not in self._lag_warned:
                self._lag_warned.add(key)
                LOG.warning("%s 发布延迟 %dms（触发=%d 发布=%d）", dev.name, lag, trigger_ms, publish_ms)

    def _reader_loop(self, dev: SensorDevice) -> None:
        topic = self.cfg.topic_for(dev)
        shm_name = {
            "camera": self.cfg.shm.camera_segment(dev.name),
            "lidar": self.cfg.shm.lidar_segment(dev.name),
            "imu": self.cfg.shm.imu_segment(dev.name),
        }.get(dev.kind, dev.name)

        while not self._stop.is_set():
            try:
                if dev.kind == "camera":
                    self._camera_loop(dev, shm_name, topic)
                elif dev.kind == "lidar":
                    self._lidar_loop(dev, shm_name, topic)
                elif dev.kind == "imu":
                    self._imu_loop(dev, shm_name, topic)
            except FileNotFoundError:
                LOG.info("等待 SHM %s …", shm_name)
                self._stop.wait(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("读取 %s 中断: %s", shm_name, exc)
                self._stop.wait(timeout=1.0)

    def _camera_loop(self, dev: SensorDevice, shm_name: str, topic: str) -> None:
        from foxglove.channels import CompressedImageChannel
        from foxglove.messages import CompressedImage, Timestamp

        reader = SharedImageReader(shm_name)
        ch = CompressedImageChannel(topic, context=self._ctx)
        last_seq = reader.latest_seq()
        LOG.info("已附着相机 %s -> %s", shm_name, topic)
        try:
            while not self._stop.is_set():
                img = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if img is None:
                    continue
                last_seq = img.meta.seq
                m = img.meta
                self._check_lag(dev, m.trigger_ms, m.recv_ms)
                ts_ns = int(m.trigger_ms or m.recv_ms) * 1_000_000
                msg = CompressedImage(
                    timestamp=Timestamp(sec=ts_ns // 1_000_000_000, nsec=ts_ns % 1_000_000_000),
                    frame_id=dev.frame_id or dev.name,
                    format="bgr8",
                    data=img.data,
                )
                ch.log(msg, log_time=ts_ns)
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()

    def _lidar_loop(self, dev: SensorDevice, shm_name: str, topic: str) -> None:
        from foxglove.channels import PointCloudChannel
        from foxglove.messages import (
            PackedElementField, PackedElementFieldNumericType,
            PointCloud, Pose, Quaternion, Vector3, Timestamp,
        )
        import numpy as np

        reader = SharedPointCloudReader(shm_name)
        ch = PointCloudChannel(topic, context=self._ctx)
        last_seq = reader.latest_seq()
        LOG.info("已附着雷达 %s -> %s", shm_name, topic)
        f32 = PackedElementFieldNumericType.Float32
        fields = [
            PackedElementField(name="x", offset=0, type=f32),
            PackedElementField(name="y", offset=4, type=f32),
            PackedElementField(name="z", offset=8, type=f32),
            PackedElementField(name="intensity", offset=12, type=f32),
        ]
        try:
            while not self._stop.is_set():
                pc = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if pc is None:
                    continue
                last_seq = pc.meta.seq
                m = pc.meta
                self._check_lag(dev, m.trigger_ms, m.recv_ms)
                ts_ns = int(m.trigger_ms or m.recv_ms) * 1_000_000
                pts = np.frombuffer(pc.data, dtype=np.float32).reshape(-1, 4)
                msg = PointCloud(
                    timestamp=Timestamp(sec=ts_ns // 1_000_000_000, nsec=ts_ns % 1_000_000_000),
                    frame_id=dev.frame_id or dev.name,
                    pose=Pose(position=Vector3(x=0, y=0, z=0),
                              orientation=Quaternion(x=0, y=0, z=0, w=1)),
                    point_stride=16, fields=fields,
                    data=pts.astype("float32").tobytes(),
                )
                ch.log(msg, log_time=ts_ns)
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()

    def _imu_loop(self, dev: SensorDevice, shm_name: str, topic: str) -> None:
        reader = SharedImuReader(shm_name)
        last_seq = reader.latest_seq()
        LOG.info("已附着 IMU %s -> %s", shm_name, topic)
        try:
            while not self._stop.is_set():
                view = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if view is None:
                    continue
                last_seq = view.meta.seq
                m = view.meta
                self._check_lag(dev, m.trigger_ms, m.recv_ms)
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()
