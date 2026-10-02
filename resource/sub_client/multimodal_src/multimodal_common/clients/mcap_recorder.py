"""MCAP 录制客户端：从所有传感器 SHM 录制到 MCAP 文件。"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from multimodal_common.logging_setup import get_logger
from multimodal_common.shm import SharedImageReader, SharedImuReader, SharedPointCloudReader
from multimodal_common.sync_grid import sync_lag_ms
from multimodal_common.unified_config import SensorDevice, UnifiedConfig

LOG = get_logger("client.mcap")


class McapRecorder:
    def __init__(self, cfg: UnifiedConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._counts: Dict[str, int] = {}
        self._writer = None

    def run(self) -> int:
        def on_sig(signum, _frame):
            LOG.info("信号 %s", signum)
            self._stop.set()
        signal.signal(signal.SIGINT, on_sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_sig)

        try:
            from mcap.writer import Writer
        except ImportError:
            LOG.error("缺少 mcap：pip install mcap")
            return 1

        out_dir = Path(self.cfg.clients.mcap_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"capture_{int(time.time())}.mcap"
        LOG.info("录制到 %s", out_path)

        with open(out_path, "wb") as f:
            self._writer = Writer(f)
            self._writer.start()
            threads = []
            for dev in self.cfg.all_enabled():
                t = threading.Thread(target=self._reader_loop, args=(dev,), daemon=True)
                t.start()
                threads.append(t)
            while not self._stop.is_set():
                self._stop.wait(timeout=1.0)
            for t in threads:
                t.join(timeout=5.0)
            self._writer.finish()

        total = sum(self._counts.values())
        LOG.info("录制完成，共 %d 条消息 -> %s", total, out_path)
        return 0

    def _reader_loop(self, dev: SensorDevice) -> None:
        shm_name = {
            "camera": self.cfg.shm.camera_segment(dev.name),
            "lidar": self.cfg.shm.lidar_segment(dev.name),
            "imu": self.cfg.shm.imu_segment(dev.name),
        }.get(dev.kind, dev.name)

        while not self._stop.is_set():
            try:
                if dev.kind == "camera":
                    self._camera_loop(dev, shm_name)
                elif dev.kind == "lidar":
                    self._lidar_loop(dev, shm_name)
                elif dev.kind == "imu":
                    self._imu_loop(dev, shm_name)
            except FileNotFoundError:
                LOG.info("等待 SHM %s …", shm_name)
                self._stop.wait(timeout=2.0)

    def _camera_loop(self, dev: SensorDevice, shm_name: str) -> None:
        reader = SharedImageReader(shm_name)
        topic = self.cfg.topic_for(dev)
        schema_id = self._writer.register_schema(
            name="sensor_msgs/CompressedImage",
            encoding="jsonschema",
            data=b'{"type":"object"}',
        )
        channel_id = self._writer.register_channel(
            topic=topic, message_encoding="json", schema_id=schema_id,
        )
        last_seq = reader.latest_seq()
        try:
            while not self._stop.is_set():
                img = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if img is None:
                    continue
                last_seq = img.meta.seq
                ts_ns = int(img.meta.trigger_ms or img.meta.recv_ms) * 1_000_000
                self._writer.add_message(
                    channel_id=channel_id, log_time=ts_ns, data=img.data,
                    publish_time=ts_ns,
                )
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()

    def _lidar_loop(self, dev: SensorDevice, shm_name: str) -> None:
        reader = SharedPointCloudReader(shm_name)
        topic = self.cfg.topic_for(dev)
        schema_id = self._writer.register_schema(
            name="sensor_msgs/PointCloud2",
            encoding="jsonschema",
            data=b'{"type":"object"}',
        )
        channel_id = self._writer.register_channel(
            topic=topic, message_encoding="json", schema_id=schema_id,
        )
        last_seq = reader.latest_seq()
        try:
            while not self._stop.is_set():
                pc = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if pc is None:
                    continue
                last_seq = pc.meta.seq
                ts_ns = int(pc.meta.trigger_ms or pc.meta.recv_ms) * 1_000_000
                self._writer.add_message(
                    channel_id=channel_id, log_time=ts_ns, data=pc.data,
                    publish_time=ts_ns,
                )
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()

    def _imu_loop(self, dev: SensorDevice, shm_name: str) -> None:
        reader = SharedImuReader(shm_name)
        topic = self.cfg.topic_for(dev)
        schema_id = self._writer.register_schema(
            name="sensor_msgs/Imu",
            encoding="jsonschema",
            data=b'{"type":"object"}',
        )
        channel_id = self._writer.register_channel(
            topic=topic, message_encoding="json", schema_id=schema_id,
        )
        last_seq = reader.latest_seq()
        try:
            while not self._stop.is_set():
                view = reader.read_new(last_seq, stop=self._stop, timeout=2.0)
                if view is None:
                    continue
                last_seq = view.meta.seq
                ts_ns = int(view.meta.trigger_ms or view.meta.recv_ms) * 1_000_000
                import json
                data = json.dumps([s.__dict__ for s in view.samples]).encode()
                self._writer.add_message(
                    channel_id=channel_id, log_time=ts_ns, data=data,
                    publish_time=ts_ns,
                )
                self._counts[dev.name] = self._counts.get(dev.name, 0) + 1
        finally:
            reader.close()
