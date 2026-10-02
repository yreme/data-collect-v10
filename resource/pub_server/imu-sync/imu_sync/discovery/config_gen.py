"""发现结果写入 YAML。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

import yaml

from ..env_config import EnvSettings
from .network_discover import DiscoveredNetworkImu
from .serial_discover import DiscoveredSerialImu

DiscoveredImu = Union[DiscoveredNetworkImu, DiscoveredSerialImu]


def write_discovered_config(
    devices: List[DiscoveredImu],
    output: Path,
    *,
    env: Optional[EnvSettings] = None,
) -> Path:
    env = env or EnvSettings.from_env()
    imus = []
    for i, dev in enumerate(devices):
        entry = {"name": f"imu{i}", "enabled": True, "frame_id": f"imu{i}"}
        entry.update(dev.to_dict())
        imus.append(entry)
    data = {
        "capture": {
            "hz": 20,
            "auto_discover": True,
            "discover_subnet": env.subnet or "192.168.1.*",
        },
        "shm": {"name_prefix": "imu_"},
        "imus": imus,
        "clients": {"foxglove": {"enabled": False}, "mcap": {"enabled": False}},
        "web": {"enabled": True, "port": env.web_port},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return output
