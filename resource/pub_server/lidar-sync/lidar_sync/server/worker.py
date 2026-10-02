"""单雷达采集线程：C++ lidar-capture 子进程或 mock 模式。

每路雷达独立线程；某路 absent/故障不影响其它路。
"""

from __future__ import annotations

import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig, CaptureConfig, LidarConfig
from ..discovery import probe_lidar, resolve_host_address, resolve_lidar_ip, resolve_lidars
from ..logging_setup import get_logger
from ..pointcloud_codec import mock_pointcloud
from ..shm import SharedPointCloudWriter
from ..sync_grid import advance_sync_trigger_ms, align_up_sync_ms, hz_to_period_ms
from .driver_binary import find_driver_binary
from .event_log import GLOBAL_EVENT_LOG
from .frame_protocol import read_frame


@dataclass
class WorkerState:
    name: str
    status: str = "init"
    connected: bool = False
    frames: int = 0
    errors: int = 0
    reconnects: int = 0
    last_error: str = ""
    last_publish_ms: int = 0
    resolved_ip: str = ""
    mac: str = ""
    absent: bool = False
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
            return {
                "name": self.name,
                "status": self.status,
                "connected": self.connected,
                "frames": self.frames,
                "errors": self.errors,
                "reconnects": self.reconnects,
                "last_error": self.last_error,
                "last_publish_ms": self.last_publish_ms,
                "resolved_ip": self.resolved_ip,
                "mac": self.mac,
                "absent": self.absent,
            }


