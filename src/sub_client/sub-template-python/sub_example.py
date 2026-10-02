#!/usr/bin/env python3
"""sub 模板：AI/分析程序从总线取数据的标准写法。

演示三种模式（按需选用）：

1. ``--mode latest``  每路传感器一个工作线程，只处理“最新一帧”（推理速度跟不上采集时自动丢旧帧）
2. ``--mode sync``    多传感器按网格时刻组装同步组（如 6 相机 + 雷达 一起送入融合模型）
3. ``--mode stats``   只读帧头统计频率/延迟（不触碰 payload，零拷贝）

    python sub_example.py --mode latest --keys 'rig/camera/*/image'
    python sub_example.py --mode sync --keys rig/camera/cam_corner_0/image,rig/lidar/lidar0/points --base-hz 10
    python sub_example.py --mode stats --keys 'rig/**'
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common" / "python"))

from sensorhub.header import IMAGE_ENCODINGS  # noqa: E402
from sensorhub.node import SensorNode  # noqa: E402
from sensorhub.session import open_session  # noqa: E402
from sensorhub.sub import Frame, LatestSlot, SyncAssembler, subscribe_frames  # noqa: E402

LOG = logging.getLogger("sub-example")


def frame_to_numpy(fr: Frame) -> np.ndarray:
    """图像帧 -> numpy（H, W, C）。注意 payload() 会拷贝一次，之后可安全长期持有。"""
    h = fr.header
    bpp = IMAGE_ENCODINGS[h.encoding][0]
    arr = np.frombuffer(fr.payload(), dtype=np.uint8)
    return arr.reshape(h.height, h.step // bpp if bpp else h.width, bpp) if bpp > 1 else arr.reshape(h.height, h.step)


def run_latest(session, keys, stop: threading.Event, app: SensorNode) -> None:
    slots: Dict[str, LatestSlot] = defaultdict(LatestSlot)
    workers: Dict[str, threading.Thread] = {}
    proc_stream = app.add_stream("objects", encoding="json", shm=False)

    def worker(key: str) -> None:
        slot = slots[key]
        while not stop.is_set():
            fr = slot.get(timeout=0.5)
            if fr is None:
                continue
            img = frame_to_numpy(fr)
            time.sleep(0.08)  # ← 这里换成真正的推理；模拟 80ms 推理
            proc_stream.publish_json({"src": key, "seq": fr.header.seq, "trigger_ns": fr.header.trigger_ns,
                                      "mean": float(img.mean()), "dropped_total": slot.dropped})

    def on_frame(fr: Frame) -> None:  # zenoh 线程：只做入队
        slots[fr.key].put(fr)
        if fr.key not in workers:
            workers[fr.key] = threading.Thread(target=worker, args=(fr.key,), daemon=True)
            workers[fr.key].start()

    for k in keys:
        subscribe_frames(session, k, on_frame)
    while not stop.wait(2):
        LOG.info("latest: %s", {k.split('/')[-2]: f"dropped={s.dropped}" for k, s in slots.items()})


def run_sync(session, keys, base_hz: float, stop: threading.Event) -> None:
    def on_group(slot_ns: int, group: Dict[str, Frame]) -> None:
        spread = (max(f.header.stamp_ns for f in group.values()) - min(f.header.stamp_ns for f in group.values())) / 1e6
        LOG.info("同步组 %s  %d/%d 路  stamp 跨度 %.2fms", time.strftime("%H:%M:%S", time.localtime(slot_ns / 1e9)),
                 len(group), len(keys), spread)

    asm = SyncAssembler(keys, on_group, base_hz=base_hz, timeout_s=0.5, emit_partial=True)
    for k in keys:
        subscribe_frames(session, k, asm.push, max_hz=base_hz)
    while not stop.wait(1):
        asm.flush()


def run_stats(session, keys, stop: threading.Event) -> None:
    counts: Dict[str, list] = defaultdict(list)

    def on_frame(fr: Frame) -> None:
        if fr.header:
            counts[fr.key].append((fr.recv_ns - fr.header.pub_ns) / 1e6)

    for k in keys:
        subscribe_frames(session, k, on_frame)
    while not stop.wait(2):
        for k, v in sorted(counts.items()):
            if v:
                LOG.info("%-45s %5.1f Hz  传输延迟 p50 %.2fms", k, len(v) / 2, sorted(v)[len(v) // 2])
        counts.clear()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["latest", "sync", "stats"], default="stats")
    ap.add_argument("--keys", default="rig/camera/*/image", help="逗号分隔的 key expr")
    ap.add_argument("--base-hz", type=float, default=10)
    ap.add_argument("--name", default="sub_example", help="本程序在总线上的实例名（kind=app）")
    ap.add_argument("--duration", type=float, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    session = open_session()
    app = SensorNode("app", args.name, session=session, meta={"subscribes": keys, "mode": args.mode}).start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if args.duration:
        threading.Timer(args.duration, stop.set).start()
    if args.mode == "latest":
        run_latest(session, keys, stop, app)
    elif args.mode == "sync":
        run_sync(session, keys, args.base_hz, stop)
    else:
        run_stats(session, keys, stop)
    app.close()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
