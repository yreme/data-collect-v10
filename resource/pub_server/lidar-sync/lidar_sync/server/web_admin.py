"""Server Web 管理：状态、发现、增删雷达、异常日志。"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Optional

from ..config import LidarConfig
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
.dot.running{background:var(--ok)}.dot.absent{background:var(--muted);animation:pulse 2s infinite}
.dot.error,.dot.degraded{background:var(--err)}.dot.init{background:var(--muted)}
.err{color:var(--err)}.hint{color:var(--muted);font-size:.75rem}
input,select{padding:.35rem .5rem;border-radius:4px;border:1px solid #30363d;background:#0d1117;color:var(--text)}
"""


def _page(title: str, body: str, active: str = "") -> str:
    nav = f"""<nav>
      <a href="/" {'style="font-weight:bold"' if active=='home' else ''}>状态</a>
      <a href="/discover" {'style="font-weight:bold"' if active=='discover' else ''}>发现雷达</a>
      <a href="/errors" {'style="font-weight:bold"' if active=='errors' else ''}>异常日志</a>
      <a href="/api/status">JSON</a>
    </nav>"""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{_CSS}</style></head><body><h1>{title}</h1>{nav}{body}</body></html>"""


def build_status_payload(server: "CaptureServer") -> dict:
    import time
    arp = load_arp_table()
    lidars = []
    running = offline = absent = 0
    total_frames = 0
    cap = server.cfg.capture
    for w in server._workers:
        s = w.state.snapshot()
        cam = w.lidar_cfg
        ip = s.get("resolved_ip") or cam.lidar_ip or ""
        mac = s.get("mac") or cam.get("mac") or ""
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
        lidars.append({
            "name": cam.name,
            "ip": ip,
            "mac": mac,
            "msop_port": cam.msop_port,
            "difop_port": cam.difop_port,
            "lidar_type": cam.lidar_type,
            "shm_segment": shm,
            "shm_read": f"lidar-sync info -c config.yaml  # 段名: {shm}",
            "topic": f"/lidar/{cam.name}/points",
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
            "total_lidars": len(lidars),
            "running": running,
            "offline": offline,
            "absent": absent,
            "total_frames": total_frames,
        },
        "lidars": lidars,
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
                    self._json([d.to_dict() for d in devs])
                elif path == "/api/errors":
                    cam = (qs.get("lidar") or [None])[0]
                    self._json(GLOBAL_EVENT_LOG.list_events(lidar=cam, limit=200))
                elif path == "/discover":
                    self._html(parent._discover_page())
                elif path == "/errors":
                    cam = (qs.get("lidar") or [None])[0]
                    self._html(parent._errors_page(cam))
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
                if path == "/api/add_lidar":
                    name = str(data.get("name") or f"lidar{len(parent._server._workers)}")
                    cfg = LidarConfig(
                        name=name,
                        frame_id=name,
                        params={
                            "lidar_ip": str(data.get("lidar_ip") or data.get("ip") or ""),
                            "mac": str(data.get("mac") or ""),
                            "msop_port": int(data.get("msop_port") or 6699),
                            "difop_port": int(data.get("difop_port") or 7788),
                            "lidar_type": str(data.get("lidar_type") or "RSE1"),
                            "host_address": "0.0.0.0",
                            "enabled": True,
                        },
                    )
                    ok = parent._server.add_lidar(cfg)
                    self._json({"ok": ok, "name": name})
                elif path == "/api/remove_lidar":
                    name = str(data.get("name") or "")
                    ok = parent._server.remove_lidar(name)
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
        for l in p["lidars"]:
            dot = l["status"]
            cards += f"""<div class="card">
              <h2><span class="dot {dot}"></span>{l['name']}</h2>
              <table>
                <tr><td>IP</td><td>{l['ip'] or '—'}</td></tr>
                <tr><td>MAC</td><td class="mono">{l['mac'] or '—'}</td></tr>
                <tr><td>MSOP/DIFOP</td><td>{l['msop_port']} / {l['difop_port']}</td></tr>
                <tr><td>共享内存</td><td class="mono">{l['shm_segment']}</td></tr>
                <tr><td>帧数</td><td>{l['frames']}</td></tr>
                <tr><td>重连</td><td>{l['reconnects']}</td></tr>
                <tr><td>最近错误</td><td class="err">{l['last_error'] or '—'}</td></tr>
              </table>
              <p class="hint">Foxglove: {l['topic']}</p>
              <form method="post" action="/api/remove_lidar" onsubmit="return removeLidar('{l['name']}')">
                <button type="button" class="btn warn" onclick="removeLidar('{l['name']}')">移除</button>
              </form>
            </div>"""
        body = f"""<p>采集 {p['capture_hz']}Hz | 运行 {p['summary']['running']}/{p['summary']['total_lidars']} | 帧 {p['summary']['total_frames']}</p>
        <div class="grid">{cards or '<p>暂无雷达，请到「发现雷达」添加</p>'}</div>
        <script>
        async function removeLidar(name) {{
          if (!confirm('移除 '+name+'?')) return false;
          await fetch('/api/remove_lidar', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{name}})}});
          location.reload();
        }}
        setTimeout(()=>location.reload(), 5000);
        </script>"""
        return _page("LiDAR 采集状态", body, "home")

    def _discover_page(self) -> str:
        subnet = self._server.cfg.capture.discover_subnet or "192.168.1.*"
        body = f"""<p class="hint">扫描子网 {subnet}，显示 ARP 表中的设备（含 MAC）。添加时请指定 MSOP/DIFOP 端口。</p>
        <p><input id="subnet" value="{subnet}" size="20">
        <button class="btn" onclick="scan()">扫描</button></p>
        <div id="list"></div>
        <h2>手动添加</h2>
        <form id="addForm">
          name <input name="name" value="lidar{len(self._server._workers)}">
          IP <input name="ip" placeholder="192.168.1.200">
          MAC <input name="mac" placeholder="aa:bb:cc:dd:ee:ff">
          MSOP <input name="msop" value="6699" size="6">
          DIFOP <input name="difop" value="7788" size="6">
          <button type="submit" class="btn">添加采集</button>
        </form>
        <script>
        async function scan() {{
          const sub = document.getElementById('subnet').value;
          const r = await fetch('/api/discover?subnet='+encodeURIComponent(sub));
          const devs = await r.json();
          let html = '<table><tr><th>IP</th><th>MAC</th><th></th></tr>';
          for (const d of devs) {{
            html += `<tr><td>${{d.ip}}</td><td class="mono">${{d.mac}}</td>
              <td><button class="btn" onclick="fill('${{d.ip}}','${{d.mac}}')">填入</button></td></tr>`;
          }}
          html += '</table>';
          document.getElementById('list').innerHTML = html || '<p>未发现设备</p>';
        }}
        function fill(ip, mac) {{
          document.querySelector('[name=ip]').value = ip;
          document.querySelector('[name=mac]').value = mac;
        }}
        document.getElementById('addForm').onsubmit = async (e) => {{
          e.preventDefault();
          const f = e.target;
          await fetch('/api/add_lidar', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{
              name: f.name.value, lidar_ip: f.ip.value, mac: f.mac.value,
              msop_port: +f.msop.value, difop_port: +f.difop.value
            }})}});
          alert('已添加'); location.href='/';
        }};
        scan();
        </script>"""
        return _page("发现雷达", body, "discover")

    def _errors_page(self, lidar: Optional[str]) -> str:
        events = GLOBAL_EVENT_LOG.list_events(lidar=lidar, limit=100)
        rows = "".join(
            f"<div class='card'><span class='err'>{e['level']}</span> "
            f"[{e['lidar']}] {e['message']}<br><span class='hint'>{e.get('hint','')}</span></div>"
            for e in events
        )
        body = rows or "<p>暂无异常</p>"
        return _page("异常日志", body, "errors")
