"""配置加载与校验（YAML）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class ConfigError(ValueError):
    """配置非法。"""


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
class ImuConfig:
    name: str
    enabled: bool = True
    frame_id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def transport(self) -> str:
        return str(self.params.get("transport") or "auto").lower()

    @property
    def imu_ip(self) -> str:
        return str(self.params.get("imu_ip") or self.params.get("ip") or "")

    @property
    def port(self) -> int:
        return int(self.params.get("port") or 2368)

    @property
    def imu_src_port(self) -> int:
        return int(self.params.get("imu_src_port") or 2369)

    @property
    def serial_port(self) -> str:
        return str(self.params.get("serial_port") or "")

    @property
    def baudrate(self) -> int:
        return int(self.params.get("baudrate") or 460800)

    @property
    def mac(self) -> str:
        return str(self.params.get("mac") or "")

    @property
    def host_address(self) -> str:
        return str(self.params.get("host_address") or "0.0.0.0")

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class CaptureConfig:
    hz: int = 20
    reconnect_interval_sec: float = 2.0
    absent_retry_sec: float = 30.0
    heartbeat_interval_sec: float = 1.0
    connect_stagger_sec: float = 0.5
    mock: bool = False
    auto_discover: bool = True
    discover_all: bool = True
    probe_on_start: bool = True
    discover_subnet: Optional[str] = None
    bind_interface: Optional[str] = None
    discover_rescan_sec: float = 30.0
    discover_packet_size: int = 0  # 0=自动匹配 59/67
    discover_packet_sizes: List[int] = field(default_factory=lambda: [59, 67])
    discover_ports: List[int] = field(default_factory=lambda: [2368])
    bind_device: Optional[str] = None

    @property
    def period_ms(self) -> int:
        return max(1, int(round(1000 / max(1, self.hz))))


@dataclass
class ShmConfig:
    name_prefix: str = "imu_"
    slot_count: int = 16
    slot_capacity_bytes: int = 4096

    def segment_name(self, imu_name: str) -> str:
        return f"{self.name_prefix}{imu_name}"


def _vec3(raw: Any, default: Tuple[float, float, float]) -> Tuple[float, float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        return float(raw[0]), float(raw[1]), float(raw[2])
    return default


@dataclass
class PoseConfig:
    """world ↔ IMU 位姿与平移模式。"""

    world_frame: str = "world"
    initial_position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    initial_orientation_rpy: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    translation_mode: str = "auto"  # auto | velocity | delta | position | accel | none
    velocity_frame: str = "enu"  # enu | ned | body
    position_relative: bool = True
    velocity_deadband: float = 1e-4
    accel_deadband: float = 0.08
    gravity: float = 9.81
    velocity_damping: float = 0.02
    publish_tf: bool = True

    @classmethod
    def from_dict(
        cls,
        raw: Dict[str, Any],
        *,
        defaults: Optional["PoseConfig"] = None,
    ) -> "PoseConfig":
        base = defaults or cls()
        return cls(
            world_frame=str(raw.get("world_frame") or base.world_frame),
            initial_position=_vec3(raw.get("initial_position"), base.initial_position),
            initial_orientation_rpy=_vec3(
                raw.get("initial_orientation_rpy"), base.initial_orientation_rpy
            ),
            translation_mode=str(raw.get("translation_mode") or base.translation_mode),
            velocity_frame=str(raw.get("velocity_frame") or base.velocity_frame),
            position_relative=_as_bool(
                raw.get("position_relative"), base.position_relative
            ),
            velocity_deadband=float(
                raw.get("velocity_deadband") if raw.get("velocity_deadband") is not None
                else base.velocity_deadband
            ),
            accel_deadband=float(
                raw.get("accel_deadband") if raw.get("accel_deadband") is not None
                else base.accel_deadband
            ),
            gravity=float(raw.get("gravity") if raw.get("gravity") is not None else base.gravity),
            velocity_damping=float(
                raw.get("velocity_damping") if raw.get("velocity_damping") is not None
                else base.velocity_damping
            ),
            publish_tf=_as_bool(raw.get("publish_tf"), base.publish_tf),
        )


DEFAULT_SPREADER_MODEL_URL = (
    "https://broadcv.oss-cn-shenzhen.aliyuncs.com/2026/hailab/crane-ship-sim-3D/spreader-glod.glb"
)


@dataclass
class SceneModelConfig:
    """SceneUpdate 3D 模型（GLB/GLTF）。"""

    url: str = DEFAULT_SPREADER_MODEL_URL
    media_type: str = "model/gltf-binary"
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    entity_id: str = "spreader"

    @classmethod
    def from_dict(
        cls,
        raw: Dict[str, Any],
        *,
        defaults: Optional["SceneModelConfig"] = None,
    ) -> "SceneModelConfig":
        base = defaults or cls()
        return cls(
            url=str(raw.get("url") or base.url),
            media_type=str(raw.get("media_type") or base.media_type),
            scale=_vec3(raw.get("scale"), base.scale),
            entity_id=str(raw.get("entity_id") or base.entity_id),
        )


@dataclass
class FoxgloveClientConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 28765
    topic_prefix: str = "/imu/"
    publish_scene: bool = True
    publish_imu: bool = True
    publish_world_pose: bool = True
    publish_scene_model: bool = True
    pose: PoseConfig = field(default_factory=PoseConfig)
    scene_model: SceneModelConfig = field(default_factory=SceneModelConfig)


@dataclass
class McapClientConfig:
    enabled: bool = False
    output_dir: str = "./mcap_out"
    rotate_sec: float = 60.0
    allow_overwrite: bool = True
    topic_prefix: str = "/imu/"
    filename_prefix: str = "imu"
    compression: str = "zstd"
    publish_scene: bool = True
    publish_imu: bool = True
    publish_world_pose: bool = True
    publish_scene_model: bool = True
    pose: PoseConfig = field(default_factory=PoseConfig)
    scene_model: SceneModelConfig = field(default_factory=SceneModelConfig)


@dataclass
class ClientsConfig:
    foxglove: FoxgloveClientConfig = field(default_factory=FoxgloveClientConfig)
    mcap: McapClientConfig = field(default_factory=McapClientConfig)


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 28090


@dataclass
class RuntimeConfig:
    duration_sec: Optional[float] = None
    status_interval_sec: float = 5.0


@dataclass
class AppConfig:
    capture: CaptureConfig
    shm: ShmConfig
    imus: List[ImuConfig]
    clients: ClientsConfig
    runtime: RuntimeConfig
    config_dir: Path
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def enabled_imus(self) -> List[ImuConfig]:
        return [i for i in self.imus if i.enabled]


def _parse_imu(idx: int, raw: Dict[str, Any]) -> ImuConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"imus[{idx}] 必须是字典")
    name = str(raw.get("name") or f"imu{idx}")
    frame_id = str(raw.get("frame_id") or name)
    enabled = _as_bool(raw.get("enabled"), True)
    return ImuConfig(name=name, enabled=enabled, frame_id=frame_id, params=dict(raw))


def apply_env_overrides(cfg: AppConfig) -> AppConfig:
    from .env_config import EnvSettings

    env = EnvSettings.from_env()
    if env.subnet:
        cfg.capture.discover_subnet = env.subnet
    if env.local_ip:
        cfg.capture.bind_interface = env.local_ip
        from .discovery.net_util import local_ips_for_pattern
        ips = local_ips_for_pattern(env.local_ip)
        if ips:
            for imu in cfg.imus:
                if not imu.host_address or imu.host_address == "0.0.0.0":
                    imu.params["host_address"] = ips[0]
    if env.bind_device:
        cfg.capture.bind_device = env.bind_device
    if env.reconnect_interval_sec != 2.0:
        cfg.capture.reconnect_interval_sec = env.reconnect_interval_sec
    if env.auto_discover:
        cfg.capture.auto_discover = True
    if env.mock:
        cfg.capture.mock = True
    if env.web_port != 28090:
        cfg.web.port = env.web_port
    out = str(env.output_dir)
    cfg.clients.mcap.output_dir = out + "/mcap"
    return cfg


def load_config(
    path: "str | os.PathLike",
    *,
    apply_env: bool = True,
    role: Optional[str] = None,
) -> AppConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是字典")

    cap_raw = data.get("capture") or {}
    hz = int(cap_raw.get("hz") or 20)
    if hz < 1:
        raise ConfigError("capture.hz 必须 >= 1")
    try:
        from .sync_grid import validate_hz
        validate_hz(hz)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    ports_raw = cap_raw.get("discover_ports")
    discover_ports = [2368]
    if isinstance(ports_raw, list) and ports_raw:
        discover_ports = [int(p) for p in ports_raw]
    sizes_raw = cap_raw.get("discover_packet_sizes")
    discover_packet_sizes = [59, 67]
    if isinstance(sizes_raw, list) and sizes_raw:
        discover_packet_sizes = [int(s) for s in sizes_raw]
    packet_size = int(cap_raw.get("discover_packet_size") or 0)

    capture = CaptureConfig(
        hz=hz,
        reconnect_interval_sec=float(cap_raw.get("reconnect_interval_sec") or 2.0),
        absent_retry_sec=float(cap_raw.get("absent_retry_sec") or 30.0),
        heartbeat_interval_sec=float(cap_raw.get("heartbeat_interval_sec") or 1.0),
        connect_stagger_sec=float(cap_raw.get("connect_stagger_sec") or 0.5),
        mock=_as_bool(cap_raw.get("mock"), False),
        auto_discover=_as_bool(cap_raw.get("auto_discover"), True),
        discover_all=_as_bool(cap_raw.get("discover_all"), True),
        probe_on_start=_as_bool(cap_raw.get("probe_on_start"), True),
        discover_subnet=_opt(cap_raw.get("discover_subnet")),
        bind_interface=_opt(cap_raw.get("bind_interface")),
        bind_device=_opt(cap_raw.get("bind_device")),
        discover_rescan_sec=float(cap_raw.get("discover_rescan_sec") or 30.0),
        discover_packet_size=packet_size,
        discover_packet_sizes=discover_packet_sizes,
        discover_ports=discover_ports,
    )

    shm_raw = data.get("shm") or {}
    shm = ShmConfig(
        name_prefix=str(shm_raw.get("name_prefix") or "imu_"),
        slot_count=int(shm_raw.get("slot_count") or 16),
        slot_capacity_bytes=int(shm_raw.get("slot_capacity_bytes") or 4096),
    )
    if shm.slot_count < 2:
        raise ConfigError("shm.slot_count 必须 >= 2")

    imus_raw = data.get("imus")
    if not isinstance(imus_raw, list) or not imus_raw:
        raise ConfigError("imus 必须是非空列表")
    imus = [_parse_imu(i, imu) for i, imu in enumerate(imus_raw)]
    seen: set = set()
    for imu in imus:
        if imu.name in seen:
            raise ConfigError(f"IMU name 重复: {imu.name}")
        seen.add(imu.name)

    cl_raw = data.get("clients") or {}
    fox_raw = cl_raw.get("foxglove") or {}
    mcap_raw = cl_raw.get("mcap") or {}
    fox_pose = PoseConfig.from_dict(fox_raw.get("pose") or {})
    mcap_pose = PoseConfig.from_dict(mcap_raw.get("pose") or {})
    fox_scene_model = SceneModelConfig.from_dict(fox_raw.get("scene_model") or {})
    mcap_scene_model = SceneModelConfig.from_dict(mcap_raw.get("scene_model") or {})
    clients = ClientsConfig(
        foxglove=FoxgloveClientConfig(
            enabled=_as_bool(fox_raw.get("enabled"), False),
            host=str(fox_raw.get("host") or "0.0.0.0"),
            port=int(fox_raw.get("port") or 28765),
            topic_prefix=str(fox_raw.get("topic_prefix") or "/imu/"),
            publish_scene=_as_bool(fox_raw.get("publish_scene"), True),
            publish_imu=_as_bool(fox_raw.get("publish_imu"), True),
            publish_world_pose=_as_bool(fox_raw.get("publish_world_pose"), True),
            publish_scene_model=_as_bool(fox_raw.get("publish_scene_model"), True),
            pose=fox_pose,
            scene_model=fox_scene_model,
        ),
        mcap=McapClientConfig(
            enabled=_as_bool(mcap_raw.get("enabled"), False),
            output_dir=str(mcap_raw.get("output_dir") or "./mcap_out"),
            rotate_sec=float(mcap_raw.get("rotate_sec") or 60.0),
            allow_overwrite=_as_bool(mcap_raw.get("allow_overwrite"), True),
            topic_prefix=str(mcap_raw.get("topic_prefix") or "/imu/"),
            filename_prefix=str(mcap_raw.get("filename_prefix") or "imu"),
            compression=str(mcap_raw.get("compression") or "zstd").lower(),
            publish_scene=_as_bool(mcap_raw.get("publish_scene"), True),
            publish_imu=_as_bool(mcap_raw.get("publish_imu"), True),
            publish_world_pose=_as_bool(mcap_raw.get("publish_world_pose"), True),
            publish_scene_model=_as_bool(mcap_raw.get("publish_scene_model"), True),
            pose=mcap_pose,
            scene_model=mcap_scene_model,
        ),
    )

    rt_raw = data.get("runtime") or {}
    dur = rt_raw.get("duration_sec")
    runtime = RuntimeConfig(
        duration_sec=(float(dur) if _opt(dur) is not None else None),
        status_interval_sec=float(rt_raw.get("status_interval_sec") or 5.0),
    )

    web_raw = data.get("web") or {}
    web = WebConfig(
        enabled=_as_bool(web_raw.get("enabled"), True),
        host=str(web_raw.get("host") or "0.0.0.0"),
        port=int(web_raw.get("port") or 28090),
    )

    cfg = AppConfig(
        capture=capture,
        shm=shm,
        imus=imus,
        clients=clients,
        runtime=runtime,
        config_dir=cfg_path.parent,
        web=web,
    )
    if apply_env:
        cfg = apply_env_overrides(cfg)
    return cfg
