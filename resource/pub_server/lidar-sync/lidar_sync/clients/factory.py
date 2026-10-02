from __future__ import annotations

from ..config import AppConfig
from .base import ShmClient
from .foxglove_client import FoxgloveClient
from .save_client import SaveClient

_CLIENTS = {
    "save": SaveClient,
    "foxglove": FoxgloveClient,
}


def build_client(kind: str, cfg: AppConfig) -> ShmClient:
    cls = _CLIENTS.get(kind)
    if cls is None:
        raise ValueError(f"未知客户端: {kind}")
    return cls(cfg)


def build_enabled_clients(cfg: AppConfig) -> list:
    out = []
    if cfg.clients.save.enabled:
        out.append(SaveClient(cfg))
    if cfg.clients.foxglove.enabled:
        out.append(FoxgloveClient(cfg))
    return out
