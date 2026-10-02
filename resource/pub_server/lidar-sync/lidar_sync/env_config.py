"""环境变量覆盖（部署时无需改 YAML）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class EnvSettings:
    subnet: Optional[str] = None
    local_ip: Optional[str] = None
    reconnect_interval_sec: float = 2.0
    web_port: int = 18090
    output_dir: str = "./output"
    auto_discover: bool = False
    mock: bool = False
    driver_binary: Optional[str] = None

    @classmethod
    def from_env(cls) -> "EnvSettings":
        def _f(key: str, default: Optional[str] = None) -> Optional[str]:
            v = os.environ.get(key)
            return v if v not in (None, "") else default

        return cls(
            subnet=_f("LIDAR_SUBNET"),
            local_ip=_f("LIDAR_LOCAL_IP"),
            reconnect_interval_sec=float(_f("LIDAR_RECONNECT_INTERVAL", "2") or "2"),
            web_port=int(_f("LIDAR_WEB_PORT", "18090") or "18090"),
            output_dir=_f("LIDAR_OUTPUT_DIR", "./output") or "./output",
            auto_discover=(_f("LIDAR_AUTO_DISCOVER", "0") or "0").lower() in ("1", "true", "yes"),
            mock=(_f("LIDAR_MOCK", "0") or "0").lower() in ("1", "true", "yes"),
            driver_binary=_f("LIDAR_DRIVER_BINARY"),
        )
