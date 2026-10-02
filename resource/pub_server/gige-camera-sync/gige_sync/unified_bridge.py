"""从统一 sensors.yaml 生成 gige-camera-sync 运行时配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from multimodal_common.unified_config import UnifiedConfig, load_unified_config


@dataclass
class CameraConfig:
    name: str
    enabled: bool = True
    frame_id: str = ""
    topic: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def serial(self) -> str:
        return str(self.params.get("serial") or "")

    @property
    def ip(self) -> str:
        return str(self.params.get("ip") or "")

    @property
    def mac(self) -> str:
        return str(self.params.get("mac") or "")

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class CaptureConfig:
    hz: int = 10
    mock: bool = False
    auto_discover: bool = True
    discover_subnet: str = "192.168.1.*"
    width: int = 1920
    height: int = 1080


@dataclass
class ShmConfig:
    name_prefix: str = "camera_"
    slot_count: int = 8
    slot_capacity_bytes: int = 8 * 1024 * 1024

    def segment_name(self, name: str) -> str:
        return f"{self.name_prefix}{name}"


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 18080


@dataclass
class AppConfig:
    capture: CaptureConfig
    shm: ShmConfig
    cameras: List[CameraConfig]
    web: WebConfig
    config_dir: Path

    @property
    def enabled_cameras(self) -> List[CameraConfig]:
        return [c for c in self.cameras if c.enabled]


def from_unified(cfg: UnifiedConfig) -> AppConfig:
    capture = CaptureConfig(
        hz=cfg.capture.hz,
        mock=cfg.capture.mock,
        auto_discover=cfg.discovery.cameras,
        discover_subnet=cfg.discovery.subnet,
    )
    shm = ShmConfig(
        name_prefix=cfg.shm.camera_prefix,
        slot_count=cfg.shm.slot_count,
        slot_capacity_bytes=cfg.shm.camera_slot_bytes,
    )
    cameras = [
        CameraConfig(
            name=d.name, enabled=d.enabled,
            frame_id=d.frame_id or d.name,
            topic=d.topic or f"/camera/{d.name}/image",
            params=dict(d.params),
        )
        for d in cfg.cameras
    ]
    if not cameras:
        cameras.append(CameraConfig(name="cam0", enabled=True, frame_id="camera", params={}))
    return AppConfig(
        capture=capture, shm=shm, cameras=cameras,
        web=WebConfig(enabled=cfg.web.enabled, host=cfg.web.host, port=cfg.web.camera_port),
        config_dir=cfg.config_path.parent,
    )


def load_from_unified(path: "str | Path") -> AppConfig:
    return from_unified(load_unified_config(path))
