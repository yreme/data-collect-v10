"""将 YAML 中的 MAC/名称绑定与自动发现合并为运行时 IMU 参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Union

from ..config import AppConfig, ImuConfig
from ..logging_setup import get_logger
from .net_util import resolve_bind_interfaces
from .network_discover import DiscoveredNetworkImu, discover_network_imus, match_mac
from .serial_discover import DiscoveredSerialImu, discover_serial_imus

LOG = get_logger("discovery.resolver")

DiscoveredImu = Union[DiscoveredNetworkImu, DiscoveredSerialImu]


@dataclass
class ResolvedImu:
    config: ImuConfig
    transport: str
    imu_ip: str
    port: int
    serial_port: str
    baudrate: int
    mac: str
    host_address: str
    from_discovery: bool = False


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


def _bind_interfaces_for(cfg: AppConfig) -> List[str]:
    from ..env_config import EnvSettings

    env = EnvSettings.from_env()
    pattern = cfg.capture.bind_interface
    if pattern in (None, "", "0.0.0.0") and env.local_ip:
        pattern = env.local_ip
    return resolve_bind_interfaces(pattern, cfg.capture.discover_subnet)


def discover_all(cfg: AppConfig, *, timeout_sec: float = 5.0) -> List[DiscoveredImu]:
    cap = cfg.capture
    serial = discover_serial_imus()
    network = discover_network_imus(
        subnet=cap.discover_subnet,
        ports=cap.discover_ports,
        timeout_sec=timeout_sec,
        packet_size=cap.discover_packet_size or None,
        packet_sizes=cap.discover_packet_sizes,
        bind_interfaces=_bind_interfaces_for(cfg),
        bind_device=cap.bind_device,
    )
    return list(serial) + list(network)


def discover_on_subnet(cfg: AppConfig) -> List[DiscoveredImu]:
    return discover_all(cfg)


def _next_auto_name(resolved: List[ResolvedImu]) -> str:
    used = {r.config.name for r in resolved}
    idx = 0
    while f"imu{idx}" in used:
        idx += 1
    return f"imu{idx}"


def _make_auto_entry(dev: DiscoveredImu, name: str) -> ResolvedImu:
    if isinstance(dev, DiscoveredSerialImu):
        cfg_entry = ImuConfig(
            name=name,
            enabled=True,
            frame_id=name,
            params={
                "mac": dev.mac,
                "transport": "serial",
                "serial_port": dev.serial_port,
                "baudrate": dev.baudrate,
                "auto": True,
            },
        )
        return ResolvedImu(
            config=cfg_entry,
            transport="serial",
            imu_ip="",
            port=0,
            serial_port=dev.serial_port,
            baudrate=dev.baudrate,
            mac=dev.mac,
            host_address="",
            from_discovery=True,
        )
    cfg_entry = ImuConfig(
        name=name,
        enabled=True,
        frame_id=name,
        params={
            "mac": dev.mac,
            "transport": dev.transport,
            "imu_ip": dev.ip,
            "port": dev.port,
            "host_address": dev.host_address,
            "auto": True,
        },
    )
    if dev.imu_src_port:
        cfg_entry.params["imu_src_port"] = dev.imu_src_port
    return ResolvedImu(
        config=cfg_entry,
        transport=dev.transport,
        imu_ip=dev.ip,
        port=dev.port,
        serial_port="",
        baudrate=460800,
        mac=dev.mac,
        host_address=dev.host_address,
        from_discovery=True,
    )


def _as_auto(imu_cfg: ImuConfig) -> bool:
    return bool(imu_cfg.get("auto")) or str(imu_cfg.name) in ("*", "auto")


def resolve_imus(cfg: AppConfig, discovered: Optional[List[DiscoveredImu]] = None) -> List[ResolvedImu]:
    if discovered is None:
        discovered = discover_on_subnet(cfg) if cfg.capture.auto_discover else []

    resolved: List[ResolvedImu] = []
    unmatched = list(discovered)
    enabled = cfg.enabled_imus

    def _take_dev(dev: DiscoveredImu) -> None:
        if dev in unmatched:
            unmatched.remove(dev)

    for imu_cfg in enabled:
        if _as_auto(imu_cfg):
            continue
        mac_cfg = _norm_mac(str(imu_cfg.get("mac") or ""))
        dev: Optional[DiscoveredImu] = None

        if mac_cfg:
            for d in list(unmatched):
                if match_mac(mac_cfg, d.mac):
                    dev = d
                    break
        elif imu_cfg.imu_ip:
            for d in list(unmatched):
                if isinstance(d, DiscoveredNetworkImu) and d.ip == imu_cfg.imu_ip:
                    dev = d
                    break
        elif imu_cfg.serial_port:
            for d in list(unmatched):
                if isinstance(d, DiscoveredSerialImu) and d.serial_port == imu_cfg.serial_port:
                    dev = d
                    break
        elif unmatched:
            dev = unmatched[0]

        if dev is not None:
            _take_dev(dev)
            r = _make_auto_entry(dev, imu_cfg.name)
            r.config = imu_cfg
            r.config.params.update(r.config.params)
            r.config.params.update({
                "mac": dev.mac or mac_cfg,
                "transport": r.transport,
            })
            if isinstance(dev, DiscoveredSerialImu):
                r.config.params["serial_port"] = dev.serial_port
                r.config.params["baudrate"] = dev.baudrate
            else:
                r.config.params["imu_ip"] = dev.ip
                r.config.params["port"] = dev.port
                r.config.params["host_address"] = dev.host_address
                if isinstance(dev, DiscoveredNetworkImu) and dev.imu_src_port:
                    r.config.params["imu_src_port"] = dev.imu_src_port
            resolved.append(r)
        else:
            transport = imu_cfg.transport if imu_cfg.transport != "auto" else (
                "serial" if imu_cfg.serial_port else "udp"
            )
            resolved.append(
                ResolvedImu(
                    config=imu_cfg,
                    transport=transport,
                    imu_ip=imu_cfg.imu_ip,
                    port=imu_cfg.port,
                    serial_port=imu_cfg.serial_port,
                    baudrate=imu_cfg.baudrate,
                    mac=mac_cfg,
                    host_address=imu_cfg.host_address,
                    from_discovery=False,
                )
            )
            if cfg.capture.auto_discover and not imu_cfg.imu_ip and not imu_cfg.serial_port:
                LOG.warning(
                    "%s: 未发现匹配 IMU（mac=%s）",
                    imu_cfg.name,
                    mac_cfg or "(任意)",
                )

    if cfg.capture.discover_all and cfg.capture.auto_discover:
        for dev in list(unmatched):
            name = _next_auto_name(resolved)
            resolved.append(_make_auto_entry(dev, name))
            _take_dev(dev)
            LOG.info("自动添加 %s -> %s", name, dev.to_dict())

    if any(_as_auto(i) for i in cfg.imus):
        for dev in unmatched:
            name = _next_auto_name(resolved)
            resolved.append(_make_auto_entry(dev, name))

    if cfg.capture.auto_discover:
        return [r for r in resolved if r.config.enabled]
    return [
        r for r in resolved
        if r.config.enabled and (r.serial_port or r.imu_ip)
    ]
