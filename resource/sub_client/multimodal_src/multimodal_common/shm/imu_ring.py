"""无锁 SPMC IMU 共享内存环形缓冲（IMU0 魔数）。

每槽存储一批 IMU 样本（固定最大样本数），网格触发时刻打包发布。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import List, Optional, Tuple

try:
    from multiprocessing import resource_tracker as _resource_tracker
except Exception:  # noqa: BLE001
    _resource_tracker = None

MAGIC = 0x494D5530  # 'IMU0'
VERSION = 1
_HEADER_SIZE = 128
_META_SIZE = 48
_SAMPLE_SIZE = 32  # ts_ms(8) + ax,ay,az,gx,gy,gz(6*4)
_MAX_SAMPLES = 64
_H = struct.Struct("<IIIII")
_WRITE_SEQ = struct.Struct("<Q")
_M = struct.Struct("<QIIQQI")  # seq, nbytes, sample_count, trigger_ms, recv_ms, frame_num
_SAMPLE = struct.Struct("<Qffffff")  # ts_ms, ax, ay, az, gx, gy, gz


@dataclass(frozen=True)
class ImuSample:
    ts_ms: int
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass(frozen=True)
class ImuMeta:
    seq: int
    nbytes: int
    sample_count: int
    trigger_ms: int
    recv_ms: int
    frame_num: int


@dataclass
class ImuView:
    meta: ImuMeta
    samples: List[ImuSample]


def _total_size(slot_count: int, slot_capacity: int) -> int:
    return _HEADER_SIZE + slot_count * _META_SIZE + slot_count * slot_capacity


def _detach_from_tracker(shm: shared_memory.SharedMemory) -> None:
    if _resource_tracker is None:
        return
    try:
        _resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def pack_samples(samples: List[ImuSample]) -> bytes:
    buf = bytearray(len(samples) * _SAMPLE_SIZE)
    for i, s in enumerate(samples):
        _SAMPLE.pack_into(buf, i * _SAMPLE_SIZE, s.ts_ms, s.ax, s.ay, s.az, s.gx, s.gy, s.gz)
    return bytes(buf)


def unpack_samples(data: bytes) -> List[ImuSample]:
    n = len(data) // _SAMPLE_SIZE
    out: List[ImuSample] = []
    for i in range(n):
        ts, ax, ay, az, gx, gy, gz = _SAMPLE.unpack_from(data, i * _SAMPLE_SIZE)
        out.append(ImuSample(ts_ms=ts, ax=ax, ay=ay, az=az, gx=gx, gy=gy, gz=gz))
    return out


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


class SharedImuWriter(_RingBase):
    def __init__(self, name: str, slot_count: int, slot_capacity: int, create: bool = True):
        size = _total_size(slot_count, slot_capacity)
        shm = _create_shm(name, size, create=create)
        super().__init__(shm, slot_count, slot_capacity)
        _H.pack_into(self._buf, 0, MAGIC, VERSION, slot_count, slot_capacity, _META_SIZE)
        _WRITE_SEQ.pack_into(self._buf, 64, 0)

    def publish(
        self, samples: List[ImuSample], *,
        trigger_ms: int = 0, recv_ms: int = 0, frame_num: int = 0,
    ) -> int:
        data = pack_samples(samples)
        n = len(data)
        if n > self.slot_capacity:
            raise ValueError(f"IMU 数据 {n} 字节超过单槽容量 {self.slot_capacity}")
        idx = self._read_write_seq()
        slot = idx % self.slot_count
        doff = self._data_off(slot)
        self._buf[doff : doff + n] = data
        _M.pack_into(
            self._buf, self._meta_off(slot),
            idx, n, len(samples),
            int(trigger_ms) & 0xFFFFFFFFFFFFFFFF,
            int(recv_ms) & 0xFFFFFFFFFFFFFFFF,
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


class SharedImuReader(_RingBase):
    def __init__(self, name: str):
        shm = shared_memory.SharedMemory(name=name, create=False)
        _detach_from_tracker(shm)
        magic, version, slot_count, slot_capacity, meta_size = _H.unpack_from(shm.buf, 0)
        if magic != MAGIC or version != VERSION:
            shm.close()
            raise ValueError(f"共享内存 {name} 魔数/版本不兼容")
        super().__init__(shm, slot_count, slot_capacity)

    def _try_read_latest(self) -> Optional[ImuView]:
        s1 = self._read_write_seq()
        if s1 == 0:
            return None
        idx = s1 - 1
        slot = idx % self.slot_count
        moff = self._meta_off(slot)
        seq, nbytes, scount, trig, recv, fnum = _M.unpack_from(self._buf, moff)
        if seq != idx or nbytes == 0 or nbytes > self.slot_capacity:
            return None
        doff = self._data_off(slot)
        data = bytes(self._buf[doff : doff + nbytes])
        if _M.unpack_from(self._buf, moff)[0] != idx:
            return None
        return ImuView(
            meta=ImuMeta(seq=idx, nbytes=nbytes, sample_count=scount,
                         trigger_ms=trig, recv_ms=recv, frame_num=fnum),
            samples=unpack_samples(data),
        )

    def latest_seq(self) -> int:
        return self._read_write_seq() - 1

    def read_latest(self, retries: int = 8) -> Optional[ImuView]:
        for _ in range(max(1, retries)):
            v = self._try_read_latest()
            if v is not None:
                return v
            time.sleep(0.0002)
        return None

    def read_new(self, last_seq: int, *, stop=None, timeout: Optional[float] = None,
                 poll_interval: float = 0.001) -> Optional[ImuView]:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            if stop is not None and stop.is_set():
                return None
            if self.latest_seq() > last_seq:
                v = self.read_latest()
                if v is not None and v.meta.seq > last_seq:
                    return v
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
