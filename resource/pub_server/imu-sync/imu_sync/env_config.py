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
    web_port: int = 28090
    output_dir: str = "./output"
    auto_discover: bool = False
    mock: bool = False
    bind_device: Optional[str] = None

    @classmethod
    def from_env(cls) -> "EnvSettings":
        def _f(key: str, default: Optional[str] = None) -> Optional[str]:
            v = os.environ.get(key)
            return v if v not in (None, "") else default

        return cls(
            subnet=_f("IMU_SUBNET"),
            local_ip=_f("IMU_LOCAL_IP"),
            reconnect_interval_sec=float(_f("IMU_RECONNECT_INTERVAL", "2") or "2"),
            web_port=int(_f("IMU_WEB_PORT", "28090") or "28090"),
            output_dir=_f("IMU_OUTPUT_DIR", "./output") or "./output",
            auto_discover=(_f("IMU_AUTO_DISCOVER", "0") or "0").lower() in ("1", "true", "yes"),
            mock=(_f("IMU_MOCK", "0") or "0").lower() in ("1", "true", "yes"),
            bind_device=_f("IMU_INTERFACE"),
        )
