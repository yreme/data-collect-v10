"""arv-tool-0.8 封装：GigE 相机发现与 GenICam 参数读取。"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

ARV_TOOL = "arv-tool-0.8"
_DEVICE_RE = re.compile(r"^(.+?)\s+\(([\d.]+)\)\s*$")
_CONTROL_RE = re.compile(r"^(\w+)\s*=\s*(\S+)")


@dataclass
class GigECamera:
    name: str
    ip: str
    model: str = ""
    serial: str = ""
    vendor: str = ""
    link_speed_mbps: int = 0
    device_link_speed_mbps: int = 0
    max_throughput_bps: int = 0
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    error: str = ""

    @property
    def link_status(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.link_speed_mbps >= 1000:
            return "1000Mbps (千兆)"
        if self.link_speed_mbps == 100:
            return "100Mbps (百兆) ⚠"
        if self.link_speed_mbps > 0:
            return f"{self.link_speed_mbps}Mbps"
        return "unknown"


def find_arv_tool() -> str:
    path = shutil.which(ARV_TOOL)
    if not path:
        raise FileNotFoundError(f"未找到 {ARV_TOOL}，请安装 aravis 工具包")
    return path


def _run_arv(args: List[str], timeout: float = 8.0) -> str:
    arv = find_arv_tool()
    try:
        return subprocess.check_output(
            [arv, *args],
            text=True,
            timeout=timeout,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        text = (exc.output or "").strip()
        raise RuntimeError(text or str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"arv-tool 超时: {' '.join(args)}") from exc


def _parse_control_value(output: str, feature: str) -> Optional[str]:
    for line in output.splitlines():
        m = _CONTROL_RE.match(line.strip())
        if m and m.group(1) == feature:
            return m.group(2)
    return None


def _parse_int(output: str, feature: str) -> int:
    val = _parse_control_value(output, feature)
    if not val:
        return 0
    try:
        return int(float(val))
    except ValueError:
        return 0


def _split_name(name: str) -> tuple[str, str, str]:
    """Hikrobot-MV-CU013-A0GC-DA6567920 -> vendor, model, serial"""
    parts = name.split("-")
    if len(parts) >= 3:
        vendor = parts[0]
        serial = parts[-1]
        model = "-".join(parts[1:-1])
        return vendor, model, serial
    return "", name, ""


def discover_devices() -> List[tuple[str, str]]:
    """返回 [(device_name, ip), ...]。"""
    out = _run_arv([], timeout=15.0)
    devices: List[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _DEVICE_RE.match(line)
        if m:
            devices.append((m.group(1), m.group(2)))
    return devices


def filter_subnet(devices: List[tuple[str, str]], subnet: str) -> List[tuple[str, str]]:
    net = ipaddress.ip_network(subnet, strict=False)
    return [(name, ip) for name, ip in devices if ipaddress.ip_address(ip) in net]


def read_camera_info(ip: str) -> GigECamera:
    cam = GigECamera(name="", ip=ip)
    try:
        out = _run_arv(
            [
                "-a", ip, "control",
                "GevLinkSpeed", "DeviceLinkSpeed", "DeviceMaxThroughput",
                "Width", "Height", "PixelFormat",
                "DeviceVendorName", "DeviceModelName", "DeviceSerialNumber",
            ],
            timeout=10.0,
        )
    except RuntimeError as exc:
        cam.error = str(exc)
        return cam

    cam.link_speed_mbps = _parse_int(out, "GevLinkSpeed")
    cam.device_link_speed_mbps = _parse_int(out, "DeviceLinkSpeed")
    cam.max_throughput_bps = _parse_int(out, "DeviceMaxThroughput")
    cam.width = _parse_int(out, "Width")
    cam.height = _parse_int(out, "Height")
    cam.pixel_format = _parse_control_value(out, "PixelFormat") or ""
    cam.vendor = _parse_control_value(out, "DeviceVendorName") or ""
    cam.model = _parse_control_value(out, "DeviceModelName") or ""
    cam.serial = _parse_control_value(out, "DeviceSerialNumber") or ""
    return cam


def enrich_camera(name: str, ip: str) -> GigECamera:
    vendor, model, serial = _split_name(name)
    info = read_camera_info(ip)
    info.name = name
    if not info.vendor:
        info.vendor = vendor
    if not info.model:
        info.model = model
    if not info.serial:
        info.serial = serial
    return info
