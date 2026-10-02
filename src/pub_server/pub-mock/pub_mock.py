#!/usr/bin/env python3
"""pub-mock：按 sensors.yaml 模拟全部传感器（相机/雷达/IMU/PLC），用于联调与示范。

它是“如何写一个 pub”的参考实现，真实驱动只需把 ``_capture()`` 换成 SDK 调用：

    python pub_mock.py -c ../../configs/sensors.yaml              # 默认 640x360 小图，省 CPU
    python pub_mock.py -c ../../configs/sensors.yaml --full-res   # 1920x1080
    python pub_mock.py --only camera --slow-link cam_side_1       # 只模拟相机，并模拟某路百兆降速

演示的规范点：
  1. 每个实例一个 SensorNode；进程共享一个 session 与一个 SHM 池
  2. 采集循环由 GridTimer 驱动，trigger_ns=网格时刻，stamp_ns=传感器时刻
  3. 参数通过 ParamSpec 自描述，set_params 在线生效（频率在下一个整秒切换）
  4. 设备信息（ip/mac/link_speed_mbps/rtt_ms）通过 set_device 随 status 发布
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

_SDK = Path(__file__).resolve().parent.parent.parent / "common" / "python"
if _SDK.is_dir():  # 源码树内直接运行；容器内 SDK 已 pip 安装
    sys.path.insert(0, str(_SDK))

from sensorhub import config as C  # noqa: E402
from sensorhub.codecs import IMU_SAMPLE_DTYPE, XYZIRT_DTYPE  # noqa: E402
from sensorhub.header import IMAGE_ENCODINGS, Encoding, Flags, FrameHeader, Kind, encoding_from_name, image_header  # noqa: E402
from sensorhub.node import SensorNode  # noqa: E402
from sensorhub.params import ParamSpec  # noqa: E402
from sensorhub.session import open_session  # noqa: E402
from sensorhub.shm import ShmPool  # noqa: E402
from sensorhub.sync_grid import GridTimer  # noqa: E402

LOG = logging.getLogger("pub-mock")
HZ_CHOICES = [1, 2, 5, 10, 20, 25]


class MockSensor:
    kind = ""

    def __init__(self, dev: Dict[str, Any], session, pool: ShmPool, prefix: str) -> None:
        self.dev = dev
        self.name = dev["name"]
        self.node = SensorNode(self.kind, self.name, session=session, prefix=prefix, shm_pool=pool,
                               version="mock-0.1", meta={"alias": dev.get("alias", ""), "frame_id": dev.get("frame_id", self.name), "mock": True})
        self.stop = threading.Event()
        self.timer = GridTimer(hz=self.initial_hz())
        self._thread = threading.Thread(target=self._loop, name=f"{self.kind}-{self.name}", daemon=True)

    def initial_hz(self) -> float:
        return self.dev.get("hz", 10)

    def start(self) -> None:
        self.node.start()
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=2)
        self.node.close()

    def set_hz(self, hz: float) -> Dict[str, Any]:
        eff = self.timer.set_hz(hz)
        for st in self.node.streams.values():
            st.target_hz = hz
        return {"effective_ns": eff}

    def _loop(self) -> None:
        while not self.stop.is_set():
            slot = self.timer.wait_next(stop=self.stop.is_set)
            if slot is None:
                break
            try:
                self.tick(slot)
            except Exception as exc:  # noqa: BLE001
                self.node.error(f"tick 失败: {exc}")

    def tick(self, slot_ns: int) -> None:
        raise NotImplementedError


class MockCamera(MockSensor):
    kind = "camera"

    def __init__(self, dev, session, pool, prefix, *, full_res: bool, slow: bool) -> None:
        super().__init__(dev, session, pool, prefix)
        self.w, self.h = (dev.get("width", 1920), dev.get("height", 1080)) if full_res else (640, 360)
        self.encoding = encoding_from_name(dev.get("publish_encoding", "bayer_rggb8"))
        self.exposure_us = int(dev.get("exposure_us", 12000))
        self.stream = self.node.add_stream("image", encoding=self.encoding.name.lower(), target_hz=self.initial_hz())
        self.node.set_meta(width=self.w, height=self.h, serial=dev.get("serial", ""))
        idx = int(dev.get("serial", "0")[-3:] or 0) if str(dev.get("serial", "0"))[-3:].isdigit() else 7
        self.node.set_device(ip=f"192.168.1.{100 + idx % 50}", mac=dev.get("mac", f"34:bd:20:00:00:{idx % 256:02x}"),
                             serial=dev.get("serial", ""), model="MV-CA023-10GC(mock)",
                             link_speed_mbps=100 if slow else 1000, expected_link_mbps=1000, rtt_ms=0.3)
        self.node.add_params([
            ParamSpec("hz", "enum", self.initial_hz(), label="采集频率", unit="Hz", choices=HZ_CHOICES, group="采集",
                      help="下一个整秒生效，保持与其它传感器网格对齐"),
            ParamSpec("publish_encoding", "enum", self.encoding.name.lower(), label="发布格式", group="采集",
                      choices=["bayer_rggb8", "bgr8", "mono8"], help="bgr8 为 3 倍数据量"),
            ParamSpec("exposure_auto", "enum", dev.get("exposure_auto", "Continuous"), label="自动曝光",
                      choices=["Off", "Once", "Continuous"], group="曝光"),
            ParamSpec("exposure_us", "int", self.exposure_us, label="曝光时间", unit="us", min=20, max=100000, group="曝光"),
            ParamSpec("gain_db", "float", float(dev.get("gain_db", 0.0)), label="增益", unit="dB", min=0, max=24, step=0.1, group="曝光"),
            ParamSpec("width", "int", self.w, label="宽", readonly=True, group="只读"),
            ParamSpec("height", "int", self.h, label="高", readonly=True, group="只读"),
        ], on_set=self._on_set)
        self.node.add_op("reconnect", lambda _a: {"reconnected": True})
        self._frame_cache: Dict[Encoding, bytes] = {}

    def _on_set(self, ch: Dict[str, Any]):
        out: Dict[str, Any] = {}
        if "hz" in ch:
            out.update(self.set_hz(ch["hz"]))
        if "publish_encoding" in ch:
            self.encoding = encoding_from_name(ch["publish_encoding"])
            self.stream.encoding = ch["publish_encoding"]
        if "exposure_us" in ch:
            self.exposure_us = ch["exposure_us"]
        return out

    def _capture(self, seq: int) -> bytearray:
        bpp = IMAGE_ENCODINGS[self.encoding][0]
        base = self._frame_cache.get(self.encoding)
        if base is None:
            y = np.linspace(0, 255, self.h, dtype=np.uint8)[:, None]
            img = np.repeat(np.broadcast_to(y, (self.h, self.w))[:, :, None], bpp, axis=2)
            base = self._frame_cache[self.encoding] = img.tobytes()
        stripe = (seq * 8) % self.h
        row = self.w * bpp
        buf = bytearray(base)
        buf[stripe * row:(stripe + 4) * row] = b"\xff" * (min(4, self.h - stripe) * row)
        return buf

    def tick(self, slot_ns: int) -> None:
        stamp = slot_ns + random.randint(20_000, 300_000)
        data = self._capture(self.stream.seq)
        time.sleep(self.exposure_us / 1e6 * 0.1)
        hdr = image_header(encoding=self.encoding, width=self.w, height=self.h, seq=self.stream.next_seq(),
                           stamp_ns=stamp, trigger_ns=slot_ns, flags=Flags.SYNC_SOFT_TRIGGER | Flags.MOCK)
        self.stream.publish(data, hdr)


class MockLidar(MockSensor):
    kind = "lidar"

    def __init__(self, dev, session, pool, prefix) -> None:
        super().__init__(dev, session, pool, prefix)
        self.npts = 57_600
        self.stream = self.node.add_stream("points", encoding="xyzirt_f32", target_hz=self.initial_hz())
        self.node.set_meta(lidar_type=dev.get("lidar_type", "RSE1"), msop_port=dev.get("msop_port"))
        self.node.set_device(ip="192.168.1.200", link_speed_mbps=1000, expected_link_mbps=1000, rtt_ms=0.2,
                             sync_mode=dev.get("sync_mode", "phase_lock"))
        self.node.add_params([
            ParamSpec("hz", "enum", self.initial_hz(), label="帧率", unit="Hz", choices=[5, 10, 20], live=False,
                      help="雷达转速需写入设备，重启生效"),
            ParamSpec("phase_lock_deg", "int", int(dev.get("phase_lock_deg", 0)), label="相位锁定角", unit="deg", min=0, max=359),
        ])
        rng = np.random.default_rng(0)
        pts = np.zeros(self.npts, dtype=XYZIRT_DTYPE)
        az = np.linspace(0, 2 * np.pi, self.npts, dtype=np.float32)
        r = 10 + rng.random(self.npts, dtype=np.float32) * 20
        pts["x"], pts["y"], pts["z"] = r * np.cos(az), r * np.sin(az), rng.random(self.npts, dtype=np.float32) * 3
        pts["intensity"] = rng.random(self.npts, dtype=np.float32) * 255
        pts["ring"] = np.arange(self.npts) % 32
        pts["t"] = np.linspace(0, 0.1, self.npts, dtype=np.float32)
        self.pts = pts

    def tick(self, slot_ns: int) -> None:
        self.pts["z"] += np.float32(0.001)
        data = self.pts.tobytes()
        hdr = FrameHeader(kind=Kind.LIDAR, encoding=Encoding.XYZIRT_F32, seq=self.stream.next_seq(),
                          trigger_ns=slot_ns, stamp_ns=slot_ns + random.randint(-200_000, 200_000),
                          width=self.npts, height=1, step=XYZIRT_DTYPE.itemsize, count=self.npts,
                          flags=Flags.SYNC_PHASE_LOCK | Flags.MOCK)
        self.stream.publish(data, hdr)


class MockImu(MockSensor):
    kind = "imu"

    def initial_hz(self) -> float:
        return self.dev.get("publish_hz", 20)

    def __init__(self, dev, session, pool, prefix) -> None:
        super().__init__(dev, session, pool, prefix)
        self.sample_hz = int(dev.get("sample_hz", 200))
        self.stream = self.node.add_stream("data", encoding="imu_v1", target_hz=self.initial_hz(), shm=False)
        self.node.add_params([ParamSpec("publish_hz", "enum", self.initial_hz(), choices=[5, 10, 20, 50], label="打包发布频率")],
                             on_set=lambda ch: self.set_hz(ch["publish_hz"]))

    def tick(self, slot_ns: int) -> None:
        n = max(1, int(self.sample_hz / self.timer.hz))
        arr = np.zeros(n, dtype=IMU_SAMPLE_DTYPE)
        dt = int(1e9 / self.sample_hz)
        arr["stamp_ns"] = slot_ns - dt * np.arange(n)[::-1]
        arr["az"], arr["qw"], arr["temp_c"] = 9.81, 1.0, 35.0
        arr["gx"] = np.sin(arr["stamp_ns"] / 1e9).astype(np.float32) * 0.1
        hdr = FrameHeader(kind=Kind.IMU, encoding=Encoding.IMU_V1, seq=self.stream.next_seq(), trigger_ns=slot_ns,
                          stamp_ns=int(arr["stamp_ns"][-1]), width=n, height=1, step=IMU_SAMPLE_DTYPE.itemsize,
                          count=n, flags=Flags.CLOCK_HOST | Flags.MOCK)
        self.stream.publish(arr.tobytes(), hdr)


class MockPlc(MockSensor):
    kind = "plc"

    def initial_hz(self) -> float:
        return 2

    def __init__(self, dev, session, pool, prefix) -> None:
        super().__init__(dev, session, pool, prefix)
        self.stream = self.node.add_stream("state", encoding="json", target_hz=2, shm=False)
        self.node.set_device(udp_port=dev.get("udp_port"))

    def tick(self, slot_ns: int) -> None:
        t = slot_ns / 1e9
        state = {"heart_beat": self.stream.seq % 256, "mh_pos_m": round(10 + 5 * np.sin(t / 10), 3),
                 "mt_pos_m": round(20 + 3 * np.cos(t / 7), 3), "spreader_locked": bool(int(t) % 20 < 10)}
        hdr = FrameHeader(kind=Kind.PLC, encoding=Encoding.JSON, seq=self.stream.next_seq(), trigger_ns=slot_ns,
                          stamp_ns=time.time_ns(), count=1, flags=Flags.CLOCK_HOST | Flags.MOCK)
        self.stream.publish(json.dumps(state).encode(), hdr)


def build(cfg: Dict[str, Any], args, session, pool, prefix) -> List[MockSensor]:
    out: List[MockSensor] = []
    want = set(args.only.split(",")) if args.only else {"camera", "lidar", "imu", "plc"}
    if "camera" in want:
        cams = C.devices(cfg, "cameras")[: args.max_cameras]
        out += [MockCamera(d, session, pool, prefix, full_res=args.full_res, slow=d["name"] in args.slow_link) for d in cams]
    if "lidar" in want:
        out += [MockLidar(d, session, pool, prefix) for d in C.devices(cfg, "lidars")]
    if "imu" in want:
        out += [MockImu(d, session, pool, prefix) for d in C.devices(cfg, "imus")]
    if "plc" in want:
        out += [MockPlc(d, session, pool, prefix) for d in C.devices(cfg, "plcs")]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=None, help="sensors.yaml（默认 $SENSORHUB_CONFIG）")
    ap.add_argument("--only", default="", help="逗号分隔：camera,lidar,imu,plc")
    ap.add_argument("--full-res", action="store_true", help="相机使用配置分辨率（默认 640x360）")
    ap.add_argument("--max-cameras", type=int, default=16)
    ap.add_argument("--slow-link", default="", help="模拟百兆降速的相机名，逗号分隔")
    ap.add_argument("--shm-mb", type=int, default=256)
    ap.add_argument("--duration", type=float, default=0, help="运行秒数，0=一直运行")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    args.slow_link = set(filter(None, args.slow_link.split(",")))
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = C.load(args.config)
    prefix = C.prefix_of(cfg)
    session = open_session()
    pool = ShmPool(args.shm_mb << 20)
    sensors = build(cfg, args, session, pool, prefix)
    for s in sensors:
        s.start()
    LOG.info("已启动 %d 个模拟实例，prefix=%s，SHM=%s", len(sensors), prefix, pool.active)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait(args.duration or None)
    for s in sensors:
        s.close()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
