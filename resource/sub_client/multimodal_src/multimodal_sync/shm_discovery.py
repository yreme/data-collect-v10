"""Discover active shared-memory segments written by sensor servers."""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .logging_setup import get_logger

LOG = get_logger("shm_discovery")

# Magic numbers from each sensor ring buffer header.
_MAGIC_GIGE = 0x47494745  # 'GIGE'
_MAGIC_IMU = 0x494D5541   # 'IMUA'
_MAGIC_LIDAR = 0x4C494441  # 'LIDA'

_SKIP_GIGE = frozenset({"gige_sync_registry"})


@dataclass
class DiscoveredSensors:
    cameras: List[str] = field(default_factory=list)
    imus: List[str] = field(default_factory=list)
    lidars: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cameras) + len(self.imus) + len(self.lidars)


def _shm_dir() -> Path:
    return Path(os.environ.get("MULTIMODAL_SHM_DIR", "/dev/shm"))


def _read_magic(path: Path) -> Optional[int]:
    try:
        with open(path, "rb") as f:
            return struct.unpack("<I", f.read(4))[0]
    except OSError:
        return None


def _name_from_segment(segment: str, prefix: str) -> str:
    if segment.startswith(prefix):
        return segment[len(prefix) :]
    return segment


def scan_shm_segments(
    *,
    camera_prefix: str = "gige_",
    imu_prefix: str = "imu_",
    lidar_prefix: str = "lidar_",
    shm_dir: Optional[Path] = None,
) -> DiscoveredSensors:
    """Scan *shm_dir* for known sensor ring-buffer segments."""
    root = shm_dir or _shm_dir()
    result = DiscoveredSensors()
    if not root.is_dir():
        LOG.warning("共享内存目录不存在: %s", root)
        return result

    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        magic = _read_magic(entry)
        if magic == _MAGIC_GIGE:
            if name in _SKIP_GIGE or not name.startswith(camera_prefix):
                continue
            result.cameras.append(_name_from_segment(name, camera_prefix))
        elif magic == _MAGIC_IMU and name.startswith(imu_prefix):
            result.imus.append(_name_from_segment(name, imu_prefix))
        elif magic == _MAGIC_LIDAR and name.startswith(lidar_prefix):
            result.lidars.append(_name_from_segment(name, lidar_prefix))

    return result


def merge_sensor_lists(
    discovered: DiscoveredSensors,
    configured: DiscoveredSensors,
    *,
    auto_discover: bool = True,
) -> DiscoveredSensors:
    """Merge configured sensor names with live SHM discovery.

    When *auto_discover* is true, any segment found on disk is included even if
    not listed in YAML. Configured-but-absent sensors are kept so readers will
    wait for them to come online.
    """
    if not auto_discover:
        return configured

    cams = _union_preserve_order(configured.cameras, discovered.cameras)
    imus = _union_preserve_order(configured.imus, discovered.imus)
    lidars = _union_preserve_order(configured.lidars, discovered.lidars)
    return DiscoveredSensors(cameras=cams, imus=imus, lidars=lidars)


def _union_preserve_order(primary: List[str], extra: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for name in primary + extra:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def subnet_to_pattern(subnet: str) -> re.Pattern[str]:
    """Turn ``192.168.1.*`` into a regex for matching host IPs."""
    parts = subnet.strip().split(".")
    regex_parts = []
    for p in parts:
        if p == "*":
            regex_parts.append(r"\d+")
        else:
            regex_parts.append(re.escape(p))
    return re.compile(r"^" + r"\.".join(regex_parts) + r"$")
