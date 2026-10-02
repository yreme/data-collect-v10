"""In-process IMU recorder manager (MCAP + CSV)."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import load_imu_record_config
from .rotate import next_window_start
from .writer import ImuMcapCsvClient

LOG = logging.getLogger("imu_record.manager")


@dataclass
class ShmStatus:
    available: bool
    imus: List[str] = field(default_factory=list)
    total: int = 0
    message: str = ""


@dataclass
class RecordingState:
    running: bool = False
    started_at: Optional[float] = None
    prefix: str = "imu"
    output_dir: str = "/data/imu"
    period_hours: int = 6
    last_mcap: Optional[str] = None
    last_csv: Optional[str] = None
    files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    frame_counts: Dict[str, int] = field(default_factory=dict)
    total_frames: int = 0
    active_sensors: List[str] = field(default_factory=list)
    current_mcap: Optional[str] = None
    current_csv: Optional[str] = None
    next_rotate_at: Optional[str] = None
    waiting_for_data: bool = False


class NoShmDataError(RuntimeError):
    pass


class RecorderManager:
    def __init__(
        self,
        *,
        config_path: str = "/app/configs/client_imu_record.yaml",
        default_output_dir: str = "/data/imu",
        default_prefix: str = "imu",
        default_period_hours: int = 6,
    ) -> None:
        self.config_path = config_path
        self.default_output_dir = default_output_dir
        self.default_prefix = default_prefix
        self.default_period_hours = default_period_hours
        self._client: Optional[ImuMcapCsvClient] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.state = RecordingState(
            output_dir=default_output_dir,
            prefix=default_prefix,
            period_hours=default_period_hours,
        )

    def shm_status(self) -> ShmStatus:
        try:
            cfg = load_imu_record_config(self.config_path)
            from multimodal_sync.shm_discovery import scan_shm_segments

            live = scan_shm_segments(imu_prefix=cfg.imu_prefix)
            if live.imus:
                msg = f"已发现 {len(live.imus)} 路 IMU：{', '.join(live.imus)}"
            else:
                msg = (
                    "共享内存 /dev/shm 中未发现 IMU 段（期望 imu_*）。"
                    "请先启动 server-imu-shm 并确认挂载了 /dev/shm。"
                )
            return ShmStatus(
                available=bool(live.imus),
                imus=list(live.imus),
                total=len(live.imus),
                message=msg,
            )
        except Exception as exc:  # noqa: BLE001
            return ShmStatus(available=False, message=f"无法检测共享内存：{exc}")

    def _list_files(self, output_dir: str) -> List[str]:
        p = Path(output_dir)
        if not p.is_dir():
            return []
        files = sorted(
            list(p.glob("*.mcap")) + list(p.glob("*.csv")),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return [str(f) for f in files[:40]]

    def _is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> RecordingState:
        with self._lock:
            running = self._is_running()
            files = self._list_files(self.state.output_dir)
            last_mcap = next((f for f in files if f.endswith(".mcap")), None)
            last_csv = next((f for f in files if f.endswith(".csv")), None)
            st = RecordingState(
                running=running,
                started_at=self.state.started_at if running else None,
                prefix=self.state.prefix,
                output_dir=self.state.output_dir,
                period_hours=self.state.period_hours,
                last_mcap=last_mcap,
                last_csv=last_csv,
                files=files,
                error=self.state.error,
            )
            client = self._client
            if client is not None and running:
                snap = client.processing_stats()
                st.frame_counts = dict(snap.get("counts") or {})
                st.total_frames = int(snap.get("total_frames") or 0)
                st.active_sensors = list(snap.get("active_sensors") or [])
                st.current_mcap = snap.get("current_mcap")
                st.current_csv = snap.get("current_csv")
                st.next_rotate_at = snap.get("next_rotate_at")
                if self.state.started_at and st.total_frames == 0:
                    st.waiting_for_data = (time.time() - self.state.started_at) < 8.0
            else:
                try:
                    st.next_rotate_at = next_window_start(
                        period_hours=self.state.period_hours
                    ).isoformat()
                except Exception:  # noqa: BLE001
                    pass
            return st

    def start(
        self,
        *,
        prefix: Optional[str] = None,
        output_dir: Optional[str] = None,
        period_hours: Optional[int] = None,
        require_shm: bool = True,
    ) -> RecordingState:
        with self._lock:
            if self._is_running():
                raise RuntimeError("已有录制任务在运行")

            shm = self.shm_status()
            if require_shm and not shm.available:
                raise NoShmDataError(shm.message)

            out = output_dir or self.default_output_dir
            Path(out).mkdir(parents=True, exist_ok=True)
            pref = prefix or self.default_prefix
            hours = int(period_hours or self.default_period_hours)

            cfg = load_imu_record_config(self.config_path)
            cfg.output_dir = out
            cfg.filename_prefix = pref
            cfg.period_hours = hours
            tz_env = os.environ.get("TZ", "").strip()
            if not cfg.timezone and tz_env:
                cfg.timezone = tz_env

            client = ImuMcapCsvClient(cfg)

            def _run() -> None:
                try:
                    client.run(install_signals=False)
                except Exception as exc:  # noqa: BLE001
                    if not self.state.error:
                        self.state.error = str(exc)
                    LOG.exception("IMU 录制线程异常")

            started_at = time.time()
            thread = threading.Thread(target=_run, name="imu-record", daemon=True)
            thread.start()
            self._client = client
            self._thread = thread
            self.state = RecordingState(
                running=True,
                started_at=started_at,
                prefix=pref,
                output_dir=out,
                period_hours=hours,
                error=None,
                waiting_for_data=True,
            )
            return self.status()

    def stop(self) -> RecordingState:
        with self._lock:
            if self._client is not None:
                self._client.stop.set()
            if self._thread is not None:
                self._thread.join(timeout=20.0)
            self._client = None
            self._thread = None
            self.state.running = False
            return self.status()

    def elapsed_sec(self) -> float:
        if self.state.started_at is None or not self._is_running():
            return 0.0
        return time.time() - self.state.started_at

    def suggested_filename(self, prefix: str) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{prefix}_{ts}.mcap"
