"""Foxglove 消息构建（sensor_msgs/Imu JSON + SceneUpdate + FrameTransform）。"""

from __future__ import annotations

import math
import time
from typing import Any, Dict

from ..config import ImuConfig, SceneModelConfig
from ..imu_codec import ImuSample
from ..shm import ImuView
from .pose_tracker import PoseState

# sensor_msgs/Imu 兼容 JSON schema（Foxglove Studio 可识别字段）
IMU_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "sensor_msgs/Imu",
    "properties": {
        "header": {
            "type": "object",
            "properties": {
                "stamp": {
                    "type": "object",
                    "properties": {
                        "sec": {"type": "integer"},
                        "nanosec": {"type": "integer"},
                    },
                },
                "frame_id": {"type": "string"},
            },
        },
        "orientation": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "w": {"type": "number"},
            },
        },
        "angular_velocity": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
        },
        "linear_acceleration": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
        },
        "orientation_covariance": {"type": "array", "items": {"type": "number"}},
        "angular_velocity_covariance": {"type": "array", "items": {"type": "number"}},
        "linear_acceleration_covariance": {"type": "array", "items": {"type": "number"}},
    },
}


def _gyro_to_rad(v_dps: float) -> float:
    return math.radians(v_dps)


def _stamp_dict(log_ns: int) -> dict:
    return {"sec": int(log_ns // 1_000_000_000), "nanosec": int(log_ns % 1_000_000_000)}


def _foxglove_timestamp(log_ns: int):
    from foxglove.messages import Timestamp

    return Timestamp(sec=int(log_ns // 1_000_000_000), nsec=int(log_ns % 1_000_000_000))


def build_imu_message(imu_cfg: ImuConfig, sample: ImuSample, log_ns: int) -> dict:
    """构建 sensor_msgs/Imu 兼容 JSON 消息。"""
    qx, qy, qz, qw = sample.euler_to_quaternion()
    zeros = [0.0] * 9
    return {
        "header": {
            "stamp": _stamp_dict(log_ns),
            "frame_id": imu_cfg.frame_id or imu_cfg.name,
        },
        "orientation": {"x": qx, "y": qy, "z": qz, "w": qw},
        "orientation_covariance": zeros,
        "angular_velocity": {
            "x": _gyro_to_rad(sample.gyro_x),
            "y": _gyro_to_rad(sample.gyro_y),
            "z": _gyro_to_rad(sample.gyro_z),
        },
        "angular_velocity_covariance": zeros,
        "linear_acceleration": {
            "x": sample.acc_x,
            "y": sample.acc_y,
            "z": sample.acc_z,
        },
        "linear_acceleration_covariance": zeros,
    }


def build_scene_update(imu_cfg: ImuConfig, pose: PoseState, log_ns: int):
    from foxglove.messages import (
        Color,
        CubePrimitive,
        Duration,
        Pose,
        Quaternion,
        SceneEntity,
        SceneUpdate,
        Vector3,
    )

    px, py, pz = pose.position
    qx, qy, qz, qw = pose.orientation

    entity = SceneEntity(
        timestamp=_foxglove_timestamp(log_ns),
        frame_id=pose.world_frame,
        id=imu_cfg.name,
        lifetime=Duration(sec=0, nsec=200_000_000),
        frame_locked=True,
        cubes=[
            CubePrimitive(
                pose=Pose(
                    position=Vector3(x=px, y=py, z=pz),
                    orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
                ),
                size=Vector3(x=0.4, y=0.2, z=0.1),
                color=Color(r=0.2, g=0.7, b=1.0, a=0.9),
            )
        ],
    )
    return SceneUpdate(entities=[entity])


def build_frame_transform(pose: PoseState, log_ns: int):
    from foxglove.messages import FrameTransform, Quaternion, Vector3

    px, py, pz = pose.position
    qx, qy, qz, qw = pose.orientation
    return FrameTransform(
        timestamp=_foxglove_timestamp(log_ns),
        parent_frame_id=pose.world_frame,
        child_frame_id=pose.child_frame,
        translation=Vector3(x=px, y=py, z=pz),
        rotation=Quaternion(x=qx, y=qy, z=qz, w=qw),
    )


def build_world_pose(pose: PoseState, log_ns: int):
    """累计 world 位姿（位置 + 四元数），用于独立 world_pose topic。"""
    from foxglove.messages import Pose, PoseInFrame, Quaternion, Vector3

    px, py, pz = pose.position
    qx, qy, qz, qw = pose.orientation
    return PoseInFrame(
        timestamp=_foxglove_timestamp(log_ns),
        frame_id=pose.world_frame,
        pose=Pose(
            position=Vector3(x=px, y=py, z=pz),
            orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
        ),
    )


def build_scene_model_update(
    imu_cfg: ImuConfig,
    pose: PoseState,
    log_ns: int,
    model_cfg: SceneModelConfig,
):
    """SceneUpdate：在 world 坐标系放置 GLB/GLTF 3D 模型。"""
    from foxglove.messages import (
        Duration,
        ModelPrimitive,
        Pose,
        Quaternion,
        SceneEntity,
        SceneUpdate,
        Vector3,
    )

    px, py, pz = pose.position
    qx, qy, qz, qw = pose.orientation
    sx, sy, sz = model_cfg.scale

    entity = SceneEntity(
        timestamp=_foxglove_timestamp(log_ns),
        frame_id=pose.world_frame,
        id=model_cfg.entity_id or imu_cfg.name,
        lifetime=Duration(sec=0, nsec=200_000_000),
        frame_locked=True,
        models=[
            ModelPrimitive(
                pose=Pose(
                    position=Vector3(x=px, y=py, z=pz),
                    orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
                ),
                scale=Vector3(x=sx, y=sy, z=sz),
                url=model_cfg.url,
                media_type=model_cfg.media_type,
            )
        ],
    )
    return SceneUpdate(entities=[entity])


def sample_from_view(view: ImuView) -> ImuSample:
    return ImuSample.unpack(view.data)


def log_ns_from_view(view: ImuView) -> int:
    m = view.meta
    ts_ms = m.trigger_ms or m.recv_ms or int(time.time() * 1000)
    return int(ts_ms) * 1_000_000
