"""MCAP Web UI with PLC-triggered multimodal collection and IMU live viz."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import asyncio
import contextlib
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from imu_sync.monitor import ImuLiveMonitor
from imu_sync.monitor.web_api import register_imu_routes

from .recorder import RecorderManager

CONFIG_PATH = os.environ.get("MCAP_WEB_CONFIG", "/app/configs/client_mcap.yaml")
DEFAULT_OUTPUT = os.environ.get("MCAP_WEB_OUTPUT_DIR", "/data/mcap")
DEFAULT_PREFIX = os.environ.get("MCAP_WEB_DEFAULT_PREFIX", "multimodal")
HOST = os.environ.get("MCAP_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCAP_WEB_PORT", "18881"))
IMU_PERSIST_DIR = os.environ.get("MCAP_WEB_IMU_PERSIST_DIR", "/data/mcap/imu_motion")
AUTO_START = os.environ.get("AUTO_START", "true").lower() in {"1", "true", "yes"}
LOG = logging.getLogger("mcap_web.main")

manager = RecorderManager(
    config_path=CONFIG_PATH,
    default_output_dir=DEFAULT_OUTPUT,
    default_prefix=DEFAULT_PREFIX,
)

imu_monitor = ImuLiveMonitor(
    shm_prefix=os.environ.get("MCAP_WEB_IMU_SHM_PREFIX", "imu_"),
    auto_discover=True,
    persist_dir=IMU_PERSIST_DIR,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    imu_monitor.start()
    if AUTO_START:
        try:
            manager.start(require_shm=False)
        except Exception as exc:  # noqa: BLE001
            manager.state.error = str(exc)

    async def _collector_watchdog() -> None:
        while True:
            await asyncio.sleep(30)
            if not AUTO_START:
                continue
            try:
                if not manager._is_running():
                    LOG.warning("采集线程已停止，AUTO_START 正在重新启动")
                    manager.start(require_shm=False)
            except Exception as exc:  # noqa: BLE001
                LOG.error("采集 watchdog 重启失败: %s", exc)

    watchdog_task = asyncio.create_task(_collector_watchdog())
    yield
    watchdog_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watchdog_task
    imu_monitor.stop()
    try:
        manager.stop()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="PLC Multimodal MCAP Collector", version="3.0.0", lifespan=_lifespan)
register_imu_routes(app, imu_monitor)


class ShmStatusResponse(BaseModel):
    available: bool
    cameras: List[str] = []
    imus: List[str] = []
    lidars: List[str] = []
    total: int = 0
    message: str = ""


class StartRequest(BaseModel):
    prefix: str = Field(default=DEFAULT_PREFIX, min_length=1, max_length=64)
    output_dir: str = Field(default=DEFAULT_OUTPUT)
    mode: Literal["plc_triggered", "mcap", "compress"] = "plc_triggered"
    rotate_sec: float = Field(default=0, ge=0)
    mock_mode: Optional[bool] = None


class StatusResponse(BaseModel):
    running: bool
    elapsed_sec: float = 0
    prefix: str
    output_dir: str
    mode: str
    last_file: Optional[str] = None
    files: list[str] = []
    error: Optional[str] = None
    suggested_filename: str
    frame_counts: Dict[str, int] = {}
    total_frames: int = 0
    active_sensors: List[str] = []
    snapshots_written: int = 0
    snapshots_skipped: int = 0
    shm: ShmStatusResponse
    mcap: Dict[str, Any] = {}


def _shm_response() -> ShmStatusResponse:
    s = manager.shm_status()
    return ShmStatusResponse(
        available=s.available,
        cameras=s.cameras,
        imus=s.imus,
        lidars=s.lidars,
        total=s.total,
        message=s.message,
    )


def _status_response(st) -> StatusResponse:
    return StatusResponse(
        running=st.running,
        elapsed_sec=manager.elapsed_sec(),
        prefix=st.prefix,
        output_dir=st.output_dir,
        mode=st.mode,
        last_file=st.last_file,
        files=st.files,
        error=st.error,
        suggested_filename=manager.suggested_filename(st.prefix),
        frame_counts=st.frame_counts,
        total_frames=st.total_frames,
        active_sensors=st.active_sensors,
        snapshots_written=st.snapshots_written,
        snapshots_skipped=st.snapshots_skipped,
        shm=_shm_response(),
        mcap=st.mcap_status,
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    static = Path(__file__).parent / "static" / "index.html"
    return static.read_text(encoding="utf-8")


@app.get("/api/shm", response_model=ShmStatusResponse)
def api_shm() -> ShmStatusResponse:
    return _shm_response()


@app.get("/api/status", response_model=StatusResponse)
def api_status() -> StatusResponse:
    return _status_response(manager.status())


@app.get("/api/collector")
def api_collector() -> dict[str, Any]:
    return manager.collector_status()


@app.get("/api/mcap/status")
def api_mcap_status() -> dict[str, Any]:
    st = manager.status()
    return st.mcap_status or {}


@app.post("/api/start", response_model=StatusResponse)
def api_start(req: StartRequest) -> StatusResponse:
    try:
        manager.start(
            prefix=req.prefix,
            output_dir=req.output_dir,
            mode=req.mode,
            rotate_sec=req.rotate_sec,
            mock_mode=req.mock_mode,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_response(manager.status())


@app.post("/api/stop", response_model=StatusResponse)
def api_stop() -> StatusResponse:
    manager.stop()
    return _status_response(manager.status())


@app.get("/api/files")
def api_files() -> dict:
    st = manager.status()
    mcap = st.mcap_status or {}
    return {
        "files": st.files,
        "hourly_dir": os.getenv("MCAP_HOURLY_DIR", "/data/mcap/hourly"),
        "short_dir": os.getenv("MCAP_SHORT_DIR", "/data/mcap/short"),
        "tiers": mcap.get("files") or {},
    }


def main() -> None:
    import uvicorn

    uvicorn.run("mcap_web_viz.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
