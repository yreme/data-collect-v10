"""IMU 可达性探测。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import ImuConfig
from .net_util import probe_tcp_yesense


@dataclass
class ProbeResult:
    ok_to_capture: bool
    reachable: bool
    reason: str = ""
    ip: str = ""
    mac: str = ""
    serial_port: str = ""


def probe_imu(imu_cfg: ImuConfig) -> ProbeResult:
    transport = imu_cfg.transport
    if transport == "serial" or imu_cfg.serial_port:
        port = imu_cfg.serial_port
        if port and os.path.exists(port):
            return ProbeResult(
                ok_to_capture=True,
                reachable=True,
                serial_port=port,
                mac=imu_cfg.mac,
            )
        return ProbeResult(
            ok_to_capture=False,
            reachable=False,
            reason=f"串口不存在: {port or '(未配置)'}",
            mac=imu_cfg.mac,
        )

    ip = imu_cfg.imu_ip
    port = imu_cfg.port
    if not ip:
        return ProbeResult(
            ok_to_capture=False,
            reachable=False,
            reason="未配置 IP",
            mac=imu_cfg.mac,
        )

    if transport == "tcp":
        ok = probe_tcp_yesense(ip, port, timeout=1.5)
        return ProbeResult(
            ok_to_capture=ok,
            reachable=ok,
            reason="" if ok else f"TCP {ip}:{port} 无 Yesense 数据",
            ip=ip,
            mac=imu_cfg.mac,
        )

    # UDP: assume reachable if IP configured (data arrives when IMU streams)
    return ProbeResult(
        ok_to_capture=True,
        reachable=True,
        ip=ip,
        mac=imu_cfg.mac,
        reason="",
    )
