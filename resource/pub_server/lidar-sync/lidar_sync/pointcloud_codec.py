"""点云二进制格式：x,y,z,intensity 各 float32（16 字节/点）。"""

from __future__ import annotations

import struct
from typing import List, Tuple

import numpy as np

_POINT_STRUCT = struct.Struct("<ffff")
BYTES_PER_POINT = 16


def pack_points(points: List[Tuple[float, float, float, float]]) -> bytes:
    return b"".join(_POINT_STRUCT.pack(x, y, z, i) for x, y, z, i in points)


def unpack_points(data: bytes) -> np.ndarray:
    """返回 shape (N, 4) 的 float32 数组 [x,y,z,intensity]。"""
    n = len(data) // BYTES_PER_POINT
    if n == 0:
        return np.zeros((0, 4), dtype=np.float32)
    arr = np.frombuffer(data[: n * BYTES_PER_POINT], dtype=np.float32)
    return arr.reshape(-1, 4)


def mock_pointcloud(num_points: int = 5000, frame_num: int = 0) -> bytes:
    """生成合成点云（测试用）。"""
    t = frame_num * 0.1
    angles = np.linspace(0, 2 * np.pi, num_points, dtype=np.float32)
    r = 5.0 + np.sin(angles * 3 + t).astype(np.float32)
    x = (r * np.cos(angles)).astype(np.float32)
    y = (r * np.sin(angles)).astype(np.float32)
    z = (np.sin(angles * 2 + t) * 0.5).astype(np.float32)
    intensity = (128 + 127 * np.sin(angles + t)).astype(np.float32)
    pts = np.stack([x, y, z, intensity], axis=1)
    return pts.astype(np.float32).tobytes()
