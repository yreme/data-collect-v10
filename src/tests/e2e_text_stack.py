#!/usr/bin/env python3
"""端到端联调：zenohd + pub-imu(mock) + pub-plc(mock) + foxglove-preview + console。

验证 IMU/PLC 文本数据可在 Foxglove 预览（无需相机/雷达硬件）。

    ulimit -l unlimited
    ZENOHD_BIN=/opt/zenoh/bin/zenohd python3 src/tests/e2e_text_stack.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
PY = sys.executable
ENV = {**os.environ, "PYTHONPATH": f"{SRC / 'common' / 'python'}:{SRC / 'console'}", "PYTHONUNBUFFERED": "1"}
CONSOLE_PORT = int(os.environ.get("E2E_CONSOLE_PORT", "18198"))
FOXGLOVE_PORT = int(os.environ.get("E2E_FOXGLOVE_PORT", "18764"))
BASE = f"http://127.0.0.1:{CONSOLE_PORT}"
procs = []


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn(name, args, log_dir):
    log = open(log_dir / f"{name}.log", "w")
    p = subprocess.Popen(args, env=ENV, stdout=log, stderr=subprocess.STDOUT, cwd=str(SRC))
    procs.append((name, p))
    return p


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if r.headers.get("content-type", "").startswith("application/json") else raw.decode()


def wait_for(pred, timeout, what):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            last = pred()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise AssertionError(f"超时等待: {what}（最后结果 {last!r}）")


def stream(ov, rel, ch):
    return next(s for s in ov["streams"] if s["rel"] == rel and s["channel"] == ch)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sensorhub-text-e2e-"))
    cfg_dir, data_dir, log_dir = work / "configs", work / "data", work / "logs"
    cfg_dir.mkdir()
    log_dir.mkdir()
    shutil.copy(SRC / "configs" / "sensors.yaml", cfg_dir / "sensors.yaml")
    print(f"工作目录 {work}")
    try:
        if not port_open(7447):
            zbin = os.environ.get("ZENOHD_BIN") or shutil.which("zenohd") or "/opt/zenoh/bin/zenohd"
            spawn("zenohd", [zbin, "-c", str(SRC / "broker" / "config" / "zenohd.json5")], log_dir)
            wait_for(lambda: port_open(7447), 10, "zenohd 7447")

        spawn("pub-imu", [PY, "pub_server/pub-imu/pub_imu.py", "-c", str(cfg_dir / "sensors.yaml"), "--mock"], log_dir)
        spawn("pub-plc", [PY, "pub_server/pub-plc/pub_plc.py", "-c", str(cfg_dir / "sensors.yaml"), "--mock"], log_dir)
        spawn("console", [PY, "-m", "sensorhub_console", "--port", str(CONSOLE_PORT),
                          "--config-dir", str(cfg_dir), "--data-dir", str(data_dir)], log_dir)
        spawn("preview", [
            PY, "sub_client/sub-foxglove-preview/foxglove_preview.py",
            "--hz", "2", "--port", str(FOXGLOVE_PORT),
            "--keys", "rig/imu/*/data,rig/plc/*/state",
        ], log_dir)

        wait_for(lambda: port_open(CONSOLE_PORT), 20, "console")

        def imu_plc_running():
            ov = api("/api/overview")
            imu = next((n for n in ov["nodes"] if n["rel"] == "imu/imu0"), None)
            plc = next((n for n in ov["nodes"] if n["rel"] == "plc/crane727r"), None)
            if imu and plc and imu["state"] == "running" and plc["state"] == "running":
                return ov
            return None

        ov = wait_for(imu_plc_running, 25, "IMU/PLC running")
        imu_s = stream(ov, "imu/imu0", "data")
        plc_s = stream(ov, "plc/crane727r", "state")
        print(f"IMU: {imu_s['rate_hz']:.1f}Hz  PLC: {plc_s['rate_hz']:.1f}Hz")
        assert imu_s["rate_hz"] > 5, f"IMU 频率过低: {imu_s['rate_hz']}"
        assert plc_s["rate_hz"] > 0.5, f"PLC 频率过低: {plc_s['rate_hz']}"

        def foxglove_sent():
            n = next(x for x in api("/api/overview")["nodes"] if x["rel"] == "app/foxglove_preview")
            s = n["extra"].get("sent", 0)
            return s if s >= 5 else None

        sent = wait_for(foxglove_sent, 20, "foxglove 预览发送")
        assert port_open(FOXGLOVE_PORT), "Foxglove WebSocket 端口未监听"
        print(f"Foxglove ws://127.0.0.1:{FOXGLOVE_PORT} 预览已发送 {sent} 条")
        print("\n文本传感器 E2E 通过 ✔")
        return 0
    finally:
        for name, p in reversed(procs):
            p.send_signal(signal.SIGTERM)
        for name, p in reversed(procs):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print(f"日志目录 {log_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
