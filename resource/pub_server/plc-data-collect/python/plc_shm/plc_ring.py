"""Lock-free SPMC PLC shared memory ring buffer (PLCC magic)."""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Optional

try:
    from multiprocessing import resource_tracker as _resource_tracker
except Exception:  # noqa: BLE001
    _resource_tracker = None

MAGIC = 0x504C4300  # 'PLC\0'
VERSION = 1

_HEADER_SIZE = 128
_META_SIZE = 64

_H = struct.Struct("<IIIII")
_H_OFF = 0
_WRITE_SEQ_OFF = 64
_WRITE_SEQ = struct.Struct("<Q")

# seq, nbytes, source_port, trigger_ms, recv_ms, host_ms, heart_beat, reserved
_M = struct.Struct("<QIIQQQII")


@dataclass(frozen=True)
class PlcShmMeta:
    seq: int
    nbytes: int
    source_port: int
    trigger_ms: int
    recv_ms: int
    host_ms: int
    heart_beat: int


@dataclass
class PlcShmView:
    meta: PlcShmMeta
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


def pack_plc_dict(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def unpack_plc_dict(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


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
        return _WRITE_SEQ.unpack_from(self._buf, _WRITE_SEQ_OFF)[0]

    def close(self) -> None:
        try:
            self._buf = None
        except Exception:  # noqa: BLE001
            pass
        try:
            self._shm.close()
        except Exception:  # noqa: BLE001
            pass


class SharedPlcWriter(_RingBase):
    """PLC SHM writer (UDP collector side)."""

    def __init__(
        self,
        name: str,
        slot_count: int = 32,
        slot_capacity: int = 512,
        create: bool = True,
    ):
        size = _total_size(slot_count, slot_capacity)
        shm = _create_shm(name, size, create=create)
        super().__init__(shm, slot_count, slot_capacity)
        self._created = create
        _H.pack_into(self._buf, _H_OFF, MAGIC, VERSION, slot_count, slot_capacity, _META_SIZE)
        _WRITE_SEQ.pack_into(self._buf, _WRITE_SEQ_OFF, 0)
        for s in range(slot_count):
            _M.pack_into(self._buf, self._meta_off(s), 0, 0, 0, 0, 0, 0, 0, 0)

    def publish(
        self,
        data: bytes,
        *,
        source_port: int = 0,
        trigger_ms: int = 0,
        recv_ms: int = 0,
        host_ms: int = 0,
        heart_beat: int = 0,
    ) -> int:
        n = len(data)
        if n > self.slot_capacity:
            raise ValueError(f"PLC 样本 {n} 字节超过单槽容量 {self.slot_capacity}")
        idx = self._read_write_seq()
        slot = idx % self.slot_count
        doff = self._data_off(slot)
        self._buf[doff : doff + n] = data
        now_ms = int(time.time() * 1000)
        _M.pack_into(
            self._buf,
            self._meta_off(slot),
            idx,
            n,
            int(source_port) & 0xFFFFFFFF,
            int(trigger_ms or now_ms) & 0xFFFFFFFFFFFFFFFF,
            int(recv_ms or now_ms) & 0xFFFFFFFFFFFFFFFF,
            int(host_ms or now_ms) & 0xFFFFFFFFFFFFFFFF,
            int(heart_beat) & 0xFFFFFFFF,
            0,
        )
        _WRITE_SEQ.pack_into(self._buf, _WRITE_SEQ_OFF, idx + 1)
        return idx

    def unlink(self) -> None:
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass


class SharedPlcReader(_RingBase):
    """PLC SHM reader (MCAP client side)."""

    def __init__(self, name: str):
        shm = shared_memory.SharedMemory(name=name, create=False)
        _detach_from_tracker(shm)
        magic, version, slot_count, slot_capacity, meta_size = _H.unpack_from(shm.buf, _H_OFF)
        if magic != MAGIC:
            shm.close()
            raise ValueError(f"共享内存 {name} 魔数不匹配 (expect PLCC)")
        if version != VERSION or meta_size != _META_SIZE:
            shm.close()
            raise ValueError(f"共享内存 {name} 版本不兼容: v{version}")
        super().__init__(shm, slot_count, slot_capacity)

    def _try_read_latest(self) -> Optional[PlcShmView]:
        s1 = self._read_write_seq()
        if s1 == 0:
            return None
        idx = s1 - 1
        slot = idx % self.slot_count
        moff = self._meta_off(slot)
        seq, nbytes, sport, trig, recv, host, hb, _ = _M.unpack_from(self._buf, moff)
        if seq != idx or nbytes == 0 or nbytes > self.slot_capacity:
            return None
        doff = self._data_off(slot)
        data = bytes(self._buf[doff : doff + nbytes])
        s2 = self._read_write_seq()
        if s2 - idx >= self.slot_count:
            return None
        if _M.unpack_from(self._buf, moff)[0] != idx:
            return None
        meta = PlcShmMeta(
            seq=idx,
            nbytes=nbytes,
            source_port=sport,
            trigger_ms=trig,
            recv_ms=recv,
            host_ms=host,
            heart_beat=hb,
        )
        return PlcShmView(meta=meta, data=data)

    def latest_seq(self) -> int:
        s = self._read_write_seq()
        return s - 1

    def read_latest(self, retries: int = 8) -> Optional[PlcShmView]:
        for _ in range(max(1, retries)):
            view = self._try_read_latest()
            if view is not None:
                return view
            time.sleep(0.0002)
        return None

    def read_new(
        self,
        last_seq: int,
        *,
        stop=None,
        timeout: Optional[float] = None,
        poll_interval: float = 0.001,
    ) -> Optional[PlcShmView]:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            if stop is not None and stop.is_set():
                return None
            if self.latest_seq() > last_seq:
                view = self.read_latest()
                if view is not None and view.meta.seq > last_seq:
                    return view
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


def default_segment_name(prefix: str = "plc_", device: str = "crane727r") -> str:
    return f"{prefix}{device}"
