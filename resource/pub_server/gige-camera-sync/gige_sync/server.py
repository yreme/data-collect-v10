"""GigE 相机采集 server 编排。"""

from __future__ import annotations

import signal
import threading
import time
from typing import List, Optional

from multimodal_common.logging_setup import get_logger
from multimodal_common.shm import SharedImageWriter

from .discovery import discover_cameras, resolve_cameras
from .unified_bridge import AppConfig, CameraConfig
from .worker import CameraWorker

LOG = get_logger("server")


class CaptureServer:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._workers: List[CameraWorker] = []
        self._writers: List[SharedImageWriter] = []

    def _resolve_cameras(self) -> List[CameraConfig]:
        cap = self.cfg.capture
        raw = []
        if cap.auto_discover:
            raw = discover_cameras(subnet=cap.discover_subnet)
        preset = [c.params | {"name": c.name, "enabled": c.enabled} for c in self.cfg.cameras]
        resolved = resolve_cameras(preset, raw, discover_all=cap.auto_discover)
        out: List[CameraConfig] = []
        for d in resolved:
            out.append(CameraConfig(
                name=d.name, enabled=True,
                frame_id=d.name, topic=f"/camera/{d.name}/image",
                params={"serial": d.serial, "ip": d.ip, "mac": d.mac, "model": d.model},
            ))
        return out or self.cfg.enabled_cameras

    def _spawn_worker(self, cam_cfg: CameraConfig) -> CameraWorker:
        shm = self.cfg.shm
        writer = SharedImageWriter(
            shm.segment_name(cam_cfg.name),
            shm.slot_count, shm.slot_capacity_bytes, create=True,
        )
        self._writers.append(writer)
        worker = CameraWorker(cam_cfg, self.cfg.capture, writer, self.cfg)
        self._workers.append(worker)
        return worker

    def start(self) -> None:
        cameras = self._resolve_cameras()
        LOG.info("采集 server：%d 路相机, hz=%d（网格对齐）, mock=%s",
                 len(cameras), self.cfg.capture.hz, self.cfg.capture.mock)
        for i, cam in enumerate(cameras):
            if i > 0:
                time.sleep(0.3)
            self._spawn_worker(cam)
        for w in self._workers:
            w.start(self._stop)

    def join(self, timeout: Optional[float] = None) -> None:
        for w in self._workers:
            w.join(timeout=timeout)

    def close(self) -> None:
        for wr in self._writers:
            try:
                wr.unlink()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> int:
        def on_sig(signum, _frame):
            LOG.info("收到信号 %s，停止…", signum)
            self._stop.set()
        signal.signal(signal.SIGINT, on_sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_sig)
        self.start()
        while not self._stop.is_set():
            self._stop.wait(timeout=1.0)
        self.join(timeout=10.0)
        self.close()
        return 0
