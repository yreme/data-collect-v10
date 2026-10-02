"""Zenoh key expression 命名规范（所有 pub/sub/控制台共用，禁止在业务代码里手拼字符串）。

    {prefix}/{kind}/{name}/{channel}

- prefix : 部署前缀，默认 ``rig``，由环境变量 ``SENSORHUB_PREFIX`` 覆盖；
           同一台服务器跑两套系统时用不同 prefix 隔离（例如 ``rig_a`` / ``rig_test``）。
- kind   : 设备/程序类别，见 ``KINDS``。
- name   : 实例名，``[a-z0-9_]+``，与 sensors.yaml 中 ``name`` 一致（如 ``cam_corner_0``）。
- channel: 数据通道，数据类见 ``DATA_CHANNELS``；保留通道见 ``RESERVED_CHANNELS``。

保留通道：
- ``status``  : 1Hz JSON 心跳/统计（控制台据此显示频率、带宽、延迟、链路速率）。
- ``meta``    : queryable，返回静态描述 JSON（分辨率、编码、序列号……）。
- ``ctrl``    : queryable，参数读写（describe / get_params / set_params）。
- ``event``   : 低频事件 JSON（掉线、重连、配置生效等）。

全局键：
- ``{prefix}/alive/{kind}/{name}``: liveliness token（实例在线即存在，进程崩溃自动消失）。
- ``{prefix}/sync/master/schedule``: 同步调度表（sync-master 发布，queryable + 变更时 put）。
- ``{prefix}/sync/master/tick``    : 网格节拍（诊断/软触发用，可选）。
- ``{prefix}/cfg/{file}``         : queryable，返回配置文件内容（控制台提供）。
- ``{prefix}/cfg/changed``        : 配置文件保存后的通知。
- ``{prefix}/net/netprobe/links``: 各设备链路协商速率/延迟汇总（netprobe 发布）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_PREFIX = "rig"

KINDS = ("camera", "lidar", "imu", "plc", "gnss", "app", "sync", "net")

DATA_CHANNELS = ("image", "preview", "points", "data", "state", "objects", "links", "schedule", "tick")
RESERVED_CHANNELS = ("status", "meta", "ctrl", "event")

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_PREFIX_RE = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*$")


class KeyNameError(ValueError):
    pass


def get_prefix(prefix: Optional[str] = None) -> str:
    p = prefix or os.environ.get("SENSORHUB_PREFIX") or DEFAULT_PREFIX
    if not _PREFIX_RE.match(p):
        raise KeyNameError(f"非法 prefix {p!r}，只允许 [a-z0-9_] 与 '/' 分隔")
    return p


def check_name(name: str, what: str = "name") -> str:
    if not _NAME_RE.match(name or ""):
        raise KeyNameError(f"非法 {what} {name!r}，只允许小写字母、数字、下划线")
    return name


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise KeyNameError(f"未知 kind {kind!r}，可选 {KINDS}（新增类别请先改规范）")
    return kind


@dataclass(frozen=True)
class SensorKeys:
    """某个实例（kind/name）的全部 key。"""

    prefix: str
    kind: str
    name: str

    @property
    def base(self) -> str:
        return f"{self.prefix}/{self.kind}/{self.name}"

    def data(self, channel: str) -> str:
        check_name(channel, "channel")
        if channel in RESERVED_CHANNELS:
            raise KeyNameError(f"{channel!r} 是保留通道，不能作为数据通道")
        return f"{self.base}/{channel}"

    @property
    def status(self) -> str:
        return f"{self.base}/status"

    @property
    def meta(self) -> str:
        return f"{self.base}/meta"

    @property
    def ctrl(self) -> str:
        return f"{self.base}/ctrl"

    @property
    def event(self) -> str:
        return f"{self.base}/event"


def sensor_keys(kind: str, name: str, prefix: Optional[str] = None) -> SensorKeys:
    return SensorKeys(get_prefix(prefix), check_kind(kind), check_name(name))


def alive(kind: str, name: str, prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/alive/{check_kind(kind)}/{check_name(name)}"


def all_alive(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/alive/*/*"


def all_status(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/*/*/status"


def all_meta(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/*/*/meta"


def all_events(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/*/*/event"


def sync_schedule(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/sync/master/schedule"


def sync_tick(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/sync/master/tick"


def cfg_file(file_stem: str, prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/cfg/{check_name(file_stem, 'file')}"


def cfg_changed(prefix: Optional[str] = None) -> str:
    return f"{get_prefix(prefix)}/cfg/changed"


def net_links(prefix: Optional[str] = None) -> str:
    """netprobe 汇总的各设备链路状态（JSON 列表），控制台“网络”页使用。"""
    return f"{get_prefix(prefix)}/net/netprobe/links"


def parse_key(key: str, prefix: Optional[str] = None) -> Optional[SensorKeys]:
    """把 ``rig/camera/cam0/image`` 解析成 SensorKeys（失败返回 None）。"""
    p = get_prefix(prefix)
    if not key.startswith(p + "/"):
        return None
    parts = key[len(p) + 1:].split("/")
    if len(parts) < 2 or parts[0] not in KINDS or not _NAME_RE.match(parts[1]):
        return None
    return SensorKeys(p, parts[0], parts[1])


def channel_of(key: str) -> str:
    return key.rsplit("/", 1)[-1]
