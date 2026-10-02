"""GigE 相机发现（GVCP 广播 + 预设序列号/MAC 匹配）。"""

from __future__ import annotations

import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from multimodal_common.logging_setup import get_logger

LOG = get_logger("discovery")
GVCP_PORT = 3956
GVCP_DISCOVERY_CMD = 0x0002


@dataclass
class DiscoveredCamera:
    name: str = ""
    serial: str = ""
    ip: str = ""
    mac: str = ""
    model: str = ""


def _normalize_mac(mac: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", mac).lower()


def _expand_subnet(subnet: str) -> List[str]:
    if subnet.endswith(".*"):
        base = subnet[:-2]
        return [f"{base}.{i}" for i in range(1, 255)]
    return [subnet]


def _gvcp_discover(timeout: float = 2.0) -> List[DiscoveredCamera]:
    """发送 GVCP Discovery 广播，解析应答。"""
    results: List[DiscoveredCamera] = []
    seen: set = set()
    pkt = struct.pack(">BBHH", 0x42, 0x01, GVCP_DISCOVERY_CMD, 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    try:
        sock.sendto(pkt, ("255.255.255.255", GVCP_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                serial = ""
                model = ""
                if len(data) > 48:
                    try:
                        serial = data[48:80].split(b"\x00")[0].decode("ascii", errors="replace")
                        model = data[16:48].split(b"\x00")[0].decode("ascii", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
                results.append(DiscoveredCamera(
                    serial=serial, ip=ip, model=model,
                    name=f"cam_{serial[-4:]}" if serial else f"cam_{ip.split('.')[-1]}",
                ))
            except socket.timeout:
                continue
    finally:
        sock.close()
    return results


def discover_cameras(subnet: str = "192.168.1.*", timeout: float = 2.0) -> List[DiscoveredCamera]:
    """发现局域网 GigE 相机。"""
    LOG.info("GVCP 发现相机（子网 %s）…", subnet)
    devs = _gvcp_discover(timeout=timeout)
    if subnet and subnet != "*":
        prefix = subnet.replace(".*", "")
        devs = [d for d in devs if d.ip.startswith(prefix)]
    for d in devs:
        LOG.info("  发现相机 ip=%s serial=%s model=%s", d.ip, d.serial or "-", d.model or "-")
    return devs


def resolve_cameras(
    preset: List[dict],
    discovered: List[DiscoveredCamera],
    *,
    discover_all: bool = True,
) -> List[DiscoveredCamera]:
    """将预设配置与发现结果合并：优先按 serial/MAC 匹配。"""
    by_serial: Dict[str, DiscoveredCamera] = {d.serial: d for d in discovered if d.serial}
    by_mac: Dict[str, DiscoveredCamera] = {_normalize_mac(d.mac): d for d in discovered if d.mac}
    by_ip: Dict[str, DiscoveredCamera] = {d.ip: d for d in discovered}
    matched_ips: set = set()
    out: List[DiscoveredCamera] = []

    for i, raw in enumerate(preset):
        name = str(raw.get("name") or f"cam{i}")
        serial = str(raw.get("serial") or "")
        mac = _normalize_mac(str(raw.get("mac") or ""))
        ip = str(raw.get("ip") or "")
        dev: Optional[DiscoveredCamera] = None
        if serial and serial in by_serial:
            dev = by_serial[serial]
        elif mac and mac in by_mac:
            dev = by_mac[mac]
        elif ip and ip in by_ip:
            dev = by_ip[ip]
        if dev:
            dev.name = name
            matched_ips.add(dev.ip)
            out.append(dev)
        elif ip:
            out.append(DiscoveredCamera(name=name, serial=serial, ip=ip, mac=raw.get("mac", "")))
        elif not discover_all:
            out.append(DiscoveredCamera(name=name, serial=serial))

    if discover_all:
        for d in discovered:
            if d.ip not in matched_ips:
                out.append(d)
    return out
