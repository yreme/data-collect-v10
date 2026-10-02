#!/usr/bin/env python3
"""sub-foxglove-preview：把总线数据 **低频** 转成 Foxglove WebSocket，用于肉眼查看效果。

- 预览频率与采集频率解耦：默认 1Hz，可设 0.1（每 10 秒一帧）；按全局网格降频，
  所以各路相机/雷达在 Foxglove 里看到的是 **同一拍** 的数据。
- 图像：bayer 原图 2x2 合并成半分辨率 RGB，再按 --max-width 缩小（不依赖 OpenCV）。
- 点云：均匀抽稀到 --max-points。
- 运行中可在控制台（app/foxglove_preview → 传感器控制）在线修改 preview_hz / max_width / max_points。

    python foxglove_preview.py --hz 1 --port 8765
    # Foxglove Studio -> Open connection -> ws://<服务器IP>:8765
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common" / "python"))

from sensorhub import keys as K  # noqa: E402
from sensorhub.codecs import IMU_SAMPLE_DTYPE, POINT_DTYPES  # noqa: E402
from sensorhub.header import Encoding  # noqa: E402
from sensorhub.node import SensorNode  # noqa: E402
from sensorhub.params import ParamSpec  # noqa: E402
from sensorhub.session import open_session  # noqa: E402
from sensorhub.sub import Decimator, Frame, LatestSlot  # noqa: E402

LOG = logging.getLogger("foxglove-preview")

BAYER_OFFSETS = {  # (R 行, R 列, B 行, B 列)
    Encoding.BAYER_RGGB8: (0, 0, 1, 1), Encoding.BAYER_BGGR8: (1, 1, 0, 0),
    Encoding.BAYER_GRBG8: (0, 1, 1, 0), Encoding.BAYER_GBRG8: (1, 0, 0, 1),
}


def to_preview_rgb(fr: Frame, max_width: int):
    """返回 (encoding, width, height, step, bytes)。"""
    h = fr.header
    raw = np.frombuffer(fr.payload(), dtype=np.uint8)
    if h.encoding in BAYER_OFFSETS:
        img = raw.reshape(h.height, h.step)[:, : h.width]
        r0, c0, b0, c1 = BAYER_OFFSETS[h.encoding]
        g = ((img[r0::2, 1 - c0::2].astype(np.uint16) + img[1 - r0::2, c0::2]) // 2).astype(np.uint8)
        rgb = np.dstack([img[r0::2, c0::2], g, img[b0::2, c1::2]])
        enc = "rgb8"
    elif h.encoding in (Encoding.BGR8, Encoding.RGB8):
        rgb = raw.reshape(h.height, h.step // 3, 3)[:, : h.width]
        enc = "bgr8" if h.encoding == Encoding.BGR8 else "rgb8"
    elif h.encoding == Encoding.MONO8:
        rgb = raw.reshape(h.height, h.step)[:, : h.width, None]
        enc = "mono8"
    else:
        return None
    k = max(1, int(np.ceil(rgb.shape[1] / max_width)))
    rgb = np.ascontiguousarray(rgb[::k, ::k])
    return enc, rgb.shape[1], rgb.shape[0], rgb.shape[1] * rgb.shape[2], rgb.tobytes()


class Preview:
    def __init__(self, session, prefix: str, args) -> None:
        import foxglove
        from foxglove import channels as fc
        from foxglove import messages as fm

        self.fg, self.fc, self.fm = foxglove, fc, fm
        self.ctx = foxglove.Context()
        self.server = foxglove.start_server(name="sensorhub-preview", host=args.host, port=args.port, context=self.ctx)
        self.session, self.prefix = session, prefix
        self.hz, self.max_width, self.max_points = args.hz, args.max_width, args.max_points
        self.decimator = Decimator(self.hz)
        self.slots: Dict[str, LatestSlot] = defaultdict(LatestSlot)
        self.channels: Dict[str, object] = {}
        self.stop = threading.Event()
        self._wake = threading.Event()
        self.sent = 0
        self._subs = [session.declare_subscriber(k, self._on_sample) for k in args.keys]
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

        self.node = SensorNode("app", "foxglove_preview", session=session, prefix=prefix,
                               meta={"ws": f"ws://<host>:{args.port}", "subscribes": args.keys})
        self.node.add_params([
            ParamSpec("preview_hz", "enum", self.hz, choices=[0.1, 0.2, 0.5, 1, 2, 5], label="预览频率", unit="Hz"),
            ParamSpec("max_width", "int", self.max_width, min=160, max=1920, label="图像最大宽度", unit="px"),
            ParamSpec("max_points", "int", self.max_points, min=1000, max=500000, label="点云最大点数"),
        ], on_set=self._on_set)
        self.node.set_device(foxglove_port=args.port)
        self.node.start()
        LOG.info("Foxglove WebSocket ws://%s:%d  预览频率 %sHz  订阅 %s", args.host, args.port, self.hz, args.keys)

    def _on_set(self, ch):
        if "preview_hz" in ch:
            self.hz = ch["preview_hz"]
            self.decimator = Decimator(self.hz)
        self.max_width = ch.get("max_width", self.max_width)
        self.max_points = ch.get("max_points", self.max_points)

    def _on_sample(self, sample) -> None:
        fr = Frame.from_sample(sample)
        if fr.header is None or K.channel_of(fr.key) in K.RESERVED_CHANNELS:
            return
        if self.decimator.accept(fr.key, fr.time_ns):
            self.slots[fr.key].put(fr)
            self._wake.set()

    def _loop(self) -> None:
        while not self.stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            for key, slot in list(self.slots.items()):
                fr = slot.get(timeout=0)
                if fr is not None:
                    try:
                        self._publish(fr)
                        self.sent += 1
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("转换 %s 失败: %s", key, exc)
            self.node.extra["sent"] = self.sent

    def _ts(self, ns: int):
        return self.fm.Timestamp(sec=ns // 1_000_000_000, nsec=ns % 1_000_000_000)

    def _topic(self, key: str) -> str:
        return "/" + key[len(self.prefix) + 1:] if key.startswith(self.prefix + "/") else "/" + key

    def _channel(self, key: str, factory):
        ch = self.channels.get(key)
        if ch is None:
            ch = self.channels[key] = factory(self._topic(key))
        return ch

    def _publish(self, fr: Frame) -> None:
        h = fr.header
        stamp = h.stamp_ns or h.trigger_ns or fr.recv_ns
        frame_id = fr.key.split("/")[-2]
        if h.encoding in (Encoding.BAYER_RGGB8, Encoding.BAYER_BGGR8, Encoding.BAYER_GBRG8, Encoding.BAYER_GRBG8,
                          Encoding.BGR8, Encoding.RGB8, Encoding.MONO8):
            out = to_preview_rgb(fr, self.max_width)
            if out is None:
                return
            enc, w, hh, step, data = out
            ch = self._channel(fr.key, lambda t: self.fc.RawImageChannel(t, context=self.ctx))
            ch.log(self.fm.RawImage(timestamp=self._ts(stamp), frame_id=frame_id, width=w, height=hh,
                                    encoding=enc, step=step, data=data), log_time=stamp)
        elif h.encoding == Encoding.JPEG:
            ch = self._channel(fr.key, lambda t: self.fc.CompressedImageChannel(t, context=self.ctx))
            ch.log(self.fm.CompressedImage(timestamp=self._ts(stamp), frame_id=frame_id, format="jpeg",
                                           data=fr.payload()), log_time=stamp)
        elif h.encoding in POINT_DTYPES:
            pts = np.frombuffer(fr.payload(), dtype=POINT_DTYPES[h.encoding])
            step = max(1, len(pts) // self.max_points)
            sel = pts[::step]
            xyzi = np.empty(len(sel), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("intensity", "<f4")])
            for f in ("x", "y", "z", "intensity"):
                xyzi[f] = sel[f]
            F = self.fm.PackedElementFieldNumericType.Float32
            ch = self._channel(fr.key, lambda t: self.fc.PointCloudChannel(t, context=self.ctx))
            ch.log(self.fm.PointCloud(
                timestamp=self._ts(stamp), frame_id=frame_id, point_stride=16,
                pose=self.fm.Pose(position=self.fm.Vector3(x=0, y=0, z=0), orientation=self.fm.Quaternion(x=0, y=0, z=0, w=1)),
                fields=[self.fm.PackedElementField(name=n, offset=4 * i, type=F) for i, n in enumerate(("x", "y", "z", "intensity"))],
                data=xyzi.tobytes()), log_time=stamp)
        elif h.encoding == Encoding.IMU_V1:
            arr = np.frombuffer(fr.payload(), dtype=IMU_SAMPLE_DTYPE)
            if len(arr):
                last = {n: arr[-1][n].item() for n in IMU_SAMPLE_DTYPE.names}
                last["samples_in_packet"] = len(arr)
                self._channel(fr.key, lambda t: self.fg.Channel(t, message_encoding="json", context=self.ctx)).log(last, log_time=stamp)
        elif h.encoding == Encoding.JSON:
            self._channel(fr.key, lambda t: self.fg.Channel(t, message_encoding="json", context=self.ctx)).log(
                json.loads(fr.payload()), log_time=stamp)

    def close(self) -> None:
        self.stop.set()
        for s in self._subs:
            s.undeclare()
        self.node.close()
        self.server.stop()


def main(argv=None) -> Optional[int]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys", default="", help="逗号分隔 key expr，默认订阅 {prefix} 下相机/雷达/IMU/PLC")
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--hz", type=float, default=1.0, help="预览频率，可为 0.1（10 秒一帧）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--max-width", type=int, default=640)
    ap.add_argument("--max-points", type=int, default=60000)
    ap.add_argument("--duration", type=float, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prefix = K.get_prefix(args.prefix)
    args.keys = [k.strip() for k in args.keys.split(",") if k.strip()] or [
        f"{prefix}/camera/*/image", f"{prefix}/camera/*/preview", f"{prefix}/lidar/*/points",
        f"{prefix}/imu/*/data", f"{prefix}/plc/*/state"]

    session = open_session()
    pv = Preview(session, prefix, args)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait(args.duration or None)
    LOG.info("已发送 %d 条预览消息", pv.sent)
    pv.close()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
