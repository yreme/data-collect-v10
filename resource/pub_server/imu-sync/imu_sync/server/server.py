"""采集 server 编排：多 IMU worker、动态增删、Web 管理。"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path
from typing import List, Optional

import yaml

from ..config import AppConfig, ConfigError, ImuConfig
from ..discovery import discover_on_subnet, resolve_imus
from ..logging_setup import get_logger
from ..shm import SharedImuWriter
from .event_log import GLOBAL_EVENT_LOG
from .web_admin import WebAdminServer, build_status_payload
from .worker import ImuWorker

LOG = get_logger("server")


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


class CaptureServer:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._workers: List[ImuWorker] = []
        self._writers: List[SharedImuWriter] = []
        self._web: Optional[WebAdminServer] = None
        self._lock = threading.Lock()
        self._config_path: Optional[Path] = None
        self._discovery_thread: Optional[threading.Thread] = None

    def set_config_path(self, path: Path) -> None:
        self._config_path = path

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def states(self) -> List[dict]:
        return [w.state.snapshot() for w in self._workers]

    def _resolve_runtime_imus(self) -> List[ImuConfig]:
        cap = self.cfg.capture
        discovered = []
        if cap.auto_discover and not cap.mock:
            subnet = cap.discover_subnet or "*"
            LOG.info(
                "UDP 嗅探子网 %s（payload %s，本机端口 %s）+ 串口…",
                subnet, cap.discover_packet_sizes, cap.discover_ports,
            )
            discovered = discover_on_subnet(self.cfg)
            for d in discovered:
                LOG.info("  发现: %s", d.to_dict())
            if not discovered:
                from ..discovery.net_util import discovery_failure_hint
                LOG.warning(discovery_failure_hint(subnet))

        resolved = resolve_imus(self.cfg, discovered)
        out: List[ImuConfig] = []
        for r in resolved:
            self._apply_resolved_params(r.config, r)
            shm = self.cfg.shm.segment_name(r.config.name)
            LOG.info(
                "  -> %s transport=%s ip=%s port=%s serial=%s mac=%s shm=%s",
                r.config.name,
                r.transport,
                r.imu_ip or "-",
                r.port or "-",
                r.serial_port or "-",
                r.mac or "-",
                shm,
            )
            out.append(r.config)
        return out

    def _apply_resolved_params(self, imu_cfg: ImuConfig, r) -> None:
        imu_cfg.params.update({
            "mac": r.mac,
            "transport": r.transport,
            "imu_ip": r.imu_ip,
            "port": r.port,
            "serial_port": r.serial_port,
            "baudrate": r.baudrate,
            "host_address": r.host_address,
        })

    def _spawn_worker(self, imu_cfg: ImuConfig) -> ImuWorker:
        shm = self.cfg.shm
        name = shm.segment_name(imu_cfg.name)
        writer = SharedImuWriter(name, shm.slot_count, shm.slot_capacity_bytes, create=True)
        self._writers.append(writer)
        worker = ImuWorker(
            imu_cfg, self.cfg.capture, writer,
            shm_name=name,
            app_cfg=self.cfg,
        )
        self._workers.append(worker)
        return worker

    def _rescan_new_imus(self) -> None:
        cap = self.cfg.capture
        if not cap.auto_discover or cap.mock or cap.discover_rescan_sec <= 0:
            return
        discovered = discover_on_subnet(self.cfg)
        resolved = resolve_imus(self.cfg, discovered)
        with self._lock:
            existing = {w.imu_cfg.name for w in self._workers}
            known_macs = {_norm_mac(w.imu_cfg.mac) for w in self._workers if w.imu_cfg.mac}
            for r in resolved:
                if r.config.name in existing:
                    continue
                mac = _norm_mac(r.mac or r.config.mac)
                if mac and mac in known_macs:
                    continue
                if not r.serial_port and not r.imu_ip:
                    continue
                self._apply_resolved_params(r.config, r)
                if not any(i.name == r.config.name for i in self.cfg.imus):
                    self.cfg.imus.append(r.config)
                worker = self._spawn_worker(r.config)
                worker.start(self._stop)
                if mac:
                    known_macs.add(mac)
                existing.add(r.config.name)
                LOG.info("运行时新发现 IMU %s", r.config.name)
                GLOBAL_EVENT_LOG.add(
                    r.config.name, "info", "discover",
                    f"新 IMU 上线 mac={r.mac or '-'}",
                )

    def _discovery_loop(self) -> None:
        cap = self.cfg.capture
        while not self._stop.is_set():
            self._stop.wait(timeout=max(5.0, cap.discover_rescan_sec))
            if self._stop.is_set():
                break
            try:
                self._rescan_new_imus()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("发现重扫失败: %s", exc)

    def _build(self) -> None:
        cap = self.cfg.capture
        imus = self._resolve_runtime_imus() if cap.auto_discover else self.cfg.enabled_imus
        LOG.info(
            "采集 server：%d 路 IMU, hz=%d, mock=%s, auto_discover=%s",
            len(imus), cap.hz, cap.mock, cap.auto_discover,
        )
        for i, imu_cfg in enumerate(imus):
            if i > 0:
                time.sleep(cap.connect_stagger_sec)
            self._spawn_worker(imu_cfg)
        if self.cfg.web.enabled:
            self._web = WebAdminServer(self.cfg.web.host, self.cfg.web.port, self)
            self._web.start()

    def add_imu(self, imu_cfg: ImuConfig) -> bool:
        with self._lock:
            for w in self._workers:
                if w.imu_cfg.name == imu_cfg.name:
                    return False
            self.cfg.imus.append(imu_cfg)
            worker = self._spawn_worker(imu_cfg)
            worker.start(self._stop)
            GLOBAL_EVENT_LOG.add(imu_cfg.name, "info", "add", "已通过 Web 添加 IMU")
            self._persist_config()
            return True

    def remove_imu(self, name: str) -> bool:
        with self._lock:
            for i, w in enumerate(self._workers):
                if w.imu_cfg.name == name:
                    w.stop_transport()
                    w.join(timeout=5.0)
                    self._workers.pop(i)
                    if i < len(self._writers):
                        try:
                            self._writers[i].unlink()
                        except Exception:  # noqa: BLE001
                            pass
                        self._writers.pop(i)
                    self.cfg.imus = [imu for imu in self.cfg.imus if imu.name != name]
                    GLOBAL_EVENT_LOG.add(name, "info", "remove", "已移除 IMU")
                    self._persist_config()
                    return True
        return False

    def _persist_config(self) -> None:
        if not self._config_path:
            return
        try:
            data = {
                "capture": {
                    "hz": self.cfg.capture.hz,
                    "reconnect_interval_sec": self.cfg.capture.reconnect_interval_sec,
                    "mock": self.cfg.capture.mock,
                    "discover_subnet": self.cfg.capture.discover_subnet,
                },
                "shm": {
                    "name_prefix": self.cfg.shm.name_prefix,
                    "slot_count": self.cfg.shm.slot_count,
                    "slot_capacity_bytes": self.cfg.shm.slot_capacity_bytes,
                },
                "imus": [
                    imu.params | {"name": imu.name, "enabled": imu.enabled, "frame_id": imu.frame_id}
                    for imu in self.cfg.imus
                ],
                "web": {
                    "enabled": self.cfg.web.enabled,
                    "host": self.cfg.web.host,
                    "port": self.cfg.web.port,
                },
            }
            with self._config_path.open("w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("保存配置失败: %s", exc)

    def discover_subnet(self, subnet: Optional[str] = None) -> list:
        from ..discovery import discover_all
        sub = subnet or self.cfg.capture.discover_subnet
        old = self.cfg.capture.discover_subnet
        if sub:
            self.cfg.capture.discover_subnet = sub
        try:
            return [d.to_dict() for d in discover_all(self.cfg)]
        finally:
            self.cfg.capture.discover_subnet = old

    def start(self) -> None:
        self._build()
        for w in self._workers:
            w.start(self._stop)
        cap = self.cfg.capture
        if cap.auto_discover and not cap.mock and cap.discover_rescan_sec > 0:
            self._discovery_thread = threading.Thread(
                target=self._discovery_loop,
                name="imu-discovery",
                daemon=True,
            )
            self._discovery_thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        for w in self._workers:
            w.join(timeout=timeout)
        if self._discovery_thread is not None:
            self._discovery_thread.join(timeout=2.0)

    def close(self) -> None:
        for w in self._workers:
            w.stop_transport()
        for wr in self._writers:
            try:
                wr.close()
            except Exception:  # noqa: BLE001
                pass
        for wr in self._writers:
            try:
                wr.unlink()
            except Exception:  # noqa: BLE001
                pass
        if self._web:
            self._web.stop()

    def run(self) -> int:
        def on_sig(signum, _frame):
            LOG.info("收到信号 %s，停止…", signum)
            self._stop.set()

        signal.signal(signal.SIGINT, on_sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_sig)

        self.start()
        rt = self.cfg.runtime
        if rt.duration_sec and rt.duration_sec > 0:
            threading.Timer(float(rt.duration_sec), self._stop.set).start()

        interval = rt.status_interval_sec
        last = 0.0
        while not self._stop.is_set():
            self._stop.wait(timeout=0.5)
            now = time.monotonic()
            if now - last >= interval:
                last = now
                total = sum(s["frames"] for s in self.states())
                LOG.info("状态: %d 帧合计, workers=%s", total, [s["status"] for s in self.states()])

        self.join(timeout=15.0)
        total = sum(s["frames"] for s in self.states())
        self.close()
        LOG.info("退出，累计帧数: %d", total)
        return 0 if total > 0 or self.cfg.capture.mock else 2
