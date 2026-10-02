"""Load IMU record YAML config."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from .writer import ImuRecordConfig


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_imu_record_config(path: str | Path) -> ImuRecordConfig:
    p = Path(path).expanduser().resolve()
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    shm = raw.get("shm") or {}
    imu_prefix = str(shm.get("imu_prefix") or shm.get("name_prefix") or "imu_")

    imu_names: List[str] = []
    frame_ids: Dict[str, str] = {}
    for item in raw.get("imus") or []:
        if not isinstance(item, dict) or "name" not in item:
            continue
        if not _as_bool(item.get("enabled"), True):
            continue
        name = str(item["name"])
        imu_names.append(name)
        if item.get("frame_id"):
            frame_ids[name] = str(item["frame_id"])

    clients = raw.get("clients") or {}
    rec = clients.get("imu_record") or clients.get("mcap") or {}
    runtime = raw.get("runtime") or {}

    if not imu_names:
        imu_names = ["imu0"]

    return ImuRecordConfig(
        output_dir=str(rec.get("output_dir") or "/data/imu"),
        filename_prefix=str(rec.get("filename_prefix") or "imu"),
        period_hours=int(rec.get("period_hours") or 6),
        timezone=str(rec.get("timezone") or ""),
        allow_overwrite=_as_bool(rec.get("allow_overwrite"), True),
        compression=str(rec.get("compression") or "zstd"),
        topic_prefix=str(rec.get("topic_prefix") or "/imu/"),
        publish_imu=_as_bool(rec.get("publish_imu"), True),
        publish_world_pose=_as_bool(rec.get("publish_world_pose"), True),
        publish_scene=_as_bool(rec.get("publish_scene"), False),
        publish_tf=_as_bool(rec.get("publish_tf"), True),
        write_mcap=_as_bool(rec.get("write_mcap"), True),
        write_csv=_as_bool(rec.get("write_csv"), True),
        imu_prefix=imu_prefix,
        imu_names=imu_names,
        frame_ids=frame_ids,
        auto_discover_shm=_as_bool(runtime.get("auto_discover_shm"), True),
        shm_rescan_sec=float(runtime.get("shm_rescan_sec") or 5.0),
        status_interval_sec=float(runtime.get("status_interval_sec") or 5.0),
    )
