"""从统一 sensors.yaml 生成 lidar-sync 运行时配置。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from .config import (
    AppConfig,
    CaptureConfig,
    ClientsConfig,
    LidarConfig,
    RuntimeConfig,
    ShmConfig,
    WebConfig,
)


def _load_raw(path: str | Path) -> Dict[str, Any]:
    cfg_dir = Path(__file__).resolve().parents[2] / "configs"
    for p in (cfg_dir, Path("/app/configs")):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from sensors_lib import load_sensors_yaml  # noqa: WPS433
    return load_sensors_yaml(path)


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def from_unified_raw(data: Dict[str, Any]) -> AppConfig:
    cap_raw = data.get("capture") or {}
    disc = data.get("discovery") or {}
    shm_raw = data.get("shm") or {}
    web_raw = data.get("web") or {}

    capture = CaptureConfig(
        hz=int(cap_raw.get("hz") or 10),
        mock=_as_bool(cap_raw.get("mock"), False),
        auto_discover=_as_bool(disc.get("lidars"), True),
        discover_subnet=str(disc.get("subnet") or "192.168.1.*"),
        bind_interface=str(disc.get("bind_interface") or "0.0.0.0"),
    )
    shm = ShmConfig(
        name_prefix=str(shm_raw.get("lidar_prefix") or "lidar_"),
        slot_count=int(shm_raw.get("slot_count") or 8),
        slot_capacity_bytes=int(shm_raw.get("lidar_slot_bytes") or 16 * 1024 * 1024),
    )
    lidars = []
    for i, raw in enumerate(data.get("lidars") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or f"lidar{i}")
        lidars.append(LidarConfig(
            name=name,
            enabled=_as_bool(raw.get("enabled"), True),
            frame_id=str(raw.get("frame_id") or name),
            params=dict(raw),
        ))
    if not lidars:
        lidars.append(LidarConfig(name="lidar0", enabled=True, frame_id="rslidar", params={}))

    config_dir = Path(data.get("_config_path", ".")).parent
    return AppConfig(
        capture=capture,
        shm=shm,
        lidars=lidars,
        clients=ClientsConfig(),
        runtime=RuntimeConfig(),
        config_dir=config_dir,
        web=WebConfig(
            enabled=True,
            host="0.0.0.0",
            port=int(web_raw.get("lidar_port") or 18090),
        ),
    )


def load_from_unified(path: str | Path) -> AppConfig:
    return from_unified_raw(_load_raw(path))
