"""串口 IMU 发现（USB 转串口 / 原生串口）。"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class DiscoveredSerialImu:
    serial_port: str
    mac: str
    baudrate: int = 460800
    usb_id: str = ""
    extra: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "transport": "serial",
            "serial_port": self.serial_port,
            "mac": self.mac,
            "baudrate": self.baudrate,
            "usb_id": self.usb_id,
            **self.extra,
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def _device_id_for_tty(tty_name: str) -> str:
    """从 sysfs 提取 USB 序列号或设备路径作为稳定标识。"""
    base = Path(f"/sys/class/tty/{tty_name}")
    if not base.exists():
        return tty_name
    device = base.resolve()
    for parent in [device] + list(device.parents):
        serial = parent / "serial"
        if serial.is_file():
            val = _read_text(serial)
            if val:
                return val
        for fname in ("idVendor", "idProduct"):
            f = parent / fname
            if f.is_file():
                vid = _read_text(parent / "idVendor")
                pid = _read_text(parent / "idProduct")
                sn = _read_text(parent / "serial")
                if vid and pid:
                    return f"usb:{vid}:{pid}:{sn or tty_name}"
    return f"tty:{tty_name}"


def _mac_for_device_id(device_id: str) -> str:
    """串口设备用伪 MAC 标识（配置绑定用）。"""
    clean = device_id.replace(":", "").replace("/", "_")[:12]
    if len(clean) < 12:
        clean = clean.ljust(12, "0")
    return ":".join(clean[i : i + 2] for i in range(0, 12, 2))


def discover_serial_imus() -> List[DiscoveredSerialImu]:
    patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttySC*"]
    found: List[DiscoveredSerialImu] = []
    seen: set = set()
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            if not os.path.exists(path):
                continue
            tty = os.path.basename(path)
            if tty in seen:
                continue
            seen.add(tty)
            device_id = _device_id_for_tty(tty)
            mac = _mac_for_device_id(device_id)
            found.append(
                DiscoveredSerialImu(
                    serial_port=path,
                    mac=mac,
                    usb_id=device_id,
                    extra={"device_id": device_id},
                )
            )
    return found
