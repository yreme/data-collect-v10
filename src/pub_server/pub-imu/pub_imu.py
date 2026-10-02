#!/usr/bin/env python3
"""pub-imu：Yesense IMU 采集，按网格打包发布到 Zenoh。

    python pub_imu.py -c ../../configs/sensors.yaml
    python pub_imu.py --mock                    # 无硬件联调
    python pub_imu.py --only imu0               # 只打开指定实例

数据流：
  1. 采集线程持续读取 UDP/串口，解码 Yesense 帧，写入样本缓冲
  2. 发布线程由 GridTimer 驱动，每个网格时刻把 (slot-period, slot] 内的样本打包发布
  3. trigger_ns = 网格时刻；stamp_ns = 批内首样本时间戳
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import numpy as np

_SDK = Path(__file__).resolve().parent.parent.parent / "common" / "python"
if _SDK.is_dir():
    sys.path.insert(0, str(_SDK))

from sensorhub import config as C  # noqa: E402
from sensorhub.codecs import IMU_SAMPLE_DTYPE, ImuSample, pack_imu  # noqa: E402
from sensorhub.header import Encoding, Flags, FrameHeader, Kind  # noqa: E402
from sensorhub.node import SensorNode  # noqa: E402
from sensorhub.params import ParamSpec  # noqa: E402
from sensorhub.session import open_session  # noqa: E402
from sensorhub.sync_grid import GridTimer, period_ns  # noqa: E402

from yesense import YesenseDecoder  # noqa: E402

LOG = logging.getLogger("pub-imu")
PUBLISH_HZ_CHOICES = [1, 2, 5, 10, 20, 50]
RECONNECT_SEC = 2.0
NO_DATA_TIMEOUT_SEC = 30.0


@dataclass
class RawSample:
    stamp_ns: int
    decoded: Dict[str, Any]
    recv_ns: int


class ClockFit:
    """设备 smp_timestamp (us) -> 主机 Unix ns 的线性拟合。"""

    def __init__(self) -> None:
        self._pairs: Deque[tuple[int, int]] = deque(maxlen=200)
        self._offset_ns: Optional[int] = None

    def update(self, dev_us: int, host_ns: int) -> None:
        if dev_us <= 0:
            return
        self._pairs.append((dev_us, host_ns))
        if len(self._pairs) >= 2:
            d0, h0 = self._pairs[0]
            d1, h1 = self._pairs[-1]
            if d1 != d0:
                slope = (h1 - h0) / (d1 - d0)
                self._offset_ns = int(h0 - slope * d0)

    def map(self, dev_us: int, fallback_ns: int) -> int:
        if dev_us > 0 and self._offset_ns is not None:
            d0, h0 = self._pairs[0]
            d1, h1 = self._pairs[-1]
            if d1 != d0:
                slope = (h1 - h0) / (d1 - d0)
                return int(slope * dev_us + self._offset_ns)
        return fallback_ns


def decoded_to_sample(decoded: Dict[str, Any], stamp_ns: int) -> ImuSample:
    return ImuSample(
        stamp_ns=stamp_ns,
        ax=float(decoded.get("acc_x", 0.0)),
        ay=float(decoded.get("acc_y", 0.0)),
        az=float(decoded.get("acc_z", 0.0)),
        gx=float(decoded.get("gyro_x", 0.0)),
        gy=float(decoded.get("gyro_y", 0.0)),
        gz=float(decoded.get("gyro_z", 0.0)),
        qw=float(decoded.get("q0", 1.0)),
        qx=float(decoded.get("q1", 0.0)),
        qy=float(decoded.get("q2", 0.0)),
        qz=float(decoded.get("q3", 0.0)),
        temp_c=float(decoded.get("sensor_temp", 0.0)),
        status=int(decoded.get("status", 0)),
    )


class _Transport:
    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def read(self) -> bytes:
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

    def read(self) -> bytes:
        if self._ser is None:
            return b""
        return self._ser.read_all() or b""


class _UdpTransport(_Transport):
    def __init__(self, ip: str, port: int, host: str, src_port: int = 0) -> None:
        self._ip = ip
        self._port = port
        self._host = host or "0.0.0.0"
        self._src_port = src_port
        self._sock: Optional[socket.socket] = None
        self.resolved_ip = ip

    def open(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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

    def read(self) -> bytes:
        if self._sock is None:
            return b""
        try:
            data, addr = self._sock.recvfrom(65535)
            if self._ip and addr[0] != self._ip:
                return b""
            if self._src_port and addr[1] != self._src_port:
                return b""
            if not self._ip:
                self._ip = addr[0]
                self.resolved_ip = addr[0]
            return data
        except socket.timeout:
            return b""
        except Exception:  # noqa: BLE001
            return b""


class ImuPublisher:
    def __init__(self, dev: Dict[str, Any], session, prefix: str, *, mock: bool = False) -> None:
        self.dev = dev
        self.name = dev["name"]
        self.mock = mock
        self.sample_hz = int(dev.get("sample_hz", 200))
        self.publish_hz = float(dev.get("publish_hz", 20))
        self.transport_name = dev.get("transport", "udp")
        self.stop = threading.Event()
        self._buf_lock = threading.Lock()
        self._samples: Deque[RawSample] = deque(maxlen=max(2000, self.sample_hz * 5))
        self._clock = ClockFit()
        self._decoder = YesenseDecoder()
        self._parse_errors = 0
        self._gaps = 0
        self._last_recv_mono = 0.0
        self._reconnects = 0

        self.node = SensorNode(
            "imu", self.name, session=session, prefix=prefix, version="pub-imu-0.1",
            meta={"alias": dev.get("alias", ""), "frame_id": dev.get("frame_id", self.name),
                  "sample_hz": self.sample_hz, "transport": self.transport_name},
        )
        self.stream = self.node.add_stream("data", encoding="imu_v1", target_hz=self.publish_hz, shm=False)
        self.timer = GridTimer(hz=self.publish_hz)
        self.node.set_device(transport=self.transport_name, port=dev.get("port"), sample_hz=self.sample_hz)
        self.node.add_params([
            ParamSpec("publish_hz", "enum", self.publish_hz, label="打包发布频率", unit="Hz",
                      choices=PUBLISH_HZ_CHOICES, group="采集",
                      help="下一个整秒生效；批大小 = sample_hz / publish_hz"),
            ParamSpec("sample_hz", "int", self.sample_hz, label="设备采样率", unit="Hz", readonly=True, group="只读"),
            ParamSpec("transport", "enum", self.transport_name, label="传输方式",
                      choices=["udp", "serial"], live=False, group="连接"),
        ], on_set=self._on_set)
        self._capture_thread = threading.Thread(target=self._capture_loop, name=f"imu-cap-{self.name}", daemon=True)
        self._publish_thread = threading.Thread(target=self._publish_loop, name=f"imu-pub-{self.name}", daemon=True)

    def _on_set(self, ch: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if "publish_hz" in ch:
            eff = self.timer.set_hz(ch["publish_hz"])
            self.publish_hz = ch["publish_hz"]
            self.stream.target_hz = self.publish_hz
            out["effective_ns"] = eff
        return out

    def start(self) -> None:
        self.node.start()
        self._capture_thread.start()
        self._publish_thread.start()

    def close(self) -> None:
        self.stop.set()
        self._capture_thread.join(timeout=2)
        self._publish_thread.join(timeout=2)
        self.node.close()

    def _make_transport(self) -> Optional[_Transport]:
        if self.mock:
            return None
        if self.transport_name == "serial":
            port = self.dev.get("serial_device")
            if not port:
                return None
            return _SerialTransport(port, int(self.dev.get("baudrate", 460800)))
        return _UdpTransport(
            self.dev.get("imu_ip", ""),
            int(self.dev.get("port", 2368)),
            self.dev.get("host_address", "0.0.0.0"),
            int(self.dev.get("imu_src_port", 0)),
        )

    def _ingest(self, decoded: Dict[str, Any], recv_ns: int) -> None:
        dev_us = int(decoded.get("smp_timestamp", 0))
        self._clock.update(dev_us, recv_ns)
        stamp_ns = self._clock.map(dev_us, recv_ns)
        with self._buf_lock:
            self._samples.append(RawSample(stamp_ns=stamp_ns, decoded=decoded, recv_ns=recv_ns))
        self._last_recv_mono = time.monotonic()

    def _mock_capture_loop(self) -> None:
        self.node.set_state("running")
        self.node.set_device(transport="mock")
        dt_ns = int(1e9 / self.sample_hz)
        seq = 0
        while not self.stop.is_set():
            recv_ns = time.time_ns()
            t = seq / self.sample_hz
            decoded = {
                "tid": seq & 0xFFFF,
                "smp_timestamp": int(recv_ns / 1000) & 0xFFFFFFFF,
                "acc_x": 0.1 * math.sin(t),
                "acc_y": 0.1 * math.cos(t),
                "acc_z": 9.81,
                "gyro_x": 0.5 * math.sin(t * 0.5),
                "gyro_y": 0.3 * math.cos(t * 0.5),
                "gyro_z": 0.1,
                "q0": 1.0, "q1": 0.0, "q2": 0.0, "q3": 0.0,
                "sensor_temp": 35.0 + 0.5 * math.sin(t),
                "status": 1,
            }
            self._ingest(decoded, recv_ns)
            seq += 1
            time.sleep(dt_ns / 1e9)

    def _capture_loop(self) -> None:
        if self.mock:
            self._mock_capture_loop()
            return

        while not self.stop.is_set():
            transport = self._make_transport()
            if transport is None:
                self.node.set_state("error", "无法创建传输层（检查 transport/serial_device/port 配置）")
                self.stop.wait(RECONNECT_SEC)
                continue

            self.node.set_state("starting")
            try:
                transport.open()
            except Exception as exc:  # noqa: BLE001
                self.node.error(f"连接失败: {exc}")
                self.node.set_state("degraded", str(exc))
                self.stop.wait(RECONNECT_SEC)
                continue

            self._decoder = YesenseDecoder()
            self.node.set_state("running")
            self.node.emit_event("info", "已连接", transport=self.transport_name)
            if isinstance(transport, _UdpTransport) and transport.resolved_ip:
                self.node.set_device(ip=transport.resolved_ip)

            had_data = False
            while not self.stop.is_set():
                chunk = transport.read()
                recv_ns = time.time_ns()
                if chunk:
                    had_data = True
                    decoded = self._decoder.feed(chunk)
                    if decoded is not None:
                        self._ingest(decoded, recv_ns)
                    elif chunk:
                        self._parse_errors += 1
                elif had_data and self._last_recv_mono > 0:
                    if time.monotonic() - self._last_recv_mono > NO_DATA_TIMEOUT_SEC:
                        self.node.emit_event("warn", "超过 30s 无数据，将重连")
                        break

                self.node.extra.update({
                    "buffered": len(self._samples),
                    "parse_errors": self._parse_errors,
                    "reconnects": self._reconnects,
                })
                time.sleep(0.001)

            transport.close()
            if self.stop.is_set():
                break
            self._reconnects += 1
            self.node.set_state("degraded", "连接断开，重连中")
            self.stop.wait(RECONNECT_SEC)

    def _drain_batch(self, slot_ns: int) -> List[ImuSample]:
        p_ns = period_ns(self.timer.hz)
        lo = slot_ns - p_ns
        batch: List[ImuSample] = []
        with self._buf_lock:
            while self._samples and self._samples[0].stamp_ns <= slot_ns:
                raw = self._samples.popleft()
                if raw.stamp_ns > lo:
                    batch.append(decoded_to_sample(raw.decoded, raw.stamp_ns))
                elif batch:
                    self._gaps += 1
        return batch

    def _publish_loop(self) -> None:
        while not self.stop.is_set():
            slot = self.timer.wait_next(stop=self.stop.is_set)
            if slot is None:
                break
            try:
                batch = self._drain_batch(slot)
                if not batch and self.mock:
                    n = max(1, int(self.sample_hz / self.timer.hz))
                    dt = int(1e9 / self.sample_hz)
                    for i in range(n):
                        t_ns = slot - dt * (n - 1 - i)
                        batch.append(ImuSample(
                            stamp_ns=t_ns, ax=0.0, ay=0.0, az=9.81,
                            gx=math.sin(t_ns / 1e9) * 0.1, temp_c=35.0, status=1,
                        ))
                if not batch:
                    continue
                data = pack_imu(batch)
                n = len(batch)
                hdr = FrameHeader(
                    kind=Kind.IMU, encoding=Encoding.IMU_V1, seq=self.stream.next_seq(),
                    trigger_ns=slot, stamp_ns=batch[0].stamp_ns,
                    width=n, height=1, step=IMU_SAMPLE_DTYPE.itemsize, count=n,
                    flags=Flags.CLOCK_HOST | (Flags.MOCK if self.mock else 0),
                )
                self.stream.publish(data, hdr)
                self.node.extra.update({
                    "last_batch_size": n,
                    "gaps": self._gaps,
                    "expected_batch": max(1, int(self.sample_hz / self.timer.hz)),
                })
            except Exception as exc:  # noqa: BLE001
                self.node.error(f"发布失败: {exc}")


def build(cfg: Dict[str, Any], session, prefix: str, args) -> List[ImuPublisher]:
    devs = C.devices(cfg, "imus", host=C.host_id(cfg) if not args.ignore_host else None)
    if args.only:
        names = set(args.only.split(","))
        devs = [d for d in devs if d["name"] in names]
    return [ImuPublisher(d, session, prefix, mock=args.mock) for d in devs]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--only", default="", help="逗号分隔实例名")
    ap.add_argument("--mock", action="store_true", help="无硬件：生成模拟 IMU 数据")
    ap.add_argument("--ignore-host", action="store_true")
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = C.load(args.config)
    prefix = C.prefix_of(cfg)
    session = open_session()
    pubs = build(cfg, session, prefix, args)
    if not pubs:
        LOG.error("没有可启动的 IMU 实例（检查 sensors.yaml imus.devices.enabled）")
        return 1
    for p in pubs:
        p.start()
    LOG.info("已启动 %d 个 IMU pub，prefix=%s mock=%s", len(pubs), prefix, args.mock)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait(args.duration or None)
    for p in pubs:
        p.close()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
