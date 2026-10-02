"""sensors.yaml 加载、校验与热更新通知。

约定：
- 文件路径来自 ``SENSORHUB_CONFIG``（默认 ``/app/configs/sensors.yaml``）。
- ``devices(cfg, "cameras")`` 返回已合并 defaults 的设备字典列表。
- 控制台保存配置后会 put ``{prefix}/cfg/changed``；pub 用 ``ConfigWatcher`` 收到后自行决定热更新或提示重启。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import yaml

from .sync_grid import is_valid_hz

LOG = logging.getLogger("sensorhub.config")

DEFAULT_PATH = "/app/configs/sensors.yaml"
SECTIONS = {"cameras": "camera", "lidars": "lidar", "imus": "imu", "plcs": "plc"}
_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_HZ_FIELDS = ("hz", "publish_hz")


class ConfigError(ValueError):
    pass


def config_path(path: Optional[Union[str, Path]] = None) -> Path:
    return Path(path or os.environ.get("SENSORHUB_CONFIG") or DEFAULT_PATH)


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse(text: str) -> Dict[str, Any]:
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 语法错误: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError("顶层必须是映射(dict)")
    return cfg


def validate(cfg: Dict[str, Any]) -> List[str]:
    """返回错误列表（空列表=通过）。控制台保存前必须调用。"""
    errs: List[str] = []
    prefix = (cfg.get("system") or {}).get("prefix", "rig")
    if not re.match(r"^[a-z0-9_]+(/[a-z0-9_]+)*$", str(prefix)):
        errs.append(f"system.prefix 非法: {prefix!r}")
    base_hz = (cfg.get("sync") or {}).get("base_hz")
    if base_hz is not None and not is_valid_hz(base_hz):
        errs.append(f"sync.base_hz={base_hz} 不能整除 1 秒")
    seen = set()
    for sec, kind in SECTIONS.items():
        block = cfg.get(sec)
        if block is None:
            continue
        if not isinstance(block, dict):
            errs.append(f"{sec} 必须是映射，包含 defaults/devices")
            continue
        defaults = block.get("defaults") or {}
        devs = block.get("devices") or []
        if not isinstance(devs, list):
            errs.append(f"{sec}.devices 必须是列表")
            continue
        for i, d in enumerate(devs):
            where = f"{sec}.devices[{i}]"
            if not isinstance(d, dict):
                errs.append(f"{where} 必须是映射")
                continue
            name = d.get("name")
            if not name or not _NAME_RE.match(str(name)):
                errs.append(f"{where}.name={name!r} 非法（[a-z0-9_]+）")
            elif (kind, name) in seen:
                errs.append(f"{where}.name={name!r} 重复")
            seen.add((kind, name))
            merged = _deep_merge(defaults, d.get("overrides") or {})
            merged.update({k: v for k, v in d.items() if k != "overrides"})
            for f in _HZ_FIELDS:
                if f in merged and merged[f] is not None and not is_valid_hz(merged[f]):
                    errs.append(f"{where}.{f}={merged[f]} 不能整除 1 秒")
    return errs


def load(path: Optional[Union[str, Path]] = None, *, strict: bool = True) -> Dict[str, Any]:
    p = config_path(path)
    cfg = parse(p.read_text(encoding="utf-8"))
    errs = validate(cfg)
    if errs and strict:
        raise ConfigError("; ".join(errs))
    for e in errs:
        LOG.warning("配置问题: %s", e)
    return cfg


def host_id(cfg: Dict[str, Any]) -> str:
    """本机标识：env SENSORHUB_HOST_ID 优先，其次 system.host_id。"""
    return os.environ.get("SENSORHUB_HOST_ID") or str((cfg.get("system") or {}).get("host_id") or "")


def devices(cfg: Dict[str, Any], section: str, *, enabled_only: bool = True,
            host: Optional[str] = None) -> List[Dict[str, Any]]:
    """合并 defaults + overrides + 设备字段。

    host 不为空时只返回 ``host`` 字段等于它（或未设置 host）的设备 —— 服务器 A/D 共用一份配置时，
    pub 用 ``devices(cfg, "cameras", host=host_id(cfg))`` 只打开接在本机的设备。
    """
    block = cfg.get(section) or {}
    defaults = block.get("defaults") or {}
    shared = {k: v for k, v in block.items() if k not in ("defaults", "devices")}
    out = []
    for d in block.get("devices") or []:
        if enabled_only and not d.get("enabled", True):
            continue
        dev_host = d.get("host", (d.get("overrides") or {}).get("host", defaults.get("host")))
        if host and dev_host and str(dev_host) != host:
            continue
        merged = _deep_merge(_deep_merge(shared, defaults), d.get("overrides") or {})
        merged.update({k: copy.deepcopy(v) for k, v in d.items() if k != "overrides"})
        out.append(merged)
    return out


def prefix_of(cfg: Dict[str, Any]) -> str:
    return os.environ.get("SENSORHUB_PREFIX") or (cfg.get("system") or {}).get("prefix") or "rig"


class ConfigWatcher:
    """订阅 ``{prefix}/cfg/changed``，文件变化时回调 ``on_change(new_cfg)``。"""

    def __init__(self, session, prefix: str, path: Optional[Union[str, Path]],
                 on_change: Callable[[Dict[str, Any]], None]) -> None:
        from . import keys as K

        self._path = config_path(path)
        self._on_change = on_change
        self._sub = session.declare_subscriber(K.cfg_changed(prefix), self._cb)

    def _cb(self, sample) -> None:
        try:
            info = json.loads(sample.payload.to_bytes())
        except Exception:  # noqa: BLE001
            info = {}
        if info.get("file") not in (None, self._path.name):
            return
        try:
            cfg = load(self._path)
        except Exception as exc:  # noqa: BLE001
            LOG.error("重新加载配置失败: %s", exc)
            return
        self._on_change(cfg)

    def close(self) -> None:
        self._sub.undeclare()
