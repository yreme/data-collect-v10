"""SensorNode：所有 pub 程序的基类/组合对象。

一个 SensorNode = 一个传感器实例（kind/name）。它负责全部“框架性”工作，
业务代码只需要：采集 → ``stream.publish(payload, header)``。

自动提供：
- liveliness token ``{prefix}/alive/{kind}/{name}``（控制台据此判断在线）
- ``.../status`` 1Hz JSON（频率、带宽、延迟、同步误差、设备信息、状态）
- ``.../meta``   queryable（静态描述）
- ``.../ctrl``   queryable（describe / get_params / set_params / 自定义 op）
- ``.../event``  事件（掉线、重连、参数生效……）

同一进程可创建多个 SensorNode（例如一个容器管理 6 路相机），共享 session 与 SHM 池。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import zenoh

from . import keys as K
from .header import FrameHeader, now_ns
from .params import ParamError, ParamSet, ParamSpec
from .shm import BytesLike, ShmPool
from .stats import StreamStats

LOG = logging.getLogger("sensorhub.node")

STATES = ("starting", "running", "degraded", "error", "absent", "stopped")

SetParamsHandler = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
OpHandler = Callable[[Dict[str, Any]], Any]


def _json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode()


class Stream:
    """一个数据通道（如 camera/cam0/image）。"""

    def __init__(
        self, node: "SensorNode", channel: str, *, encoding: str, target_hz: Optional[float],
        shm: bool, congestion: str, priority: str, express: bool, reliable: bool,
    ) -> None:
        self.node = node
        self.channel = channel
        self.key = node.keys.data(channel)
        self.encoding = encoding
        self.target_hz = target_hz
        self.use_shm = shm
        self.stats = StreamStats()
        self.seq = 0
        cc = zenoh.CongestionControl.DROP if congestion == "drop" else zenoh.CongestionControl.BLOCK
        prio = getattr(zenoh.Priority, priority.upper())
        rel = zenoh.Reliability.RELIABLE if reliable else zenoh.Reliability.BEST_EFFORT
        self._pub = node.session.declare_publisher(
            self.key, congestion_control=cc, priority=prio, express=express, reliability=rel,
        )

    def next_seq(self) -> int:
        s = self.seq
        self.seq += 1
        return s

    def publish(self, payload: BytesLike, header: FrameHeader) -> FrameHeader:
        """发布一帧。header.pub_ns 会被自动填为当前时间。"""
        hdr = header.with_pub_now()
        data = self.node.shm_pool.wrap(payload) if (self.use_shm and self.node.shm_pool) else payload
        self._pub.put(data, attachment=hdr.pack())
        self.stats.record(
            len(payload), latency_ns=hdr.latency_ns, sync_err_ns=hdr.sync_error_ns,
            seq=hdr.seq, t_ns=hdr.pub_ns,
        )
        return hdr

    def publish_json(self, obj: Any) -> None:
        raw = _json(obj)
        self._pub.put(raw, encoding=zenoh.Encoding.APPLICATION_JSON)
        self.stats.record(len(raw))

    def status(self) -> Dict[str, Any]:
        s = self.stats.snapshot()
        s.update({
            "key": self.key, "encoding": self.encoding, "target_hz": self.target_hz,
            "shm": bool(self.use_shm and self.node.shm_pool and self.node.shm_pool.active),
        })
        return s

    def close(self) -> None:
        try:
            self._pub.undeclare()
        except Exception:  # noqa: BLE001
            pass


class SensorNode:
    def __init__(
        self,
        kind: str,
        name: str,
        *,
        session: zenoh.Session,
        prefix: Optional[str] = None,
        version: str = "0.0.0",
        shm_pool: Optional[ShmPool] = None,
        meta: Optional[Dict[str, Any]] = None,
        status_period_s: float = 1.0,
    ) -> None:
        self.keys = K.sensor_keys(kind, name, prefix)
        self.kind, self.name = kind, name
        self.session = session
        self.version = version
        self.shm_pool = shm_pool
        self.meta: Dict[str, Any] = dict(meta or {})
        self.device: Dict[str, Any] = {}
        self.extra: Dict[str, Any] = {}
        self.params = ParamSet()
        self.state = "starting"
        self.state_reason = ""
        self.streams: Dict[str, Stream] = {}
        self._on_set: Optional[SetParamsHandler] = None
        self._ops: Dict[str, OpHandler] = {}
        self._errors: List[Dict[str, Any]] = []
        self._status_period = status_period_s
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._entities: List[Any] = []
        self._status_thread: Optional[threading.Thread] = None
        self.started_ns = now_ns()
        self.log = logging.getLogger(f"sensorhub.{kind}.{name}")

    # ------------------------------------------------------------------ 声明
    def add_stream(
        self, channel: str, *, encoding: str, target_hz: Optional[float] = None,
        shm: bool = True, congestion: str = "drop", priority: str = "data",
        express: bool = True, reliable: bool = True,
    ) -> Stream:
        """大数据通道默认 congestion=drop：慢订阅者/网络拥塞时丢帧，绝不阻塞采集线程。"""
        st = Stream(self, channel, encoding=encoding, target_hz=target_hz, shm=shm,
                    congestion=congestion, priority=priority, express=express, reliable=reliable)
        self.streams[channel] = st
        return st

    def add_params(self, specs: Sequence[ParamSpec], on_set: Optional[SetParamsHandler] = None) -> None:
        for s in specs:
            self.params.specs[s.name] = s
        if on_set is not None:
            self._on_set = on_set

    def add_op(self, op: str, fn: OpHandler) -> None:
        if op in ("describe", "get_params", "set_params", "status", "meta"):
            raise ValueError(f"{op} 是保留操作")
        self._ops[op] = fn

    def set_meta(self, **kw: Any) -> None:
        with self._lock:
            self.meta.update(kw)

    def set_device(self, **kw: Any) -> None:
        """设备运行信息（ip、mac、serial、link_speed_mbps、温度……），随 status 发布。"""
        with self._lock:
            self.device.update(kw)

    def set_state(self, state: str, reason: str = "") -> None:
        if state not in STATES:
            raise ValueError(state)
        with self._lock:
            changed = state != self.state
            self.state, self.state_reason = state, reason
        if changed:
            self.emit_event("info" if state == "running" else "warn", f"state -> {state}", reason=reason)

    def error(self, msg: str, **kw: Any) -> None:
        item = {"ts_ns": now_ns(), "msg": msg, **kw}
        with self._lock:
            self._errors.append(item)
            del self._errors[:-20]
        self.log.error(msg)
        self.emit_event("error", msg, **kw)

    def emit_event(self, level: str, msg: str, **kw: Any) -> None:
        try:
            self.session.put(self.keys.event, _json({"ts_ns": now_ns(), "level": level, "msg": msg, **kw}),
                             encoding=zenoh.Encoding.APPLICATION_JSON)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "SensorNode":
        s = self.session
        self._entities.append(s.liveliness().declare_token(K.alive(self.kind, self.name, self.keys.prefix)))
        self._entities.append(s.declare_queryable(self.keys.meta, self._on_meta_query))
        self._entities.append(s.declare_queryable(self.keys.ctrl, self._on_ctrl_query))
        self._status_thread = threading.Thread(target=self._status_loop, name=f"status-{self.name}", daemon=True)
        self._status_thread.start()
        if self.state == "starting":
            self.set_state("running")
        return self

    def close(self) -> None:
        self._stop.set()
        self.state = "stopped"
        try:
            self.session.put(self.keys.status, _json(self.status()), encoding=zenoh.Encoding.APPLICATION_JSON)
        except Exception:  # noqa: BLE001
            pass
        for st in self.streams.values():
            st.close()
        for e in self._entities:
            try:
                e.undeclare()
            except Exception:  # noqa: BLE001
                pass
        if self._status_thread:
            self._status_thread.join(timeout=2)

    def __enter__(self) -> "SensorNode":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ status/meta
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind, "name": self.name, "key": self.keys.base,
                "state": self.state, "reason": self.state_reason,
                "host": socket.gethostname(), "pid": os.getpid(), "version": self.version,
                "zid": str(self.session.zid()), "ts_ns": now_ns(), "started_ns": self.started_ns,
                "streams": {c: st.status() for c, st in self.streams.items()},
                "device": dict(self.device), "params": self.params.values(),
                "errors": list(self._errors[-5:]), "extra": dict(self.extra),
                "shm_active": bool(self.shm_pool and self.shm_pool.active),
            }

    def describe_meta(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind, "name": self.name, "key": self.keys.base, "version": self.version,
                "streams": {c: {"key": st.key, "encoding": st.encoding, "target_hz": st.target_hz}
                            for c, st in self.streams.items()},
                **self.meta,
            }

    def _status_loop(self) -> None:
        while not self._stop.wait(self._status_period):
            try:
                self.session.put(self.keys.status, _json(self.status()),
                                 encoding=zenoh.Encoding.APPLICATION_JSON)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("status put failed: %s", exc)

    def _on_meta_query(self, query: zenoh.Query) -> None:
        query.reply(self.keys.meta, _json(self.describe_meta()), encoding=zenoh.Encoding.APPLICATION_JSON)

    # ------------------------------------------------------------------ ctrl
    def handle_ctrl(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op", "describe")
        args = req.get("params") or {}
        try:
            if op == "describe":
                return {"ok": True, "result": {"params": self.params.describe(), "ops": sorted(self._ops)}}
            if op == "get_params":
                return {"ok": True, "result": self.params.values()}
            if op == "status":
                return {"ok": True, "result": self.status()}
            if op == "meta":
                return {"ok": True, "result": self.describe_meta()}
            if op == "set_params":
                changes = self.params.validate(args)
                extra = self._on_set(changes) if self._on_set else None
                self.params.commit(changes)
                restart = [k for k in changes if not self.params.specs[k].live]
                self.emit_event("info", "params applied", applied=changes)
                return {"ok": True, "result": {"applied": changes, "restart_required": restart, **(extra or {})}}
            if op in self._ops:
                return {"ok": True, "result": self._ops[op](args)}
            return {"ok": False, "error": f"未知 op {op!r}"}
        except ParamError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.log.exception("ctrl %s 失败", op)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _on_ctrl_query(self, query: zenoh.Query) -> None:
        try:
            req = json.loads(query.payload.to_bytes()) if query.payload is not None else {}
        except Exception:  # noqa: BLE001
            req = {}
        if not req:
            req = {"op": query.parameters.get("op") or "describe"}
        query.reply(self.keys.ctrl, _json(self.handle_ctrl(req)), encoding=zenoh.Encoding.APPLICATION_JSON)


def ctrl_call(session: zenoh.Session, ctrl_key: str, op: str, params: Optional[Dict[str, Any]] = None,
              timeout: float = 3.0) -> Dict[str, Any]:
    """调用某个实例的 ctrl（控制台/脚本/其他程序共用）。"""
    replies = session.get(ctrl_key, payload=_json({"op": op, "params": params or {}}), timeout=timeout)
    for r in replies:
        if r.ok is not None:
            return json.loads(r.ok.payload.to_bytes())
        return {"ok": False, "error": r.err.payload.to_string() if r.err else "error reply"}
    return {"ok": False, "error": f"{ctrl_key} 无应答（实例不在线或超时）"}


def query_json(session: zenoh.Session, selector: str, timeout: float = 2.0) -> Dict[str, Any]:
    """对 meta 等 queryable 发 get，返回 {key: json}。"""
    out: Dict[str, Any] = {}
    for r in session.get(selector, timeout=timeout):
        if r.ok is not None:
            try:
                out[str(r.ok.key_expr)] = json.loads(r.ok.payload.to_bytes())
            except Exception:  # noqa: BLE001
                pass
    return out
