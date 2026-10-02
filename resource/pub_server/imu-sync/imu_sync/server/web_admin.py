"""Server Web 管理：状态、发现、增删 IMU、异常日志。"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Optional

from ..config import ImuConfig
from ..discovery import load_arp_table
from ..logging_setup import get_logger
from .event_log import GLOBAL_EVENT_LOG

if TYPE_CHECKING:
    from .server import CaptureServer

LOG = get_logger("web")

_CSS = """
:root{--bg:#0f1419;--card:#1a2332;--text:#e6edf3;--muted:#8b949e;--ok:#3fb950;--warn:#d29922;--err:#f85149;--accent:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);padding:1.2rem;line-height:1.5}
a{color:var(--accent)}nav{margin-bottom:1rem;font-size:.9rem}nav a{margin-right:1rem}
h1{font-size:1.3rem;margin-bottom:.5rem}
.btn{display:inline-block;padding:.4rem .9rem;background:var(--accent);color:#fff;border-radius:6px;border:none;cursor:pointer;font-size:.85rem;margin:.25rem .5rem .25rem 0}
.card{background:var(--card);border-radius:8px;padding:1rem;border:1px solid #30363d;margin-bottom:1rem}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.mono{font-family:ui-monospace,monospace;color:var(--accent);font-size:.78rem}
table{width:100%;font-size:.8rem;border-collapse:collapse}td{padding:.2rem 0}
td:first-child{color:var(--muted);width:38%}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:.4rem}
.dot.running{background:var(--ok)}.dot.absent{background:var(--muted)}
.dot.error,.dot.degraded{background:var(--err)}.dot.init{background:var(--muted)}
.err{color:var(--err)}.hint{color:var(--muted);font-size:.75rem}
input,select{padding:.35rem .5rem;border-radius:4px;border:1px solid #30363d;background:#0d1117;color:var(--text)}
"""


def _page(title: str, body: str, active: str = "") -> str:
    nav = f"""<nav>
      <a href="/" {'style="font-weight:bold"' if active=='home' else ''}>状态</a>
      <a href="/discover" {'style="font-weight:bold"' if active=='discover' else ''}>发现 IMU</a>
      <a href="/errors" {'style="font-weight:bold"' if active=='errors' else ''}>异常日志</a>
      <a href="/api/status">JSON</a>
    </nav>"""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{_CSS}</style></head><body><h1>{title}</h1>{nav}{body}</body></html>"""


def build_status_payload(server: "CaptureServer") -> dict:
    import time
    arp = load_arp_table()
    imus = []
    running = offline = absent = 0
    total_frames = 0
    cap = server.cfg.capture
    for w in server._workers:
        s = w.state.snapshot()
        cam = w.imu_cfg
        ip = s.get("resolved_ip") or cam.imu_ip or ""
        mac = s.get("mac") or cam.get("mac") or ""
        serial = s.get("serial_port") or cam.serial_port or ""
        if not mac and ip:
            for m, aip in arp.items():
                if aip == ip:
                    mac = m
                    break
        st = s["status"]
        if st == "running" and s["connected"]:
            running += 1
        elif st == "absent" or s.get("absent"):
            absent += 1
            offline += 1
        else:
            offline += 1
        total_frames += s["frames"]
        shm = server.cfg.shm.segment_name(cam.name)
        transport = s.get("transport") or cam.transport
        imus.append({
            "name": cam.name,
            "transport": transport,
            "ip": ip,
            "mac": mac,
            "serial_port": serial,
            "port": cam.port,
            "baudrate": cam.baudrate,
            "shm_segment": shm,
            "shm_read": f"imu-sync shm-watch {shm}",
            "topic_imu": f"/imu/{cam.name}/imu",
            "topic_scene": f"/imu/{cam.name}/scene",
            "status": st,
            "connected": s["connected"],
            "frames": s["frames"],
            "errors": s["errors"],
            "reconnects": s["reconnects"],
            "last_error": s["last_error"],
            "absent": s.get("absent", False),
        })
    return {
        "timestamp_ms": int(time.time() * 1000),
        "capture_hz": cap.hz,
        "summary": {
            "total_imus": len(imus),
            "running": running,
            "offline": offline,
            "absent": absent,
            "total_frames": total_frames,
        },
        "imus": imus,
    }


class WebAdminServer:
    def __init__(self, host: str, port: int, server: "CaptureServer") -> None:
        self.host = host
        self.port = port
        self._server = server
        self._httpd: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ANN001
                pass

            def _json(self, data, code=200):  # noqa: ANN001
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def _html(self, text: str, code=200):  # noqa: ANN001
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(text.encode("utf-8"))

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if path == "/api/status":
                    self._json(build_status_payload(parent._server))
                elif path == "/api/discover":
                    subnet = (qs.get("subnet") or [None])[0]
                    devs = parent._server.discover_subnet(subnet)
                    self._json(devs)
                elif path == "/api/errors":
                    imu = (qs.get("imu") or [None])[0]
                    self._json(GLOBAL_EVENT_LOG.list_events(imu=imu, limit=200))
                elif path == "/discover":
                    self._html(parent._discover_page())
                elif path == "/errors":
                    imu = (qs.get("imu") or [None])[0]
                    self._html(parent._errors_page(imu))
                elif path == "/":
                    self._html(parent._home_page())
                else:
                    self.send_error(404)

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    data = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    data = {}
                if path == "/api/add_imu":
                    name = str(data.get("name") or f"imu{len(parent._server._workers)}")
                    transport = str(data.get("transport") or "tcp")
                    params = {
                        "transport": transport,
                        "mac": str(data.get("mac") or ""),
                        "enabled": True,
                    }
                    if transport == "serial":
                        params["serial_port"] = str(data.get("serial_port") or "")
                        params["baudrate"] = int(data.get("baudrate") or 460800)
                    else:
                        params["imu_ip"] = str(data.get("imu_ip") or data.get("ip") or "")
                        params["port"] = int(data.get("port") or 9000)
                        params["host_address"] = str(data.get("host_address") or "0.0.0.0")
                    cfg = ImuConfig(name=name, frame_id=name, params=params)
                    ok = parent._server.add_imu(cfg)
                    self._json({"ok": ok, "name": name})
                elif path == "/api/remove_imu":
                    name = str(data.get("name") or "")
                    ok = parent._server.remove_imu(name)
                    self._json({"ok": ok})
                else:
                    self.send_error(404)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        import threading
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="web-admin").start()
        LOG.info("Web 管理界面 http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()

    def _home_page(self) -> str:
        p = build_status_payload(self._server)
        cards = ""
        for l in p["imus"]:
            dot = l["status"]
            conn = f"{l['transport']}"
            if l["serial_port"]:
                conn += f" {l['serial_port']} @{l['baudrate']}"
            elif l["ip"]:
                conn += f" {l['ip']}:{l['port']}"
            cards += f"""<div class="card">
              <h2><span class="dot {dot}"></span>{l['name']}</h2>
              <table>
                <tr><td>连接</td><td>{conn}</td></tr>
                <tr><td>MAC</td><td class="mono">{l['mac'] or '—'}</td></tr>
                <tr><td>共享内存</td><td class="mono">{l['shm_segment']}</td></tr>
                <tr><td>帧数</td><td>{l['frames']}</td></tr>
                <tr><td>重连</td><td>{l['reconnects']}</td></tr>
                <tr><td>最近错误</td><td class="err">{l['last_error'] or '—'}</td></tr>
              </table>
              <p class="hint">Foxglove Imu: {l['topic_imu']} | Scene: {l['topic_scene']}</p>
              <button type="button" class="btn" onclick="removeImu('{l['name']}')">移除</button>
            </div>"""
        body = f"""<p>采集 {p['capture_hz']}Hz | 运行 {p['summary']['running']}/{p['summary']['total_imus']} | 帧 {p['summary']['total_frames']}</p>
        <div class="grid">{cards or '<p>暂无 IMU，请到「发现 IMU」添加</p>'}</div>
        <script>
        async function removeImu(name) {{
          if (!confirm('移除 '+name+'?')) return false;
          await fetch('/api/remove_imu', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{name}})}});
          location.reload();
        }}
        setTimeout(()=>location.reload(), 5000);
        </script>"""
        return _page("IMU 采集状态", body, "home")

    def _discover_page(self) -> str:
        subnet = self._server.cfg.capture.discover_subnet or "192.168.1.*"
        body = f"""<p class="hint">扫描子网 {subnet}：监听本机 UDP 端口，匹配 payload 67 字节（如 192.168.1.201:2369 → 本机:2368）。</p>
        <p><input id="subnet" value="{subnet}" size="20">
        <button class="btn" onclick="scan()">扫描</button></p>
        <div id="list"></div>
        <h2>手动添加</h2>
        <form id="addForm">
          name <input name="name" value="imu{len(self._server._workers)}">
          transport <select name="transport"><option value="tcp">tcp</option><option value="udp">udp</option><option value="serial">serial</option></select>
          IP <input name="ip" placeholder="192.168.1.100">
          MAC <input name="mac" placeholder="aa:bb:cc:dd:ee:ff">
          port <input name="port" value="9000" size="6">
          serial <input name="serial_port" placeholder="/dev/ttyUSB0">
          baud <input name="baudrate" value="460800" size="8">
          <button type="submit" class="btn">添加采集</button>
        </form>
        <script>
        async function scan() {{
          const sub = document.getElementById('subnet').value;
          const r = await fetch('/api/discover?subnet='+encodeURIComponent(sub));
          const devs = await r.json();
          let html = '<table><tr><th>类型</th><th>地址</th><th>MAC</th><th></th></tr>';
          for (const d of devs) {{
            const addr = d.serial_port || (d.imu_ip+':'+d.port);
            html += `<tr><td>${{d.transport}}</td><td>${{addr}}</td><td class="mono">${{d.mac||'—'}}</td>
              <td><button class="btn" onclick='fill(${{JSON.stringify(d)}})'>填入</button></td></tr>`;
          }}
          html += '</table>';
          document.getElementById('list').innerHTML = html || '<p>未发现设备</p>';
        }}
        function fill(d) {{
          document.querySelector('[name=transport]').value = d.transport || 'tcp';
          document.querySelector('[name=ip]').value = d.imu_ip || d.ip || '';
          document.querySelector('[name=mac]').value = d.mac || '';
          document.querySelector('[name=port]').value = d.port || 9000;
          document.querySelector('[name=serial_port]').value = d.serial_port || '';
        }}
        document.getElementById('addForm').onsubmit = async (e) => {{
          e.preventDefault();
          const f = e.target;
          await fetch('/api/add_imu', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{
              name: f.name.value, transport: f.transport.value,
              imu_ip: f.ip.value, mac: f.mac.value, port: +f.port.value,
              serial_port: f.serial_port.value, baudrate: +f.baudrate.value
            }})}});
          alert('已添加'); location.href='/';
        }};
        scan();
        </script>"""
        return _page("发现 IMU", body, "discover")

    def _errors_page(self, imu: Optional[str]) -> str:
        events = GLOBAL_EVENT_LOG.list_events(imu=imu, limit=100)
        rows = "".join(
            f"<div class='card'><span class='err'>{e['level']}</span> "
            f"[{e['imu']}] {e['message']}<br><span class='hint'>{e.get('hint','')}</span></div>"
            for e in events
        )
        body = rows or "<p>暂无异常</p>"
        return _page("异常日志", body, "errors")
