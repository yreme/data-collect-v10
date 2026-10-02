"""IMU SHM → dual MCAP + CSV writer with clock-aligned 6h rotation."""

from __future__ import annotations

import csv
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from .rotate import next_window_start, window_id

CSV_FIELDS = [
    "imu_name",
    "seq",
    "trigger_ms",
    "recv_ms",
    "host_ms",
    "frame_num",
    "log_ns",
    "tid",
    "smp_timestamp",
    "ready_timestamp",
    "acc_x",
    "acc_y",
    "acc_z",
    "gyro_x",
    "gyro_y",
    "gyro_z",
    "roll",
    "pitch",
    "yaw",
    "q0",
    "q1",
    "q2",
    "q3",
    "pos_x",
    "pos_y",
    "pos_z",
    "vel_e",
    "vel_n",
    "vel_u",
    "sensor_temp",
    "status",
]


def _mcap_compression(kind: str):
    from foxglove.mcap import MCAPCompression

    k = (kind or "zstd").strip().lower()
    if k in ("none", "off", ""):
        return None
    if k == "lz4":
        return MCAPCompression.Lz4
    return MCAPCompression.Zstd


@dataclass
class ImuRecordConfig:
    output_dir: str = "/data/imu"
    filename_prefix: str = "imu"
    period_hours: int = 6
    timezone: str = ""
    allow_overwrite: bool = True
    compression: str = "zstd"
    topic_prefix: str = "/imu/"
    publish_imu: bool = True
    publish_world_pose: bool = True
    publish_scene: bool = False
    publish_tf: bool = True
    write_mcap: bool = True
    write_csv: bool = True
    imu_prefix: str = "imu_"
    imu_names: List[str] = field(default_factory=lambda: ["imu0"])
    frame_ids: Dict[str, str] = field(default_factory=dict)
    auto_discover_shm: bool = True
    shm_rescan_sec: float = 5.0
    status_interval_sec: float = 5.0


