"""网络 IMU 发现（UDP payload 59B / tcpdump length 67）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .udp_discover import (
    DEFAULT_LOCAL_PORTS,
    IMU_DEFAULT_LOCAL_PORT,
    IMU_DEFAULT_SRC_PORT,
    IMU_PAYLOAD_SIZE,
    IMU_PAYLOAD_SIZES,
    IMU_TCPDUMP_LENGTH,
    discover_imus_udp,
    resolve_bind_hosts,
)


@dataclass
class DiscoveredNetworkImu:
    ip: str
    mac: str
    port: int
    transport: str = "udp"
    host_address: str = "0.0.0.0"
    extra: Dict = field(default_factory=dict)

    @property
    def imu_src_port(self) -> int:
        return int(self.extra.get("imu_src_port") or IMU_DEFAULT_SRC_PORT)

    @property
    def is_likely_imu(self) -> bool:
        return bool(self.ip and self.port)

    def to_dict(self) -> dict:
        return {
            "transport": self.transport,
            "imu_ip": self.ip,
            "ip": self.ip,
            "mac": self.mac,
            "port": self.port,
            "imu_src_port": self.imu_src_port,
            "host_address": self.host_address,
            **self.extra,
        }


def match_mac(a: str, b: str) -> bool:
    return (a or "").strip().lower().replace("-", ":") == (b or "").strip().lower().replace("-", ":")


def discover_network_imus(
    *,
    subnet: Optional[str] = None,
    ports: Optional[List[int]] = None,
    timeout_sec: float = 5.0,
    packet_size: Optional[int] = None,
    packet_sizes: Optional[List[int]] = None,
    bind_interfaces: Optional[List[str]] = None,
    bind_device: Optional[str] = None,
) -> List[DiscoveredNetworkImu]:
    """发现发往本机 dst 端口（默认 2368）的 IMU；2369 为 IMU 源端口。"""
    hosts = resolve_bind_hosts(bind_interfaces, subnet)
    return discover_imus_udp(
        subnet=subnet,
        timeout_sec=timeout_sec,
        packet_size=packet_size,
        packet_sizes=packet_sizes,
        local_ports=ports or list(DEFAULT_LOCAL_PORTS),
        bind_hosts=hosts,
        bind_interface=bind_device,
    )
