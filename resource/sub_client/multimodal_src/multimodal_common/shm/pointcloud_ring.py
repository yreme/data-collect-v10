"""无锁 SPMC 点云共享内存环形缓冲（LIDA 魔数）。"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Optional

try:
    from multiprocessing import resource_tracker as _resource_tracker
except Exception:  # noqa: BLE001
    _resource_tracker = None

MAGIC = 0x4C494441  # 'LIDA'
VERSION = 1
_HEADER_SIZE = 128
_META_SIZE = 64
_H = struct.Struct("<IIIII")
_WRITE_SEQ = struct.Struct("<Q")
_M = struct.Struct("<QIIIIQQQI")
ENCODING_XYZI = 2
_ENCODING_NAME = {ENCODING_XYZI: "xyzi"}


@dataclass(frozen=True)
class PointCloudMeta:
    seq: int
    nbytes: int
    point_count: int
    width: int
    encoding: str
    trigger_ms: int
    recv_ms: int
    host_ms: int
    frame_num: int


@dataclass
class PointCloudView:
    meta: PointCloudMeta
    data: bytes


def _total_size(slot_count: int, slot_capacity: int) -> int:
    return _HEADER_SIZE + slot_count * _META_SIZE + slot_count * slot_capacity


def _detach_from_tracker(shm: shared_memory.SharedMemory) -> None:
    if _resource_tracker is None:
        return
    try:
        _resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


class _RingBase:
    def __init__(self, shm: shared_memory.SharedMemory, slot_count: int, slot_capacity: int):
        self._shm = shm
        self._buf = shm.buf
        self.slot_count = slot_count
        self.slot_capacity = slot_capacity
        self._meta_base = _HEADER_SIZE
        self._data_base = _HEADER_SIZE + slot_count * _META_SIZE

    @property
    def name(self) -> str:
        return self._shm.name

    def _meta_off(self, slot: int) -> int:
        return self._meta_base + slot * _META_SIZE

    def _data_off(self, slot: int) -> int:
        return self._data_base + slot * self.slot_capacity

    def _read_write_seq(self) -> int:
        return _WRITE_SEQ.unpack_from(self._buf, 64)[0]

    def close(self) -> None:
        try:
            self._buf = None
        except Exception:  # noqa: BLE001
            pass
        try:
            self._shm.close()
        except Exception:  # noqa: BLE001
            pass


class SharedPointCloudWriter(_RingBase):
    def __init__(self, name: str, slot_count: int, slot_capacity: int, create: bool = True):
        size = _total_size(slot_count, slot_capacity)
        shm = _create_shm(name, size, create=create)
        super().__init__(shm, slot_count, slot_capacity)
        _H.pack_into(self._buf, 0, MAGIC, VERSION, slot_count, slot_capacity, _META_SIZE)
        _WRITE_SEQ.pack_into(self._buf, 64, 0)
        for s in range(slot_count):
            _M.pack_into(self._buf, self._meta_off(s), 0, 0, 0, 0, 0, 0, 0, 0, 0)

    def publish(
        self, data: bytes, *, point_count: int = 0, width: int = 0,
        trigger_ms: int = 0, recv_ms: int = 0, host_ms: int = 0, frame_num: int = 0,
    ) -> int:
        n = len(data)
        if n > self.slot_capacity:
            raise ValueError(f"点云 {n} 字节超过单槽容量 {self.slot_capacity}")
        idx = self._read_write_seq()
        slot = idx % self.slot_count
        doff = self._data_off(slot)
        self._buf[doff : doff + n] = data
        _M.pack_into(
            self._buf, self._meta_off(slot),
            idx, n, int(point_count), int(width), ENCODING_XYZI,
            int(trigger_ms) & 0xFFFFFFFFFFFFFFFF,
            int(recv_ms) & 0xFFFFFFFFFFFFFFFF,
            int(host_ms) & 0xFFFFFFFFFFFFFFFF,
            int(frame_num) & 0xFFFFFFFF,
        )
        _WRITE_SEQ.pack_into(self._buf, 64, idx + 1)
        return idx

    def unlink(self) -> None:
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass


class SharedPointCloudReader(_RingBase):
    def __init__(self, name: str):
        shm = shared_memory.SharedMemory(name=name, create=False)
        _detach_from_tracker(shm)
        magic, version, slot_count, slot_capacity, meta_size = _H.unpack_from(shm.buf, 0)
        if magic != MAGIC or version != VERSION or meta_size != _META_SIZE:
            shm.close()
            raise ValueError(f"共享内存 {name} 魔数/版本不兼容")
        super().__init__(shm, slot_count, slot_capacity)

    def _try_read_latest(self) -> Optional[PointCloudView]:
        s1 = self._read_write_seq()
        if s1 == 0:
            return None
        idx = s1 - 1
        slot = idx % self.slot_count
        moff = self._meta_off(slot)
        seq, nbytes, pcount, width, enc, trig, recv, host, fnum = _M.unpack_from(self._buf, moff)
        if seq != idx or nbytes == 0 or nbytes > self.slot_capacity:
            return None
        doff = self._data_off(slot)
        data = bytes(self._buf[doff : doff + nbytes])
        if _M.unpack_from(self._buf, moff)[0] != idx:
            return None
        return PointCloudView(
            meta=PointCloudMeta(
                seq=idx, nbytes=nbytes, point_count=pcount, width=width,
                encoding=_ENCODING_NAME.get(enc, "xyzi"),
                trigger_ms=trig, recv_ms=recv, host_ms=host, frame_num=fnum,
            ),
            data=data,
        )

    def latest_seq(self) -> int:
        return self._read_write_seq() - 1

    def read_latest(self, retries: int = 8) -> Optional[PointCloudView]:
        for _ in range(max(1, retries)):
            pc = self._try_read_latest()
            if pc is not None:
                return pc
            time.sleep(0.0002)
        return None

    def read_new(self, last_seq: int, *, stop=None, timeout: Optional[float] = None,
                 poll_interval: float = 0.001) -> Optional[PointCloudView]:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            if stop is not None and stop.is_set():
                return None
            if self.latest_seq() > last_seq:
                pc = self.read_latest()
                if pc is not None and pc.meta.seq > last_seq:
                    return pc
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)


def _create_shm(name: str, size: int, create: bool) -> shared_memory.SharedMemory:
    if not create:
        return shared_memory.SharedMemory(name=name, create=False)
    try:
        old = shared_memory.SharedMemory(name=name, create=False)
        old.close()
        old.unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return shared_memory.SharedMemory(name=name, create=True, size=size)
