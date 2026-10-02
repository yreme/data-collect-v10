"""IMU 位姿跟踪：world 坐标系与 IMU 坐标系之间的变换。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Tuple

from ..config import ImuConfig, PoseConfig, SceneModelConfig
from ..imu_codec import ImuSample

Quat = Tuple[float, float, float, float]  # x, y, z, w
Vec3 = Tuple[float, float, float]


@dataclass
class PoseState:
    """IMU 在 world 坐标系下的位姿。"""

    position: Vec3
    orientation: Quat
    world_frame: str
    child_frame: str
    translation_source: str = ""


def _vec3_norm(v: Vec3) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _quat_normalize(q: Quat) -> Quat:
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        return 0.0, 0.0, 0.0, 1.0
    return x / n, y / n, z / n, w / n


def _quat_multiply(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _rpy_deg_to_quat(roll_deg: float, pitch_deg: float, yaw_deg: float) -> Quat:
    roll, pitch, yaw = map(math.radians, (roll_deg, pitch_deg, yaw_deg))
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return _quat_normalize((x, y, z, w))


def _rotate_vector_by_quat(v: Vec3, q: Quat) -> Vec3:
    x, y, z = v
    qx, qy, qz, qw = _quat_normalize(q)
    tx = 2.0 * (qy * z - qz * y)
    ty = 2.0 * (qz * x - qx * z)
    tz = 2.0 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def _velocity_world(sample: ImuSample, velocity_frame: str) -> Vec3:
    ve, vn, vu = sample.vel_e, sample.vel_n, sample.vel_u
    frame = (velocity_frame or "enu").strip().lower()
    if frame == "ned":
        return vn, ve, -vu
    return ve, vn, vu


def _has_sensor_velocity(sample: ImuSample, deadband: float) -> bool:
    return _vec3_norm(_velocity_world(sample, "enu")) >= deadband


def _has_sensor_position(sample: ImuSample) -> bool:
    return abs(sample.pos_x) + abs(sample.pos_y) + abs(sample.pos_z) > 1e-9


def pose_config_for_imu(base: PoseConfig, imu_cfg: ImuConfig) -> PoseConfig:
    """合并客户端默认 pose 配置与单路 IMU 覆盖项。"""
    raw = imu_cfg.params.get("pose")
    if not isinstance(raw, dict):
        return base
    return PoseConfig.from_dict(raw, defaults=base)


def scene_model_config_for_imu(base: SceneModelConfig, imu_cfg: ImuConfig) -> SceneModelConfig:
    """合并客户端默认 scene_model 配置与单路 IMU 覆盖项。"""
    raw = imu_cfg.params.get("scene_model")
    if not isinstance(raw, dict):
        return base
    return SceneModelConfig.from_dict(raw, defaults=base)


@dataclass
class ImuPoseTracker:
    """根据 IMU 样本计算 T_world_imu。"""

    config: PoseConfig
    imu_cfg: ImuConfig
    _position: Vec3 = field(init=False)
    _velocity: Vec3 = field(init=False)
    _init_quat: Quat = field(init=False)
    _last_log_ns: int | None = field(default=None, init=False)
    _position_origin: Vec3 | None = field(default=None, init=False)
    _translation_source: str = field(default="", init=False)

    def __post_init__(self) -> None:
        p = self.config.initial_position
        self._position = (float(p[0]), float(p[1]), float(p[2]))
        self._velocity = (0.0, 0.0, 0.0)
        rpy = self.config.initial_orientation_rpy
        self._init_quat = _rpy_deg_to_quat(float(rpy[0]), float(rpy[1]), float(rpy[2]))

    @property
    def child_frame(self) -> str:
        return self.imu_cfg.frame_id or self.imu_cfg.name

    def reset(self) -> None:
        p = self.config.initial_position
        self._position = (float(p[0]), float(p[1]), float(p[2]))
        self._velocity = (0.0, 0.0, 0.0)
        rpy = self.config.initial_orientation_rpy
        self._init_quat = _rpy_deg_to_quat(float(rpy[0]), float(rpy[1]), float(rpy[2]))
        self._last_log_ns = None
        self._position_origin = None
        self._translation_source = ""

    def update(self, sample: ImuSample, log_ns: int) -> PoseState:
        q_imu = _quat_normalize(sample.euler_to_quaternion())
        q_world = _quat_normalize(_quat_multiply(self._init_quat, q_imu))

        mode = (self.config.translation_mode or "auto").strip().lower()
        if mode == "auto":
            self._integrate_auto(sample, log_ns, q_world)
        elif mode == "position":
            self._position = self._position_from_sensor(sample)
            self._translation_source = "position"
        elif mode == "delta":
            self._integrate_delta(sample)
        elif mode == "velocity":
            self._integrate_velocity(sample, log_ns, q_world)
        elif mode == "accel":
            self._integrate_accel(sample, log_ns, q_world)

        return PoseState(
            position=self._position,
            orientation=q_world,
            world_frame=self.config.world_frame,
            child_frame=self.child_frame,
            translation_source=self._translation_source,
        )

    def _integrate_auto(self, sample: ImuSample, log_ns: int, q_world: Quat) -> None:
        if _has_sensor_velocity(sample, self.config.velocity_deadband):
            self._integrate_velocity(sample, log_ns, q_world)
            return
        if _has_sensor_position(sample):
            self._position = self._position_from_sensor(sample)
            self._translation_source = "position"
            return
        self._integrate_accel(sample, log_ns, q_world)

    def _position_from_sensor(self, sample: ImuSample) -> Vec3:
        px, py, pz = sample.pos_x, sample.pos_y, sample.pos_z
        if self.config.position_relative:
            if self._position_origin is None:
                self._position_origin = (px, py, pz)
            ox, oy, oz = self._position_origin
            dx, dy, dz = px - ox, py - oy, pz - oz
        else:
            dx, dy, dz = px, py, pz
        ix, iy, iz = self.config.initial_position
        return ix + dx, iy + dy, iz + dz

    def _integrate_delta(self, sample: ImuSample) -> None:
        """将每帧相对位移 pos_x/y/z 累加到 world 坐标。"""
        x, y, z = self._position
        self._position = (
            x + sample.pos_x,
            y + sample.pos_y,
            z + sample.pos_z,
        )
        self._translation_source = "delta"

    def _dt_seconds(self, log_ns: int) -> float | None:
        if self._last_log_ns is None:
            self._last_log_ns = log_ns
            return None
        dt = (log_ns - self._last_log_ns) / 1_000_000_000.0
        self._last_log_ns = log_ns
        if dt <= 0.0 or dt > 1.0:
            return None
        return dt

    def _apply_velocity_damping(self) -> None:
        damp = max(0.0, min(1.0, self.config.velocity_damping))
        if damp <= 0.0:
            return
        scale = max(0.0, 1.0 - damp)
        vx, vy, vz = self._velocity
        self._velocity = (vx * scale, vy * scale, vz * scale)

    def _integrate_velocity(self, sample: ImuSample, log_ns: int, q_world: Quat) -> None:
        dt = self._dt_seconds(log_ns)
        if dt is None:
            return

        vx, vy, vz = _velocity_world(sample, self.config.velocity_frame)
        frame = (self.config.velocity_frame or "enu").strip().lower()
        if frame == "body":
            vx, vy, vz = _rotate_vector_by_quat((vx, vy, vz), q_world)

        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        if speed < self.config.velocity_deadband:
            self._apply_velocity_damping()
            return

        x, y, z = self._position
        self._position = (x + vx * dt, y + vy * dt, z + vz * dt)
        self._velocity = (vx, vy, vz)
        self._translation_source = "velocity"

    def _integrate_accel(self, sample: ImuSample, log_ns: int, q_world: Quat) -> None:
        """用 body 加速度（去重力）积分得到 world 平移。"""
        dt = self._dt_seconds(log_ns)
        if dt is None:
            return

        a_body = (sample.acc_x, sample.acc_y, sample.acc_z)
        a_world = _rotate_vector_by_quat(a_body, q_world)
        g = self.config.gravity
        # ENU：静止时 body z 朝上，加速度计读数约 +g，线性加速度 = a_world - (0,0,g)
        linear = (a_world[0], a_world[1], a_world[2] - g)
        if _vec3_norm(linear) < self.config.accel_deadband:
            self._apply_velocity_damping()
            return

        vx, vy, vz = self._velocity
        vx += linear[0] * dt
        vy += linear[1] * dt
        vz += linear[2] * dt
        self._velocity = (vx, vy, vz)

        x, y, z = self._position
        self._position = (x + vx * dt, y + vy * dt, z + vz * dt)
        self._translation_source = "accel"


class PoseTrackerRegistry:
    """按 IMU 名称维护独立的位姿跟踪器。"""

    def __init__(self, base_pose: PoseConfig) -> None:
        self._base = base_pose
        self._trackers: dict[str, ImuPoseTracker] = {}

    def update(self, imu_cfg: ImuConfig, sample: ImuSample, log_ns: int) -> PoseState:
        tracker = self._trackers.get(imu_cfg.name)
        if tracker is None:
            cfg = pose_config_for_imu(self._base, imu_cfg)
            tracker = ImuPoseTracker(config=cfg, imu_cfg=imu_cfg)
            self._trackers[imu_cfg.name] = tracker
        return tracker.update(sample, log_ns)
