"""PLC-triggered multimodal collector: PLC from SHM + sensors from SHM -> dual-tier MCAP."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from multimodal_sync.config import AppConfig, SensorRef, load_config
from multimodal_sync.shm_discovery import scan_shm_segments

from plc.foxglove_messages import VelocityTracker
from plc.shm.plc_ring import default_segment_name, unpack_plc_dict
from plc.udp_parser import packet_from_dict, packet_value_signature

from .foxglove_multimodal_recorder import (
    CameraSample,
    FoxgloveMultimodalRecorder,
    ImuSample,
    LidarSample,
    MultimodalCarryover,
    PlcSnapshot,
    mcap_config_from_env,
)

LOG = logging.getLogger("mcap_web.collector")


@dataclass
class CollectorStats:
    running: bool = False
    packets_received: int = 0
    snapshots_written: int = 0
    snapshots_skipped: int = 0
    frame_counts: Dict[str, int] = field(default_factory=dict)
    total_sensor_frames: int = 0
    active_sensors: List[str] = field(default_factory=list)
    error: Optional[str] = None


class SensorCache:
    """Cache latest SHM samples; track seq written to MCAP."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.cameras: Dict[str, CameraSample] = {}
        self.imus: Dict[str, ImuSample] = {}
        self.lidars: Dict[str, LidarSample] = {}
        self._last_written_seq: Dict[str, int] = {}

    def update_camera(self, sensor: SensorRef, fv) -> bool:
        m = fv.meta
        ts_ms = m.trigger_ms or m.recv_ms or m.host_ms
        log_ns = int(ts_ms) * 1_000_000 if ts_ms else int(time.time() * 1e9)
        sample = CameraSample(
            name=sensor.name,
            sensor=sensor,
            encoding=m.encoding,
            data=fv.data,
            log_ns=log_ns,
            seq=m.seq,
            width=int(getattr(m, "width", 0) or 0),
            height=int(getattr(m, "height", 0) or 0),
        )
        with self._lock:
            is_new = sensor.name not in self.cameras
            self.cameras[sensor.name] = sample
        return is_new

    def update_imu(self, sensor: SensorRef, view) -> bool:
        from imu_sync.clients.messages import log_ns_from_view

        log_ns = log_ns_from_view(view)
        sample = ImuSample(
            name=sensor.name,
            sensor=sensor,
            view=view,
            log_ns=log_ns,
            seq=view.meta.seq,
        )
        with self._lock:
            is_new = sensor.name not in self.imus
            self.imus[sensor.name] = sample
        return is_new

    def update_lidar(self, sensor: SensorRef, pc) -> bool:
        m = pc.meta
        ts_ms = m.trigger_ms or m.recv_ms or int(time.time() * 1000)
        log_ns = int(ts_ms) * 1_000_000
        sample = LidarSample(
            name=sensor.name,
            sensor=sensor,
            pc=pc,
            log_ns=log_ns,
            seq=m.seq,
        )
        with self._lock:
            is_new = sensor.name not in self.lidars
            self.lidars[sensor.name] = sample
        return is_new

    def carryover(self) -> MultimodalCarryover:
        with self._lock:
            return MultimodalCarryover(
                cameras=dict(self.cameras),
                imus=dict(self.imus),
                lidars=dict(self.lidars),
            )

    def updated_since_last_write(self) -> tuple[dict[str, CameraSample], dict[str, ImuSample], dict[str, LidarSample]]:
        cameras: dict[str, CameraSample] = {}
        imus: dict[str, ImuSample] = {}
        lidars: dict[str, LidarSample] = {}
        with self._lock:
            for name, sample in self.cameras.items():
                key = f"camera:{name}"
                if sample.seq > self._last_written_seq.get(key, -1):
                    cameras[name] = sample
            for name, sample in self.imus.items():
                key = f"imu:{name}"
                if sample.seq > self._last_written_seq.get(key, -1):
                    imus[name] = sample
            for name, sample in self.lidars.items():
                key = f"lidar:{name}"
                if sample.seq > self._last_written_seq.get(key, -1):
                    lidars[name] = sample
        return cameras, imus, lidars

    def mark_written(
        self,
        cameras: dict[str, CameraSample],
        imus: dict[str, ImuSample],
        lidars: dict[str, LidarSample],
    ) -> None:
        with self._lock:
            for name, sample in cameras.items():
                self._last_written_seq[f"camera:{name}"] = sample.seq
            for name, sample in imus.items():
                self._last_written_seq[f"imu:{name}"] = sample.seq
            for name, sample in lidars.items():
                self._last_written_seq[f"lidar:{name}"] = sample.seq


