#!/usr/bin/env python3
"""727R crane PLC UDP monitor with MCAP recording and Foxglove support."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from connection_health import ConnectionHealth, LinkState
from foxglove_messages import VelocityTracker
from mcap_recorder import McapRecorder, mcap_config_from_env
from plc_shm.plc_ring import SharedPlcWriter, default_segment_name, pack_plc_dict
from udp_anomaly import UdpAnomalyTracker, VariableChangeTracker
from udp_parser import OicrFlags, PlcPacket, SmhFlags, build_packet, parse_packet

BUFFER_SECONDS = 300
DISPLAY_HZ = 2.0
HEALTH_CHECK_INTERVAL = 0.5

PRESETS = [
    {"name": "罗东副数据中心 #1", "ip": "10.172.241.102", "port": 12730},
    {"name": "罗东副数据中心 #2", "ip": "10.172.241.103", "port": 12730},
    {"name": "727R 起重机本机", "ip": "10.172.237.32", "port": 12730},
]


class RollingBuffer:
    def __init__(self, max_seconds: float = BUFFER_SECONDS) -> None:
        self.max_seconds = max_seconds
        self._items: deque[dict[str, Any]] = deque()

    def clear(self) -> None:
        self._items.clear()

    def add(self, packet: PlcPacket) -> None:
        self._items.append(packet.to_dict())
        cutoff = time.time() - self.max_seconds
        while self._items and self._items[0]["received_at"] < cutoff:
            self._items.popleft()

    def snapshot(self) -> list[dict[str, Any]]:
        cutoff = time.time() - self.max_seconds
        return [item for item in self._items if item["received_at"] >= cutoff]

    def latest(self) -> dict[str, Any] | None:
        return self._items[-1] if self._items else None


class MonitorState:
    def __init__(self) -> None:
        self.listen_port = 12730
        self.filter_ip = ""
        self.mock_mode = os.getenv("MOCK_MODE", "false").lower() in {"1", "true", "yes"}
        self.running = False
        self.packets_received = 0
        self.last_error = ""
        self.buffer = RollingBuffer()
        self.health = ConnectionHealth(timeout_seconds=float(os.getenv("HEALTH_TIMEOUT", "5")))
        self.velocity = VelocityTracker()
        self.anomaly = UdpAnomalyTracker()
        self.changes = VariableChangeTracker()
        self.mcap = McapRecorder(mcap_config_from_env())
        self.shm_enabled = os.getenv("PLC_SHM_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.shm_segment = os.getenv(
            "PLC_SHM_SEGMENT",
            default_segment_name(
                os.getenv("PLC_SHM_PREFIX", "plc_"),
                os.getenv("PLC_SHM_DEVICE", "crane727r"),
            ),
        )
        self.plc_writer: SharedPlcWriter | None = None
        self.shm_published = 0
        if self.shm_enabled:
            try:
                self.plc_writer = SharedPlcWriter(self.shm_segment, create=True)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"PLC 共享内存初始化失败: {exc}"
        self._udp_thread: threading.Thread | None = None
        self._mock_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def status(self) -> dict[str, Any]:
        health = self.health.snapshot(self.mcap.config.enabled)
        return {
            "listen_port": self.listen_port,
            "filter_ip": self.filter_ip,
            "mock_mode": self.mock_mode,
            "running": self.running,
            "packets_received": self.packets_received,
            "last_error": self.last_error or health.message if health.state in {
                LinkState.DISCONNECTED,
                LinkState.ERROR,
                LinkState.WAITING,
            } and not self.mock_mode else self.last_error,
            "buffer_size": len(self.buffer.snapshot()),
            "latest": self.buffer.latest(),
            "presets": PRESETS,
            "health": health.to_dict(),
            "anomaly": self.anomaly.to_dict(),
            "changes": self.changes.to_dict(),
            "mcap": self.mcap.get_status(),
            "shm": {
                "enabled": self.shm_enabled,
                "segment": self.shm_segment,
                "published": self.shm_published,
                "active": self.plc_writer is not None,
            },
        }

    async def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._loop or not self._clients:
            return
        payload = json.dumps(message, ensure_ascii=False)

        async def _send_all() -> None:
            dead: list[WebSocket] = []
            for ws in list(self._clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

        asyncio.run_coroutine_threadsafe(_send_all(), self._loop)

    def _publish_shm(self, packet: PlcPacket) -> None:
        if self.plc_writer is None:
            return
        recv_ms = int(packet.received_at * 1000)
        try:
            self.plc_writer.publish(
                pack_plc_dict(packet.to_dict()),
                source_port=packet.source_port,
                recv_ms=recv_ms,
                heart_beat=packet.heart_beat,
            )
            self.shm_published += 1
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"PLC 共享内存写入失败: {exc}"

    def _handle_packet(self, packet: PlcPacket) -> None:
        if self.filter_ip and packet.source_ip != self.filter_ip:
            return

        self.health.on_packet()
        health = self.health.snapshot(self.mcap.config.enabled)
        if not health.can_collect:
            return

        self.packets_received += 1
        self.anomaly.on_parsed_packet(packet)
        velocities = self.velocity.compute(packet)
        self.buffer.add(packet)
        recent_changes = self.changes.update(packet)
        self._publish_shm(packet)

        if health.can_record_mcap:
            self.mcap.write_packet(packet, velocities, health.to_dict())

        self._broadcast(
            {
                "type": "packet",
                "packet": packet.to_dict(),
                "velocities": {
                    "mh_vel": round(velocities[0], 2),
                    "mt_vel": round(velocities[1], 2),
                    "mc_vel": round(velocities[2], 2),
                    "cntrh_vel": round(velocities[3], 2),
                    "unit": "mm/s",
                },
                "recent_changes": recent_changes,
                "history": self.buffer.snapshot(),
                "status": self.status(),
            }
        )

    def _broadcast_health(self) -> None:
        self._broadcast({"type": "health", "status": self.status()})

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False
        self.health.stop()
        self.mcap.stop()
        if self.plc_writer is not None:
            try:
                self.plc_writer.close()
            except Exception:  # noqa: BLE001
                pass
            self.plc_writer = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def start(self, listen_port: int, filter_ip: str, mock_mode: bool) -> None:
        self.stop()
        self._stop_event.clear()
        self.listen_port = listen_port
        self.filter_ip = filter_ip.strip()
        self.mock_mode = mock_mode
        self.packets_received = 0
        self.last_error = ""
        self.buffer.clear()
        self.velocity.reset()
        self.anomaly.reset()
        self.changes.reset()
        if self.shm_enabled and self.plc_writer is None:
            try:
                self.plc_writer = SharedPlcWriter(self.shm_segment, create=True)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"PLC 共享内存初始化失败: {exc}"
        self.running = True
        self.health.reset(mock_mode)

        if mock_mode:
            self._mock_thread = threading.Thread(target=self._mock_loop, daemon=True)
            self._mock_thread.start()
        else:
            self._udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
            self._udp_thread.start()

        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def _health_loop(self) -> None:
        last_state = None
        while not self._stop_event.is_set():
            health = self.health.snapshot(self.mcap.config.enabled)
            if health.state != last_state:
                last_state = health.state
                self._broadcast_health()
            elif health.state in {LinkState.WAITING, LinkState.DISCONNECTED}:
                self._broadcast_health()
            time.sleep(HEALTH_CHECK_INTERVAL)

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.listen_port))
            sock.settimeout(1.0)
            self._sock = sock
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                    self.anomaly.on_raw_packet(data, addr[0], addr[1])
                    packet = parse_packet(
                        data,
                        source_ip=addr[0],
                        source_port=addr[1],
                        received_at=time.time(),
                    )
                    if packet:
                        self._handle_packet(packet)
                    else:
                        self.anomaly.on_parse_failed(
                            "UDP 帧解析失败",
                            addr[0],
                            addr[1],
                        )
                        self._broadcast_health()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self.last_error = str(exc)
                        self.health.set_error(str(exc))
                        self._broadcast_health()
                    break
        except OSError as exc:
            self.last_error = str(exc)
            self.health.set_error(f"无法绑定 UDP 端口 {self.listen_port}: {exc}")
            self.running = False
            self._broadcast_health()
        finally:
            sock.close()

    def _mock_loop(self) -> None:
        start = time.time()
        heart_beat = 0
        interval = 1.0 / DISPLAY_HZ
        while not self._stop_event.is_set():
            now = time.time()
            t = now - start
            cycle = t % 120.0
            mh_pos = int(8000 + 3500 * math.sin(t * 0.35))
            mt_pos = int(12000 + 5000 * math.sin(t * 0.22 + 1.2))
            mc_pos = int(45000 + 8000 * math.sin(t * 0.08))
            cntrh_pos = int(2500 + 1200 * abs(math.sin(t * 0.5)))
            smh = SmhFlags(
                spr_sp20=cycle < 40,
                spr_sp40=40 <= cycle < 80,
                spr_sp45=cycle >= 80,
                mh_spr_lcked=cycle > 15,
                mh_spr_unlcked=cycle < 10,
                mh_spr_landed=(cycle % 30) < 8,
                hoist_up=math.sin(t * 0.35) > 0,
                hoist_down=math.sin(t * 0.35) <= 0,
            )
            oicr = OicrFlags(
                outside=(int(t) % 20) < 10,
                inside=(int(t) % 20) >= 10,
                crane_maintain=False,
                crane_left=math.sin(t * 0.08) > 0.2,
                crane_right=math.sin(t * 0.08) < -0.2,
                crane_street=(int(t) % 60) > 50,
            )
            data = build_packet(smh, oicr, mh_pos, mt_pos, mc_pos, cntrh_pos, heart_beat)
            packet = parse_packet(
                data,
                source_ip="127.0.0.1",
                source_port=12730,
                received_at=now,
            )
            if packet:
                self._handle_packet(packet)
            heart_beat = (heart_beat + 1) & 0xFFFF
            time.sleep(interval)


state = MonitorState()
BASE_DIR = Path(__file__).resolve().parent


def _ensure_mcap_dirs(config: McapRecorder) -> None:
    for key in ("hourly_dir", "short_dir"):
        path = Path(getattr(config.config, key))
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = Path(__file__).resolve().parent / "data" / "mcap" / key.replace("_dir", "")
            fallback.mkdir(parents=True, exist_ok=True)
            setattr(config.config, key, str(fallback))


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.set_loop(asyncio.get_running_loop())
    _ensure_mcap_dirs(state.mcap)
    auto_start = os.getenv("AUTO_START", "true").lower() in {"1", "true", "yes"}
    if auto_start:
        state.start(
            listen_port=int(os.getenv("UDP_PORT", "12730")),
            filter_ip=os.getenv("UDP_FILTER_IP", "").strip(),
            mock_mode=state.mock_mode,
        )
    yield
    state.stop()


app = FastAPI(title="727R PLC Monitor", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    with open(BASE_DIR / "static" / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return state.status()


@app.get("/api/history")
async def api_history() -> list[dict[str, Any]]:
    return state.buffer.snapshot()


@app.get("/api/mcap/status")
async def api_mcap_status() -> dict[str, Any]:
    return state.mcap.get_status()


@app.get("/api/mcap/files")
async def api_mcap_files() -> list[dict[str, Any]]:
    return state.mcap.get_status()["files"]


@app.get("/api/mcap/download/{tier}/{filename}")
async def api_mcap_download(tier: str, filename: str) -> FileResponse:
    safe_name = Path(filename).name
    path = state.mcap.resolve_download_path(tier, safe_name)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=safe_name)


@app.get("/foxglove/layout.json")
async def foxglove_layout() -> FileResponse:
    layout = BASE_DIR.parent / "foxglove" / "layout.json"
    return FileResponse(layout, media_type="application/json")


@app.post("/api/start")
async def api_start(body: dict[str, Any]) -> dict[str, Any]:
    listen_port = int(body.get("listen_port", 12730))
    filter_ip = str(body.get("filter_ip", ""))
    mock_mode = bool(body.get("mock_mode", False))
    health_timeout = body.get("health_timeout")
    if health_timeout is not None:
        state.health.timeout_seconds = float(health_timeout)
    state.start(listen_port, filter_ip, mock_mode)
    return state.status()


@app.post("/api/stop")
async def api_stop() -> dict[str, Any]:
    state.stop()
    return state.status()


@app.post("/api/mcap/config")
async def api_mcap_config(body: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])
    for key in (
        "hourly_dir",
        "short_dir",
        "hourly_rotation_seconds",
        "short_rotation_seconds",
        "hourly_retention_days",
        "short_retention_days",
        "list_limit",
    ):
        if key in body:
            kwargs[key] = body[key]
    state.mcap.update_config(**kwargs)
    _ensure_mcap_dirs(state.mcap)
    return state.mcap.get_status()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await state.register(ws)
    await ws.send_text(
        json.dumps(
            {
                "type": "init",
                "history": state.buffer.snapshot(),
                "status": state.status(),
            },
            ensure_ascii=False,
        )
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("action") == "start":
                if msg.get("health_timeout") is not None:
                    state.health.timeout_seconds = float(msg["health_timeout"])
                state.start(
                    int(msg.get("listen_port", 12730)),
                    str(msg.get("filter_ip", "")),
                    bool(msg.get("mock_mode", False)),
                )
                await ws.send_text(
                    json.dumps({"type": "status", "status": state.status()}, ensure_ascii=False)
                )
            elif msg.get("action") == "stop":
                state.stop()
                await ws.send_text(
                    json.dumps({"type": "status", "status": state.status()}, ensure_ascii=False)
                )
            elif msg.get("action") == "mcap_config":
                state.mcap.update_config(**{k: msg[k] for k in (
                    "enabled",
                    "hourly_dir",
                    "short_dir",
                    "hourly_rotation_seconds",
                    "short_rotation_seconds",
                    "hourly_retention_days",
                    "short_retention_days",
                    "list_limit",
                ) if k in msg})
                _ensure_mcap_dirs(state.mcap)
                await ws.send_text(
                    json.dumps({"type": "mcap", "mcap": state.mcap.get_status()}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        pass
    finally:
        await state.unregister(ws)


def main() -> None:
    parser = argparse.ArgumentParser(description="727R PLC UDP monitor")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--listen-udp", type=int, default=12730)
    parser.add_argument("--mock", action="store_true", help="Start in mock mode")
    parser.add_argument("--no-mock", action="store_true", help="Start in UDP mode")
    args = parser.parse_args()

    if args.mock:
        state.mock_mode = True
    elif args.no_mock:
        state.mock_mode = False

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
