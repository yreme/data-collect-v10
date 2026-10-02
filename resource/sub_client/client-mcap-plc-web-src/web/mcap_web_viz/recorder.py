"""PLC-triggered multimodal MCAP recording manager."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from multimodal_sync.config import load_config
from multimodal_sync.shm_discovery import scan_shm_segments

from .plc_multimodal_collector import PlcMultimodalCollector


@dataclass
class ShmStatus:
    available: bool
    cameras: List[str] = field(default_factory=list)
    imus: List[str] = field(default_factory=list)
    lidars: List[str] = field(default_factory=list)
    total: int = 0
    message: str = ""
    plc_segment: str = ""
    plc_available: bool = False


@dataclass
class RecordingState:
    running: bool = False
    started_at: Optional[float] = None
    prefix: str = "multimodal"
    output_dir: str = "/data/mcap"
    mode: str = "plc_triggered"
    last_file: Optional[str] = None
    files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    frame_counts: Dict[str, int] = field(default_factory=dict)
    total_frames: int = 0
    active_sensors: List[str] = field(default_factory=list)
    snapshots_written: int = 0
    snapshots_skipped: int = 0
    mcap_status: Dict[str, Any] = field(default_factory=dict)


class RecorderManager:
    """Manages auto-start PLC-triggered multimodal collection."""

    def __init__(
        self,
        *,
        config_path: str = "/app/configs/client_mcap.yaml",
        default_output_dir: str = "/data/mcap",
        default_prefix: str = "multimodal",
    ) -> None:
        self.config_path = config_path
        self.default_output_dir = default_output_dir
        self.default_prefix = default_prefix
        self._collector: Optional[PlcMultimodalCollector] = None
        self._lock = threading.RLock()
        self.state = RecordingState(output_dir=default_output_dir, prefix=default_prefix)
        self._hourly_dir = os.getenv("MCAP_HOURLY_DIR", "/data/mcap/hourly")
        self._short_dir = os.getenv("MCAP_SHORT_DIR", "/data/mcap/short")

    def _ensure_collector(self) -> PlcMultimodalCollector:
        if self._collector is None:
            self._collector = PlcMultimodalCollector(config_path=self.config_path)
        return self._collector

    def shm_status(self) -> ShmStatus:
        collector = self._collector
        if collector is not None:
            shm = collector.shm_status()
            return ShmStatus(
                available=bool(shm.get("available")),
                cameras=list(shm.get("cameras") or []),
                imus=list(shm.get("imus") or []),
                lidars=list(shm.get("lidars") or []),
                total=int(shm.get("total") or 0),
                message=str(shm.get("message") or ""),
                plc_segment=str(shm.get("plc_segment") or ""),
                plc_available=bool(shm.get("plc_available")),
            )
        try:
            cfg = load_config(self.config_path)
            live = scan_shm_segments(
                camera_prefix=cfg.shm.camera,
                imu_prefix=cfg.shm.imu,
                lidar_prefix=cfg.shm.lidar,
            )
            if live.total > 0:
                msg = (
                    f"已发现 {live.total} 路传感器（相机 {len(live.cameras)} / "
                    f"IMU {len(live.imus)} / 雷达 {len(live.lidars)}）"
                )
            else:
                msg = (
                    "共享内存 /dev/shm 中未发现传感器数据。"
                    "请先启动 server（gige / imu / lidar）并确认各容器均挂载了宿主机 /dev/shm。"
                )
            return ShmStatus(
                available=live.total > 0,
                cameras=live.cameras,
                imus=live.imus,
                lidars=live.lidars,
                total=live.total,
                message=msg,
            )
        except Exception as exc:  # noqa: BLE001
            return ShmStatus(available=False, message=f"无法检测共享内存：{exc}")

    def _list_mcap_files(self, directory: str) -> List[str]:
        p = Path(directory)
        if not p.is_dir():
            return []
        files = sorted(p.glob("*.mcap"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [str(f) for f in files[:20]]

    def _is_running(self) -> bool:
        c = self._collector
        return c is not None and c.get_status().get("running", False)

    def status(self) -> RecordingState:
        with self._lock:
            running = self._is_running()
            collector = self._collector
            mcap_status: Dict[str, Any] = {}
            frame_counts: Dict[str, int] = {}
            total_frames = 0
            active_sensors: List[str] = []
            snapshots_written = 0
            snapshots_skipped = 0
            error = self.state.error

            if collector is not None:
                st = collector.get_status()
                mcap_status = st.get("mcap") or {}
                proc = collector.processing_stats()
                frame_counts = dict(proc.get("counts") or {})
                total_frames = int(proc.get("total_frames") or 0)
                active_sensors = list(proc.get("active_sensors") or [])
                snapshots_written = int(proc.get("snapshots_written") or 0)
                snapshots_skipped = int(proc.get("snapshots_skipped") or 0)
                if st.get("error"):
                    error = st["error"]

            hourly_files = self._list_mcap_files(self._hourly_dir)
            short_files = self._list_mcap_files(self._short_dir)
            all_files = hourly_files + short_files
            last_file = all_files[0] if all_files else None

            self.state = RecordingState(
                running=running,
                started_at=self.state.started_at if running else None,
                prefix=self.default_prefix,
                output_dir=self.default_output_dir,
                mode="plc_triggered",
                last_file=last_file,
                files=all_files,
                error=error,
                frame_counts=frame_counts,
                total_frames=total_frames,
                active_sensors=active_sensors,
                snapshots_written=snapshots_written,
                snapshots_skipped=snapshots_skipped,
                mcap_status=mcap_status,
            )
            return self.state

    def start(
        self,
        *,
        prefix: Optional[str] = None,
        output_dir: Optional[str] = None,
        mode: str = "plc_triggered",
        rotate_sec: float = 0,
        require_shm: bool = False,
        mock_mode: bool | None = None,
    ) -> RecordingState:
        del mode, rotate_sec  # dual-tier rotation comes from env
        with self._lock:
            if self._is_running():
                raise RuntimeError("采集已在运行")

            shm = self.shm_status()
            if require_shm and not shm.available:
                raise RuntimeError(shm.message)

            if prefix:
                self.default_prefix = prefix
            if output_dir:
                self.default_output_dir = output_dir

            collector = self._ensure_collector()
            collector.start()
            import time

            self.state.started_at = time.time()
            self.state.error = None
            return self.status()

    def stop(self) -> RecordingState:
        with self._lock:
            if self._collector is not None:
                self._collector.stop_collecting()
            self.state.running = False
            return self.status()

    def elapsed_sec(self) -> float:
        if self.state.started_at is None or not self._is_running():
            return 0.0
        import time

        return time.time() - self.state.started_at

    def collector_status(self) -> dict[str, Any]:
        if self._collector is None:
            return {"running": False}
        return self._collector.get_status()

    def suggested_filename(self, prefix: str) -> str:
        from datetime import datetime

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{prefix}_{ts}.mcap"
