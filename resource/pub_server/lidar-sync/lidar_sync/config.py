"""配置加载与校验（YAML）。

根结构::

    capture:                       # 采集（server）参数
      hz: 10                       # 发布到共享内存的频率（默认 10Hz）
      reconnect_interval_sec: 2    # 故障后重连间隔
      driver_binary: null          # lidar-capture 可执行文件路径（留空自动查找）
      mock: false                  # 合成点云（无需硬件）

    shm:                           # 共享内存环形缓冲
      name_prefix: lidar_
      slot_count: 8
      slot_capacity_bytes: 16777216  # 16MB，约 1M 点 (16B/点)

    lidars:                        # 雷达列表（支持多雷达）
      - name: lidar0
        lidar_ip: 192.168.1.200    # 可选，用于发现/显示
        mac: aa:bb:cc:dd:ee:ff     # 可选，按 MAC 解析 IP
        msop_port: 6699
        difop_port: 7788
        lidar_type: RSE1
        host_address: 192.168.1.102   # 本机网卡 IP；组播时作为 join 接口
        group_address: 224.0.0.205    # E1R 组播；单播模式填 0.0.0.0
        frame_id: rslidar
        enabled: true

    clients:
      save:     {enabled: false, output_dir: ./captures}
      foxglove: {enabled: false, host: 0.0.0.0, port: 38765}

    web:
      enabled: true
      host: 0.0.0.0
      port: 18090
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
class LidarConfig:
    name: str
    enabled: bool = True
    frame_id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def lidar_ip(self) -> str:
        return str(self.params.get("lidar_ip") or self.params.get("ip") or "")

    @property
    def msop_port(self) -> int:
        return int(self.params.get("msop_port") or 6699)

    @property
    def difop_port(self) -> int:
        return int(self.params.get("difop_port") or 7788)

    @property
    def lidar_type(self) -> str:
        return str(self.params.get("lidar_type") or "RSE1")

    @property
    def host_address(self) -> str:
        return str(self.params.get("host_address") or "0.0.0.0")

    @property
    def group_address(self) -> str:
        return str(self.params.get("group_address") or "0.0.0.0")

    @property
    def mac(self) -> str:
        return str(self.params.get("mac") or "")

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class CaptureConfig:
    hz: int = 10
    reconnect_interval_sec: float = 2.0
    absent_retry_sec: float = 30.0
    heartbeat_interval_sec: float = 1.0
    connect_stagger_sec: float = 0.5
    driver_binary: Optional[str] = None
    mock: bool = False
    auto_discover: bool = True
    discover_all: bool = True
    probe_on_start: bool = True
    discover_subnet: Optional[str] = None
    bind_interface: Optional[str] = None

    @property
    def period_ms(self) -> int:
        return max(1, int(round(1000 / max(1, self.hz))))

    @property
    def fps(self) -> float:
        return float(self.hz)


@dataclass
class ShmConfig:
    name_prefix: str = "lidar_"
    slot_count: int = 8
    slot_capacity_bytes: int = 16 * 1024 * 1024

    def segment_name(self, lidar_name: str) -> str:
        return f"{self.name_prefix}{lidar_name}"


@dataclass
class SaveClientConfig:
    enabled: bool = False
    output_dir: str = "./captures"
    format: str = "pcd"
    per_lidar_subdir: bool = True


@dataclass
class FoxgloveClientConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 38765
    topic_prefix: str = "/lidar/"


@dataclass
class ClientsConfig:
    save: SaveClientConfig = field(default_factory=SaveClientConfig)
    foxglove: FoxgloveClientConfig = field(default_factory=FoxgloveClientConfig)


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 18090


@dataclass
class RuntimeConfig:
    duration_sec: Optional[float] = None
    status_interval_sec: float = 5.0


@dataclass
class AppConfig:
    capture: CaptureConfig
    shm: ShmConfig
    lidars: List[LidarConfig]
    clients: ClientsConfig
    runtime: RuntimeConfig
    config_dir: Path
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def enabled_lidars(self) -> List[LidarConfig]:
        return [l for l in self.lidars if l.enabled]


def _parse_lidar(idx: int, raw: Dict[str, Any]) -> LidarConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"lidars[{idx}] 必须是字典")
    name = str(raw.get("name") or f"lidar{idx}")
    frame_id = str(raw.get("frame_id") or name)
    enabled = _as_bool(raw.get("enabled"), True)
    return LidarConfig(name=name, enabled=enabled, frame_id=frame_id, params=dict(raw))


def apply_env_overrides(cfg: AppConfig) -> AppConfig:
    from .env_config import EnvSettings

    env = EnvSettings.from_env()
    if env.subnet:
        cfg.capture.discover_subnet = env.subnet
    if env.local_ip:
        cfg.capture.bind_interface = env.local_ip
    if env.reconnect_interval_sec != 2.0:
        cfg.capture.reconnect_interval_sec = env.reconnect_interval_sec
    if env.auto_discover:
        cfg.capture.auto_discover = True
    if env.mock:
        cfg.capture.mock = True
    if env.driver_binary:
        cfg.capture.driver_binary = env.driver_binary
    if env.web_port != 18090:
        cfg.web.port = env.web_port
    out = str(env.output_dir)
    cfg.clients.save.output_dir = out + "/captures"
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
    hz = int(cap_raw.get("hz") or 10)
    if hz < 1:
        raise ConfigError("capture.hz 必须 >= 1")
    try:
        from .sync_grid import validate_hz
        validate_hz(hz)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    capture = CaptureConfig(
        hz=hz,
        reconnect_interval_sec=float(cap_raw.get("reconnect_interval_sec") or 2.0),
        absent_retry_sec=float(cap_raw.get("absent_retry_sec") or 30.0),
        heartbeat_interval_sec=float(cap_raw.get("heartbeat_interval_sec") or 1.0),
        connect_stagger_sec=float(cap_raw.get("connect_stagger_sec") or 0.5),
        driver_binary=_opt(cap_raw.get("driver_binary")),
        mock=_as_bool(cap_raw.get("mock"), False),
        auto_discover=_as_bool(cap_raw.get("auto_discover"), True),
        discover_all=_as_bool(cap_raw.get("discover_all"), True),
        probe_on_start=_as_bool(cap_raw.get("probe_on_start"), True),
        discover_subnet=_opt(cap_raw.get("discover_subnet")),
        bind_interface=_opt(cap_raw.get("bind_interface")),
    )

    shm_raw = data.get("shm") or {}
    shm = ShmConfig(
        name_prefix=str(shm_raw.get("name_prefix") or "lidar_"),
        slot_count=int(shm_raw.get("slot_count") or 8),
        slot_capacity_bytes=int(shm_raw.get("slot_capacity_bytes") or 16 * 1024 * 1024),
    )
    if shm.slot_count < 2:
        raise ConfigError("shm.slot_count 必须 >= 2")

    lidars_raw = data.get("lidars")
    if not isinstance(lidars_raw, list) or not lidars_raw:
        raise ConfigError("lidars 必须是非空列表")
    lidars = [_parse_lidar(i, l) for i, l in enumerate(lidars_raw)]
    seen: set = set()
    ports: set = set()
    validate_ports = role != "client" and not capture.auto_discover
    for i, l in enumerate(lidars):
        if l.name in seen:
            raise ConfigError(f"雷达 name 重复: {l.name}")
        seen.add(l.name)
        raw = lidars_raw[i]
        has_explicit_ports = "msop_port" in raw or "difop_port" in raw
        if l.enabled and not capture.mock and validate_ports and has_explicit_ports:
            key = (l.msop_port, l.difop_port)
            if key in ports:
                raise ConfigError(
                    f"雷达 {l.name} 端口 {key} 与其它雷达冲突；"
                    "多雷达时必须在雷达 Web 配置中修改目的端口"
                )
            ports.add(key)

    cl_raw = data.get("clients") or {}
    save_raw = cl_raw.get("save") or {}
    fox_raw = cl_raw.get("foxglove") or {}
    clients = ClientsConfig(
        save=SaveClientConfig(
            enabled=_as_bool(save_raw.get("enabled"), False),
            output_dir=str(save_raw.get("output_dir") or "./captures"),
            format=str(save_raw.get("format") or "pcd").lower(),
            per_lidar_subdir=_as_bool(save_raw.get("per_lidar_subdir"), True),
        ),
        foxglove=FoxgloveClientConfig(
            enabled=_as_bool(fox_raw.get("enabled"), False),
            host=str(fox_raw.get("host") or "0.0.0.0"),
            port=int(fox_raw.get("port") or 38765),
            topic_prefix=str(fox_raw.get("topic_prefix") or "/lidar/"),
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
        port=int(web_raw.get("port") or 18090),
    )

    cfg = AppConfig(
        capture=capture,
        shm=shm,
        lidars=lidars,
        clients=clients,
        runtime=runtime,
        config_dir=cfg_path.parent,
        web=web,
    )
    if apply_env:
        cfg = apply_env_overrides(cfg)
    return cfg
