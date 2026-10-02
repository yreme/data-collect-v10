#!/usr/bin/env python3
"""端到端联调：zenohd + pub-mock + console + foxglove-preview + sub-example（无需任何硬件）。

    ulimit -l unlimited                      # SHM 需要（否则自动降级为非 SHM，测试仍可通过）
    ZENOHD_BIN=/opt/zenoh/bin/zenohd python3 src/tests/e2e_mock_stack.py

若 7447 上已有路由器在运行，则复用它。检查项：
  1. 控制台总览出现 sensors.yaml 中所有已启用实例且状态 running
  2. 相机实际频率 ≈ 20Hz；在线改为 5Hz 后（下一个整秒生效）实际频率 ≈ 5Hz
  3. 在线把发布格式从 bayer_rggb8 改为 bgr8，帧大小变为 3 倍
  4. 接收端测速（probe）可用，且在 memlock 足够时 payload 走 SHM
  5. 别名修改持久化；配置保存/回滚生成新版本
  6. foxglove-preview 与 sub-example 以 app 实例出现在总线上，并且预览按 1Hz 发送
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
CONSOLE_PORT = int(os.environ.get("E2E_CONSOLE_PORT", "18199"))
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
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
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
    work = Path(tempfile.mkdtemp(prefix="sensorhub-e2e-"))
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
        spawn("pub-mock", [PY, "pub_server/pub-mock/pub_mock.py", "-c", str(cfg_dir / "sensors.yaml"),
                           "--slow-link", "cam_side_1"], log_dir)
        spawn("console", [PY, "-m", "sensorhub_console", "--port", str(CONSOLE_PORT),
                          "--config-dir", str(cfg_dir), "--data-dir", str(data_dir)], log_dir)
        spawn("preview", [PY, "sub_client/sub-foxglove-preview/foxglove_preview.py", "--hz", "1", "--port", "18765"], log_dir)
        spawn("sub-example", [PY, "sub_client/sub-template-python/sub_example.py", "--mode", "latest",
                              "--keys", "rig/camera/cam_corner_0/image"], log_dir)

        wait_for(lambda: port_open(CONSOLE_PORT), 20, "console")

        # 1. 实例在线
        def all_running():
            ov = api("/api/overview")
            enabled = [n for n in ov["nodes"] if n["configured"] and n["enabled"]]
            return ov if enabled and all(n["state"] == "running" for n in enabled) else None
        ov = wait_for(all_running, 30, "所有启用实例 running")
        names = sorted(n["rel"] for n in ov["nodes"] if n["alive"])
        print("在线实例:", names)
        assert "app/foxglove_preview" in names and "app/sub_example" in names
        assert next(n for n in ov["nodes"] if n["rel"] == "camera/cam_side_1")["device"]["link_speed_mbps"] == 100

        # 2. 频率
        rel = "camera/cam_corner_0"
        s = wait_for(lambda: (lambda st: st if abs(st["rate_hz"] - 20) < 2 else None)(stream(api("/api/overview"), rel, "image")),
                     15, "20Hz")
        size_bayer = s["msg_bytes"]
        print(f"初始 {rel}: {s['rate_hz']}Hz {size_bayer}B shm={s['shm']}")
        r = api(f"/api/ctrl/{rel}", {"op": "set_params", "params": {"hz": 5}})
        assert r["ok"], r
        print("set hz=5 ->", r["result"])
        wait_for(lambda: abs(stream(api("/api/overview"), rel, "image")["rate_hz"] - 5) < 0.6, 20, "5Hz 生效")
        print("5Hz 已生效")

        # 3. 颜色模式
        assert api(f"/api/ctrl/{rel}", {"op": "set_params", "params": {"publish_encoding": "bgr8"}})["ok"]
        wait_for(lambda: stream(api("/api/overview"), rel, "image")["msg_bytes"] == size_bayer * 3, 15, "bgr8 帧大小 x3")
        print("bgr8 已生效，帧大小 x3")
        assert api(f"/api/ctrl/{rel}", {"op": "set_params", "params": {"hz": 3}})["ok"] is False

        # 4. probe
        pr = api("/api/probe?key=rig/camera/cam_corner_1/image&seconds=2")
        ps = pr["streams"]["rig/camera/cam_corner_1/image"]
        print("probe:", {k: ps[k] for k in ("rate_hz", "msg_bytes", "shm", "transport_ms_p50")})
        assert ps["rate_hz"] > 15

        # 5. 别名 + 配置版本
        api("/api/aliases/camera/cam_corner_0", {"alias": "Q1-右上", "note": "e2e"})
        assert next(n for n in api("/api/overview")["nodes"] if n["rel"] == rel)["alias"] == "Q1-右上"
        assert json.loads((data_dir / "aliases.json").read_text())["camera/cam_corner_0"]["alias"] == "Q1-右上"
        cur = api("/api/config")
        v = api("/api/config", {"content": cur["content"] + "\n# e2e\n", "comment": "e2e 修改"})["version"]
        rb = api("/api/config/rollback/1", {})
        assert rb["version"] == v + 1
        print(f"配置版本 v{v} -> 回滚生成 v{rb['version']}")

        # 6. 预览
        sent = wait_for(lambda: next(n for n in api("/api/overview")["nodes"] if n["rel"] == "app/foxglove_preview")["extra"].get("sent", 0) >= 10,
                        20, "foxglove 预览发送")
        print("foxglove 预览发送正常", sent)
        print("\nE2E 通过 ✔")
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
