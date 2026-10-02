"""统一多传感器 YAML 配置加载。

三种传感器（GigE 相机、雷达、IMU）共用一份配置文件::

    capture:
      hz: 10

    cameras:
      - name: cam0
        serial: "12345678"      # 预设序列号
        topic: /camera/cam0/image
        enabled: true

    lidars:
      - name: lidar0
        mac: "aa:bb:cc:dd:ee:ff"  # 预设 MAC
        topic: /lidar/lidar0/points
        enabled: true

    imus:
      - name: imu0
        port: /dev/ttyUSB0
        topic: /imu/imu0/data
        enabled: true

    discovery:
      cameras: true
      lidars: true
      subnet: 192.168.1.*

    shm:
      camera_prefix: camera_
      lidar_prefix: lidar_
      imu_prefix: imu_
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .sync_grid import validate_hz


class ConfigError(ValueError):
    pass


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _opt(v: Any) -> Optional[Any]:
    if v in (None, "", "null", "None"):
        return None
    return v


@dataclass
class CaptureConfig:
    hz: int = 10
    mock: bool = False

    @property
    def period_ms(self) -> int:
        return max(1, 1000 // max(1, self.hz))


@dataclass
class ShmConfig:
    camera_prefix: str = "camera_"
    lidar_prefix: str = "lidar_"
    imu_prefix: str = "imu_"
    slot_count: int = 8
    camera_slot_bytes: int = 8 * 1024 * 1024
    lidar_slot_bytes: int = 16 * 1024 * 1024
    imu_slot_bytes: int = 64 * 1024

    def camera_segment(self, name: str) -> str:
        return f"{self.camera_prefix}{name}"

    def lidar_segment(self, name: str) -> str:
        return f"{self.lidar_prefix}{name}"

    def imu_segment(self, name: str) -> str:
        return f"{self.imu_prefix}{name}"


@dataclass
class DiscoveryConfig:
    cameras: bool = True
    lidars: bool = True
    imus: bool = False
    subnet: str = "192.168.1.*"
    bind_interface: str = "0.0.0.0"


@dataclass
class SensorDevice:
    """通用传感器设备描述。"""
    name: str
    kind: str  # camera | lidar | imu
    enabled: bool = True
    topic: str = ""
    frame_id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    camera_port: int = 18080
    lidar_port: int = 18090
    imu_port: int = 18100
    client_port: int = 18200


@dataclass
class ClientConfig:
    foxglove_enabled: bool = True
    foxglove_host: str = "0.0.0.0"
    foxglove_port: int = 8765
    mcap_enabled: bool = True
    mcap_output_dir: str = "./captures"
    max_lag_ms: int = 50
    min_lag_ms: int = -5


@dataclass
class UnifiedConfig:
    capture: CaptureConfig
    shm: ShmConfig
    discovery: DiscoveryConfig
    cameras: List[SensorDevice]
    lidars: List[SensorDevice]
    imus: List[SensorDevice]
    web: WebConfig
    clients: ClientConfig
    config_path: Path
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def enabled_cameras(self) -> List[SensorDevice]:
        return [c for c in self.cameras if c.enabled]

    @property
    def enabled_lidars(self) -> List[SensorDevice]:
        return [l for l in self.lidars if l.enabled]

    @property
    def enabled_imus(self) -> List[SensorDevice]:
        return [i for i in self.imus if i.enabled]

    def all_enabled(self) -> List[SensorDevice]:
        return self.enabled_cameras + self.enabled_lidars + self.enabled_imus

    def topic_for(self, dev: SensorDevice) -> str:
        if dev.topic:
            return dev.topic
        defaults = {
            "camera": f"/camera/{dev.name}/image",
            "lidar": f"/lidar/{dev.name}/points",
            "imu": f"/imu/{dev.name}/data",
        }
        return defaults.get(dev.kind, f"/{dev.kind}/{dev.name}")


def _parse_device(kind: str, idx: int, raw: Dict[str, Any]) -> SensorDevice:
    if not isinstance(raw, dict):
        raise ConfigError(f"{kind}[{idx}] 必须是字典")
    name = str(raw.get("name") or f"{kind}{idx}")
    topic = str(raw.get("topic") or "")
    frame_id = str(raw.get("frame_id") or name)
    enabled = _as_bool(raw.get("enabled"), True)
    return SensorDevice(
        name=name, kind=kind, enabled=enabled,
        topic=topic, frame_id=frame_id, params=dict(raw),
    )


def load_unified_config(
    path: "str | os.PathLike",
    *,
    apply_env: bool = True,
) -> UnifiedConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是字典")

    cap_raw = data.get("capture") or {}
    hz = int(cap_raw.get("hz") or 10)
    try:
        validate_hz(hz)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    capture = CaptureConfig(
        hz=hz,
        mock=_as_bool(cap_raw.get("mock"), False),
    )

    shm_raw = data.get("shm") or {}
    shm = ShmConfig(
        camera_prefix=str(shm_raw.get("camera_prefix") or "camera_"),
        lidar_prefix=str(shm_raw.get("lidar_prefix") or "lidar_"),
        imu_prefix=str(shm_raw.get("imu_prefix") or "imu_"),
        slot_count=int(shm_raw.get("slot_count") or 8),
        camera_slot_bytes=int(shm_raw.get("camera_slot_bytes") or 8 * 1024 * 1024),
        lidar_slot_bytes=int(shm_raw.get("lidar_slot_bytes") or 16 * 1024 * 1024),
        imu_slot_bytes=int(shm_raw.get("imu_slot_bytes") or 64 * 1024),
    )

    disc_raw = data.get("discovery") or {}
    discovery = DiscoveryConfig(
        cameras=_as_bool(disc_raw.get("cameras"), True),
        lidars=_as_bool(disc_raw.get("lidars"), True),
        imus=_as_bool(disc_raw.get("imus"), False),
        subnet=str(disc_raw.get("subnet") or "192.168.1.*"),
        bind_interface=str(disc_raw.get("bind_interface") or "0.0.0.0"),
    )

    cameras = [_parse_device("camera", i, c) for i, c in enumerate(data.get("cameras") or [])]
    lidars = [_parse_device("lidar", i, l) for i, l in enumerate(data.get("lidars") or [])]
    imus = [_parse_device("imu", i, m) for i, m in enumerate(data.get("imus") or [])]

    web_raw = data.get("web") or {}
    web = WebConfig(
        enabled=_as_bool(web_raw.get("enabled"), True),
        host=str(web_raw.get("host") or "0.0.0.0"),
        camera_port=int(web_raw.get("camera_port") or 18080),
        lidar_port=int(web_raw.get("lidar_port") or 18090),
        imu_port=int(web_raw.get("imu_port") or 18100),
        client_port=int(web_raw.get("client_port") or 18200),
    )

    cl_raw = data.get("clients") or {}
    fox_raw = cl_raw.get("foxglove") or {}
    mcap_raw = cl_raw.get("mcap") or {}
    clients = ClientConfig(
        foxglove_enabled=_as_bool(fox_raw.get("enabled", cl_raw.get("foxglove_enabled")), True),
        foxglove_host=str(fox_raw.get("host") or "0.0.0.0"),
        foxglove_port=int(fox_raw.get("port") or 8765),
        mcap_enabled=_as_bool(mcap_raw.get("enabled", cl_raw.get("mcap_enabled")), True),
        mcap_output_dir=str(mcap_raw.get("output_dir") or "./captures"),
        max_lag_ms=int(cl_raw.get("max_lag_ms") or 50),
        min_lag_ms=int(cl_raw.get("min_lag_ms") or -5),
    )

    if apply_env:
        if os.environ.get("CAPTURE_HZ"):
            capture.hz = int(os.environ["CAPTURE_HZ"])
            validate_hz(capture.hz)
        if os.environ.get("CAPTURE_MOCK", "").lower() in ("1", "true", "yes"):
            capture.mock = True
        if os.environ.get("DISCOVERY_SUBNET"):
            discovery.subnet = os.environ["DISCOVERY_SUBNET"]

    return UnifiedConfig(
        capture=capture,
        shm=shm,
        discovery=discovery,
        cameras=cameras,
        lidars=lidars,
        imus=imus,
        web=web,
        clients=clients,
        config_path=cfg_path,
        raw=data,
    )


def write_unified_config(cfg: UnifiedConfig, path: Optional[Path] = None) -> Path:
    """将当前配置写回 YAML（Web 发现新设备后持久化）。"""
    out = path or cfg.config_path
    data = dict(cfg.raw)
    data["capture"] = {"hz": cfg.capture.hz, "mock": cfg.capture.mock}
    data["cameras"] = [d.params | {"name": d.name, "enabled": d.enabled, "topic": d.topic, "frame_id": d.frame_id}
                       for d in cfg.cameras]
    data["lidars"] = [d.params | {"name": d.name, "enabled": d.enabled, "topic": d.topic, "frame_id": d.frame_id}
                      for d in cfg.lidars]
    data["imus"] = [d.params | {"name": d.name, "enabled": d.enabled, "topic": d.topic, "frame_id": d.frame_id}
                    for d in cfg.imus]
    with out.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return out
