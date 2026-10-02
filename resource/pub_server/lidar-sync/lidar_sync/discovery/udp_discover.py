"""基于 UDP 包特征自动发现 RoboSense 雷达（MSOP 1200B + DIFOP 256B）。"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..sync_grid import DIFOP_PACKET_SIZE, MSOP_PACKET_SIZE
from .lidar_scan import DiscoveredLidar, filter_by_subnet, load_arp_table
from .net_util import is_multicast_address, local_ip_for_peer, resolve_host_address

DEFAULT_MSOP_PORTS = tuple(range(6695, 6710))
DEFAULT_DIFOP_PORTS = tuple(range(7783, 7793))
DEFAULT_MULTICAST_GROUPS = ("224.0.0.205", "224.0.0.102")


def _norm_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


@dataclass
class _IpTraffic:
    msop_ports: Dict[int, int] = field(default_factory=dict)
    difop_ports: Dict[int, int] = field(default_factory=dict)
    group_addresses: Set[str] = field(default_factory=set)


def _join_multicast(sock: socket.socket, group: str, iface: str) -> None:
    mreq = struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def _best_port(counts: Dict[int, int]) -> int:
    if not counts:
        return 0
    return max(counts.items(), key=lambda kv: kv[1])[0]


def sniff_lidar_signatures(
    *,
    timeout_sec: float = 3.0,
    host: str = "0.0.0.0",
    group_addresses: Tuple[str, ...] = DEFAULT_MULTICAST_GROUPS,
    msop_ports: Tuple[int, ...] = DEFAULT_MSOP_PORTS,
    difop_ports: Tuple[int, ...] = DEFAULT_DIFOP_PORTS,
    bind_interface: Optional[str] = None,
    bind_interfaces: Optional[List[str]] = None,
) -> Dict[str, _IpTraffic]:
    """监听常见端口，按源 IP 统计 MSOP(1200B)/DIFOP(256B) 包。"""
    ifaces = bind_interfaces if bind_interfaces else (
        [bind_interface] if bind_interface else [host if host != "0.0.0.0" else ""]
    )
    merged: Dict[str, _IpTraffic] = {}
    per_iface_timeout = max(0.2, timeout_sec / max(1, len(ifaces)))
    for iface in ifaces:
        partial = _sniff_on_interface(
            iface=iface or "",
            timeout_sec=per_iface_timeout,
            group_addresses=group_addresses,
            msop_ports=msop_ports,
            difop_ports=difop_ports,
        )
        for ip, sig in partial.items():
            dst = merged.setdefault(ip, _IpTraffic())
            for port, cnt in sig.msop_ports.items():
                dst.msop_ports[port] = dst.msop_ports.get(port, 0) + cnt
            for port, cnt in sig.difop_ports.items():
                dst.difop_ports[port] = dst.difop_ports.get(port, 0) + cnt
            dst.group_addresses.update(sig.group_addresses)
            if iface and "bind_interface" not in dst.__dict__:
                pass
    return merged


def _sniff_on_interface(
    *,
    iface: str,
    timeout_sec: float,
    group_addresses: Tuple[str, ...],
    msop_ports: Tuple[int, ...],
    difop_ports: Tuple[int, ...],
) -> Dict[str, _IpTraffic]:
    """在单个本机地址上嗅探（组播 join 到该网卡）。"""
    ports = sorted(set(msop_ports) | set(difop_ports))
    sockets: List[Tuple[int, socket.socket]] = []
    traffic: Dict[str, _IpTraffic] = {}

    try:
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
            except OSError:
                s.close()
                continue
            if iface:
                for grp in group_addresses:
                    if is_multicast_address(grp):
                        try:
                            _join_multicast(s, grp, iface)
                        except OSError:
                            pass
            s.setblocking(False)
            sockets.append((port, s))

        deadline = time.monotonic() + max(0.2, timeout_sec)
        while time.monotonic() < deadline:
            for port, s in sockets:
                try:
                    data, addr = s.recvfrom(65535)
                except BlockingIOError:
                    continue
                except OSError:
                    continue
                if not data or not addr:
                    continue
                src_ip = addr[0]
                n = len(data)
                sig = traffic.setdefault(src_ip, _IpTraffic())
                if n == MSOP_PACKET_SIZE:
                    sig.msop_ports[port] = sig.msop_ports.get(port, 0) + 1
                elif n == DIFOP_PACKET_SIZE:
                    sig.difop_ports[port] = sig.difop_ports.get(port, 0) + 1
            time.sleep(0.005)
    finally:
        for _, s in sockets:
            try:
                s.close()
            except OSError:
                pass

    return traffic


def discover_lidars_udp(
    *,
    subnet: Optional[str] = None,
    timeout_sec: float = 3.0,
    bind_interface: Optional[str] = None,
    bind_interfaces: Optional[List[str]] = None,
    sample_ip: Optional[str] = None,
) -> List[DiscoveredLidar]:
    """发现子网内疑似雷达：同一 IP 同时出现 MSOP(1200) 与 DIFOP(256)。"""
    from .net_util import local_ip_for_peer, resolve_bind_interfaces, resolve_host_address

    ifaces = bind_interfaces
    if not ifaces:
        if bind_interface:
            from .net_util import local_ips_for_pattern
            ifaces = local_ips_for_pattern(bind_interface)
        else:
            ifaces = resolve_bind_interfaces(None, subnet)
    if not ifaces and sample_ip:
        lip = local_ip_for_peer(sample_ip)
        ifaces = [lip] if lip else []

    traffic = sniff_lidar_signatures(
        timeout_sec=timeout_sec,
        bind_interfaces=ifaces,
    )
    arp = load_arp_table()
    ip_to_mac = {ip: mac for mac, ip in arp.items()}

    out: List[DiscoveredLidar] = []
    for ip, sig in traffic.items():
        if not sig.msop_ports or not sig.difop_ports:
            continue
        msop_port = _best_port(sig.msop_ports)
        difop_port = _best_port(sig.difop_ports)
        mac = ip_to_mac.get(ip, "")
        host = local_ip_for_peer(ip) or (ifaces[0] if ifaces else "")
        dev = DiscoveredLidar(
            ip=ip,
            mac=mac,
            msop_port=msop_port,
            difop_port=difop_port,
            is_likely_lidar=True,
            extra={
                "group_address": next(iter(sig.group_addresses), "224.0.0.205"),
                "host_address": host or resolve_host_address(ip, "0.0.0.0", "224.0.0.205"),
                "msop_packets": str(sum(sig.msop_ports.values())),
                "difop_packets": str(sum(sig.difop_ports.values())),
            },
        )
        out.append(dev)

    out.sort(key=lambda d: d.ip)
    return filter_by_subnet(out, subnet)


def match_mac(configured_mac: str, discovered_mac: str) -> bool:
    a = _norm_mac(configured_mac)
    b = _norm_mac(discovered_mac)
    if not a or not b:
        return False
    return a == b
