"""局域网雷达/设备发现。"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..logging_setup import get_logger

LOG = get_logger("discovery")

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")

_discover_lock = threading.Lock()
_discover_cache: List["DiscoveredLidar"] = []
_discover_cache_ts: float = 0.0
_DISCOVER_CACHE_TTL = 20.0


@dataclass
class DiscoveredLidar:
    ip: str
    mac: str = ""
    model: str = ""
    msop_port: int = 6699
    difop_port: int = 7788
    hostname: str = ""
    is_likely_lidar: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    def suggested_name(self, index: int) -> str:
        return f"lidar{index}"

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "model": self.model,
            "msop_port": self.msop_port,
            "difop_port": self.difop_port,
            "hostname": self.hostname,
            "is_likely_lidar": self.is_likely_lidar,
        }


def _norm_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def load_arp_table() -> Dict[str, str]:
    """MAC -> IP。"""
    out: Dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[2] == "lladdr":
                ip, mac = parts[0], parts[4].lower()
                if _MAC_RE.match(mac):
                    out[mac] = ip
    except Exception as exc:  # noqa: BLE001
        LOG.debug("读取 ARP 失败: %s", exc)
    return out


def filter_by_subnet(devices: List[DiscoveredLidar], subnet: Optional[str]) -> List[DiscoveredLidar]:
    if not subnet:
        return devices
    pat = subnet.strip().replace("*", ".*")
    if not pat.endswith(".*") and "." in pat and not pat.endswith("."):
        try:
            net = ipaddress.ip_network(pat if "/" in pat else pat + "/32", strict=False)
            return [d for d in devices if ipaddress.ip_address(d.ip) in net]
        except ValueError:
            pass
    rx = re.compile("^" + pat.replace(".", r"\."))
    return [d for d in devices if rx.match(d.ip)]


def _ping_sweep(subnet: str) -> None:
    """触发 ARP 缓存填充。"""
    base = subnet.replace("*", "0").rstrip(".0")
    if "/" in base:
        try:
            net = ipaddress.ip_network(base, strict=False)
            hosts = list(net.hosts())[:254]
        except ValueError:
            return
    else:
        parts = base.split(".")
        if len(parts) != 3:
            return
        prefix = ".".join(parts[:3])
        hosts = [f"{prefix}.{i}" for i in range(1, 255)]

    def _ping(ip: str) -> None:
        try:
            subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True, timeout=3, check=False,
            )
        except Exception:  # noqa: BLE001
            pass

    threads = []
    for ip in hosts[:64]:
        t = threading.Thread(target=_ping, args=(str(ip),), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=4.0)


def discover_lidars(subnet: Optional[str] = None, *, do_ping: bool = True) -> List[DiscoveredLidar]:
    """扫描局域网设备（ARP + 可选 ping），返回 IP/MAC 列表。"""
    global _discover_cache, _discover_cache_ts
    now = time.monotonic()
    with _discover_lock:
        if _discover_cache and (now - _discover_cache_ts) < _DISCOVER_CACHE_TTL:
            return filter_by_subnet(list(_discover_cache), subnet)

    if do_ping and subnet:
        LOG.info("正在 ping 扫描子网 %s …", subnet)
        _ping_sweep(subnet)

    arp = load_arp_table()
    devices: List[DiscoveredLidar] = []
    seen_ips: set = set()
    for mac, ip in arp.items():
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        dev = DiscoveredLidar(ip=ip, mac=mac)
        devices.append(dev)

    devices = filter_by_subnet(devices, subnet)
    with _discover_lock:
        _discover_cache = devices
        _discover_cache_ts = time.monotonic()
    return list(devices)


def invalidate_discover_cache() -> None:
    global _discover_cache_ts
    _discover_cache_ts = 0.0


def enrich_mac_from_arp(lidars: List[DiscoveredLidar]) -> List[DiscoveredLidar]:
    arp = load_arp_table()
    for l in lidars:
        if l.mac:
            continue
        for mac, ip in arp.items():
            if ip == l.ip:
                l.mac = mac
                break
    return lidars


def resolve_lidar_ip(
    *,
    mac: Optional[str] = None,
    lidar_ip: Optional[str] = None,
    subnet: Optional[str] = None,
) -> Optional[DiscoveredLidar]:
    if lidar_ip:
        return DiscoveredLidar(ip=str(lidar_ip), mac=_norm_mac(mac) if mac else "")
    want_mac = _norm_mac(mac) if mac else ""
    if not want_mac:
        return None
    found = discover_lidars(subnet=subnet)
    for l in found:
        if l.mac and _norm_mac(l.mac) == want_mac:
            return l
    arp = load_arp_table()
    ip = arp.get(want_mac)
    if ip:
        return DiscoveredLidar(ip=ip, mac=want_mac)
    return None
