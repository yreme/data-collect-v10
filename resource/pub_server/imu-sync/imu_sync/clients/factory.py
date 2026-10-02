from __future__ import annotations

from ..config import AppConfig
from .base import ShmClient
from .foxglove_client import FoxgloveClient
from .mcap_client import McapClient


def build_client(kind: str, cfg: AppConfig) -> ShmClient:
    if kind == "foxglove":
        return FoxgloveClient(cfg)
    if kind == "mcap":
        return McapClient(cfg)
    raise ValueError(f"未知客户端类型: {kind}")


def build_enabled_clients(cfg: AppConfig) -> list:
    out = []
    if cfg.clients.foxglove.enabled:
        out.append(FoxgloveClient(cfg))
    if cfg.clients.mcap.enabled:
        out.append(McapClient(cfg))
    return out
