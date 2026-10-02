"""采集 server 编排：多雷达 worker、动态增删、Web 管理。"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path
from typing import List, Optional

import yaml

from ..config import AppConfig, ConfigError, LidarConfig
from ..discovery import discover_on_subnet, resolve_lidars
from ..logging_setup import get_logger
from ..shm import SharedPointCloudWriter
from .driver_binary import find_driver_binary
from .event_log import GLOBAL_EVENT_LOG
from .web_admin import WebAdminServer, build_status_payload
from .worker import LidarWorker

LOG = get_logger("server")


class CaptureServer:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._workers: List[LidarWorker] = []
        self._writers: List[SharedPointCloudWriter] = []
        self._web: Optional[WebAdminServer] = None
        self._lock = threading.Lock()
        self._config_path: Optional[Path] = None
        self._driver_binary = find_driver_binary(cfg.capture.driver_binary)

    def set_config_path(self, path: Path) -> None:
        self._config_path = path

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def states(self) -> List[dict]:
        return [w.state.snapshot() for w in self._workers]

    def _resolve_runtime_lidars(self) -> List[LidarConfig]:
        """UDP 自动发现 + MAC 绑定，填充 IP/端口到运行时配置。"""
        cap = self.cfg.capture
        discovered = []
        if cap.auto_discover and not cap.mock:
            subnet = cap.discover_subnet or "*"
            LOG.info("UDP 嗅探子网 %s（MSOP=%dB DIFOP=%dB）…", subnet, 1200, 256)
            discovered = discover_on_subnet(self.cfg)
            for d in discovered:
                LOG.info(
                    "  发现雷达 ip=%s mac=%s msop=%d difop=%d",
                    d.ip, d.mac or "-", d.msop_port, d.difop_port,
                )
            if not discovered:
                LOG.warning("  未发现符合特征的雷达 UDP 流量")

        resolved = resolve_lidars(self.cfg, discovered)
        out: List[LidarConfig] = []
        for r in resolved:
            r.config.params.update({
                "lidar_ip": r.ip,
                "mac": r.mac,
                "msop_port": r.msop_port,
                "difop_port": r.difop_port,
                "host_address": r.host_address,
                "group_address": r.group_address,
                "lidar_type": r.lidar_type,
            })
            LOG.info(
                "  -> %s ip=%s mac=%s msop=%d difop=%d group=%s shm=%s",
                r.config.name,
                r.ip or "(待发现)",
                r.mac or "-",
                r.msop_port,
                r.difop_port,
                r.group_address,
                self.cfg.shm.segment_name(r.config.name),
            )
            out.append(r.config)
        return out

    def _spawn_worker(self, lidar_cfg: LidarConfig) -> LidarWorker:
        shm = self.cfg.shm
        name = shm.segment_name(lidar_cfg.name)
        writer = SharedPointCloudWriter(name, shm.slot_count, shm.slot_capacity_bytes, create=True)
        self._writers.append(writer)
        worker = LidarWorker(
            lidar_cfg, self.cfg.capture, writer,
            shm_name=name,
            driver_binary=self._driver_binary,
            app_cfg=self.cfg,
        )
        self._workers.append(worker)
        return worker

    def _startup_probe(self) -> None:
        """启动前快速验证（已由 _resolve_runtime_lidars 完成主要发现）。"""
        pass

    def _build(self) -> None:
        cap = self.cfg.capture
        lidars = self._resolve_runtime_lidars() if cap.auto_discover else self.cfg.enabled_lidars
        if cap.probe_on_start and not cap.auto_discover:
            self._startup_probe()
        LOG.info(
            "采集 server：%d 路雷达, hz=%d（网格对齐）, mock=%s, auto_discover=%s",
            len(lidars), cap.hz, cap.mock, cap.auto_discover,
        )
        for i, lidar_cfg in enumerate(lidars):
            if i > 0:
                time.sleep(cap.connect_stagger_sec)
            self._spawn_worker(lidar_cfg)

        if self.cfg.web.enabled:
            self._web = WebAdminServer(self.cfg.web.host, self.cfg.web.port, self)
            self._web.start()

    def add_lidar(self, lidar_cfg: LidarConfig) -> bool:
        with self._lock:
            for w in self._workers:
                if w.lidar_cfg.name == lidar_cfg.name:
                    return False
            self.cfg.lidars.append(lidar_cfg)
            worker = self._spawn_worker(lidar_cfg)
            worker.start(self._stop)
            GLOBAL_EVENT_LOG.add(lidar_cfg.name, "info", "add", "已通过 Web 添加雷达")
            self._persist_config()
            return True

    def remove_lidar(self, name: str) -> bool:
        with self._lock:
            for i, w in enumerate(self._workers):
                if w.lidar_cfg.name == name:
                    w.stop_process()
                    w.join(timeout=5.0)
                    self._workers.pop(i)
                    if i < len(self._writers):
                        try:
                            self._writers[i].unlink()
                        except Exception:  # noqa: BLE001
                            pass
                        self._writers.pop(i)
                    self.cfg.lidars = [l for l in self.cfg.lidars if l.name != name]
                    GLOBAL_EVENT_LOG.add(name, "info", "remove", "已移除雷达")
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
                "lidars": [l.params | {"name": l.name, "enabled": l.enabled, "frame_id": l.frame_id}
                           for l in self.cfg.lidars],
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

    def start(self) -> None:
        self._build()
        for w in self._workers:
            w.start(self._stop)

    def join(self, timeout: Optional[float] = None) -> None:
        for w in self._workers:
            w.join(timeout=timeout)

    def close(self) -> None:
        for w in self._workers:
            w.stop_process()
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

    def discover_subnet(self, subnet: Optional[str] = None) -> list:
        from ..discovery import enrich_mac_from_arp, discover_lidars_udp, invalidate_discover_cache
        invalidate_discover_cache()
        sub = subnet or self.cfg.capture.discover_subnet
        return enrich_mac_from_arp(discover_lidars_udp(subnet=sub))
