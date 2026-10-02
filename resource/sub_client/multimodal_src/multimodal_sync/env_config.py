"""Environment variable overrides for multimodal clients."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _f(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v


@dataclass
class EnvSettings:
    subnet: Optional[str] = None
    output_dir: Optional[str] = None
    foxglove_host: Optional[str] = None
    foxglove_port: Optional[int] = None
    mcap_rotate_sec: Optional[float] = None
    mcap_callback_url: Optional[str] = None
    shm_rescan_sec: Optional[float] = None

    @classmethod
    def from_env(cls) -> "EnvSettings":
        port_raw = _f("MULTIMODAL_FOXGLOVE_PORT")
        rotate_raw = _f("MULTIMODAL_MCAP_ROTATE_SEC")
        rescan_raw = _f("MULTIMODAL_SHM_RESCAN_SEC")
        return cls(
            subnet=_f("MULTIMODAL_SUBNET"),
            output_dir=_f("MULTIMODAL_OUTPUT_DIR"),
            foxglove_host=_f("MULTIMODAL_FOXGLOVE_HOST"),
            foxglove_port=int(port_raw) if port_raw else None,
            mcap_rotate_sec=float(rotate_raw) if rotate_raw else None,
            mcap_callback_url=_f("MULTIMODAL_MCAP_CALLBACK_URL"),
            shm_rescan_sec=float(rescan_raw) if rescan_raw else None,
        )
