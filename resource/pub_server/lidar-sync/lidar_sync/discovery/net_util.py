"""本机网卡 / 与雷达通信相关的网络工具。"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import time
from typing import Dict, List, Tuple


def list_local_ipv4() -> List[str]:
    """枚举本机非 loopback 的 IPv4 地址。"""
    ips: List[str] = []
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    if ip and not ip.startswith("127."):
                        ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if not ips:
        try:
            host = socket.gethostname()
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ips.append(ip)
        except Exception:  # noqa: BLE001
            pass
    return sorted(set(ips))


def ip_matches_pattern(ip: str, pattern: str) -> bool:
    pat = (pattern or "").strip()
    if not pat or pat in ("0.0.0.0", "*"):
        return True
    rx = "^" + pat.replace(".", r"\.").replace("*", r"\d+") + "$"
    return bool(re.match(rx, ip))


def local_ips_for_pattern(pattern: Optional[str]) -> List[str]:
    """解析 LIDAR_LOCAL_IP / bind_interface。

    - ``192.168.1.103`` → 若本机有则 ``[192.168.1.103]``，否则仍尝试该地址
    - ``192.168.1.*`` → 本机所有落在该网段的 IPv4（多网卡穷举）
    - ``0.0.0.0`` / 空 → 本机全部 IPv4
    """
    pat = (pattern or "").strip()
    if not pat or pat == "0.0.0.0":
        return list_local_ipv4()
    if "*" not in pat and pat.count(".") == 3:
        local = list_local_ipv4()
        return [pat] if pat in local else [pat]
    matched = [ip for ip in list_local_ipv4() if ip_matches_pattern(ip, pat)]
    return matched if matched else []


def resolve_bind_interfaces(
    bind_pattern: Optional[str],
    subnet: Optional[str] = None,
) -> List[str]:
    """返回用于 UDP 嗅探/组播 join 的本机地址列表。"""
    if bind_pattern and bind_pattern not in ("0.0.0.0", ""):
        ips = local_ips_for_pattern(bind_pattern)
        if ips:
            return ips
    if subnet:
        ips = [ip for ip in list_local_ipv4() if ip_matches_pattern(ip, subnet)]
        if ips:
            return ips
    return list_local_ipv4() or ["0.0.0.0"]


def local_ip_for_peer(peer_ip: str, port: int = 1) -> str:
    """返回访问 peer_ip 时本机将使用的源 IPv4（无需真正发包）。"""
    if not peer_ip:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer_ip, port))
            return s.getsockname()[0]
    except OSError:
        return ""


def is_multicast_address(addr: str) -> bool:
    a = (addr or "").strip()
    if not a or a == "0.0.0.0":
        return False
    try:
        first = int(a.split(".")[0])
        return 224 <= first <= 239
    except (ValueError, IndexError):
        return False


def resolve_host_address(
    lidar_ip: str,
    configured: str = "0.0.0.0",
    group_address: str = "0.0.0.0",
) -> str:
    """解析 rs_driver 的 host_address。

    组播模式必须指定本机网卡 IP（用于 IP_ADD_MEMBERSHIP），不能为 0.0.0.0。
    """
    cfg = (configured or "0.0.0.0").strip()
    if is_multicast_address(group_address):
        if cfg and cfg != "0.0.0.0":
            return cfg
        if lidar_ip:
            lip = local_ip_for_peer(lidar_ip)
            if lip:
                return lip
        return cfg or "0.0.0.0"
    if cfg and cfg != "0.0.0.0":
        return cfg
    if lidar_ip:
        lip = local_ip_for_peer(lidar_ip)
        if lip:
            return lip
    return "0.0.0.0"


def _join_multicast(sock: socket.socket, group: str, iface: str) -> None:
    mreq = struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def sniff_udp_ports(
    msop_port: int,
    difop_port: int,
    *,
    host: str = "0.0.0.0",
    group_address: str = "0.0.0.0",
    timeout_sec: float = 2.0,
) -> Tuple[int, int, Dict[int, str]]:
    """在 timeout 内监听 MSOP/DIFOP 端口。返回 (msop_count, difop_count, {port: src_ip})。"""
    ports = [p for p in (msop_port, difop_port) if p > 0]
    if not ports:
        return 0, 0, {}

    bind_host = host if is_multicast_address(group_address) else host
    join_iface = host if is_multicast_address(group_address) and host != "0.0.0.0" else ""

    sockets = []
    counts = {msop_port: 0, difop_port: 0}
    sources: Dict[int, str] = {}
    try:
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host if not is_multicast_address(group_address) else "", port))
            if is_multicast_address(group_address) and join_iface:
                _join_multicast(s, group_address, join_iface)
            s.setblocking(False)
            sockets.append((port, s))

        deadline = time.monotonic() + max(0.1, timeout_sec)
        while time.monotonic() < deadline:
            for port, s in sockets:
                try:
                    data, addr = s.recvfrom(65535)
                except BlockingIOError:
                    continue
                except OSError:
                    continue
                if not data:
                    continue
                counts[port] = counts.get(port, 0) + 1
                sources.setdefault(port, addr[0])
            time.sleep(0.02)
    finally:
        for _, s in sockets:
            try:
                s.close()
            except OSError:
                pass

    return counts.get(msop_port, 0), counts.get(difop_port, 0), sources
