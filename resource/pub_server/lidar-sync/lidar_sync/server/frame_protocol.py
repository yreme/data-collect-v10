"""从 lidar-capture stdout 读取 FRAME 二进制帧。"""

from __future__ import annotations

from typing import BinaryIO, Optional, Tuple


def read_frame(stream: BinaryIO) -> Optional[Tuple[int, int, bytes, int]]:
    """读取一帧。返回 (seq, point_count, data, trigger_ms) 或 None（EOF）。"""
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("FRAME "):
            continue
        parts = text.split()
        if len(parts) < 5:
            continue
        seq = int(parts[1])
        point_count = int(parts[2])
        nbytes = int(parts[3])
        trigger_ms = int(parts[4])
        if nbytes < 0:
            continue
        data = stream.read(nbytes) if nbytes else b""
        if len(data) != nbytes:
            return None
        return seq, point_count, data, trigger_ms
