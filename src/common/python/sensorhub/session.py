"""统一打开 Zenoh session（所有 pub/sub/控制台必须通过这里，保证拓扑一致）。

环境变量（docker-compose 中统一设置，见 deploy/common.yml）：

=====================  =========================  =========================================
变量                    默认                        说明
=====================  =========================  =========================================
ZENOH_CONFIG           (空)                        json5 配置文件路径；设置后作为基础配置
ZENOH_MODE             peer                        peer：本机直连 + SHM；client：只连路由器
ZENOH_CONNECT          tcp/127.0.0.1:7447          逗号分隔的路由器地址
ZENOH_LISTEN           tcp/127.0.0.1:0             peer 监听地址（只在本机，跨机流量走路由器）
ZENOH_SHM              1                           是否启用共享内存
ZENOH_MULTICAST        0                           是否启用组播发现（生产环境关闭，行为可预期）
=====================  =========================  =========================================

拓扑：每台服务器一个 zenohd 路由器；本机所有进程以 peer 模式连接路由器，
并通过 gossip 发现彼此后 **直接建立本地连接**，大块数据走 SHM，不经过路由器转发；
跨服务器（A↔D）流量由两台路由器之间的一条 TCP 连接承载，只传输被订阅的 key。
"""

from __future__ import annotations

import json
import logging
import os
import resource
from typing import Iterable, List, Optional

import zenoh

LOG = logging.getLogger("sensorhub.session")

DEFAULT_ROUTER = "tcp/127.0.0.1:7447"


def _split(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def memlock_limit_bytes() -> int:
    """当前进程 RLIMIT_MEMLOCK（-1 表示无限制）。"""
    soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    return -1 if soft == resource.RLIM_INFINITY else int(soft)


def check_memlock(required_bytes: int) -> bool:
    """Zenoh SHM 会 mlock 共享内存段；memlock 不足时 SHM 发布会被静默丢弃。"""
    lim = memlock_limit_bytes()
    if lim != -1 and lim < required_bytes:
        LOG.error(
            "RLIMIT_MEMLOCK=%d 字节 < 需要的 %d 字节：Zenoh SHM 将无法工作！"
            " docker-compose 请加 `ulimits: {memlock: -1}`；systemd 加 `LimitMEMLOCK=infinity`；"
            " 命令行可用 `ulimit -l unlimited`。", lim, required_bytes,
        )
        return False
    return True


def build_config(
    *,
    mode: Optional[str] = None,
    connect: Optional[Iterable[str]] = None,
    listen: Optional[Iterable[str]] = None,
    shm: Optional[bool] = None,
    multicast: Optional[bool] = None,
    config_file: Optional[str] = None,
) -> zenoh.Config:
    path = config_file if config_file is not None else os.environ.get("ZENOH_CONFIG")
    conf = zenoh.Config.from_file(path) if path else zenoh.Config()

    mode = mode or os.environ.get("ZENOH_MODE") or "peer"
    conf.insert_json5("mode", json.dumps(mode))

    eps = list(connect) if connect is not None else (_split(os.environ.get("ZENOH_CONNECT")) or [DEFAULT_ROUTER])
    if eps:
        conf.insert_json5("connect/endpoints", json.dumps(eps))

    if mode == "peer":
        lis = list(listen) if listen is not None else (_split(os.environ.get("ZENOH_LISTEN")) or ["tcp/127.0.0.1:0"])
        conf.insert_json5("listen/endpoints", json.dumps(lis))

    mc = multicast if multicast is not None else _env_bool("ZENOH_MULTICAST", False)
    conf.insert_json5("scouting/multicast/enabled", json.dumps(mc))

    use_shm = shm if shm is not None else _env_bool("ZENOH_SHM", True)
    if use_shm and not check_memlock(32 * 1024 * 1024):
        # 必须在协商层关闭：否则对端会把 SHM 缓冲发过来，本进程无法 mlock 映射，数据被静默丢弃
        LOG.error("已自动关闭本进程的 Zenoh SHM，对端将改用网络字节发送")
        use_shm = False
    conf.insert_json5("transport/shared_memory/enabled", json.dumps(use_shm))
    return conf


def open_session(**kwargs) -> zenoh.Session:
    zenoh.try_init_log_from_env()
    conf = build_config(**kwargs)
    sess = zenoh.open(conf)
    LOG.info("Zenoh session 已打开 zid=%s", sess.zid())
    return sess
