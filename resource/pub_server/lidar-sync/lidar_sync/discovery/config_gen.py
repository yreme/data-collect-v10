"""发现结果写 YAML 配置。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import yaml

from ..env_config import EnvSettings
from .lidar_scan import DiscoveredLidar


def generate_config_yaml(
    lidars: List[DiscoveredLidar],
    *,
    hz: int = 10,
    subnet: Optional[str] = None,
) -> str:
    lidar_entries = []
    for i, l in enumerate(lidars):
        lidar_entries.append({
            "name": l.suggested_name(i),
            "lidar_ip": l.ip,
            "mac": l.mac,
            "msop_port": 6699 if i == 0 else 6699 - i,
            "difop_port": 7788 if i == 0 else 7788 - i,
            "lidar_type": "RSE1",
            "host_address": "0.0.0.0",
            "frame_id": l.suggested_name(i),
            "enabled": True,
        })
    data = {
        "capture": {
            "hz": hz,
            "reconnect_interval_sec": 2,
            "discover_subnet": subnet,
        },
        "shm": {
            "name_prefix": "lidar_",
            "slot_count": 8,
            "slot_capacity_bytes": 16777216,
        },
        "lidars": lidar_entries,
        "clients": {
            "save": {"enabled": False, "output_dir": "./captures"},
            "foxglove": {"enabled": True, "host": "0.0.0.0", "port": 38765},
        },
        "web": {"enabled": True, "host": "0.0.0.0", "port": 18090},
        "runtime": {"duration_sec": None, "status_interval_sec": 5.0},
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def write_discovered_config(
    lidars: List[DiscoveredLidar],
    output: Path,
    *,
    env: Optional[EnvSettings] = None,
) -> Path:
    env = env or EnvSettings.from_env()
    text = generate_config_yaml(lidars, hz=10, subnet=env.subnet)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output
