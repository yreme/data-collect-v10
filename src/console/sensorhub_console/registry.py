"""注册表：汇总 liveliness / status / meta / event / net links，供 Web 面板展示。

控制台 **不订阅任何原始数据流**（图像/点云），频率、带宽、延迟全部来自发布端的 status，
因此控制台对数据通路零负担。需要接收端视角时用 ``probe()``：临时订阅几秒且只读帧头。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

import zenoh

from sensorhub import config as C
from sensorhub import keys as K
from sensorhub.header import FrameHeader
from sensorhub.node import query_json
from sensorhub.stats import StreamStats

LOG = logging.getLogger("console.registry")

STALE_S = 5.0


class Registry:
    def __init__(self, session: Optional[zenoh.Session], prefix: str, *,
                 config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
                 alias_lookup: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        self.session = session
        self.prefix = prefix
        self._config_loader = config_loader
        self._alias_lookup = alias_lookup or (lambda _k: {})
        self._lock = threading.RLock()
        self.nodes: Dict[str, Dict[str, Any]] = {}   # rel key "camera/cam0" -> entry
        self.events: Deque[Dict[str, Any]] = deque(maxlen=500)
        self.net_links: Dict[str, Any] = {}
        self._subs: List[Any] = []
        self._stop = threading.Event()
        self._meta_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 启动
    def start(self) -> None:
        if self.session is None:
            return
        s, p = self.session, self.prefix
        self._subs.append(s.liveliness().declare_subscriber(K.all_alive(p), self._on_alive, history=True))
        self._subs.append(s.declare_subscriber(K.all_status(p), self._on_status))
        self._subs.append(s.declare_subscriber(K.all_events(p), self._on_event))
        self._subs.append(s.declare_subscriber(K.net_links(p), self._on_net))
        self._meta_thread = threading.Thread(target=self._meta_loop, daemon=True, name="meta-refresh")
        self._meta_thread.start()

    def close(self) -> None:
        self._stop.set()
        for sub in self._subs:
            try:
                sub.undeclare()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 回调
    def _rel(self, kind: str, name: str) -> str:
        return f"{kind}/{name}"

    def _entry(self, kind: str, name: str) -> Dict[str, Any]:
        rel = self._rel(kind, name)
        e = self.nodes.get(rel)
        if e is None:
            e = {"kind": kind, "name": name, "rel": rel, "key": f"{self.prefix}/{rel}",
                 "alive": False, "status": None, "status_rx": 0.0, "meta": None, "first_seen": time.time()}
            self.nodes[rel] = e
        return e

    def _on_alive(self, sample: zenoh.Sample) -> None:
        parts = str(sample.key_expr).split("/")
        if len(parts) < 2:
            return
        kind, name = parts[-2], parts[-1]
        put = sample.kind == zenoh.SampleKind.PUT
        with self._lock:
            e = self._entry(kind, name)
            e["alive"] = put
            e["alive_change"] = time.time()
        self.events.append({"ts_ns": time.time_ns(), "level": "info" if put else "warn",
                            "key": f"{kind}/{name}", "msg": "上线" if put else "离线"})
        if put:
            threading.Thread(target=self.refresh_meta, args=(kind, name), daemon=True).start()

    def _on_status(self, sample: zenoh.Sample) -> None:
        sk = K.parse_key(str(sample.key_expr), self.prefix)
        if sk is None:
            return
        try:
            st = json.loads(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            e = self._entry(sk.kind, sk.name)
            e["status"] = st
            e["status_rx"] = time.time()

    def _on_event(self, sample: zenoh.Sample) -> None:
        sk = K.parse_key(str(sample.key_expr), self.prefix)
        try:
            ev = json.loads(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            return
        ev["key"] = f"{sk.kind}/{sk.name}" if sk else str(sample.key_expr)
        self.events.append(ev)

    def _on_net(self, sample: zenoh.Sample) -> None:
        try:
            self.net_links = {"rx": time.time(), "data": json.loads(sample.payload.to_bytes())}
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ meta
    def refresh_meta(self, kind: Optional[str] = None, name: Optional[str] = None) -> None:
        if self.session is None:
            return
        sel = f"{self.prefix}/{kind}/{name}/meta" if kind and name else K.all_meta(self.prefix)
        try:
            res = query_json(self.session, sel, timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("meta query failed: %s", exc)
            return
        with self._lock:
            for key, meta in res.items():
                sk = K.parse_key(key, self.prefix)
                if sk:
                    self._entry(sk.kind, sk.name)["meta"] = meta

    def _meta_loop(self) -> None:
        self.refresh_meta()
        while not self._stop.wait(30):
            self.refresh_meta()

    # ------------------------------------------------------------------ 视图
    def _configured(self) -> Dict[str, Dict[str, Any]]:
        if not self._config_loader:
            return {}
        try:
            cfg = self._config_loader()
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for sec, kind in C.SECTIONS.items():
            for d in C.devices(cfg, sec, enabled_only=False):
                out[f"{kind}/{d['name']}"] = d
        return out

    def overview(self) -> Dict[str, Any]:
        now = time.time()
        configured = self._configured()
        with self._lock:
            entries = {k: dict(v) for k, v in self.nodes.items()}
        for rel, d in configured.items():
            if rel not in entries:
                kind, name = rel.split("/", 1)
                entries[rel] = {"kind": kind, "name": name, "rel": rel, "key": f"{self.prefix}/{rel}",
                                "alive": False, "status": None, "status_rx": 0.0, "meta": None}
        nodes, streams = [], []
        for rel, e in sorted(entries.items()):
            cfg = configured.get(rel, {})
            al = self._alias_lookup(rel)
            st = e.get("status") or {}
            stale = (now - e.get("status_rx", 0)) > STALE_S
            if not e.get("alive") and not st:
                state = "disabled" if cfg and not cfg.get("enabled", True) else "offline"
            elif not e.get("alive"):
                state = "offline"
            elif stale:
                state = "stale"
            else:
                state = st.get("state", "running")
            node = {
                "rel": rel, "key": e["key"], "kind": e["kind"], "name": e["name"],
                "alias": al.get("alias") or cfg.get("alias") or "",
                "note": al.get("note", ""), "location": al.get("location", ""),
                "configured": bool(cfg), "enabled": cfg.get("enabled", True) if cfg else None,
                "alive": e.get("alive", False), "state": state, "reason": st.get("reason", ""),
                "host": st.get("host"), "version": st.get("version"),
                "device": st.get("device", {}), "params": st.get("params", {}),
                "errors": st.get("errors", []), "extra": st.get("extra", {}),
                "shm_active": st.get("shm_active"), "last_status_age_s": round(now - e["status_rx"], 1) if e.get("status_rx") else None,
                "meta": e.get("meta"),
            }
            nodes.append(node)
            for ch, s in (st.get("streams") or {}).items():
                streams.append({
                    "rel": rel, "alias": node["alias"], "kind": e["kind"], "name": e["name"], "channel": ch,
                    "state": state if not stale else "stale", **s,
                })
        return {"ts": now, "prefix": self.prefix, "nodes": nodes, "streams": streams}

    # ------------------------------------------------------------------ probe
    def probe(self, key_expr: str, seconds: float = 3.0) -> Dict[str, Any]:
        """接收端视角测频：临时订阅 key_expr，只解析 attachment 帧头，不读取 payload。"""
        if self.session is None:
            return {"ok": False, "error": "no session"}
        seconds = max(0.5, min(seconds, 10.0))
        stats: Dict[str, StreamStats] = {}
        shm_flags: Dict[str, bool] = {}
        transport: Dict[str, List[int]] = {}

        def cb(sample: zenoh.Sample) -> None:
            k = str(sample.key_expr)
            now = time.time_ns()
            hdr = None
            if sample.attachment is not None:
                try:
                    hdr = FrameHeader.unpack(sample.attachment.to_bytes())
                except ValueError:
                    pass
            st = stats.setdefault(k, StreamStats(window_s=seconds + 1))
            st.record(len(sample.payload), latency_ns=(now - hdr.stamp_ns) if hdr and hdr.stamp_ns else 0,
                      seq=hdr.seq if hdr else None, t_ns=now)
            shm_flags[k] = sample.payload.as_shm() is not None
            if hdr and hdr.pub_ns:
                transport.setdefault(k, []).append(now - hdr.pub_ns)

        sub = self.session.declare_subscriber(key_expr, cb)
        time.sleep(seconds)
        sub.undeclare()
        out = {}
        for k, st in stats.items():
            snap = st.snapshot()
            tl = sorted(transport.get(k, []))
            snap["transport_ms_p50"] = round(tl[len(tl) // 2] / 1e6, 3) if tl else None
            snap["shm"] = shm_flags.get(k)
            snap.pop("sync_err_ms", None)
            snap["end_to_end_ms"] = snap.pop("latency_ms")
            out[k] = snap
        return {"ok": True, "seconds": seconds, "streams": out}

    def router_info(self) -> List[Dict[str, Any]]:
        if self.session is None:
            return []
        res = []
        for r in self.session.get("@/*/router", timeout=2.0):
            if r.ok is not None:
                try:
                    res.append(json.loads(r.ok.payload.to_bytes()))
                except Exception:  # noqa: BLE001
                    pass
        return res
