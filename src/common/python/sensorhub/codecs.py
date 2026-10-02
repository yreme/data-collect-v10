"""payload 编解码（与 header.Encoding 对应）。所有结构均为小端、无填充。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

import numpy as np

from .header import Encoding

# ---------------- 点云 ----------------
XYZI_DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("intensity", "<f4")])
XYZIRT_DTYPE = np.dtype([
    ("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("intensity", "<f4"),
    ("ring", "<u2"), ("_pad", "<u2"), ("t", "<f4"),  # t: 相对 stamp_ns 的秒偏移
])
assert XYZI_DTYPE.itemsize == 16 and XYZIRT_DTYPE.itemsize == 24

POINT_DTYPES = {Encoding.XYZI_F32: XYZI_DTYPE, Encoding.XYZIRT_F32: XYZIRT_DTYPE}


def points_from_bytes(data: bytes, encoding: Encoding) -> np.ndarray:
    return np.frombuffer(data, dtype=POINT_DTYPES[encoding])


# ---------------- IMU ----------------
IMU_SAMPLE_DTYPE = np.dtype([
    ("stamp_ns", "<i8"),
    ("ax", "<f4"), ("ay", "<f4"), ("az", "<f4"),      # m/s^2
    ("gx", "<f4"), ("gy", "<f4"), ("gz", "<f4"),      # rad/s
    ("qw", "<f4"), ("qx", "<f4"), ("qy", "<f4"), ("qz", "<f4"),  # 姿态四元数（无则 1,0,0,0）
    ("temp_c", "<f4"),
    ("status", "<u4"),
])
assert IMU_SAMPLE_DTYPE.itemsize == 56


@dataclass(frozen=True)
class ImuSample:
    stamp_ns: int
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    qw: float = 1.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    temp_c: float = 0.0
    status: int = 0


def pack_imu(samples: Sequence[ImuSample]) -> bytes:
    arr = np.zeros(len(samples), dtype=IMU_SAMPLE_DTYPE)
    for i, s in enumerate(samples):
        arr[i] = (s.stamp_ns, s.ax, s.ay, s.az, s.gx, s.gy, s.gz,
                  s.qw, s.qx, s.qy, s.qz, s.temp_c, s.status)
    return arr.tobytes()


def unpack_imu(data: bytes) -> List[ImuSample]:
    arr = np.frombuffer(data, dtype=IMU_SAMPLE_DTYPE)
    return [ImuSample(*(r.item() if hasattr(r, "item") else r for r in row)) for row in arr]


def imu_array(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=IMU_SAMPLE_DTYPE)


def concat(chunks: Iterable[bytes]) -> bytes:
    return b"".join(chunks)