class PlcMultimodalCollector:
    """Collect SHM sensors and write MCAP only when PLC payload changes."""

    def __init__(
        self,
        *,
        config_path: str,
        plc_shm_segment: str | None = None,
    ) -> None:
        self.config_path = config_path
        self.app_cfg = load_config(config_path)
        self.plc_shm_segment = plc_shm_segment or os.getenv(
            "PLC_SHM_SEGMENT",
            default_segment_name(
                os.getenv("PLC_SHM_PREFIX", "plc_"),
                os.getenv("PLC_SHM_DEVICE", "crane727r"),
            ),
        )
        self.plc_name = self.plc_shm_segment.split("_", 1)[-1] if "_" in self.plc_shm_segment else "plc0"
        self._sensor_append_on_update = os.getenv(
            "MCAP_SENSOR_APPEND_ON_UPDATE", "false"
        ).lower() in {"1", "true", "yes"}
        self.velocity = VelocityTracker()
        self.cache = SensorCache()
        self.recorder = FoxgloveMultimodalRecorder(self.app_cfg, mcap_config_from_env())
        self.stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._counts: Dict[str, int] = {}
        self._counts_lock = threading.Lock()
        self._active_sensors: Set[str] = set()
        self._sensor_lock = threading.Lock()
        self._stats = CollectorStats()
        self._stats_lock = threading.Lock()
        self._last_plc_at: float | None = None
        self._last_plc_snap: PlcSnapshot | None = None
        self._last_status_log = 0.0

    def shm_status(self) -> dict[str, Any]:
        live = scan_shm_segments(
            camera_prefix=self.app_cfg.shm.camera,
            imu_prefix=self.app_cfg.shm.imu,
            lidar_prefix=self.app_cfg.shm.lidar,
        )
        plc_present = os.path.exists(f"/dev/shm/{self.plc_shm_segment}")
        total = live.total + (1 if plc_present else 0)
        msg = (
            f"已发现 {total} 路数据源（PLC {'1' if plc_present else '0'} / "
            f"相机 {len(live.cameras)} / IMU {len(live.imus)} / 雷达 {len(live.lidars)}）"
        )
        if not plc_present:
            msg += "。请先启动 plc-data-collect 写入 PLC 共享内存。"
        return {
            "available": live.total > 0 or plc_present,
            "plc_segment": self.plc_shm_segment,
            "plc_available": plc_present,
            "cameras": live.cameras,
            "imus": live.imus,
            "lidars": live.lidars,
            "total": total,
            "message": msg,
        }

    def _set_error(self, message: str) -> None:
        with self._stats_lock:
            self._stats.error = message

    def _bump_count(self, key: str) -> None:
        with self._counts_lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def _mark_active(self, kind: str, name: str) -> None:
        key = f"{kind}:{name}"
        with self._sensor_lock:
            if key not in self._active_sensors:
                self._active_sensors.add(key)

    def _mark_inactive(self, kind: str, name: str) -> None:
        key = f"{kind}:{name}"
        with self._sensor_lock:
            self._active_sensors.discard(key)

    def processing_stats(self) -> dict[str, Any]:
        with self._counts_lock:
            counts = dict(self._counts)
        with self._sensor_lock:
            active = sorted(self._active_sensors)
        with self._stats_lock:
            return {
                "counts": counts,
                "total_frames": sum(counts.values()),
                "active_sensors": active,
                "snapshots_written": self._stats.snapshots_written,
                "snapshots_skipped": self._stats.snapshots_skipped,
                "packets_received": self._stats.packets_received,
            }

    def get_status(self) -> dict[str, Any]:
        shm = self.shm_status()
        with self._stats_lock:
            stats = CollectorStats(
                running=self._stats.running,
                packets_received=self._stats.packets_received,
                snapshots_written=self._stats.snapshots_written,
                snapshots_skipped=self._stats.snapshots_skipped,
                frame_counts=dict(self._counts),
                total_sensor_frames=sum(self._counts.values()),
                active_sensors=sorted(self._active_sensors),
                error=self._stats.error,
            )
        return {
            "running": stats.running,
            "plc_shm_segment": self.plc_shm_segment,
            "packets_received": stats.packets_received,
            "snapshots_written": stats.snapshots_written,
            "snapshots_skipped": stats.snapshots_skipped,
            "frame_counts": stats.frame_counts,
            "total_sensor_frames": stats.total_sensor_frames,
            "active_sensors": stats.active_sensors,
            "cached_sensors": {
                "cameras": sorted(self.cache.cameras.keys()),
                "imus": sorted(self.cache.imus.keys()),
                "lidars": sorted(self.cache.lidars.keys()),
            },
            "error": stats.error,
            "last_plc_at": self._last_plc_at,
            "shm": shm,
            "mcap": self.recorder.get_status(),
        }

    def _handle_packet(self, packet) -> None:
        with self._stats_lock:
            self._stats.packets_received += 1
        self._last_plc_at = time.time()

        velocities = self.velocity.compute(packet)
        stamp_ns = int(packet.received_at * 1_000_000_000)
        health = {
            "state": "connected",
            "message": "PLC 共享内存通路正常",
            "can_collect": True,
            "can_record_mcap": self.recorder.config.enabled,
            "mock_mode": False,
            "running": True,
            "last_packet_at": packet.received_at,
            "seconds_since_last_packet": 0.0,
            "timeout_seconds": float(os.getenv("HEALTH_TIMEOUT", "5")),
        }
        plc_snap = PlcSnapshot(
            packet=packet,
            velocities=velocities,
            health=health,
            log_ns=stamp_ns,
        )
        self._last_plc_snap = plc_snap
        carryover = self.cache.carryover()
        carryover.plc = plc_snap
        snapshot_cams = dict(self.cache.cameras)
        snapshot_imus = dict(self.cache.imus)
        snapshot_lidars = dict(self.cache.lidars)

        signature = packet_value_signature(packet)
        written = self.recorder.write_snapshot(
            carryover,
            signature,
            cameras=snapshot_cams,
            imus=snapshot_imus,
            lidars=snapshot_lidars,
        )
        if written:
            self.cache.mark_written(snapshot_cams, snapshot_imus, snapshot_lidars)
            with self._stats_lock:
                self._stats.snapshots_written += 1
        else:
            with self._stats_lock:
                self._stats.snapshots_skipped += 1

    def _maybe_write_sensor_update(self, reason: str) -> None:
        """Write baseline when a sensor first comes online (does not bypass PLC dedup cadence)."""
        if self._last_plc_snap is None:
            return
        carryover = self.cache.carryover()
        carryover.plc = self._last_plc_snap
        snapshot_cams = dict(self.cache.cameras)
        snapshot_imus = dict(self.cache.imus)
        snapshot_lidars = dict(self.cache.lidars)
        if not snapshot_cams and not snapshot_imus and not snapshot_lidars:
            return
        self.recorder.append_sensor_snapshot(
            carryover,
            cameras=snapshot_cams,
            imus=snapshot_imus,
            lidars=snapshot_lidars,
        )
        self.cache.mark_written(snapshot_cams, snapshot_imus, snapshot_lidars)
        LOG.info(
            "新传感器数据已写入 MCAP (%s): cameras=%s imus=%s lidars=%s",
            reason,
            sorted(snapshot_cams),
            sorted(snapshot_imus),
            sorted(snapshot_lidars),
        )

    def _on_camera_frame(self, sensor: SensorRef, view) -> None:
        is_new = self.cache.update_camera(sensor, view)
        if is_new:
            LOG.info("传感器上线: camera/%s (segment=%s%s)", sensor.name, self.app_cfg.shm.camera, sensor.name)
            self._maybe_write_sensor_update(f"camera/{sensor.name}")
        elif self._sensor_append_on_update:
            updated_cams, updated_imus, updated_lidars = self.cache.updated_since_last_write()
            if updated_cams or updated_imus or updated_lidars:
                self._maybe_write_sensor_update(f"camera/{sensor.name}")

    def _on_imu_frame(self, sensor: SensorRef, view) -> None:
        is_new = self.cache.update_imu(sensor, view)
        if is_new:
            LOG.info("传感器上线: imu/%s (segment=%s%s)", sensor.name, self.app_cfg.shm.imu, sensor.name)
            self._maybe_write_sensor_update(f"imu/{sensor.name}")
        elif self._sensor_append_on_update:
            updated_cams, updated_imus, updated_lidars = self.cache.updated_since_last_write()
            if updated_cams or updated_imus or updated_lidars:
                self._maybe_write_sensor_update(f"imu/{sensor.name}")

    def _on_lidar_frame(self, sensor: SensorRef, view) -> None:
        is_new = self.cache.update_lidar(sensor, view)
        if is_new:
            LOG.info("传感器上线: lidar/%s (segment=%s%s)", sensor.name, self.app_cfg.shm.lidar, sensor.name)
            self._maybe_write_sensor_update(f"lidar/{sensor.name}")
        elif self._sensor_append_on_update:
            updated_cams, updated_imus, updated_lidars = self.cache.updated_since_last_write()
            if updated_cams or updated_imus or updated_lidars:
                self._maybe_write_sensor_update(f"lidar/{sensor.name}")

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        for t in self._threads:
            if t.name == name and t.is_alive():
                return
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _start_sensor_readers(self) -> None:
        live = scan_shm_segments(
            camera_prefix=self.app_cfg.shm.camera,
            imu_prefix=self.app_cfg.shm.imu,
            lidar_prefix=self.app_cfg.shm.lidar,
        )
        sensors = self.app_cfg.resolve_sensors()
        for name in live.cameras:
            if name not in sensors.cameras:
                sensors.cameras.append(name)
        for name in live.imus:
            if name not in sensors.imus:
                sensors.imus.append(name)
        for name in live.lidars:
            if name not in sensors.lidars:
                sensors.lidars.append(name)

        refs = {c.name: c for c in self.app_cfg.enabled_cameras}
        for name in sensors.cameras:
            sensor = refs.get(name) or SensorRef(name=name)
            self._spawn(f"shm-camera-{name}", lambda s=sensor: self._camera_loop(s))

        refs = {i.name: i for i in self.app_cfg.enabled_imus}
        for name in sensors.imus:
            sensor = refs.get(name) or SensorRef(name=name)
            self._spawn(f"shm-imu-{name}", lambda s=sensor: self._imu_loop(s))

        refs = {l.name: l for l in self.app_cfg.enabled_lidars}
        for name in sensors.lidars:
            sensor = refs.get(name) or SensorRef(name=name)
            self._spawn(f"shm-lidar-{name}", lambda s=sensor: self._lidar_loop(s))

        waiting = []
        configured = self.app_cfg.configured_sensors()
        for name in configured.cameras:
            if name not in live.cameras:
                waiting.append(f"camera:{name}")
        for name in configured.imus:
            if name not in live.imus:
                waiting.append(f"imu:{name}")
        for name in configured.lidars:
            if name not in live.lidars:
                waiting.append(f"lidar:{name}")
        if waiting:
            LOG.debug("等待共享内存上线: %s", ", ".join(waiting))

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
                    LOG.info("等待共享内存 %s", segment)
                    warned = True
                self._mark_inactive(kind, name)
                self.stop.wait(timeout=1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.warning("打开 %s 失败: %s", segment, exc)
                self._mark_inactive(kind, name)
                self.stop.wait(timeout=1.0)
                continue

            warned = False
            self._mark_active(kind, name)
            LOG.info("已附着 %s", segment)
            last_seq = reader.latest_seq()
            try:
                while not self.stop.is_set():
                    view = reader.read_new(last_seq, stop=self.stop, timeout=2.0)
                    if view is None:
                        continue
                    last_seq = view.meta.seq
                    handle(view)
                    self._bump_count(f"{kind}:{name}")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("读取 %s 中断: %s", segment, exc)
            finally:
                self._mark_inactive(kind, name)
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(timeout=1.0)

    def _camera_loop(self, sensor: SensorRef) -> None:
        from gige_sync.shm.ring import SharedFrameReader

        segment = f"{self.app_cfg.shm.camera}{sensor.name}"
        self._generic_loop(
            "camera",
            sensor.name,
            segment,
            lambda: SharedFrameReader(segment),
            lambda view: self._on_camera_frame(sensor, view),
        )

    def _imu_loop(self, sensor: SensorRef) -> None:
        from imu_sync.shm import SharedImuReader

        segment = f"{self.app_cfg.shm.imu}{sensor.name}"
        self._generic_loop(
            "imu",
            sensor.name,
            segment,
            lambda: SharedImuReader(segment),
            lambda view: self._on_imu_frame(sensor, view),
        )

    def _lidar_loop(self, sensor: SensorRef) -> None:
        from lidar_sync.shm import SharedPointCloudReader

        segment = f"{self.app_cfg.shm.lidar}{sensor.name}"
        self._generic_loop(
            "lidar",
            sensor.name,
            segment,
            lambda: SharedPointCloudReader(segment),
            lambda view: self._on_lidar_frame(sensor, view),
        )

    def _rescan_loop(self) -> None:
        while not self.stop.wait(self.app_cfg.runtime.shm_rescan_sec):
            self._start_sensor_readers()

    def _status_loop(self) -> None:
        interval = max(1.0, float(self.app_cfg.runtime.status_interval_sec))
        while not self.stop.wait(interval):
            with self._counts_lock:
                counts = dict(self._counts)
            with self._sensor_lock:
                active = sorted(self._active_sensors)
            with self._stats_lock:
                written = self._stats.snapshots_written
                skipped = self._stats.snapshots_skipped
            cached = sorted(self.cache.cameras.keys())
            LOG.info(
                "已处理: %s（合计 %d）活跃: %s 缓存相机: %s snapshots=%d skipped=%d",
                counts,
                sum(counts.values()),
                active,
                cached,
                written,
                skipped,
            )

    def _plc_shm_loop(self) -> None:
        from plc.shm.plc_ring import SharedPlcReader

        segment = self.plc_shm_segment
        warned = False
        while not self.stop.is_set():
            reader = None
            try:
                reader = SharedPlcReader(segment)
            except FileNotFoundError:
                if not warned:
                    LOG.info("等待 PLC 共享内存 %s（请先启动 plc-data-collect）", segment)
                    warned = True
                self._mark_inactive("plc", self.plc_name)
                self.stop.wait(timeout=1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.warning("打开 PLC 共享内存 %s 失败: %s", segment, exc)
                self._mark_inactive("plc", self.plc_name)
                self.stop.wait(timeout=1.0)
                continue

            warned = False
            self._mark_active("plc", self.plc_name)
            last_seq = reader.latest_seq()
            try:
                while not self.stop.is_set():
                    view = reader.read_new(last_seq, stop=self.stop, timeout=2.0)
                    if view is None:
                        continue
                    last_seq = view.meta.seq
                    payload = unpack_plc_dict(view.data)
                    packet = packet_from_dict(payload)
                    if packet.received_at <= 0:
                        packet.received_at = view.meta.recv_ms / 1000.0
                    self._handle_packet(packet)
                    self._bump_count(f"plc:{self.plc_name}")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("读取 PLC 共享内存中断: %s", exc)
            finally:
                self._mark_inactive("plc", self.plc_name)
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(timeout=1.0)

    def start(self) -> None:
        if self._stats.running:
            return
        self.stop.clear()
        self.velocity.reset()
        with self._stats_lock:
            self._stats = CollectorStats(running=True)
        for d in (
            self.recorder.config.hourly_dir,
            self.recorder.config.short_dir,
        ):
            os.makedirs(d, exist_ok=True)
        self._start_sensor_readers()
        self._spawn("shm-rescan", self._rescan_loop)
        self._spawn("shm-status", self._status_loop)
        self._spawn("plc-shm", self._plc_shm_loop)
        LOG.info(
            "PLC(SHM) 触发式多模态采集已启动 segment=%s hourly=%s short=%s",
            self.plc_shm_segment,
            self.recorder.config.hourly_dir,
            self.recorder.config.short_dir,
        )

    def stop_collecting(self) -> None:
        self.stop.set()
        self.recorder.stop()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        with self._stats_lock:
            self._stats.running = False
