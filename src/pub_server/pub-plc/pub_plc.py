#!/usr/bin/env python3
"""pub-plc：727R 起重机 PLC UDP 解析并发布到 Zenoh。

    python pub_plc.py -c ../../configs/sensors.yaml
    python pub_plc.py --mock                    # 无硬件：内部生成模拟 UDP 包

配合 mock_sender（resource 或本目录 tests）联调：
    python mock_sender.py --port 12730 &
    python pub_plc.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SDK = Path(__file__).resolve().parent.parent.parent / "common" / "python"
if _SDK.is_dir():
    sys.path.insert(0, str(_SDK))

from sensorhub import config as C  # noqa: E402
from sensorhub.header import Encoding, Flags, FrameHeader, Kind  # noqa: E402
from sensorhub.node import SensorNode  # noqa: E402
from sensorhub.session import open_session  # noqa: E402

from udp_parser import (  # noqa: E402
    OicrFlags,
    PLC_FIELD_META,
    SmhFlags,
    build_packet,
    flatten_state,
    parse_packet,
)

LOG = logging.getLogger("pub-plc")


class PlcPublisher:
    def __init__(self, dev: Dict[str, Any], session, prefix: str, *, mock: bool = False) -> None:
        self.dev = dev
        self.name = dev["name"]
        self.mock = mock
        self.udp_port = int(dev.get("udp_port", 12730))
        self.health_timeout = float(dev.get("health_timeout_sec", 5))
        self.stop = threading.Event()
        self._parse_errors = 0
        self._packets = 0
        self._last_packet_mono = 0.0
        self._source_ip = ""
        self._latest_state: Optional[Dict[str, Any]] = None

        self.node = SensorNode(
            "plc", self.name, session=session, prefix=prefix, version="pub-plc-0.1",
            meta={"alias": dev.get("alias", ""), "fields": PLC_FIELD_META, "udp_port": self.udp_port},
        )
        self.stream = self.node.add_stream("state", encoding="json", target_hz=2, shm=False)
        self.node.set_device(udp_port=self.udp_port, protocol="727r_udp")
        self.node.add_op("inject_packet", self._op_inject)
        self._thread = threading.Thread(target=self._loop, name=f"plc-{self.name}", daemon=True)
        self._health_thread = threading.Thread(target=self._health_loop, name=f"plc-hlth-{self.name}", daemon=True)

    def _op_inject(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """测试用：注入十六进制 UDP 包。"""
        raw = bytes.fromhex(str(args.get("hex", "")))
        pkt = parse_packet(raw, "inject", 0, time.time())
        if pkt is None:
            return {"ok": False, "error": "解析失败"}
        self._on_packet(pkt)
        return {"ok": True, "state": self._latest_state}

    def start(self) -> None:
        self.node.start()
        self._thread.start()
        self._health_thread.start()

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=2)
        self._health_thread.join(timeout=2)
        self.node.close()

    def _on_packet(self, pkt) -> None:
        self._packets += 1
        self._last_packet_mono = time.monotonic()
        self._source_ip = pkt.source_ip
        state = flatten_state(pkt)
        self._latest_state = state
        stamp_ns = time.time_ns()
        hdr = FrameHeader(
            kind=Kind.PLC, encoding=Encoding.JSON, seq=self.stream.next_seq(),
            trigger_ns=stamp_ns, stamp_ns=stamp_ns, count=1,
            flags=Flags.CLOCK_HOST | (Flags.MOCK if self.mock else 0),
        )
        self.stream.publish(json.dumps(state, separators=(",", ":")).encode(), hdr)
        self.node.set_device(source_ip=self._source_ip, source_port=pkt.source_port)
        if self.node.state != "running":
            self.node.set_state("running")
            self.node.emit_event("info", "UDP 数据恢复")
        self.node.extra.update({"packets": self._packets, "parse_errors": self._parse_errors})

    def _mock_frame(self, t: float, heart_beat: int) -> bytes:
        cycle = t % 120.0
        smh = SmhFlags(
            spr_sp20=cycle < 40,
            spr_sp40=40 <= cycle < 80,
            spr_sp45=cycle >= 80,
            mh_spr_lcked=cycle > 15,
            mh_spr_unlcked=cycle < 10,
            hoist_up=math.sin(t * 0.35) > 0,
        )
        oicr = OicrFlags(outside=(int(t) % 20) < 10, crane_left=math.sin(t * 0.08) > 0.2)
        return build_packet(
            smh, oicr,
            int(8000 + 3500 * math.sin(t * 0.35)),
            int(12000 + 5000 * math.sin(t * 0.22 + 1.2)),
            int(45000 + 8000 * math.sin(t * 0.08)),
            int(2500 + 1200 * abs(math.sin(t * 0.5))),
            heart_beat,
        )

    def _mock_loop(self) -> None:
        self.node.set_state("running")
        self.node.set_device(transport="mock")
        heart = 0
        start = time.time()
        interval = 0.5
        while not self.stop.is_set():
            now = time.time()
            raw = self._mock_frame(now - start, heart)
            pkt = parse_packet(raw, "127.0.0.1", self.udp_port, now)
            if pkt:
                self._on_packet(pkt)
            heart = (heart + 1) & 0xFFFF
            self.stop.wait(interval)

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError as exc:
            self.node.set_state("error", f"无法绑定 UDP {self.udp_port}: {exc}")
            self.node.error(str(exc))
            return
        sock.settimeout(0.5)
        self.node.set_state("running", f"监听 UDP :{self.udp_port}")
        LOG.info("%s 监听 UDP :%d", self.name, self.udp_port)

        while not self.stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                self.node.error(f"recv 失败: {exc}")
                continue
            now = time.time()
            pkt = parse_packet(data, addr[0], addr[1], now)
            if pkt is None:
                self._parse_errors += 1
                continue
            self._on_packet(pkt)
        sock.close()

    def _loop(self) -> None:
        if self.mock:
            self._mock_loop()
        else:
            self._udp_loop()

    def _health_loop(self) -> None:
        while not self.stop.wait(1.0):
            if self.mock:
                continue
            if self._last_packet_mono <= 0:
                self.node.set_state("degraded", f"等待 UDP 数据（:{self.udp_port}）")
                continue
            elapsed = time.monotonic() - self._last_packet_mono
            if elapsed > self.health_timeout:
                msg = f"超过 {self.health_timeout:.0f}s 未收到 UDP 包"
                if self.node.state == "running":
                    self.node.emit_event("warn", "health timeout", elapsed=elapsed)
                self.node.set_state("degraded", msg)
            self.node.extra["seconds_since_packet"] = round(elapsed, 2)


def build(cfg: Dict[str, Any], session, prefix: str, args) -> List[PlcPublisher]:
    devs = C.devices(cfg, "plcs", host=C.host_id(cfg) if not args.ignore_host else None)
    if args.only:
        names = set(args.only.split(","))
        devs = [d for d in devs if d["name"] in names]
    return [PlcPublisher(d, session, prefix, mock=args.mock) for d in devs]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--mock", action="store_true")
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
        LOG.error("没有可启动的 PLC 实例")
        return 1
    for p in pubs:
        p.start()
    LOG.info("已启动 %d 个 PLC pub，prefix=%s mock=%s", len(pubs), prefix, args.mock)

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
