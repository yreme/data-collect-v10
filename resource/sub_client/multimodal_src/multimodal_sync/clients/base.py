"""Multimodal client base: robust per-sensor SHM reader threads."""

from __future__ import annotations

import signal
import threading
import time
from typing import Callable, Dict, List, Optional, Set

from ..config import AppConfig, SensorRef
from ..logging_setup import get_logger
from ..shm_discovery import DiscoveredSensors, scan_shm_segments

LOG = get_logger("client.base")


class MultimodalClient:
    """Base for clients that read camera / IMU / lidar shared-memory segments."""

    client_name = "client"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.log = get_logger(f"client.{self.client_name}")
        self._counts: Dict[str, int] = {}
        self._counts_lock = threading.Lock()
        self._active_sensors: Set[str] = set()
        self._sensor_lock = threading.Lock()

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    # ---- sensor hooks (subclasses implement) -------------------------
    def on_camera(self, sensor: SensorRef, data) -> None:
        raise NotImplementedError

    def on_imu(self, sensor: SensorRef, data) -> None:
        raise NotImplementedError

    def on_lidar(self, sensor: SensorRef, data) -> None:
        raise NotImplementedError

    def on_sensor_online(self, kind: str, name: str) -> None:
        self.log.info("传感器上线: %s/%s", kind, name)

    def on_sensor_offline(self, kind: str, name: str) -> None:
        self.log.warning("传感器离线: %s/%s（将持续等待恢复）", kind, name)

    # ---- run loop ---------------------------------------------------
    def run(self, install_signals: bool = True) -> int:
        if install_signals:
            def on_sig(signum, _frame):
                self.log.info("收到信号 %s，停止…", signum)
                self.stop.set()

            signal.signal(signal.SIGINT, on_sig)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, on_sig)

        self.setup()
        sensors = self.cfg.resolve_sensors()
        self.log.info(
            "%s 启动 — 相机 %d / IMU %d / 雷达 %d",
            self.client_name,
            len(sensors.cameras),
            len(sensors.imus),
            len(sensors.lidars),
        )

        self._start_readers(sensors)

        rt = self.cfg.runtime
        if rt.duration_sec and rt.duration_sec > 0:
            threading.Timer(float(rt.duration_sec), self.stop.set).start()

        last_status = 0.0
        last_rescan = 0.0
        while not self.stop.is_set():
            self.stop.wait(timeout=0.5)
            now = time.monotonic()
            if now - last_status >= rt.status_interval_sec:
                last_status = now
                with self._counts_lock:
                    snap = dict(self._counts)
                with self._sensor_lock:
                    active = sorted(self._active_sensors)
                self.log.info("已处理: %s（合计 %d）活跃: %s", snap, sum(snap.values()), active)
            if now - last_rescan >= rt.shm_rescan_sec:
                last_rescan = now
                self._maybe_add_new_sensors()

        for t in self._threads:
            t.join(timeout=5.0)
        self.teardown()
        total = sum(self._counts.values())
        self.log.info("%s 退出，累计 %d 帧", self.client_name, total)
        return 0

    def _sensor_key(self, kind: str, name: str) -> str:
        return f"{kind}:{name}"

    def _bump_count(self, key: str) -> None:
        with self._counts_lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def _mark_active(self, kind: str, name: str) -> None:
        key = self._sensor_key(kind, name)
        with self._sensor_lock:
            if key not in self._active_sensors:
                self._active_sensors.add(key)
                self.on_sensor_online(kind, name)

    def _mark_inactive(self, kind: str, name: str) -> None:
        key = self._sensor_key(kind, name)
        with self._sensor_lock:
            if key in self._active_sensors:
                self._active_sensors.discard(key)
                self.on_sensor_offline(kind, name)

    def _start_readers(self, sensors: DiscoveredSensors) -> None:
        refs = {c.name: c for c in self.cfg.enabled_cameras}
        for name in sensors.cameras:
            ref = refs.get(name) or SensorRef(name=name)
            self._spawn_reader("camera", name, lambda r=ref: self._camera_loop(r))

        refs = {i.name: i for i in self.cfg.enabled_imus}
        for name in sensors.imus:
            ref = refs.get(name) or SensorRef(name=name)
            self._spawn_reader("imu", name, lambda r=ref: self._imu_loop(r))

        refs = {l.name: l for l in self.cfg.enabled_lidars}
        for name in sensors.lidars:
            ref = refs.get(name) or SensorRef(name=name)
            self._spawn_reader("lidar", name, lambda r=ref: self._lidar_loop(r))

    def _spawn_reader(self, kind: str, name: str, target: Callable[[], None]) -> None:
        tname = f"{self.client_name}-{kind}-{name}"
        for t in self._threads:
            if t.name == tname:
                return
        t = threading.Thread(target=target, name=tname, daemon=True)
        t.start()
        self._threads.append(t)

    def _maybe_add_new_sensors(self) -> None:
        live = scan_shm_segments(
            camera_prefix=self.cfg.shm.camera,
            imu_prefix=self.cfg.shm.imu,
            lidar_prefix=self.cfg.shm.lidar,
        )
        current = self.cfg.resolve_sensors()
        for name in live.cameras:
            if name not in current.cameras:
                current.cameras.append(name)
        for name in live.imus:
            if name not in current.imus:
                current.imus.append(name)
        for name in live.lidars:
            if name not in current.lidars:
                current.lidars.append(name)
        self._start_readers(current)

    # ---- per-sensor read loops --------------------------------------
    def _camera_loop(self, sensor: SensorRef) -> None:
        from gige_sync.shm.ring import SharedFrameReader

        segment = f"{self.cfg.shm.camera}{sensor.name}"
        self._generic_loop(
            "camera", sensor.name, segment,
            lambda: SharedFrameReader(segment),
            lambda view: self.on_camera(sensor, view),
        )

    def _imu_loop(self, sensor: SensorRef) -> None:
        from imu_sync.shm import SharedImuReader

        segment = f"{self.cfg.shm.imu}{sensor.name}"
        self._generic_loop(
            "imu", sensor.name, segment,
            lambda: SharedImuReader(segment),
            lambda view: self.on_imu(sensor, view),
        )

    def _lidar_loop(self, sensor: SensorRef) -> None:
        from lidar_sync.shm import SharedPointCloudReader

        segment = f"{self.cfg.shm.lidar}{sensor.name}"
        self._generic_loop(
            "lidar", sensor.name, segment,
            lambda: SharedPointCloudReader(segment),
            lambda view: self.on_lidar(sensor, view),
        )

    def _generic_loop(
        self,
        kind: str,
        name: str,
        segment: str,
        open_reader,
        handle,
    ) -> None:
        warned = False
        while not self.stop.is_set():
            reader = None
            try:
                reader = open_reader()
            except FileNotFoundError:
                if not warned:
                    self.log.info("等待共享内存 %s（%s server 是否已启动？）", segment, kind)
                    warned = True
                self._mark_inactive(kind, name)
                self.stop.wait(timeout=1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                self.log.warning("打开 %s 失败: %s", segment, exc)
                self._mark_inactive(kind, name)
                self.stop.wait(timeout=1.0)
                continue

            warned = False
            self.log.info("已附着 %s", segment)
            self._mark_active(kind, name)
            last_seq = reader.latest_seq()
            try:
                while not self.stop.is_set():
                    view = reader.read_new(last_seq, stop=self.stop, timeout=2.0)
                    if view is None:
                        continue
                    last_seq = view.meta.seq
                    try:
                        handle(view)
                        self._bump_count(self._sensor_key(kind, name))
                    except Exception as exc:  # noqa: BLE001
                        self.log.error("处理 %s/%s 失败: %s", kind, name, exc, exc_info=True)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("读取 %s 中断，将重新附着: %s", segment, exc)
            finally:
                self._mark_inactive(kind, name)
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(timeout=1.0)
