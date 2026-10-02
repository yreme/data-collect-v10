"""单 IMU 采集线程：串口 / TCP / UDP 或 mock 模式。"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig, CaptureConfig, ImuConfig
from ..discovery import probe_imu, resolve_imus
from ..imu_codec import ImuSample, mock_imu_sample
from ..logging_setup import get_logger
from ..protocol import YesenseDecoder
from ..shm import SharedImuWriter
from ..sync_grid import advance_sync_trigger_ms, align_up_sync_ms, hz_to_period_ms
from .event_log import GLOBAL_EVENT_LOG


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
    serial_port: str = ""
    mac: str = ""
    transport: str = ""
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
                "serial_port": self.serial_port,
                "mac": self.mac,
                "transport": self.transport,
                "absent": self.absent,
            }


class _Transport:
    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def read(self, timeout: float = 0.05) -> bytes:
        raise NotImplementedError


class _SerialTransport(_Transport):
    def __init__(self, port: str, baudrate: int) -> None:
        self._port = port
        self._baudrate = baudrate
        self._ser = None

    def open(self) -> None:
        import serial
        self._ser = serial.Serial(self._port, self._baudrate, timeout=0.05)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._ser = None

    def read(self, timeout: float = 0.05) -> bytes:
        if self._ser is None:
            return b""
        return self._ser.read_all() or b""


class _TcpTransport(_Transport):
    def __init__(self, ip: str, port: int) -> None:
        self._ip = ip
        self._port = port
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        s = socket.create_connection((self._ip, self._port), timeout=5.0)
        s.settimeout(0.05)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
        self._sock = None

    def read(self, timeout: float = 0.05) -> bytes:
        if self._sock is None:
            return b""
        try:
            return self._sock.recv(65535)
        except socket.timeout:
            return b""
        except Exception:  # noqa: BLE001
            return b""


class _UdpTransport(_Transport):
    def __init__(
        self,
        ip: str,
        port: int,
        host: str,
        src_port: int = 0,
        device: Optional[str] = None,
    ) -> None:
        self._ip = ip
        self._port = port
        self._host = host or "0.0.0.0"
        self._src_port = src_port
        self._device = device
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self._device:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self._device.encode())
            except OSError as exc:
                raise OSError(f"无法绑定网卡 {self._device}: {exc}") from exc
        s.bind((self._host, self._port))
        s.settimeout(0.05)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
        self._sock = None

    def read(self, timeout: float = 0.05) -> bytes:
        if self._sock is None:
            return b""
        try:
            data, addr = self._sock.recvfrom(65535)
            if self._ip and addr[0] != self._ip:
                return b""
            if self._src_port and addr[1] != self._src_port:
                return b""
            # 首次收到数据时记录源 IP（自动发现模式）
            if not self._ip and addr[0]:
                self._ip = addr[0]
            return data
        except socket.timeout:
            return b""
        except Exception:  # noqa: BLE001
            return b""


class ImuWorker:
    def __init__(
        self,
        imu_cfg: ImuConfig,
        cap_cfg: CaptureConfig,
        writer: SharedImuWriter,
        *,
        shm_name: str,
        app_cfg: Optional[AppConfig] = None,
    ) -> None:
        self.imu_cfg = imu_cfg
        self.cap_cfg = cap_cfg
        self._app_cfg = app_cfg
        self._writer = writer
        self._shm_name = shm_name
        self.state = WorkerState(
            name=imu_cfg.name,
            mac=str(imu_cfg.get("mac") or ""),
        )
        self.log = get_logger(f"worker.{imu_cfg.name}")
        self._thread: Optional[threading.Thread] = None
        self._transport: Optional[_Transport] = None
        self._decoder = YesenseDecoder()
        self._had_frames = False

    def _refresh_runtime(self) -> bool:
        if self._app_cfg is None or not self.cap_cfg.auto_discover:
            return self._apply_static()
        resolved = resolve_imus(self._app_cfg)
        for r in resolved:
            if r.config.name != self.imu_cfg.name:
                continue
            self.imu_cfg.params.update({
                "mac": r.mac,
                "transport": r.transport,
                "imu_ip": r.imu_ip,
                "port": r.port,
                "serial_port": r.serial_port,
                "baudrate": r.baudrate,
                "host_address": r.host_address,
            })
            self.state.set(
                resolved_ip=r.imu_ip or "",
                serial_port=r.serial_port or "",
                mac=r.mac or self.imu_cfg.mac,
                transport=r.transport,
            )
            if r.serial_port or r.imu_ip:
                return True
            if r.transport == "udp" and r.port:
                return True
            return False
        return self._apply_static()

    def _apply_static(self) -> bool:
        transport = self.imu_cfg.transport
        if transport == "auto":
            transport = "serial" if self.imu_cfg.serial_port else "udp"
        self.state.set(
            resolved_ip=self.imu_cfg.imu_ip,
            serial_port=self.imu_cfg.serial_port,
            transport=transport,
            mac=self.imu_cfg.mac,
        )
        if self.imu_cfg.serial_port or self.imu_cfg.imu_ip:
            return True
        # UDP 监听模式：即使未发现 IP，也可绑定本机端口等待数据
        if transport == "udp" and self.imu_cfg.port:
            return True
        return False

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, args=(stop,),
            name=f"imu-{self.imu_cfg.name}", daemon=True,
        )
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop_transport(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        self._transport = None

    def _retry_delay(self) -> float:
        snap = self.state.snapshot()
        if snap.get("absent"):
            return self.cap_cfg.absent_retry_sec
        return self.cap_cfg.reconnect_interval_sec

    def _probe(self) -> bool:
        pr = probe_imu(self.imu_cfg)
        if pr.ip:
            self.state.set(resolved_ip=pr.ip)
        if pr.mac:
            self.state.set(mac=pr.mac)
        if pr.serial_port:
            self.state.set(serial_port=pr.serial_port)
        if not pr.reachable:
            self.state.set(status="absent", connected=False, absent=True, last_error=pr.reason)
            return False
        if not pr.ok_to_capture:
            self.state.set(status="degraded", connected=False, absent=False, last_error=pr.reason)
            GLOBAL_EVENT_LOG.add(self.imu_cfg.name, "warning", "probe", pr.reason)
            return False
        self.state.set(absent=False)
        return True

    def _make_transport(self) -> Optional[_Transport]:
        transport = self.imu_cfg.transport
        if transport == "auto":
            transport = "serial" if self.imu_cfg.serial_port else "udp"
        if transport == "serial":
            port = self.imu_cfg.serial_port
            if not port:
                return None
            return _SerialTransport(port, self.imu_cfg.baudrate)
        ip = self.imu_cfg.imu_ip
        port = self.imu_cfg.port
        if transport == "udp":
            host = self.imu_cfg.host_address or "0.0.0.0"
            device = None
            if self._app_cfg and self._app_cfg.capture.bind_device:
                device = self._app_cfg.capture.bind_device
            elif ip:
                from ..discovery.net_util import interface_for_peer
                device = interface_for_peer(ip) or None
            return _UdpTransport(
                ip or "",
                port,
                host,
                self.imu_cfg.imu_src_port,
                device=device,
            )
        if not ip:
            return None
        return _TcpTransport(ip, port)

    def _run_loop(self, stop: threading.Event) -> None:
        if self.cap_cfg.mock:
            self._mock_loop(stop)
        else:
            self._capture_loop(stop)

    def _mock_loop(self, stop: threading.Event) -> None:
        self.state.set(status="running", connected=True, transport="mock")
        period_ms = hz_to_period_ms(self.cap_cfg.hz)
        frame = 0
        next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
        while not stop.is_set():
            now_ms = int(time.time() * 1000)
            if now_ms >= next_slot_ms:
                sample = mock_imu_sample(frame)
                self._publish(sample, next_slot_ms, now_ms, frame)
                frame += 1
                next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)
            wait_ms = max(1, next_slot_ms - int(time.time() * 1000))
            stop.wait(timeout=min(wait_ms, 50) / 1000.0)

    def _publish(self, sample: ImuSample, trigger_ms: int, recv_ms: int, frame: int) -> None:
        try:
            data = sample.pack()
            self._writer.publish(
                data,
                tid=sample.tid,
                trigger_ms=trigger_ms,
                recv_ms=recv_ms,
                frame_num=frame,
            )
        except Exception as exc:  # noqa: BLE001
            self.state.incr("errors")
            self.state.set(last_error=str(exc))
            self.log.error("写入 SHM 失败: %s", exc)
            return
        self._had_frames = True
        self.state.incr("frames")
        self.state.set(last_publish_ms=trigger_ms, status="running")

    def _capture_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self._refresh_runtime():
                self.state.set(
                    status="absent", connected=False, absent=True,
                    last_error="未发现匹配 IMU",
                )
                stop.wait(timeout=self._retry_delay())
                continue

            if self.cap_cfg.probe_on_start and not self.cap_cfg.auto_discover:
                if not self._probe():
                    stop.wait(timeout=self._retry_delay())
                    continue

            transport = self._make_transport()
            if transport is None:
                self.state.set(status="error", last_error="无法创建传输层")
                stop.wait(timeout=self._retry_delay())
                continue

            self.state.set(status="connecting", connected=False)
            try:
                transport.open()
            except Exception as exc:  # noqa: BLE001
                self.state.incr("errors")
                self.state.set(last_error=str(exc), status="degraded")
                GLOBAL_EVENT_LOG.add(self.imu_cfg.name, "error", "connect", str(exc))
                stop.wait(timeout=self._retry_delay())
                continue

            self._transport = transport
            self._decoder = YesenseDecoder()
            self.state.set(status="running", connected=True)
            GLOBAL_EVENT_LOG.add(
                self.imu_cfg.name, "info", "connect",
                f"已连接 transport={self.imu_cfg.transport}",
            )

            period_ms = hz_to_period_ms(self.cap_cfg.hz)
            next_slot_ms = align_up_sync_ms(int(time.time() * 1000), period_ms)
            pending_sample: Optional[ImuSample] = None
            frame = 0
            last_frame_mono = time.monotonic()

            while not stop.is_set():
                chunk = transport.read()
                if chunk:
                    if isinstance(transport, _UdpTransport) and transport._ip:
                        self.state.set(resolved_ip=transport._ip)
                        self.imu_cfg.params["imu_ip"] = transport._ip
                    decoded = self._decoder.feed(chunk)
                    if decoded is not None:
                        pending_sample = ImuSample.from_decoder_dict(decoded)
                        last_frame_mono = time.monotonic()
                elif pending_sample is None and self._had_frames:
                    if time.monotonic() - last_frame_mono > 30.0:
                        self.log.warning("%s 超过 30s 无数据，将重连", self.imu_cfg.name)
                        break

                now_ms = int(time.time() * 1000)
                if pending_sample is not None and now_ms >= next_slot_ms:
                    self._publish(pending_sample, next_slot_ms, now_ms, frame)
                    pending_sample = None
                    frame += 1
                    next_slot_ms = advance_sync_trigger_ms(next_slot_ms, period_ms)

                stop.wait(timeout=0.001)

            self.stop_transport()
            self.state.set(connected=False)
            if stop.is_set():
                break
            self.state.incr("reconnects")
            msg = f"连接断开，{self._retry_delay():.0f}s 后重连"
            self.log.warning("%s: %s", self.imu_cfg.name, msg)
            GLOBAL_EVENT_LOG.add(self.imu_cfg.name, "warning", "reconnect", msg)
            self.state.set(status="reconnecting", last_error=msg)
            stop.wait(timeout=self._retry_delay())
