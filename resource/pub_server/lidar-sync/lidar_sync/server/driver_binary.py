"""查找 lidar-capture 可执行文件。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from ..logging_setup import get_logger

LOG = get_logger("driver")


def find_driver_binary(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
        LOG.warning("配置的 driver_binary 不存在或不可执行: %s", p)

    env_bin = os.environ.get("LIDAR_DRIVER_BINARY")
    if env_bin and Path(env_bin).is_file():
        return env_bin

    which = shutil.which("lidar-capture")
    if which:
        return which

    candidates = [
        Path(__file__).resolve().parents[2] / "cpp" / "build" / "lidar-capture",
        Path(__file__).resolve().parents[2] / "bin" / "lidar-capture",
        Path("/usr/local/bin/lidar-capture"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c.resolve())
    return None
