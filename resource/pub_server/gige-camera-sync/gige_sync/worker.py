"""单相机采集线程：网格同步发布到 SHM。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from multimodal_common.logging_setup import get_logger
from multimodal_common.shm import SharedImageWriter
from multimodal_common.sync_grid import advance_sync_trigger_ms, align_up_sync_ms, hz_to_period_ms

from .unified_bridge import AppConfig, CameraConfig, CaptureConfig


@dataclass
class WorkerState:
    name: str
    status: str = "init"
    connected: bool = False
    frames: int = 0
    errors: int = 0
    last_publish_ms: int = 0
    resolved_ip: str = ""
    serial: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def incr(self, attr: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + n)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: getattr(self, k) for k in (
                "name", "status", "connected", "frames", "errors",
                "last_publish_ms", "resolved_ip", "serial",
            )}


class CameraWorker:
    def __init__(
        self,
        cam_cfg: CameraConfig,
        cap_cfg: CaptureConfig,
        writer: SharedImageWriter,
        app_cfg: AppConfig,
    ) -> None:
        self.cam_cfg = cam_cfg
        self.cap_cfg = cap_cfg
        self._writer = writer
        self._app_cfg = app_cfg
        self.state = WorkerState(name=cam_cfg.name, serial=cam_cfg.serial)
        self.log = get_logger(f"worker.{cam_cfg.name}")
        self._thread: Optional[threading.Thread] = None

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, args=(stop,),
            name=f"camera-{self.cam_cfg.name}", daemon=True,
        )
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run_loop(self, stop: threading.Event) -> None:
        if self.cap_cfg.mock:
            self._mock_loop(stop)
        else:
            self._capture_loop(stop)

    def _mock_loop(self, stop: threading.Event) -> None:
        w, h = self.cap_cfg.width, self.cap_cfg.height
        period_ms = hz_to_period_ms(self.cap_cfg.hz)
        self.state.set(status="running", connected=True)
        self.log.info("mock 模式：合成图像 %dx%d @ %dHz", w, h, self.cap_cfg.hz)
        frame = 0
        next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
        while not stop.is_set():
            now_ms = int(time.time() * 1000)
            if now_ms >= next_slot_ms:
                img = np.zeros((h, w, 3), dtype=np.uint8)
                img[:, :, 0] = (frame * 7) % 256
                img[:, :, 1] = (frame * 3) % 256
                data = img.tobytes()
                self._writer.publish(
                    data, width=w, height=h, encoding="bgr8",
                    trigger_ms=next_slot_ms, recv_ms=now_ms, frame_num=frame,
                )
                self.state.incr("frames")
                self.state.set(last_publish_ms=next_slot_ms)
                frame += 1
                next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)
            wait_ms = max(1, next_slot_ms - int(time.time() * 1000))
            stop.wait(timeout=min(wait_ms, 50) / 1000.0)

    def _capture_loop(self, stop: threading.Event) -> None:
        """真机采集：通过 GigE SDK 子进程或 Python 绑定获取帧，按网格发布。"""
        w, h = self.cap_cfg.width, self.cap_cfg.height
        period_ms = hz_to_period_ms(self.cap_cfg.hz)
        self.state.set(status="running", connected=True, resolved_ip=self.cam_cfg.ip)
        self.log.info(
            "采集 %s (ip=%s, serial=%s) @ %dHz 网格 %dms",
            self.cam_cfg.name, self.cam_cfg.ip or "-", self.cam_cfg.serial or "-",
            self.cap_cfg.hz, period_ms,
        )
        frame = 0
        next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
        pending: Optional[bytes] = None

        while not stop.is_set():
            if pending is None:
                pending = self._grab_frame(w, h)
            now_ms = int(time.time() * 1000)
            if pending is not None and now_ms >= next_slot_ms:
                try:
                    self._writer.publish(
                        pending, width=w, height=h, encoding="bgr8",
                        trigger_ms=next_slot_ms, recv_ms=now_ms, frame_num=frame,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.state.incr("errors")
                    self.log.error("写入 SHM 失败: %s", exc)
                else:
                    self.state.incr("frames")
                    self.state.set(last_publish_ms=next_slot_ms)
                    frame += 1
                    pending = None
                    next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)
            wait_ms = max(1, next_slot_ms - int(time.time() * 1000))
            stop.wait(timeout=min(wait_ms, 20) / 1000.0)

    def _grab_frame(self, w: int, h: int) -> Optional[bytes]:
        """从 GigE SDK 获取一帧。生产环境替换为实际 SDK 调用。"""
        try:
            from .driver import grab_frame  # noqa: WPS433
            return grab_frame(self.cam_cfg, w, h)
        except ImportError:
            return np.zeros((h, w, 3), dtype=np.uint8).tobytes()
