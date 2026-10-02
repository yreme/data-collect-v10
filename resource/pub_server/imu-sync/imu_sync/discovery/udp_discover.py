"""基于 UDP 包特征自动发现 Yesense 网络 IMU。

tcpdump 示例::

    192.168.1.201.2369 > 192.168.1.102.2368: UDP, length 67

- **2369** = IMU 源端口（imu_src_port，仅用于识别/过滤）
- **2368** = 本机目的端口（port，程序 bind 监听此端口）
- **length 67** = tcpdump UDP 长度（含 8B 头）；recvfrom payload 通常为 59B
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..logging_setup import get_logger
from .net_util import (
    discovery_failure_hint,
    ip_matches_pattern,
    load_arp_table,
    local_ip_for_peer,
    resolve_bind_interfaces,
    resolve_sniff_interfaces,
)

LOG = get_logger("discovery.udp")

IMU_TCPDUMP_LENGTH = 67
IMU_PAYLOAD_SIZE = 59
IMU_PAYLOAD_SIZES = (59, 67)
IMU_DEFAULT_SRC_PORT = 2369
IMU_DEFAULT_LOCAL_PORT = 2368
DEFAULT_LOCAL_PORTS = (IMU_DEFAULT_LOCAL_PORT,)
SCAN_LOCAL_PORTS = tuple(range(2360, 2381))

_TCPDUMP_RE = re.compile(
    r"IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+): UDP, length (\d+)"
)


def normalize_packet_sizes(
    packet_size: Optional[int] = None,
    packet_sizes: Optional[List[int]] = None,
) -> Tuple[int, ...]:
    if packet_sizes:
        return tuple(sorted(set(int(s) for s in packet_sizes)))
    if packet_size and packet_size > 0:
        if packet_size == IMU_TCPDUMP_LENGTH:
            return IMU_PAYLOAD_SIZES
        if packet_size == IMU_PAYLOAD_SIZE:
            return (IMU_PAYLOAD_SIZE,)
        return (packet_size, packet_size - 8) if packet_size > 8 else (packet_size,)
    return IMU_PAYLOAD_SIZES


def is_imu_payload_size(n: int, sizes: Tuple[int, ...]) -> bool:
    return n in sizes


def resolve_bind_hosts(
    bind_interfaces: Optional[List[str]] = None,
    subnet: Optional[str] = None,
) -> List[str]:
    """解析本机 bind 地址：按子网自动匹配网卡 IP，无需 IMU_LOCAL_IP。"""
    pattern = None
    if bind_interfaces:
        for pat in bind_interfaces:
            pat = (pat or "").strip()
            if pat and pat not in ("0.0.0.0", "*"):
                pattern = pat
                break
    hosts = resolve_bind_interfaces(pattern, subnet)
    uniq = list(dict.fromkeys(hosts))
    return uniq if uniq else ["0.0.0.0"]


@dataclass
class _IpTraffic:
    local_ports: Dict[int, int] = field(default_factory=dict)
    src_ports: Set[int] = field(default_factory=set)
    dst_hosts: Set[str] = field(default_factory=set)
    packets: int = 0


def _merge_traffic(dst: Dict[str, _IpTraffic], src: Dict[str, _IpTraffic]) -> None:
    for ip, sig in src.items():
        d = dst.setdefault(ip, _IpTraffic())
        for port, cnt in sig.local_ports.items():
            d.local_ports[port] = d.local_ports.get(port, 0) + cnt
        d.src_ports.update(sig.src_ports)
        d.dst_hosts.update(sig.dst_hosts)
        d.packets += sig.packets


def sniff_imu_signatures(
    *,
    timeout_sec: float = 5.0,
    packet_sizes: Tuple[int, ...] = IMU_PAYLOAD_SIZES,
    local_ports: Tuple[int, ...] = DEFAULT_LOCAL_PORTS,
    bind_hosts: Optional[List[str]] = None,
    subnet: Optional[str] = None,
) -> Dict[str, _IpTraffic]:
    """在本机 UDP 端口上嗅探（socket bind）。"""
    hosts = bind_hosts or ["0.0.0.0"]
    merged: Dict[str, _IpTraffic] = {}
    per_host_timeout = max(0.5, timeout_sec / max(1, len(hosts)))
    for host in hosts:
        partial = _sniff_on_bind(
            bind_host=host,
            timeout_sec=per_host_timeout,
            packet_sizes=packet_sizes,
            local_ports=local_ports,
            subnet=subnet,
        )
        _merge_traffic(merged, partial)
    return merged


def sniff_via_tcpdump(
    *,
    interface: Optional[str] = None,
    local_ports: Tuple[int, ...] = DEFAULT_LOCAL_PORTS,
    udp_lengths: Tuple[int, ...] = (IMU_TCPDUMP_LENGTH,),
    subnet: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> Dict[str, _IpTraffic]:
    """被动嗅探（与 tcpdump 相同，无需 bind 端口）。"""
    iface = (interface or "any").strip()
    port_expr = " or ".join(f"dst port {p}" for p in local_ports)
    cmd = ["tcpdump", "-i", iface, "-n", "-l", "udp", "and", f"({port_expr})"]
    LOG.info("tcpdump 被动嗅探: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        LOG.warning("未安装 tcpdump，跳过被动嗅探（可: sudo apt install tcpdump）")
        return {}
    except OSError as exc:
        LOG.warning("启动 tcpdump 失败: %s", exc)
        return {}

    traffic: Dict[str, _IpTraffic] = {}
    deadline = time.monotonic() + max(1.0, timeout_sec)
    try:
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            m = _TCPDUMP_RE.search(line)
            if not m:
                continue
            src_ip, src_port_s, dst_ip, dst_port_s, udp_len_s = m.groups()
            udp_len = int(udp_len_s)
            if udp_len not in udp_lengths:
                continue
            if subnet and not ip_matches_pattern(src_ip, subnet):
                continue
            dst_port = int(dst_port_s)
            if local_ports and dst_port not in local_ports:
                continue
            sig = traffic.setdefault(src_ip, _IpTraffic())
            sig.local_ports[dst_port] = sig.local_ports.get(dst_port, 0) + 1
            sig.src_ports.add(int(src_port_s))
            sig.dst_hosts.add(dst_ip)
            sig.packets += 1
            LOG.debug("tcpdump 命中: %s", line.strip())
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    if traffic:
        LOG.info(
            "tcpdump 发现 %d 个 IMU 源（本机 dst 端口 %s）",
            len(traffic), list(local_ports),
        )
    else:
        err = (proc.stderr.read() if proc.stderr else "")[:300]
        if "Permission denied" in err or "perm" in err.lower():
            LOG.warning("tcpdump 需要权限，请用 sudo 运行或: sudo setcap cap_net_raw,cap_net_admin+eip $(which tcpdump)")
        elif err:
            LOG.debug("tcpdump stderr: %s", err)
    return traffic


def _sniff_on_bind(
    *,
    bind_host: str,
    timeout_sec: float,
    packet_sizes: Tuple[int, ...],
    local_ports: Tuple[int, ...],
    subnet: Optional[str],
) -> Dict[str, _IpTraffic]:
    sockets: List[Tuple[int, socket.socket]] = []
    traffic: Dict[str, _IpTraffic] = {}
    size_hist: Dict[int, int] = {}
    bind_failures: List[str] = []

    try:
        for port in local_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind_host, port))
            except OSError as exc:
                bind_failures.append(f"{bind_host}:{port} ({exc})")
                s.close()
                continue
            s.setblocking(False)
            sockets.append((port, s))
            LOG.info("已绑定 UDP 本机 %s:%d（等待 IMU 发往此端口）", bind_host, port)

        if bind_failures:
            LOG.warning("绑定失败: %s", "; ".join(bind_failures))
        if not sockets:
            LOG.error(
                "无法绑定端口 %s，请检查占用: ss -ulnp | grep -E '%s'",
                list(local_ports), "|".join(str(p) for p in local_ports),
            )
            return traffic

        deadline = time.monotonic() + max(0.5, timeout_sec)
        while time.monotonic() < deadline:
            for local_port, s in sockets:
                try:
                    data, addr = s.recvfrom(65535)
                except BlockingIOError:
                    continue
                except OSError:
                    continue
                if not data or not addr:
                    continue
                src_ip, src_port = addr[0], addr[1]
                n = len(data)
                size_hist[n] = size_hist.get(n, 0) + 1
                if subnet and not ip_matches_pattern(src_ip, subnet):
                    continue
                if not is_imu_payload_size(n, packet_sizes):
                    continue
                sig = traffic.setdefault(src_ip, _IpTraffic())
                sig.local_ports[local_port] = sig.local_ports.get(local_port, 0) + 1
                sig.src_ports.add(src_port)
                sig.packets += 1
            time.sleep(0.005)
    finally:
        for _, s in sockets:
            try:
                s.close()
            except OSError:
                pass

    if not traffic and size_hist:
        LOG.warning(
            "本机 %s 收到 UDP 但 payload 长度不匹配 %s，实际: %s",
            bind_host, list(packet_sizes), dict(sorted(size_hist.items())),
        )
    elif not traffic and not size_hist:
        LOG.warning(
            "本机 %s 在 %.1fs 内未收到 UDP（监听端口 %s）",
            bind_host, timeout_sec, [p for p, _ in sockets] or list(local_ports),
        )
    return traffic


def _best_port(counts: Dict[int, int]) -> int:
    if not counts:
        return IMU_DEFAULT_LOCAL_PORT
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _traffic_to_devices(
    traffic: Dict[str, _IpTraffic],
    *,
    bind_host: str,
    sizes: Tuple[int, ...],
) -> List["DiscoveredNetworkImu"]:
    from .network_discover import DiscoveredNetworkImu

    arp = load_arp_table()
    ip_to_mac = {ip: mac for mac, ip in arp.items()}
    out: List[DiscoveredNetworkImu] = []
    for ip, sig in traffic.items():
        if not sig.local_ports:
            continue
        local_port = _best_port(sig.local_ports)
        src_port = (
            IMU_DEFAULT_SRC_PORT
            if IMU_DEFAULT_SRC_PORT in sig.src_ports
            else (min(sig.src_ports) if sig.src_ports else IMU_DEFAULT_SRC_PORT)
        )
        host = next(iter(sig.dst_hosts), None) or local_ip_for_peer(ip) or bind_host
        out.append(
            DiscoveredNetworkImu(
                ip=ip,
                mac=ip_to_mac.get(ip, ""),
                port=local_port,
                transport="udp",
                host_address=host if host not in ("", "0.0.0.0") else bind_host,
                extra={
                    "imu_src_port": src_port,
                    "packets": sig.packets,
                    "payload_sizes": list(sizes),
                },
            )
        )
    out.sort(key=lambda d: d.ip)
    return out


def discover_imus_udp(
    *,
    subnet: Optional[str] = None,
    timeout_sec: float = 5.0,
    packet_size: Optional[int] = None,
    packet_sizes: Optional[List[int]] = None,
    local_ports: Optional[List[int]] = None,
    bind_hosts: Optional[List[str]] = None,
    bind_interface: Optional[str] = None,
) -> List["DiscoveredNetworkImu"]:
    ports = tuple(local_ports) if local_ports else DEFAULT_LOCAL_PORTS
    sizes = normalize_packet_sizes(packet_size, packet_sizes)
    hosts = bind_hosts or resolve_bind_hosts(None, subnet)
    primary_host = hosts[0] if hosts else "0.0.0.0"
    sniff_ifaces = resolve_sniff_interfaces(subnet, bind_interface)

    LOG.info(
        "发现 IMU: 本机监听端口 %s（IMU 源端口通常 %d），bind %s，子网 %s，嗅探网卡 %s，payload %s，超时 %.1fs",
        list(ports), IMU_DEFAULT_SRC_PORT, hosts, subnet or "*", sniff_ifaces, list(sizes), timeout_sec,
    )

    traffic = sniff_imu_signatures(
        timeout_sec=timeout_sec * 0.5,
        packet_sizes=sizes,
        local_ports=ports,
        bind_hosts=hosts,
        subnet=subnet,
    )

    if not traffic:
        per_iface = max(0.5, (timeout_sec * 0.5) / max(1, len(sniff_ifaces)))
        for iface in sniff_ifaces:
            partial = sniff_via_tcpdump(
                interface=iface,
                local_ports=ports,
                udp_lengths=(IMU_TCPDUMP_LENGTH,),
                subnet=subnet,
                timeout_sec=per_iface,
            )
            _merge_traffic(traffic, partial)

    out = _traffic_to_devices(traffic, bind_host=primary_host, sizes=sizes)
    if subnet:
        out = [d for d in out if ip_matches_pattern(d.ip, subnet)]
    if out:
        for d in out:
            LOG.info(
                "  IMU %s:%d -> 本机 %s:%d mac=%s",
                d.ip, d.imu_src_port, d.host_address or primary_host, d.port, d.mac or "-",
            )
    else:
        dst_hosts: Set[str] = set()
        for sig in traffic.values():
            dst_hosts.update(sig.dst_hosts)
        LOG.warning(discovery_failure_hint(subnet, observed_dst_hosts=dst_hosts or None))
    return out
