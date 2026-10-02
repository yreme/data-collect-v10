"""启动前探测雷达是否在线、端口是否可用。"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from typing import Optional

from .net_util import is_multicast_address, resolve_host_address, sniff_udp_ports
from ..config import LidarConfig
from ..logging_setup import get_logger
from .lidar_scan import load_arp_table, resolve_lidar_ip

LOG = get_logger("discovery.probe")


@dataclass
class ProbeResult:
    reachable: bool
    ip: str
    mac: str = ""
    msop_port_free: bool = True
    difop_port_free: bool = True
    reason: str = ""
    suggested_host_address: str = ""
    udp_msop_packets: int = 0
    udp_difop_packets: int = 0
    udp_source_ip: str = ""

    @property
    def ok_to_capture(self) -> bool:
        return self.reachable and self.msop_port_free and self.difop_port_free

    @property
    def udp_ok(self) -> bool:
        return self.udp_msop_packets > 0 or self.udp_difop_packets > 0


def ping_host(ip: str, timeout_sec: float = 1.0) -> bool:
    if not ip:
        return False
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, int(timeout_sec))), ip],
            capture_output=True,
            timeout=timeout_sec + 1.0,
            check=False,
        )
        if proc.returncode != 0:
            return False
        out = (proc.stdout or b"").decode(errors="ignore").lower()
        return "1 received" in out or "1 packets received" in out
    except Exception:  # noqa: BLE001
        return False


def is_ip_in_arp(ip: str) -> bool:
    arp = load_arp_table()
    return ip in arp.values()


def is_udp_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """检测本机 UDP 端口是否可绑定（未被其它进程占用）。"""
    if port <= 0:
        return True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False


def probe_lidar(
    lidar_cfg: LidarConfig,
    *,
    subnet: Optional[str] = None,
    check_ports: bool = True,
) -> ProbeResult:
    """探测单台雷达：IP 是否可达、采集端口是否空闲。"""
    ip = lidar_cfg.lidar_ip
    mac = str(lidar_cfg.get("mac") or "")

    if not ip and mac:
        r = resolve_lidar_ip(mac=mac, subnet=subnet)
        if r:
            ip, mac = r.ip, r.mac or mac

    if not ip:
        # 未配置 IP/MAC：仅检查端口，允许采集（雷达主动推 UDP 到本机）
        msop_ok = is_udp_port_free(lidar_cfg.msop_port) if check_ports else True
        difop_ok = is_udp_port_free(lidar_cfg.difop_port) if check_ports else True
        reason = ""
        if not msop_ok:
            reason = f"MSOP 端口 {lidar_cfg.msop_port} 已被占用"
        elif not difop_ok:
            reason = f"DIFOP 端口 {lidar_cfg.difop_port} 已被占用"
        return ProbeResult(
            reachable=True,
            ip="",
            mac=mac,
            msop_port_free=msop_ok,
            difop_port_free=difop_ok,
            reason=reason,
        )

    reachable = ping_host(ip) or is_ip_in_arp(ip)
    if not reachable:
        return ProbeResult(
            reachable=False,
            ip=ip,
            mac=mac,
            reason=f"雷达 {ip} 不在线（ping/ARP 均无响应）",
        )

    if not mac:
        arp = load_arp_table()
        for m, aip in arp.items():
            if aip == ip:
                mac = m
                break

    msop_ok = is_udp_port_free(lidar_cfg.msop_port) if check_ports else True
    difop_ok = is_udp_port_free(lidar_cfg.difop_port) if check_ports else True
    suggested_host = resolve_host_address(ip, lidar_cfg.host_address, lidar_cfg.group_address)
    grp = (lidar_cfg.group_address or "0.0.0.0").strip()
    udp_msop = udp_difop = 0
    udp_src = ""
    if msop_ok and difop_ok:
        sniff_host = suggested_host if is_multicast_address(grp) else "0.0.0.0"
        udp_msop, udp_difop, srcs = sniff_udp_ports(
            lidar_cfg.msop_port,
            lidar_cfg.difop_port,
            host=sniff_host,
            group_address=grp,
            timeout_sec=2.0,
        )
        if srcs:
            udp_src = next(iter(srcs.values()))

    reason = ""
    if not msop_ok:
        reason = f"MSOP 端口 {lidar_cfg.msop_port} 已被占用"
    elif not difop_ok:
        reason = f"DIFOP 端口 {lidar_cfg.difop_port} 已被占用"
    elif not udp_msop and not udp_difop:
        if is_multicast_address(grp):
            reason = (
                f"2s 内未收到组播 MSOP/DIFOP（group={grp}，本机接口={suggested_host or '?'}，"
                f"端口 {lidar_cfg.msop_port}/{lidar_cfg.difop_port}）。"
                f"请确认 server.yaml 中 group_address/host_address 与 tcpdump 一致"
            )
        else:
            host_hint = suggested_host or "本机与雷达同网段的 IP"
            reason = (
                f"ping 通但 2s 内未收到 MSOP/DIFOP UDP（端口 {lidar_cfg.msop_port}/{lidar_cfg.difop_port}）。"
                f"请到雷达 Web 将目的 IP 设为 {host_hint}，或配置 group_address 组播地址"
            )

    return ProbeResult(
        reachable=True,
        ip=ip,
        mac=mac,
        msop_port_free=msop_ok,
        difop_port_free=difop_ok,
        reason=reason,
        suggested_host_address=suggested_host,
        udp_msop_packets=udp_msop,
        udp_difop_packets=udp_difop,
        udp_source_ip=udp_src,
    )


def probe_all_configured(
    lidars: list,
    *,
    subnet: Optional[str] = None,
) -> dict:
    """批量探测，返回 name -> ProbeResult。"""
    out = {}
    for l in lidars:
        out[l.name] = probe_lidar(l, subnet=subnet)
    return out
