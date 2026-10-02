"""FastAPI web UI + auto-start IMU MCAP/CSV recorder."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .recorder import NoShmDataError, RecorderManager

logging.basicConfig(
    level=os.environ.get("IMU_RECORD_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CONFIG_PATH = os.environ.get("IMU_RECORD_CONFIG", "/app/configs/client_imu_record.yaml")
DEFAULT_OUTPUT = os.environ.get("IMU_RECORD_OUTPUT_DIR", "/data/imu")
DEFAULT_PREFIX = os.environ.get("IMU_RECORD_DEFAULT_PREFIX", "imu")
DEFAULT_PERIOD_HOURS = int(os.environ.get("IMU_RECORD_PERIOD_HOURS", "6"))
HOST = os.environ.get("IMU_RECORD_HOST", "0.0.0.0")
PORT = int(os.environ.get("IMU_RECORD_PORT", "18882"))
AUTO_START = os.environ.get("IMU_RECORD_AUTO_START", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

manager = RecorderManager(
    config_path=CONFIG_PATH,
    default_output_dir=DEFAULT_OUTPUT,
    default_prefix=DEFAULT_PREFIX,
    default_period_hours=DEFAULT_PERIOD_HOURS,
)

app = FastAPI(title="IMU Recorder", version="1.0.0")


class ShmStatusResponse(BaseModel):
    available: bool
    imus: List[str] = []
    total: int = 0
    message: str = ""


class StartRequest(BaseModel):
    prefix: str = Field(default=DEFAULT_PREFIX, min_length=1, max_length=64)
    output_dir: str = Field(default=DEFAULT_OUTPUT)
    period_hours: int = Field(default=DEFAULT_PERIOD_HOURS, ge=1, le=24)


class StatusResponse(BaseModel):
    running: bool
    elapsed_sec: float = 0
    prefix: str
    output_dir: str
    period_hours: int = 6
    last_mcap: Optional[str] = None
    last_csv: Optional[str] = None
    files: list[str] = []
    error: Optional[str] = None
    suggested_filename: str
    frame_counts: Dict[str, int] = {}
    total_frames: int = 0
    active_sensors: List[str] = []
    current_mcap: Optional[str] = None
    current_csv: Optional[str] = None
    next_rotate_at: Optional[str] = None
    waiting_for_data: bool = False
    shm: ShmStatusResponse


def _shm_response() -> ShmStatusResponse:
    s = manager.shm_status()
    return ShmStatusResponse(
        available=s.available,
        imus=s.imus,
        total=s.total,
        message=s.message,
    )


def _status_response(st) -> StatusResponse:
    return StatusResponse(
        running=st.running,
        elapsed_sec=manager.elapsed_sec(),
        prefix=st.prefix,
        output_dir=st.output_dir,
        period_hours=st.period_hours,
        last_mcap=st.last_mcap,
        last_csv=st.last_csv,
        files=st.files,
        error=st.error,
        suggested_filename=manager.suggested_filename(st.prefix),
        frame_counts=st.frame_counts,
        total_frames=st.total_frames,
        active_sensors=st.active_sensors,
        current_mcap=st.current_mcap,
        current_csv=st.current_csv,
        next_rotate_at=st.next_rotate_at,
        waiting_for_data=st.waiting_for_data,
        shm=_shm_response(),
    )


@app.on_event("startup")
def _auto_start() -> None:
    if not AUTO_START:
        return
    try:
        manager.start(require_shm=False)
        logging.getLogger("imu_record").info(
            "已自动开始录制 → %s（每 %dh 整点切分）",
            DEFAULT_OUTPUT,
            DEFAULT_PERIOD_HOURS,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("imu_record").warning("自动启动录制失败: %s", exc)


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


@app.post("/api/start", response_model=StatusResponse)
def api_start(req: StartRequest) -> StatusResponse:
    try:
        st = manager.start(
            prefix=req.prefix,
            output_dir=req.output_dir,
            period_hours=req.period_hours,
            require_shm=True,
        )
        return _status_response(st)
    except NoShmDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/stop", response_model=StatusResponse)
def api_stop() -> StatusResponse:
    return _status_response(manager.stop())


def main() -> int:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
