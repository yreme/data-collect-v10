"""本机网卡 / 网络工具。"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import time
from typing import Dict, List, Optional, Set, Tuple


def list_local_ipv4() -> List[str]:
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
    if bind_pattern and bind_pattern not in ("0.0.0.0", "", "*"):
        ips = local_ips_for_pattern(bind_pattern)
        if ips:
            return ips
    if subnet:
        ips = local_ips_for_pattern(subnet)
        if ips:
            return ips
    local = list_local_ipv4()
    return local if local else ["0.0.0.0"]


def list_interface_ipv4() -> List[Tuple[str, str]]:
    """返回 [(iface, ipv4), ...]，不含 loopback。"""
    out: List[Tuple[str, str]] = []
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
            if len(parts) < 4:
                continue
            iface = parts[1].rstrip(":")
            for i, part in enumerate(parts):
                if part == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    if ip and not ip.startswith("127."):
                        out.append((iface, ip))
    except Exception:  # noqa: BLE001
        pass
    return out


def list_up_interfaces() -> List[str]:
    """返回 state UP 的非 loopback 网卡名。"""
    ifaces: List[str] = []
    try:
        proc = subprocess.run(
            ["ip", "-o", "link", "show", "up"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            iface = parts[1].strip()
            if iface and iface != "lo":
                ifaces.append(iface)
    except Exception:  # noqa: BLE001
        pass
    return ifaces


def probe_ip_for_subnet(subnet: Optional[str]) -> str:
    """从子网通配符生成用于路由探测的 IP，如 192.168.1.* -> 192.168.1.1。"""
    pat = (subnet or "192.168.1.*").strip()
    if "*" in pat:
        return pat.replace("*", "1")
    if pat.count(".") == 3:
        return pat
    return "192.168.1.1"


def suggest_host_ip(subnet: Optional[str]) -> str:
    """根据子网建议本机应配置的 IP（Yesense 常见目标 192.168.1.102）。"""
    pat = (subnet or "192.168.1.*").strip()
    if "*" in pat:
        return pat.replace("*", "102")
    if pat.count(".") == 3:
        return pat.rsplit(".", 1)[0] + ".102"
    return "192.168.1.102"


def interface_for_peer(peer_ip: str) -> str:
    """通过路由表查找访问 peer_ip 时使用的网卡名。"""
    if not peer_ip:
        return ""
    try:
        proc = subprocess.run(
            ["ip", "route", "get", peer_ip],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        parts = proc.stdout.split()
        for i, part in enumerate(parts):
            if part == "dev" and i + 1 < len(parts):
                return parts[i + 1]
    except Exception:  # noqa: BLE001
        pass
    return ""


def resolve_sniff_interfaces(
    subnet: Optional[str] = None,
    bind_device: Optional[str] = None,
) -> List[str]:
    """为 tcpdump 被动嗅探选择网卡：优先显式指定，再按子网自动推断。"""
    if bind_device and bind_device.strip() not in ("", "any"):
        return [bind_device.strip()]

    ifaces: List[str] = []
    seen: Set[str] = set()

    def _add(name: str) -> None:
        n = (name or "").strip()
        if n and n not in seen and n != "lo":
            seen.add(n)
            ifaces.append(n)

    if subnet:
        for iface, ip in list_interface_ipv4():
            if ip_matches_pattern(ip, subnet):
                _add(iface)

    probe = probe_ip_for_subnet(subnet)
    route_iface = interface_for_peer(probe)
    if route_iface:
        _add(route_iface)

    with_ip = {iface for iface, _ in list_interface_ipv4()}
    for iface in list_up_interfaces():
        if iface not in with_ip:
            _add(iface)

    if not ifaces:
        return ["any"]
    return ifaces


def discovery_failure_hint(
    subnet: Optional[str],
    *,
    observed_dst_hosts: Optional[Set[str]] = None,
) -> str:
    """未发现 IMU 时给用户的配置提示。"""
    sub = (subnet or "192.168.1.*").strip()
    suggested = sorted(observed_dst_hosts) if observed_dst_hosts else [suggest_host_ip(sub)]
    ip_hint = " / ".join(suggested)
    return (
        f"未发现 IMU（子网 {sub}）。"
        f"请确认网线已连接；本机连接 IMU 的网卡需配置同网段 IP（例如 {ip_hint}）。"
        f"Yesense 出厂默认向固定目标 IP 发 UDP（端口 2368），"
        f"可用 sudo tcpdump -i any udp port 2368 查看 IMU 实际发往哪个地址。"
    )


def local_ip_for_peer(peer_ip: str) -> Optional[str]:
    if not peer_ip:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((peer_ip, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return None


def load_arp_table() -> Dict[str, str]:
    """mac -> ip"""
    table: Dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "lladdr":
                ip = parts[0]
                mac = parts[4].lower()
                table[mac] = ip
    except Exception:  # noqa: BLE001
        pass
    return table


def mac_for_ip(ip: str, arp: Optional[Dict[str, str]] = None) -> str:
    arp = arp or load_arp_table()
    for mac, aip in arp.items():
        if aip == ip:
            return mac
    return ""


def sniff_udp_for_yesense(
    ports: List[int],
    *,
    host: str = "0.0.0.0",
    timeout_sec: float = 3.0,
    subnet: Optional[str] = None,
) -> Dict[Tuple[str, int], int]:
    """返回 {(src_ip, port): packet_count} 含 Yesense 帧头的 UDP 包。"""
    from ..protocol.yesense_decoder import has_yesense_header

    hits: Dict[Tuple[str, int], int] = {}
    socks = []
    deadline = time.monotonic() + timeout_sec
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.setblocking(False)
            socks.append((port, s))
        except OSError:
            continue
    try:
        while time.monotonic() < deadline:
            for port, s in socks:
                try:
                    data, addr = s.recvfrom(65535)
                except BlockingIOError:
                    continue
                except Exception:  # noqa: BLE001
                    continue
                src_ip = addr[0]
                if subnet and not ip_matches_pattern(src_ip, subnet):
                    continue
                if has_yesense_header(data):
                    key = (src_ip, port)
                    hits[key] = hits.get(key, 0) + 1
            time.sleep(0.01)
    finally:
        for _, s in socks:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return hits


def probe_tcp_yesense(ip: str, port: int, timeout: float = 1.0) -> bool:
    from ..protocol.yesense_decoder import has_yesense_header

    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.settimeout(timeout)
        data = s.recv(4096)
        s.close()
        return bool(data) and has_yesense_header(data)
    except Exception:  # noqa: BLE001
        return False
