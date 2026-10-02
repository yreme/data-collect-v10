"""Zenoh SHM 发布封装：SHM 不可用时自动降级为普通字节发送（功能不变，仅多一次拷贝）。

注意（zenoh-python 1.10）：
- ``ZShmMut`` 只接受 ``bytes``/``bytearray`` 赋值，不接受 memoryview/numpy，
  因此 Python 发布端每帧至少一次内存拷贝（1080p BGR ≈ 6MB ≈ 1~3ms）。
  对 CPU 敏感的大流量发布端（雷达点云、多路原图）推荐用 C++（zenoh-cpp）真正零拷贝。
- 订阅端 ``sample.payload.to_bytes()`` 同样会拷贝一次（6MB ≈ 1ms）；只读帧头时不触碰 payload。
- 共享内存池大小 >= 单帧大小 × (订阅方最慢处理期间积压帧数 + 2)。
"""

from __future__ import annotations

import logging
from typing import Optional, Union

import zenoh

from .session import check_memlock

LOG = logging.getLogger("sensorhub.shm")

BytesLike = Union[bytes, bytearray]


class ShmPool:
    def __init__(self, size_bytes: int, *, enabled: bool = True) -> None:
        self.size_bytes = size_bytes
        self.provider: Optional[zenoh.shm.ShmProvider] = None
        self.fallback_count = 0
        if not enabled:
            return
        if not check_memlock(size_bytes + 16 * 1024 * 1024):
            LOG.warning("memlock 不足，SHM 关闭，降级为网络字节传输")
            return
        try:
            self.provider = zenoh.shm.ShmProvider.default_backend(size_bytes)
            LOG.info("SHM 池已创建 %.1f MB", size_bytes / 1048576)
        except Exception as exc:  # noqa: BLE001
            LOG.error("创建 SHM 池失败（%s），降级为字节传输", exc)

    @property
    def active(self) -> bool:
        return self.provider is not None

    def wrap(self, data: BytesLike):
        """返回可直接 ``publisher.put()`` 的对象（ZShmMut 或原始 bytes）。"""
        if self.provider is None or len(data) == 0:
            return data
        try:
            buf = self.provider.alloc(
                len(data), policy=zenoh.shm.BlockOn(zenoh.shm.GarbageCollect()),
            )
        except Exception as exc:  # noqa: BLE001
            self.fallback_count += 1
            if self.fallback_count in (1, 10, 100) or self.fallback_count % 1000 == 0:
                LOG.warning("SHM 分配失败 %d 次（%s），本帧走字节传输", self.fallback_count, exc)
            return data
        buf[:] = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        return buf
