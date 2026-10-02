"""Yesense IMU 样本编解码（固定二进制布局）。"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

# tid(H) smp_ts(I) ready_ts(I) acc(3d) gyro(3d) euler(3d) quat(4d) pos(3d) vel(3d) temp(f) status(B) pad(3)
_SAMPLE_FMT = "<HII" + "d" * 19 + "fB3x"
_SAMPLE_SIZE = struct.calcsize(_SAMPLE_FMT)


@dataclass
class ImuSample:
    tid: int = 0
    smp_timestamp: int = 0
    ready_timestamp: int = 0
    acc_x: float = 0.0
    acc_y: float = 0.0
    acc_z: float = 0.0
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    q0: float = 1.0
    q1: float = 0.0
    q2: float = 0.0
    q3: float = 0.0
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0
    vel_e: float = 0.0
    vel_n: float = 0.0
    vel_u: float = 0.0
    sensor_temp: float = 25.0
    status: int = 0
    # extra fields not in binary payload
    lat: float = 0.0
    longt: float = 0.0
    alt: float = 0.0
    norm_mag_x: float = 0.0
    norm_mag_y: float = 0.0
    norm_mag_z: float = 0.0
    extra: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_decoder_dict(cls, d: Dict) -> "ImuSample":
        s = cls()
        for key in (
            "tid", "smp_timestamp", "ready_timestamp",
            "acc_x", "acc_y", "acc_z",
            "gyro_x", "gyro_y", "gyro_z",
            "roll", "pitch", "yaw",
            "q0", "q1", "q2", "q3",
            "vel_e", "vel_n", "vel_u",
            "sensor_temp", "status",
            "lat", "longt", "alt",
            "pos_x", "pos_y", "pos_z",
            "norm_mag_x", "norm_mag_y", "norm_mag_z",
        ):
            if key in d:
                setattr(s, key, d[key])
        if "pos_x" not in d and "pos_y" not in d and "pos_z" not in d:
            if s.lat or s.longt or s.alt:
                s.pos_x = float(s.longt)
                s.pos_y = float(s.lat)
                s.pos_z = float(s.alt)
        return s

    def pack(self) -> bytes:
        return struct.pack(
            _SAMPLE_FMT,
            int(self.tid) & 0xFFFF,
            int(self.smp_timestamp) & 0xFFFFFFFF,
            int(self.ready_timestamp) & 0xFFFFFFFF,
            self.acc_x, self.acc_y, self.acc_z,
            self.gyro_x, self.gyro_y, self.gyro_z,
            self.roll, self.pitch, self.yaw,
            self.q0, self.q1, self.q2, self.q3,
            self.pos_x, self.pos_y, self.pos_z,
            self.vel_e, self.vel_n, self.vel_u,
            float(self.sensor_temp),
            int(self.status) & 0xFF,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ImuSample":
        if len(data) < _SAMPLE_SIZE:
            raise ValueError(f"IMU 样本过短: {len(data)} < {_SAMPLE_SIZE}")
        vals = struct.unpack(_SAMPLE_FMT, data[:_SAMPLE_SIZE])
        s = cls(
            tid=vals[0],
            smp_timestamp=vals[1],
            ready_timestamp=vals[2],
            acc_x=vals[3], acc_y=vals[4], acc_z=vals[5],
            gyro_x=vals[6], gyro_y=vals[7], gyro_z=vals[8],
            roll=vals[9], pitch=vals[10], yaw=vals[11],
            q0=vals[12], q1=vals[13], q2=vals[14], q3=vals[15],
            pos_x=vals[16], pos_y=vals[17], pos_z=vals[18],
            vel_e=vals[19], vel_n=vals[20], vel_u=vals[21],
            sensor_temp=vals[22],
            status=vals[23],
        )
        return s

    def euler_to_quaternion(self) -> tuple:
        """若四元数无效，从欧拉角生成。"""
        if abs(self.q0) + abs(self.q1) + abs(self.q2) + abs(self.q3) > 1e-6:
            n = math.sqrt(self.q0**2 + self.q1**2 + self.q2**2 + self.q3**2)
            if n > 1e-6:
                return self.q1 / n, self.q2 / n, self.q3 / n, self.q0 / n
        roll, pitch, yaw = map(math.radians, (self.roll, self.pitch, self.yaw))
        cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
        cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
        cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
        w = cr * cp * cy + sr * sp * sy
        x = sr * cp * cy - cr * sp * sy
        y = cr * sp * cy + sr * cp * sy
        z = cr * cp * sy - sr * sp * cy
        return x, y, z, w


def mock_imu_sample(frame: int) -> ImuSample:
    t = frame * 0.05
    return ImuSample(
        tid=frame & 0xFFFF,
        smp_timestamp=int(time.time() * 1000) & 0xFFFFFFFF,
        acc_x=0.1 * math.sin(t),
        acc_y=0.1 * math.cos(t),
        acc_z=9.81,
        gyro_x=0.5 * math.sin(t * 0.5),
        gyro_y=0.3 * math.cos(t * 0.5),
        gyro_z=0.1,
        roll=10.0 * math.sin(t * 0.2),
        pitch=5.0 * math.cos(t * 0.2),
        yaw=(frame * 2.0) % 360.0,
        vel_e=0.2 * math.sin(t * 0.1),
        vel_n=0.1 * math.cos(t * 0.1),
        vel_u=0.05,
        pos_x=0.5 * math.sin(t * 0.1),
        pos_y=0.5 * math.cos(t * 0.1),
        pos_z=1.0 + 0.1 * math.sin(t * 0.3),
        sensor_temp=25.0 + 0.5 * math.sin(t),
        status=1,
    )


SAMPLE_BYTES = _SAMPLE_SIZE
