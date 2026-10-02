"""sensorhub 控制台：FastAPI + 单页 UI。

运行::

    SENSORHUB_CONFIG_DIR=src/configs SENSORHUB_DATA_DIR=/tmp/console \
      python -m sensorhub_console --port 18100

环境变量：
  SENSORHUB_CONFIG_DIR  配置目录（含 sensors.yaml），控制台读写；默认 /app/configs
  SENSORHUB_CONFIG_FILE 配置文件名，默认 sensors.yaml
  SENSORHUB_DATA_DIR    控制台数据目录（别名等），默认 /data/console
  SENSORHUB_PREFIX      key 前缀（缺省取 sensors.yaml 的 system.prefix）
  CONSOLE_HOST/PORT     监听地址，默认 0.0.0.0:18100
  ZENOH_*               见 sensorhub.session
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from sensorhub import config as C
from sensorhub import keys as K
from sensorhub.node import ctrl_call

from .registry import Registry
from .store import AliasStore, ConfigStore

LOG = logging.getLogger("console")
STATIC = Path(__file__).parent / "static"


class Console:
    def __init__(self, *, config_dir: Path, data_dir: Path, config_file: str = "sensors.yaml",
                 session=None, prefix: Optional[str] = None) -> None:
        self.config_store = ConfigStore(Path(config_dir) / config_file)
        self.aliases = AliasStore(Path(data_dir) / "aliases.json")
        self.session = session
        self.prefix = prefix or self._prefix_from_config()
        self.registry = Registry(session, self.prefix, config_loader=self._load_cfg, alias_lookup=self.aliases.get)
        self._cfg_queryable = None

    def _load_cfg(self) -> Dict[str, Any]:
        return C.parse(self.config_store.current()["content"])

    def _prefix_from_config(self) -> str:
        try:
            return C.prefix_of(self._load_cfg())
        except Exception:  # noqa: BLE001
            return K.get_prefix()

    def start(self) -> None:
        self.registry.start()
        if self.session is not None:
            stem = self.config_store.path.stem
            self._cfg_queryable = self.session.declare_queryable(K.cfg_file(stem, self.prefix), self._on_cfg_query)

    def close(self) -> None:
        self.registry.close()
        if self._cfg_queryable is not None:
            self._cfg_queryable.undeclare()

    def _on_cfg_query(self, query) -> None:
        cur = self.config_store.current()
        query.reply(str(query.key_expr), cur["content"].encode(), attachment=json.dumps(
            {"version": cur["version"], "sha256": cur["sha256"]}).encode())

    def notify_changed(self, result: Dict[str, Any]) -> None:
        if self.session is None or not result.get("changed"):
            return
        self.session.put(K.cfg_changed(self.prefix), json.dumps({
            "file": self.config_store.path.name, "version": result.get("version"), "sha256": result.get("sha256"),
        }).encode())


def create_app(console: Console) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        console.start()
        yield
        console.close()

    app = FastAPI(title="sensorhub console", lifespan=lifespan)
    cs, reg = console.config_store, console.registry

    # ---------------------------------------------------------------- 总览
    @app.get("/api/overview")
    def overview():
        return reg.overview()

    @app.get("/api/events")
    def events(limit: int = 200):
        return list(reg.events)[-limit:][::-1]

    @app.get("/api/net")
    def net():
        ov = reg.overview()
        from_nodes = []
        for n in ov["nodes"]:
            d = n.get("device") or {}
            if any(k in d for k in ("link_speed_mbps", "ip", "rtt_ms")):
                from_nodes.append({"rel": n["rel"], "alias": n["alias"], "state": n["state"], "source": "node", **d})
        return {"nodes": from_nodes, "netprobe": reg.net_links}

    @app.get("/api/router")
    async def router():
        return await run_in_threadpool(reg.router_info)

    @app.get("/api/probe")
    async def probe(key: str = Query(..., description="key expr，如 rig/camera/*/image"), seconds: float = 3.0):
        if not key.startswith(console.prefix + "/"):
            raise HTTPException(400, f"key 必须以 {console.prefix}/ 开头")
        return await run_in_threadpool(reg.probe, key, seconds)

    # ---------------------------------------------------------------- 别名
    @app.get("/api/aliases")
    def aliases():
        return console.aliases.all()

    @app.post("/api/aliases/{kind}/{name}")
    def set_alias(kind: str, name: str, body: Dict[str, Any] = Body(...)):
        try:
            K.check_kind(kind)
            K.check_name(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return console.aliases.set(f"{kind}/{name}", alias=body.get("alias"), note=body.get("note"),
                                   location=body.get("location"))

    # ---------------------------------------------------------------- 控制
    @app.post("/api/ctrl/{kind}/{name}")
    async def ctrl(kind: str, name: str, body: Dict[str, Any] = Body(...)):
        if console.session is None:
            raise HTTPException(503, "未连接 zenoh")
        key = K.sensor_keys(kind, name, console.prefix).ctrl
        return await run_in_threadpool(ctrl_call, console.session, key, body.get("op", "describe"),
                                       body.get("params") or {}, float(body.get("timeout", 3.0)))

    # ---------------------------------------------------------------- 配置
    @app.get("/api/config")
    def get_config():
        return cs.current()

    @app.post("/api/config/validate")
    def validate_config(body: Dict[str, Any] = Body(...)):
        errs = cs.validate(body.get("content", ""))
        return {"ok": not errs, "errors": errs}

    @app.post("/api/config")
    def save_config(body: Dict[str, Any] = Body(...)):
        res = cs.save(body.get("content", ""), comment=body.get("comment", ""),
                      author=body.get("author", ""), force=bool(body.get("force")))
        if not res.get("ok"):
            raise HTTPException(422, res)
        console.notify_changed(res)
        return res

    @app.get("/api/config/history")
    def config_history():
        return cs.history()

    @app.get("/api/config/history/{version}", response_class=PlainTextResponse)
    def config_version(version: int):
        try:
            return cs.get(version)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/config/diff", response_class=PlainTextResponse)
    def config_diff(a: int, b: Optional[int] = None):
        try:
            return cs.diff(a, b)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/config/rollback/{version}")
    def config_rollback(version: int, body: Dict[str, Any] = Body(default={})):
        try:
            res = cs.rollback(version, author=body.get("author", ""))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        console.notify_changed(res)
        return res

    @app.post("/api/config/tag/{version}")
    def config_tag(version: int, body: Dict[str, Any] = Body(...)):
        try:
            return cs.tag(version, body.get("tag", "known_good"), remove=bool(body.get("remove")))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/info")
    def info():
        return {"prefix": console.prefix, "config_path": str(cs.path),
                "zenoh": console.session is not None,
                "zid": str(console.session.zid()) if console.session is not None else None}

    # ---------------------------------------------------------------- 静态页面
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sensorhub-console")
    ap.add_argument("--host", default=os.environ.get("CONSOLE_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CONSOLE_PORT", "18100")))
    ap.add_argument("--config-dir", default=os.environ.get("SENSORHUB_CONFIG_DIR", "/app/configs"))
    ap.add_argument("--config-file", default=os.environ.get("SENSORHUB_CONFIG_FILE", "sensors.yaml"))
    ap.add_argument("--data-dir", default=os.environ.get("SENSORHUB_DATA_DIR", "/data/console"))
    ap.add_argument("--no-zenoh", action="store_true", help="只启用配置/别名管理（调试用）")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    session = None
    if not args.no_zenoh:
        from sensorhub.session import open_session
        session = open_session()
    console = Console(config_dir=Path(args.config_dir), data_dir=Path(args.data_dir),
                      config_file=args.config_file, session=session)
    import uvicorn
    uvicorn.run(create_app(console), host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