class ImuMcapCsvClient:
    """Read IMU shared-memory segments; write clock-aligned MCAP + CSV."""

    client_name = "imu-record"

    def __init__(self, cfg: ImuRecordConfig) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        self._counts_lock = threading.Lock()
        self._active: Set[str] = set()
        self._active_lock = threading.Lock()

        self._out_dir = Path(cfg.output_dir).expanduser()
        if not self._out_dir.is_absolute():
            self._out_dir = (Path.cwd() / self._out_dir).resolve()

        self._window = -1
        self._mcap_path: Optional[Path] = None
        self._csv_path: Optional[Path] = None
        self._writer = None
        self._csv_fp = None
        self._csv_writer = None
        self._foxglove = None
        self._ctx = None
        self._imu_channels: Dict[str, object] = {}
        self._world_pose_channels: Dict[str, object] = {}
        self._scene_channels: Dict[str, object] = {}
        self._tf_channels: Dict[str, object] = {}
        self._pose_registry = None
        self._known_imus: Set[str] = set(cfg.imu_names)

        import logging

        self.log = logging.getLogger("imu_record.client")

    # ---- status -----------------------------------------------------
    def processing_stats(self) -> dict:
        with self._counts_lock:
            counts = dict(self._counts)
        with self._active_lock:
            active = sorted(self._active)
        with self._lock:
            mcap = str(self._mcap_path) if self._mcap_path else None
            csvp = str(self._csv_path) if self._csv_path else None
            nxt = next_window_start(
                period_hours=self.cfg.period_hours, tz_name=self.cfg.timezone
            )
        return {
            "counts": counts,
            "total_frames": sum(counts.values()),
            "active_sensors": active,
            "current_mcap": mcap,
            "current_csv": csvp,
            "next_rotate_at": nxt.isoformat(),
            "period_hours": self.cfg.period_hours,
        }

    # ---- lifecycle --------------------------------------------------
    def setup(self) -> None:
        if self.cfg.write_mcap:
            import foxglove
            from imu_sync.clients.pose_tracker import PoseTrackerRegistry
            from imu_sync.config import PoseConfig

            self._foxglove = foxglove
            self._ctx = foxglove.Context()
            self._pose_registry = PoseTrackerRegistry(
                PoseConfig(publish_tf=self.cfg.publish_tf)
            )
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self.log.info(
            "IMU 录制输出: %s（每 %dh 整点切分，前缀=%s，mcap=%s csv=%s）",
            self._out_dir,
            self.cfg.period_hours,
            self.cfg.filename_prefix,
            self.cfg.write_mcap,
            self.cfg.write_csv,
        )

    def teardown(self) -> None:
        with self._lock:
            self._close_files()

    def run(self, install_signals: bool = True) -> int:
        if install_signals:
            def on_sig(signum, _frame):
                self.log.info("收到信号 %s，停止…", signum)
                self.stop.set()

            signal.signal(signal.SIGINT, on_sig)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, on_sig)

        self.setup()
        names = self._resolve_imu_names()
        self.log.info("订阅 IMU: %s", names)
        for name in names:
            self._spawn_reader(name)

        last_status = 0.0
        last_rescan = 0.0
        while not self.stop.is_set():
            self.stop.wait(timeout=0.5)
            now = time.monotonic()
            if now - last_status >= self.cfg.status_interval_sec:
                last_status = now
                snap = self.processing_stats()
                self.log.info(
                    "已处理: %s（合计 %d）活跃: %s 下一切分: %s",
                    snap["counts"],
                    snap["total_frames"],
                    snap["active_sensors"],
                    snap["next_rotate_at"],
                )
            if self.cfg.auto_discover_shm and now - last_rescan >= self.cfg.shm_rescan_sec:
                last_rescan = now
                for name in self._resolve_imu_names():
                    self._spawn_reader(name)

        for t in self._threads:
            t.join(timeout=5.0)
        self.teardown()
        total = self.processing_stats()["total_frames"]
        self.log.info("退出，累计 %d 帧", total)
        return 0

    def _resolve_imu_names(self) -> List[str]:
        names = list(self.cfg.imu_names)
        if self.cfg.auto_discover_shm:
            try:
                from multimodal_sync.shm_discovery import scan_shm_segments

                live = scan_shm_segments(imu_prefix=self.cfg.imu_prefix)
                for n in live.imus:
                    if n not in names:
                        names.append(n)
            except Exception:  # noqa: BLE001
                pass
        self._known_imus.update(names)
        return names

    def _spawn_reader(self, name: str) -> None:
        tname = f"{self.client_name}-{name}"
        for t in self._threads:
            if t.name == tname:
                return
        t = threading.Thread(target=self._imu_loop, args=(name,), name=tname, daemon=True)
        t.start()
        self._threads.append(t)

    def _imu_loop(self, name: str) -> None:
        from imu_sync.shm import SharedImuReader

        segment = f"{self.cfg.imu_prefix}{name}"
        warned = False
        while not self.stop.is_set():
            reader = None
            try:
                reader = SharedImuReader(segment)
            except FileNotFoundError:
                if not warned:
                    self.log.info("等待共享内存 %s", segment)
                    warned = True
                with self._active_lock:
                    self._active.discard(name)
                self.stop.wait(timeout=1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                self.log.warning("打开 %s 失败: %s", segment, exc)
                self.stop.wait(timeout=1.0)
                continue

            warned = False
            self.log.info("已附着 %s", segment)
            with self._active_lock:
                self._active.add(name)
            last_seq = reader.latest_seq()
            try:
                while not self.stop.is_set():
                    view = reader.read_new(last_seq, stop=self.stop, timeout=2.0)
                    if view is None:
                        continue
                    last_seq = view.meta.seq
                    try:
                        self._handle(name, view)
                        with self._counts_lock:
                            self._counts[name] = self._counts.get(name, 0) + 1
                    except Exception as exc:  # noqa: BLE001
                        self.log.error("处理 %s 失败: %s", name, exc, exc_info=True)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("读取 %s 中断: %s", segment, exc)
            finally:
                with self._active_lock:
                    self._active.discard(name)
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            self.stop.wait(timeout=1.0)

    # ---- write path -------------------------------------------------
    def _handle(self, name: str, view) -> None:
        from imu_sync.clients.messages import (
            build_frame_transform,
            build_imu_message,
            build_scene_update,
            build_world_pose,
            log_ns_from_view,
            sample_from_view,
        )
        from imu_sync.config import ImuConfig

        sample = sample_from_view(view)
        log_ns = log_ns_from_view(view)
        imu_cfg = ImuConfig(
            name=name,
            frame_id=self.cfg.frame_ids.get(name) or "imu_link",
        )

        with self._lock:
            self._ensure_window()
            if self.cfg.write_mcap and self._writer is not None:
                pose = None
                if self._pose_registry is not None:
                    pose = self._pose_registry.update(imu_cfg, sample, log_ns)
                if self.cfg.publish_imu:
                    self._imu_channel(name).log(
                        build_imu_message(imu_cfg, sample, log_ns), log_time=log_ns
                    )
                if pose is not None and self.cfg.publish_world_pose:
                    self._world_pose_channel(name).log(
                        build_world_pose(pose, log_ns), log_time=log_ns
                    )
                if pose is not None and self.cfg.publish_scene:
                    self._scene_channel(name).log(
                        build_scene_update(imu_cfg, pose, log_ns), log_time=log_ns
                    )
                if pose is not None and self.cfg.publish_tf:
                    self._tf_channel(name).log(
                        build_frame_transform(pose, log_ns), log_time=log_ns
                    )
            if self.cfg.write_csv and self._csv_writer is not None:
                m = view.meta
                self._csv_writer.writerow(
                    {
                        "imu_name": name,
                        "seq": m.seq,
                        "trigger_ms": m.trigger_ms,
                        "recv_ms": m.recv_ms,
                        "host_ms": m.host_ms,
                        "frame_num": m.frame_num,
                        "log_ns": log_ns,
                        "tid": sample.tid,
                        "smp_timestamp": sample.smp_timestamp,
                        "ready_timestamp": sample.ready_timestamp,
                        "acc_x": sample.acc_x,
                        "acc_y": sample.acc_y,
                        "acc_z": sample.acc_z,
                        "gyro_x": sample.gyro_x,
                        "gyro_y": sample.gyro_y,
                        "gyro_z": sample.gyro_z,
                        "roll": sample.roll,
                        "pitch": sample.pitch,
                        "yaw": sample.yaw,
                        "q0": sample.q0,
                        "q1": sample.q1,
                        "q2": sample.q2,
                        "q3": sample.q3,
                        "pos_x": sample.pos_x,
                        "pos_y": sample.pos_y,
                        "pos_z": sample.pos_z,
                        "vel_e": sample.vel_e,
                        "vel_n": sample.vel_n,
                        "vel_u": sample.vel_u,
                        "sensor_temp": sample.sensor_temp,
                        "status": sample.status,
                    }
                )
                if self._csv_fp is not None:
                    self._csv_fp.flush()

    def _ensure_window(self) -> None:
        wid = window_id(
            period_hours=self.cfg.period_hours, tz_name=self.cfg.timezone
        )
        if wid != self._window:
            self._rotate(wid)

    def _close_files(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        if self._csv_fp is not None:
            try:
                self._csv_fp.close()
            except Exception:  # noqa: BLE001
                pass
            self._csv_fp = None
            self._csv_writer = None
        # Channels are bound to context/writer; clear so next rotate recreates.
        self._imu_channels.clear()
        self._world_pose_channels.clear()
        self._scene_channels.clear()
        self._tf_channels.clear()

    def _rotate(self, wid: int) -> None:
        self._close_files()
        # Filename uses the window start (00:00 / 06:00 / 12:00 / 18:00).
        from .rotate import resolve_tz, window_start

        tz = resolve_tz(self.cfg.timezone)
        start = window_start(
            datetime.fromtimestamp(wid, tz=tz),
            period_hours=self.cfg.period_hours,
            tz_name=self.cfg.timezone,
        )
        ts = start.strftime("%Y-%m-%d_%H-%M-%S")
        stem = f"{self.cfg.filename_prefix}_{ts}"

        if self.cfg.write_mcap:
            path = self._out_dir / f"{stem}.mcap"
            from foxglove.mcap import MCAPWriteOptions

            opts = None
            comp = _mcap_compression(self.cfg.compression)
            if comp is not None:
                opts = MCAPWriteOptions(compression=comp)
            kwargs = {
                "allow_overwrite": self.cfg.allow_overwrite,
                "context": self._ctx,
            }
            if opts is not None:
                kwargs["writer_options"] = opts
            # Fresh context per file so channels rebind cleanly.
            self._ctx = self._foxglove.Context()
            self._writer = self._foxglove.open_mcap(str(path), **kwargs)
            self._mcap_path = path
            self.log.info("新 MCAP: %s", path)

        if self.cfg.write_csv:
            path = self._out_dir / f"{stem}.csv"
            mode = "w" if self.cfg.allow_overwrite else "x"
            self._csv_fp = open(path, mode, newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(self._csv_fp, fieldnames=CSV_FIELDS)
            self._csv_writer.writeheader()
            self._csv_fp.flush()
            self._csv_path = path
            self.log.info("新 CSV: %s", path)

        self._window = wid

    def _imu_channel(self, name: str):
        ch = self._imu_channels.get(name)
        if ch is None:
            from foxglove import Channel
            from imu_sync.clients.messages import IMU_JSON_SCHEMA

            topic = f"{self.cfg.topic_prefix.rstrip('/')}/{name}/imu"
            ch = Channel(
                topic,
                schema=IMU_JSON_SCHEMA,
                context=self._ctx,
                metadata={"schema_name": "sensor_msgs/Imu"},
            )
            self._imu_channels[name] = ch
            self.log.info("MCAP 通道: %s", topic)
        return ch

    def _world_pose_channel(self, name: str):
        ch = self._world_pose_channels.get(name)
        if ch is None:
            from foxglove.channels import PoseInFrameChannel

            topic = f"{self.cfg.topic_prefix.rstrip('/')}/{name}/world_pose"
            ch = PoseInFrameChannel(topic, context=self._ctx)
            self._world_pose_channels[name] = ch
        return ch

    def _scene_channel(self, name: str):
        ch = self._scene_channels.get(name)
        if ch is None:
            from foxglove.channels import SceneUpdateChannel

            topic = f"{self.cfg.topic_prefix.rstrip('/')}/{name}/scene"
            ch = SceneUpdateChannel(topic, context=self._ctx)
            self._scene_channels[name] = ch
        return ch

    def _tf_channel(self, name: str):
        ch = self._tf_channels.get(name)
        if ch is None:
            from foxglove.channels import FrameTransformChannel

            topic = f"{self.cfg.topic_prefix.rstrip('/')}/{name}/tf"
            ch = FrameTransformChannel(topic, context=self._ctx)
            self._tf_channels[name] = ch
        return ch
