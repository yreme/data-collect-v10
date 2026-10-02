"""将 YAML 中的 MAC/名称绑定与 UDP 自动发现合并为运行时雷达参数。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ..config import AppConfig, LidarConfig
from ..logging_setup import get_logger
from .lidar_scan import DiscoveredLidar
from .net_util import local_ip_for_peer, resolve_bind_interfaces
from .udp_discover import discover_lidars_udp, match_mac

LOG = get_logger("discovery.resolver")


@dataclass
class ResolvedLidar:
    config: LidarConfig
    ip: str
    mac: str
    msop_port: int
    difop_port: int
    host_address: str
    group_address: str
    lidar_type: str
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


def discover_on_subnet(cfg: AppConfig) -> List[DiscoveredLidar]:
    ifaces = _bind_interfaces_for(cfg)
    if ifaces:
        LOG.info("本机嗅探网卡: %s", ", ".join(ifaces))
    sample_ip = None
    for l in cfg.lidars:
        if l.lidar_ip:
            sample_ip = l.lidar_ip
            break
    return discover_lidars_udp(
        subnet=cfg.capture.discover_subnet,
        timeout_sec=3.0,
        bind_interfaces=ifaces,
        sample_ip=sample_ip,
    )


def _next_auto_name(resolved: List[ResolvedLidar]) -> str:
    used = {r.config.name for r in resolved}
    idx = 0
    while f"lidar{idx}" in used:
        idx += 1
    return f"lidar{idx}"


def _make_auto_entry(dev: DiscoveredLidar, name: str) -> ResolvedLidar:
    host = str(dev.extra.get("host_address") or local_ip_for_peer(dev.ip) or "0.0.0.0")
    grp = str(dev.extra.get("group_address") or "224.0.0.205")
    cfg_entry = LidarConfig(
        name=name,
        enabled=True,
        frame_id=name,
        params={
            "mac": dev.mac,
            "lidar_ip": dev.ip,
            "msop_port": dev.msop_port,
            "difop_port": dev.difop_port,
            "group_address": grp,
            "host_address": host,
            "lidar_type": "RSE1",
            "auto": True,
        },
    )
    return ResolvedLidar(
        config=cfg_entry,
        ip=dev.ip,
        mac=dev.mac,
        msop_port=dev.msop_port,
        difop_port=dev.difop_port,
        host_address=host,
        group_address=grp,
        lidar_type="RSE1",
        from_discovery=True,
    )


def resolve_lidars(cfg: AppConfig, discovered: Optional[List[DiscoveredLidar]] = None) -> List[ResolvedLidar]:
    """把配置项与 UDP 发现结果合并。MAC 优先；其余按顺序绑定；默认采集全部发现雷达。"""
    if discovered is None:
        discovered = discover_on_subnet(cfg) if cfg.capture.auto_discover else []

    resolved: List[ResolvedLidar] = []
    unmatched = list(discovered)
    enabled = cfg.enabled_lidars

    def _take_dev(dev: DiscoveredLidar) -> None:
        if dev in unmatched:
            unmatched.remove(dev)

    for lidar_cfg in enabled:
        if _as_auto(lidar_cfg):
            continue
        mac_cfg = _norm_mac(str(lidar_cfg.get("mac") or ""))
        dev: Optional[DiscoveredLidar] = None

        if mac_cfg:
            for d in list(unmatched):
                if match_mac(mac_cfg, d.mac):
                    dev = d
                    break
        elif lidar_cfg.lidar_ip:
            for d in list(unmatched):
                if d.ip == lidar_cfg.lidar_ip:
                    dev = d
                    break
        elif unmatched:
            dev = unmatched[0]

        if dev is not None:
            _take_dev(dev)
            ip = dev.ip
            mac = dev.mac or mac_cfg
            msop = dev.msop_port or lidar_cfg.msop_port
            difop = dev.difop_port or lidar_cfg.difop_port
            grp = str(dev.extra.get("group_address") or lidar_cfg.group_address or "224.0.0.205")
            host = str(
                dev.extra.get("host_address")
                or local_ip_for_peer(ip)
                or lidar_cfg.host_address
                or "0.0.0.0"
            )
            from_disc = True
        else:
            ip = lidar_cfg.lidar_ip
            mac = mac_cfg
            msop = lidar_cfg.msop_port
            difop = lidar_cfg.difop_port
            grp = lidar_cfg.group_address
            host = lidar_cfg.host_address or local_ip_for_peer(ip) or "0.0.0.0"
            from_disc = False
            if cfg.capture.auto_discover and not ip:
                LOG.warning(
                    "%s: 未在子网 %s 发现匹配雷达（mac=%s）",
                    lidar_cfg.name,
                    cfg.capture.discover_subnet or "*",
                    mac_cfg or "(任意)",
                )

        resolved.append(
            ResolvedLidar(
                config=lidar_cfg,
                ip=ip,
                mac=mac,
                msop_port=msop,
                difop_port=difop,
                host_address=host,
                group_address=grp,
                lidar_type=lidar_cfg.lidar_type,
                from_discovery=from_disc,
            )
        )

    discover_all = cfg.capture.discover_all
    if discover_all and cfg.capture.auto_discover:
        for dev in list(unmatched):
            name = _next_auto_name(resolved)
            resolved.append(_make_auto_entry(dev, name))
            _take_dev(dev)
            LOG.info(
                "自动添加 %s -> ip=%s mac=%s msop=%d difop=%d",
                name, dev.ip, dev.mac or "-", dev.msop_port, dev.difop_port,
            )

    if any(_as_auto(l) for l in cfg.lidars):
        for dev in unmatched:
            name = _next_auto_name(resolved)
            resolved.append(_make_auto_entry(dev, name))

    return [r for r in resolved if r.config.enabled and r.ip]


def _as_auto(lidar_cfg: LidarConfig) -> bool:
    return bool(lidar_cfg.get("auto")) or str(lidar_cfg.name) in ("*", "auto")
