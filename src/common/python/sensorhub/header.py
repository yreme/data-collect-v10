"""数据帧头 FrameHeader v1（64 字节，小端），放在 Zenoh sample 的 *attachment* 中。

payload 只放原始数据（图像像素 / 点云 / IMU 样本数组），可以直接走 SHM；
帧头放 attachment，订阅方无需触碰 payload 就能拿到时间戳、序号、尺寸——
控制台测频率/延迟时只读 attachment，因此几乎零开销。

C++ 等价定义：``src/common/cpp/sensorhub/frame_header.hpp``（两边必须同步修改，并升级 VERSION）。

布局::

    off size field        说明
    0   4    magic        b"SHF1"
    4   2    version      1
    6   1    kind         Kind 枚举
    7   1    encoding     Encoding 枚举
    8   8    seq          发布序号（从 0 递增，断档=丢帧）
    16  8    trigger_ns   同步网格时刻（Unix ns）；未参与同步为 0
    24  8    stamp_ns     传感器时间（曝光开始/扫描起点/采样时刻，Unix ns）
    32  8    pub_ns       发布时刻（Unix ns，host 时钟）
    40  4    width        图像宽；点云=点数；IMU=样本数
    44  4    height       图像高；点云=1（无序）或线数
    48  4    step         图像每行字节；点云/IMU=单元素字节数
    52  4    count        元素数（点数/样本数/图像=1）
    56  4    flags        Flags 位
    60  4    reserved
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, replace
from enum import IntEnum, IntFlag
from typing import Optional, Union

MAGIC = b"SHF1"
VERSION = 1
HEADER_SIZE = 64
_FMT = struct.Struct("<4sHBBQqqqIIIIII")
assert _FMT.size == HEADER_SIZE


class Kind(IntEnum):
    UNKNOWN = 0
    CAMERA = 1
    LIDAR = 2
    IMU = 3
    PLC = 4
    GNSS = 5
    APP = 10


KIND_BY_NAME = {
    "camera": Kind.CAMERA, "lidar": Kind.LIDAR, "imu": Kind.IMU,
    "plc": Kind.PLC, "gnss": Kind.GNSS, "app": Kind.APP,
}


class Encoding(IntEnum):
    UNKNOWN = 0
    # --- 图像（未压缩） ---
    MONO8 = 1
    BGR8 = 2
    RGB8 = 3
    BAYER_RGGB8 = 4
    BAYER_BGGR8 = 5
    BAYER_GBRG8 = 6
    BAYER_GRBG8 = 7
    MONO16 = 8
    # --- 图像（压缩） ---
    JPEG = 10
    PNG = 11
    # --- 点云 ---
    XYZI_F32 = 20      # x,y,z,intensity: 4*float32 = 16B
    XYZIRT_F32 = 21    # x,y,z,intensity f32, ring u16, pad u16, t_off_s f32 = 24B
    # --- IMU ---
    IMU_V1 = 30        # 见 codecs.IMU_SAMPLE（56B/样本）
    # --- 通用 ---
    JSON = 40
    CBOR = 41
    PROTOBUF = 42


class Flags(IntFlag):
    NONE = 0
    SYNC_HW_TRIGGER = 1 << 0    # 硬件触发线
    SYNC_ACTION_CMD = 1 << 1    # GigE Vision Action Command（PTP 计划触发）
    SYNC_SOFT_TRIGGER = 1 << 2  # 软件触发（按网格时刻）
    SYNC_PHASE_LOCK = 1 << 3    # 雷达相位锁定（PTP/PPS）
    CLOCK_PTP = 1 << 4          # stamp_ns 来自 PTP 同步后的设备时钟
    CLOCK_HOST = 1 << 5         # stamp_ns 是主机接收时刻（设备无可靠时钟）
    GAP_BEFORE = 1 << 6         # 本帧之前发生丢帧
    MOCK = 1 << 7               # 模拟数据


# 编码 -> (每像素字节数, foxglove/ROS 编码名)
IMAGE_ENCODINGS = {
    Encoding.MONO8: (1, "mono8"),
    Encoding.BGR8: (3, "bgr8"),
    Encoding.RGB8: (3, "rgb8"),
    Encoding.BAYER_RGGB8: (1, "bayer_rggb8"),
    Encoding.BAYER_BGGR8: (1, "bayer_bggr8"),
    Encoding.BAYER_GBRG8: (1, "bayer_gbrg8"),
    Encoding.BAYER_GRBG8: (1, "bayer_grbg8"),
    Encoding.MONO16: (2, "mono16"),
}

ENCODING_BY_NAME = {e.name.lower(): e for e in Encoding}


def encoding_from_name(name: Union[str, int, Encoding]) -> Encoding:
    if isinstance(name, Encoding):
        return name
    if isinstance(name, int):
        return Encoding(name)
    try:
        return ENCODING_BY_NAME[str(name).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"未知编码 {name!r}，可选 {sorted(ENCODING_BY_NAME)}") from exc


def now_ns() -> int:
    return time.time_ns()


@dataclass(frozen=True)
class FrameHeader:
    kind: Kind = Kind.UNKNOWN
    encoding: Encoding = Encoding.UNKNOWN
    seq: int = 0
    trigger_ns: int = 0
    stamp_ns: int = 0
    pub_ns: int = 0
    width: int = 0
    height: int = 0
    step: int = 0
    count: int = 0
    flags: int = 0
    version: int = VERSION

    def pack(self) -> bytes:
        return _FMT.pack(
            MAGIC, self.version, int(self.kind), int(self.encoding),
            self.seq, self.trigger_ns, self.stamp_ns, self.pub_ns,
            self.width, self.height, self.step, self.count, int(self.flags), 0,
        )

    @classmethod
    def unpack(cls, data: Union[bytes, bytearray, memoryview]) -> "FrameHeader":
        if len(data) < HEADER_SIZE:
            raise ValueError(f"帧头长度 {len(data)} < {HEADER_SIZE}")
        (magic, version, kind, enc, seq, trig, stamp, pub,
         w, h, step, count, flags, _r) = _FMT.unpack_from(data, 0)
        if magic != MAGIC:
            raise ValueError(f"帧头 magic 错误 {magic!r}")
        if version > VERSION:
            raise ValueError(f"帧头版本 {version} 高于本 SDK 支持的 {VERSION}，请升级 sensorhub")
        try:
            kind_e = Kind(kind)
        except ValueError:
            kind_e = Kind.UNKNOWN
        try:
            enc_e = Encoding(enc)
        except ValueError:
            enc_e = Encoding.UNKNOWN
        return cls(kind_e, enc_e, seq, trig, stamp, pub, w, h, step, count, flags, version)

    def with_pub_now(self) -> "FrameHeader":
        return replace(self, pub_ns=now_ns())

    # ---- 便捷属性 ----
    @property
    def latency_ns(self) -> int:
        """发布时刻 - 传感器时刻（采集+处理延迟）。"""
        return self.pub_ns - self.stamp_ns if self.stamp_ns and self.pub_ns else 0

    @property
    def sync_error_ns(self) -> int:
        """传感器时刻 - 网格时刻（触发同步误差）。"""
        return self.stamp_ns - self.trigger_ns if self.stamp_ns and self.trigger_ns else 0

    def expected_payload_size(self) -> Optional[int]:
        if self.encoding in IMAGE_ENCODINGS:
            return self.step * self.height
        if self.encoding in (Encoding.XYZI_F32, Encoding.XYZIRT_F32, Encoding.IMU_V1):
            return self.step * self.count
        return None


def image_header(
    *, encoding: Union[str, Encoding], width: int, height: int, seq: int,
    stamp_ns: int, trigger_ns: int = 0, flags: int = 0, step: Optional[int] = None,
) -> FrameHeader:
    enc = encoding_from_name(encoding)
    if enc not in IMAGE_ENCODINGS and enc not in (Encoding.JPEG, Encoding.PNG):
        raise ValueError(f"{enc.name} 不是图像编码")
    bpp = IMAGE_ENCODINGS.get(enc, (0, ""))[0]
    return FrameHeader(
        kind=Kind.CAMERA, encoding=enc, seq=seq, trigger_ns=trigger_ns,
        stamp_ns=stamp_ns, width=width, height=height,
        step=step if step is not None else width * bpp, count=1, flags=flags,
    )
