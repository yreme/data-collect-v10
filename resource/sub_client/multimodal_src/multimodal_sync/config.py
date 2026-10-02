"""Unified multimodal configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .env_config import EnvSettings
from .shm_discovery import DiscoveredSensors, merge_sensor_lists, scan_shm_segments


class ConfigError(ValueError):
    pass


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SensorRef:
    name: str
    enabled: bool = True
    frame_id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ShmPrefixes:
    camera: str = "gige_"
    imu: str = "imu_"
    lidar: str = "lidar_"


@dataclass
class FoxgloveClientConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    camera_topic_prefix: str = "/gige/"
    imu_topic_prefix: str = "/imu/"
    lidar_topic_prefix: str = "/lidar/"
    publish_imu: bool = True
    publish_world_pose: bool = True
    publish_scene: bool = True
    publish_scene_model: bool = False
    publish_tf: bool = True


@dataclass
class McapClientConfig:
    output_dir: str = "./mcap_out"
    rotate_sec: float = 60.0
    filename_prefix: str = "multimodal"
    allow_overwrite: bool = True
    compression: str = "zstd"
    callback_url: Optional[str] = None
    camera_topic_prefix: str = "/gige/"
    imu_topic_prefix: str = "/imu/"
    lidar_topic_prefix: str = "/lidar/"
    publish_imu: bool = True
    publish_world_pose: bool = True
    publish_scene: bool = False
    publish_scene_model: bool = False
    publish_tf: bool = True


@dataclass
class RuntimeConfig:
    duration_sec: Optional[float] = None
    status_interval_sec: float = 5.0
    shm_rescan_sec: float = 5.0
    auto_discover_shm: bool = True
    discover_subnet: str = "192.168.1.*"


@dataclass
class AppConfig:
    shm: ShmPrefixes = field(default_factory=ShmPrefixes)
    cameras: List[SensorRef] = field(default_factory=list)
    imus: List[SensorRef] = field(default_factory=list)
    lidars: List[SensorRef] = field(default_factory=list)
    foxglove: FoxgloveClientConfig = field(default_factory=FoxgloveClientConfig)
    mcap: McapClientConfig = field(default_factory=McapClientConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    # Optional paths to per-sensor server configs (for discover command).
    server_configs: Dict[str, str] = field(default_factory=dict)

    @property
    def enabled_cameras(self) -> List[SensorRef]:
        return [c for c in self.cameras if c.enabled]

    @property
    def enabled_imus(self) -> List[SensorRef]:
        return [i for i in self.imus if i.enabled]

    @property
    def enabled_lidars(self) -> List[SensorRef]:
        return [l for l in self.lidars if l.enabled]

    def configured_sensors(self) -> DiscoveredSensors:
        return DiscoveredSensors(
            cameras=[c.name for c in self.enabled_cameras],
            imus=[i.name for i in self.enabled_imus],
            lidars=[l.name for l in self.enabled_lidars],
        )

    def resolve_sensors(self) -> DiscoveredSensors:
        configured = self.configured_sensors()
        discovered = scan_shm_segments(
            camera_prefix=self.shm.camera,
            imu_prefix=self.shm.imu,
            lidar_prefix=self.shm.lidar,
        )
        return merge_sensor_lists(
            discovered,
            configured,
            auto_discover=self.runtime.auto_discover_shm,
        )


def _parse_sensors(raw: Any) -> List[SensorRef]:
    out: List[SensorRef] = []
    for item in raw or []:
        if not isinstance(item, dict) or "name" not in item:
            continue
        params = {k: v for k, v in item.items() if k not in ("name", "enabled", "frame_id")}
        out.append(
            SensorRef(
                name=str(item["name"]),
                enabled=_as_bool(item.get("enabled"), True),
                frame_id=str(item.get("frame_id") or ""),
                params=params,
            )
        )
    return out


def load_config(path: str | Path) -> AppConfig:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    shm_raw = raw.get("shm") or {}
    shm = ShmPrefixes(
        camera=str(shm_raw.get("camera_prefix") or "gige_"),
        imu=str(shm_raw.get("imu_prefix") or "imu_"),
        lidar=str(shm_raw.get("lidar_prefix") or "lidar_"),
    )

    fg_raw = (raw.get("clients") or {}).get("foxglove") or raw.get("foxglove") or {}
    pose_raw = fg_raw.get("pose") or {}
    scene_model_raw = fg_raw.get("scene_model") or {}
    foxglove = FoxgloveClientConfig(
        host=str(fg_raw.get("host") or "0.0.0.0"),
        port=int(fg_raw.get("port") or 8765),
        camera_topic_prefix=str(fg_raw.get("camera_topic_prefix") or "/gige/"),
        imu_topic_prefix=str(fg_raw.get("imu_topic_prefix") or "/imu/"),
        lidar_topic_prefix=str(fg_raw.get("lidar_topic_prefix") or "/lidar/"),
        publish_imu=_as_bool(fg_raw.get("publish_imu"), True),
        publish_world_pose=_as_bool(fg_raw.get("publish_world_pose"), True),
        publish_scene=_as_bool(fg_raw.get("publish_scene"), True),
        publish_scene_model=_as_bool(fg_raw.get("publish_scene_model"), False),
        publish_tf=_as_bool(pose_raw.get("publish_tf", fg_raw.get("publish_tf")), True),
    )
    _ = scene_model_raw  # reserved for scene model URL overrides

    mc_raw = (raw.get("clients") or {}).get("mcap") or raw.get("mcap") or {}
    mc_pose = mc_raw.get("pose") or {}
    mcap = McapClientConfig(
        output_dir=str(mc_raw.get("output_dir") or "./mcap_out"),
        rotate_sec=float(mc_raw.get("rotate_sec") or 60),
        filename_prefix=str(mc_raw.get("filename_prefix") or "multimodal"),
        allow_overwrite=_as_bool(mc_raw.get("allow_overwrite"), True),
        compression=str(mc_raw.get("compression") or "zstd"),
        callback_url=mc_raw.get("callback_url"),
        camera_topic_prefix=str(mc_raw.get("camera_topic_prefix") or "/gige/"),
        imu_topic_prefix=str(mc_raw.get("imu_topic_prefix") or "/imu/"),
        lidar_topic_prefix=str(mc_raw.get("lidar_topic_prefix") or "/lidar/"),
        publish_imu=_as_bool(mc_raw.get("publish_imu"), True),
        publish_world_pose=_as_bool(mc_raw.get("publish_world_pose"), True),
        publish_scene=_as_bool(mc_raw.get("publish_scene"), False),
        publish_scene_model=_as_bool(mc_raw.get("publish_scene_model"), False),
        publish_tf=_as_bool(mc_pose.get("publish_tf", mc_raw.get("publish_tf")), True),
    )

    rt_raw = raw.get("runtime") or {}
    runtime = RuntimeConfig(
        duration_sec=rt_raw.get("duration_sec"),
        status_interval_sec=float(rt_raw.get("status_interval_sec") or 5.0),
        shm_rescan_sec=float(rt_raw.get("shm_rescan_sec") or 5.0),
        auto_discover_shm=_as_bool(rt_raw.get("auto_discover_shm"), True),
        discover_subnet=str(rt_raw.get("discover_subnet") or "192.168.1.*"),
    )

    server_configs = dict(raw.get("server_configs") or {})

    cfg = AppConfig(
        shm=shm,
        cameras=_parse_sensors(raw.get("cameras")),
        imus=_parse_sensors(raw.get("imus")),
        lidars=_parse_sensors(raw.get("lidars")),
        foxglove=foxglove,
        mcap=mcap,
        runtime=runtime,
        server_configs=server_configs,
    )
    return apply_env_overrides(cfg)


def apply_env_overrides(cfg: AppConfig) -> AppConfig:
    env = EnvSettings.from_env()
    if env.subnet:
        cfg.runtime.discover_subnet = env.subnet
    if env.foxglove_host:
        cfg.foxglove.host = env.foxglove_host
    if env.foxglove_port is not None:
        cfg.foxglove.port = env.foxglove_port
    if env.output_dir:
        cfg.mcap.output_dir = f"{env.output_dir.rstrip('/')}/mcap"
    if env.mcap_rotate_sec is not None:
        cfg.mcap.rotate_sec = env.mcap_rotate_sec
    if env.mcap_callback_url:
        cfg.mcap.callback_url = env.mcap_callback_url
    if env.shm_rescan_sec is not None:
        cfg.runtime.shm_rescan_sec = env.shm_rescan_sec
    camera_prefix = os.getenv("MCAP_SHM_CAMERA_PREFIX", "").strip()
    imu_prefix = os.getenv("MCAP_SHM_IMU_PREFIX", "").strip()
    lidar_prefix = os.getenv("MCAP_SHM_LIDAR_PREFIX", "").strip()
    if camera_prefix:
        cfg.shm.camera = camera_prefix
    if imu_prefix:
        cfg.shm.imu = imu_prefix
    if lidar_prefix:
        cfg.shm.lidar = lidar_prefix
    return cfg
