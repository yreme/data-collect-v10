"""日志初始化。"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"lidar_sync.{name}")