class LidarWorker:
    def __init__(
        self,
        lidar_cfg: LidarConfig,
        cap_cfg: CaptureConfig,
        writer: SharedPointCloudWriter,
        *,
        shm_name: str,
        driver_binary: Optional[str] = None,
        app_cfg: Optional[AppConfig] = None,
    ) -> None:
        self.lidar_cfg = lidar_cfg
        self.cap_cfg = cap_cfg
        self._app_cfg = app_cfg
        self._writer = writer
        self._shm_name = shm_name
        self._driver_binary = driver_binary
        self.state = WorkerState(
            name=lidar_cfg.name,
            mac=str(lidar_cfg.get("mac") or ""),
        )
        self.log = get_logger(f"worker.{lidar_cfg.name}")
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._driver_error: Optional[str] = None
        self._msop_timeouts = 0
        self._udp_warned = False
        self._had_frames = False

    def _refresh_runtime(self) -> bool:
        """重新 UDP 发现并按 MAC 刷新 IP/端口（雷达改 IP 后仍可工作）。"""
        if self._app_cfg is None or not self.cap_cfg.auto_discover:
            return bool(self._resolve_ip())
        resolved = resolve_lidars(self._app_cfg)
        for r in resolved:
            if r.config.name != self.lidar_cfg.name:
                continue
            self.lidar_cfg.params.update({
                "lidar_ip": r.ip,
                "mac": r.mac,
                "msop_port": r.msop_port,
                "difop_port": r.difop_port,
                "host_address": r.host_address,
                "group_address": r.group_address,
                "lidar_type": r.lidar_type,
            })
            self.state.set(resolved_ip=r.ip or "", mac=r.mac or self.lidar_cfg.mac)
            if r.ip:
                self.log.info(
                    "%s 运行时解析: ip=%s mac=%s msop=%d difop=%d group=%s",
                    self.lidar_cfg.name, r.ip, r.mac or "-",
                    r.msop_port, r.difop_port, r.group_address,
                )
                return True
            return False
        return bool(self._resolve_ip())

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, args=(stop,),
            name=f"lidar-{self.lidar_cfg.name}", daemon=True,
        )
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop_process(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._proc = None

    def _resolve_ip(self) -> str:
        ip = self.lidar_cfg.lidar_ip
        if ip:
            return ip
        mac = self.lidar_cfg.get("mac")
        if mac:
            r = resolve_lidar_ip(mac=mac, subnet=self.cap_cfg.discover_subnet)
            if r:
                self.state.set(resolved_ip=r.ip, mac=r.mac or mac)
                return r.ip
        return ""

    def _probe(self) -> bool:
        """返回 True 表示可以尝试启动采集。"""
        pr = probe_lidar(self.lidar_cfg, subnet=self.cap_cfg.discover_subnet)
        if pr.ip:
            self.state.set(resolved_ip=pr.ip)
        if pr.mac:
            self.state.set(mac=pr.mac)

        if not pr.reachable:
            self.state.set(
                status="absent",
                connected=False,
                absent=True,
                last_error=pr.reason,
            )
            return False

        if not pr.ok_to_capture:
            self.state.set(
                status="degraded",
                connected=False,
                absent=False,
                last_error=pr.reason,
            )
            GLOBAL_EVENT_LOG.add(
                self.lidar_cfg.name, "warning", "port",
                pr.reason, hint="关闭占用端口的其它进程（如 ROS rslidar_sdk）",
            )
            return False

        self.state.set(absent=False)
        return True

    def _retry_delay(self) -> float:
        snap = self.state.snapshot()
        if snap.get("absent"):
            return self.cap_cfg.absent_retry_sec
        return self.cap_cfg.reconnect_interval_sec

    def _run_loop(self, stop: threading.Event) -> None:
        if self.cap_cfg.mock:
            self._mock_loop(stop)
        else:
            self._driver_loop(stop)

    def _mock_loop(self, stop: threading.Event) -> None:
        self.state.set(status="running", connected=True)
        period_ms = hz_to_period_ms(self.cap_cfg.hz)
        self.log.info("mock 模式：合成点云 @ %dHz（网格 %dms）", self.cap_cfg.hz, period_ms)
        frame = 0
        next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
        while not stop.is_set():
            now_ms = int(time.time() * 1000)
            if now_ms >= next_slot_ms:
                data = mock_pointcloud(8000, frame)
                self._writer.publish(
                    data, point_count=8000, width=8000,
                    trigger_ms=next_slot_ms, recv_ms=now_ms, frame_num=frame,
                )
                self.state.incr("frames")
                self.state.set(last_publish_ms=next_slot_ms)
                frame += 1
                next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)
            wait_ms = max(1, next_slot_ms - int(time.time() * 1000))
            stop.wait(timeout=min(wait_ms, 50) / 1000.0)

    def _drain_stderr(self, stop: threading.Event) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            if stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            if "ERROR Driver init failed" in line:
                self._driver_error = line
                self.log.error("%s: %s", self.lidar_cfg.name, line)
                GLOBAL_EVENT_LOG.add(
                    self.lidar_cfg.name, "error", "driver", line,
                    hint="检查端口是否被占用、lidar_type 是否正确",
                )
            elif "ERROR" in line or "bind:" in line:
                self.log.warning("driver: %s", line)
                GLOBAL_EVENT_LOG.add(
                    self.lidar_cfg.name, "warning", "capture", line,
                )
            elif "ERRCODE_MSOPTIMEOUT" in line:
                self._msop_timeouts += 1
                if self._msop_timeouts == 1 or self._msop_timeouts % 10 == 0:
                    host = resolve_host_address(
                        self.lidar_cfg.lidar_ip,
                        self.lidar_cfg.host_address,
                        self.lidar_cfg.group_address,
                    )
                    grp = (self.lidar_cfg.group_address or "0.0.0.0").strip()
                    hint = (
                        f"组播 group={grp} 本机接口={host}"
                        if grp not in ("", "0.0.0.0")
                        else f"目的 IP 应设为 {host or '本机同网段 IP'}"
                    )
                    self.log.warning(
                        "%s: 未收到 MSOP UDP（%s）。%s；端口 MSOP=%d DIFOP=%d",
                        self.lidar_cfg.name,
                        line,
                        hint,
                        self.lidar_cfg.msop_port,
                        self.lidar_cfg.difop_port,
                    )
                    GLOBAL_EVENT_LOG.add(
                        self.lidar_cfg.name, "warning", "udp",
                        f"未收到 MSOP UDP，目的 IP 应设为 {host or '本机同网段 IP'}",
                        hint="雷达 Web → 网络 → 目的 IP/端口",
                    )
            elif line.startswith("PUBLISH "):
                self.log.debug("driver: %s", line)
            else:
                self.log.debug(line)

    def _driver_loop(self, stop: threading.Event) -> None:
        binary = self._driver_binary or find_driver_binary(self.cap_cfg.driver_binary)
        if not binary:
            msg = "未找到 lidar-capture；请先编译 cpp/ 或设置 LIDAR_DRIVER_BINARY"
            self.state.set(status="error", last_error=msg)
            GLOBAL_EVENT_LOG.add(
                self.lidar_cfg.name, "error", "driver", msg,
                hint="cd lidar-sync/cpp && ./build.sh",
            )
            self.log.error(msg)
            return

        while not stop.is_set():
            if not self._refresh_runtime():
                self.state.set(
                    status="absent",
                    connected=False,
                    absent=True,
                    last_error="UDP 未发现匹配雷达",
                )
                self.log.info(
                    "%s 未发现，%ds 后重试",
                    self.lidar_cfg.name, int(self._retry_delay()),
                )
                stop.wait(timeout=self._retry_delay())
                continue

            if self.cap_cfg.probe_on_start and not self.cap_cfg.auto_discover:
                if not self._probe():
                    snap = self.state.snapshot()
                    if snap["status"] == "absent":
                        self.log.info(
                            "%s 不在线，%ds 后重试（其它雷达不受影响）",
                            self.lidar_cfg.name, int(self._retry_delay()),
                        )
                        GLOBAL_EVENT_LOG.add(
                            self.lidar_cfg.name, "info", "absent",
                            snap["last_error"],
                            hint="雷达上线后将自动恢复采集",
                        )
                    stop.wait(timeout=self._retry_delay())
                    continue

            ip = self.lidar_cfg.lidar_ip or self.state.snapshot().get("resolved_ip", "")
            grp = (self.lidar_cfg.group_address or "0.0.0.0").strip()
            host_address = resolve_host_address(
                ip or self.lidar_cfg.lidar_ip,
                self.lidar_cfg.host_address,
                grp,
            )
            self.state.set(resolved_ip=ip or self.state.snapshot().get("resolved_ip", ""),
                           status="connecting", connected=False)
            cmd = [
                binary,
                "--msop-port", str(self.lidar_cfg.msop_port),
                "--difop-port", str(self.lidar_cfg.difop_port),
                "--lidar-type", self.lidar_cfg.lidar_type,
                "--host-address", host_address,
                "--group-address", grp,
                "--hz", "1000",
            ]
            # Python 侧按 sync_grid 网格发布；C++ 仅解码不限速

            self.log.info(
                "启动 %s (雷达 IP=%s, host=%s, group=%s)",
                self.lidar_cfg.name, ip or "未指定", host_address, grp,
            )
            self.log.debug("cmd: %s", " ".join(cmd))
            GLOBAL_EVENT_LOG.add(
                self.lidar_cfg.name, "info", "connect",
                f"启动采集 msop={self.lidar_cfg.msop_port} difop={self.lidar_cfg.difop_port} ip={ip or '-'}",
            )
            try:
                self._driver_error = None
                self._msop_timeouts = 0
                self._udp_warned = False
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except Exception as exc:  # noqa: BLE001
                self.state.incr("errors")
                self.state.set(last_error=str(exc), status="degraded")
                GLOBAL_EVENT_LOG.add(self.lidar_cfg.name, "error", "connect", str(exc))
                stop.wait(timeout=self._retry_delay())
                continue

            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(stop,),
                name=f"stderr-{self.lidar_cfg.name}",
                daemon=True,
            )
            self._stderr_thread.start()
            self.state.set(status="running", connected=True)
            init_failed = False
            no_frame_sec = 0.0
            last_frame_mono = time.monotonic()
            period_ms = hz_to_period_ms(self.cap_cfg.hz)
            next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
            pending_frame: Optional[tuple] = None

            assert self._proc.stdout is not None
            while not stop.is_set() and self._proc.poll() is None:
                if self._driver_error:
                    init_failed = True
                    break
                ready, _, _ = select.select([self._proc.stdout], [], [], 0.05)
                if ready:
                    frame = read_frame(self._proc.stdout)
                    if frame is not None:
                        pending_frame = frame
                        last_frame_mono = time.monotonic()
                        no_frame_sec = 0.0
                elif pending_frame is None:
                    no_frame_sec = time.monotonic() - last_frame_mono
                    if self._had_frames and no_frame_sec > 30.0:
                        self.log.warning("%s 超过 30s 无点云，将重启采集", self.lidar_cfg.name)
                        break

                now_ms = int(time.time() * 1000)
                if pending_frame is not None and now_ms >= next_slot_ms:
                    seq, point_count, data, _ = pending_frame
                    try:
                        self._writer.publish(
                            data,
                            point_count=point_count,
                            width=point_count,
                            trigger_ms=next_slot_ms,
                            recv_ms=now_ms,
                            frame_num=seq,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.state.incr("errors")
                        self.state.set(last_error=str(exc))
                        self.log.error("写入 SHM 失败: %s", exc)
                    else:
                        self._had_frames = True
                        self.state.incr("frames")
                        self.state.set(last_publish_ms=next_slot_ms, status="running")
                        pending_frame = None
                        next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)

            rc = self._proc.poll()
            if rc is None:
                self.stop_process()
                rc = self._proc.poll() if self._proc else -1

            self.stop_process()
            self.state.set(connected=False)

            if stop.is_set():
                break

            if init_failed:
                self.state.incr("errors")
                self.state.set(status="degraded", last_error="Driver init failed")
                stop.wait(timeout=self._retry_delay())
                continue

            self.state.incr("reconnects")
            msg = f"采集进程退出 code={rc}，{self._retry_delay():.0f}s 后重连"
            self.log.warning("%s: %s", self.lidar_cfg.name, msg)
            GLOBAL_EVENT_LOG.add(
                self.lidar_cfg.name, "warning", "reconnect", msg,
                hint="单路故障不影响其它雷达",
            )
            self.state.set(status="reconnecting", last_error=msg)
            stop.wait(timeout=self._retry_delay())
