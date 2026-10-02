"""Build Foxglove-compatible messages and JSON schemas for 727R PLC data."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from foxglove_schemas_protobuf.CubePrimitive_pb2 import CubePrimitive
from foxglove_schemas_protobuf.LinePrimitive_pb2 import LinePrimitive
from foxglove_schemas_protobuf.Point3_pb2 import Point3
from foxglove_schemas_protobuf.SceneEntity_pb2 import SceneEntity
from foxglove_schemas_protobuf.SceneUpdate_pb2 import SceneUpdate
from foxglove_schemas_protobuf.TextPrimitive_pb2 import TextPrimitive
from foxglove_schemas_protobuf.TriangleListPrimitive_pb2 import TriangleListPrimitive

from udp_parser import PlcPacket

TOPIC_RAW = "/crane727r/plc/raw"
TOPIC_SCENE = "/crane727r/spreader/scene"
TOPIC_POSITION = "/crane727r/kinematics/position"
TOPIC_VELOCITY = "/crane727r/kinematics/velocity"
TOPIC_SMH = "/crane727r/signals/smh"
TOPIC_OICR = "/crane727r/signals/oicr"
TOPIC_HEARTBEAT = "/crane727r/diagnostics/heartbeat"
TOPIC_CONNECTION = "/crane727r/diagnostics/connection"

JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "crane727r.PlcRaw": {
        "type": "object",
        "properties": {
            "smh": {"type": "object"},
            "oicr": {"type": "object"},
            "mh_pos": {"type": "integer"},
            "mt_pos": {"type": "integer"},
            "mc_pos": {"type": "integer"},
            "cntrh_pos": {"type": "integer"},
            "heart_beat": {"type": "integer"},
            "source_ip": {"type": "string"},
            "source_port": {"type": "integer"},
            "received_at": {"type": "number"},
        },
    },
    "crane727r.Position": {
        "type": "object",
        "properties": {
            "mh_pos": {"type": "number"},
            "mt_pos": {"type": "number"},
            "mc_pos": {"type": "number"},
            "cntrh_pos": {"type": "number"},
        },
    },
    "crane727r.Velocity": {
        "type": "object",
        "properties": {
            "mh_vel": {"type": "number"},
            "mt_vel": {"type": "number"},
            "mc_vel": {"type": "number"},
            "cntrh_vel": {"type": "number"},
            "unit": {"type": "string"},
        },
    },
    "crane727r.Signals": {
        "type": "object",
        "additionalProperties": {"type": "boolean"},
    },
    "crane727r.Heartbeat": {
        "type": "object",
        "properties": {
            "heart_beat": {"type": "integer"},
            "source_ip": {"type": "string"},
        },
    },
    "crane727r.Connection": {
        "type": "object",
        "properties": {
            "state": {"type": "string"},
            "message": {"type": "string"},
            "can_collect": {"type": "boolean"},
            "can_record_mcap": {"type": "boolean"},
        },
    },
}


@dataclass(frozen=True)
class SceneConfig:
    frame_id: str
    trolley_fixed_height_m: float
    ground_size_m: float
    marker_size_m: float
    ground_alpha: float


def scene_config_from_env() -> SceneConfig:
    return SceneConfig(
        frame_id=os.getenv("SCENE_FRAME_ID", "ground"),
        trolley_fixed_height_m=float(os.getenv("SCENE_TROLLEY_FIXED_HEIGHT_M", "30")),
        ground_size_m=float(os.getenv("SCENE_GROUND_SIZE_M", "80")),
        marker_size_m=float(os.getenv("SCENE_MARKER_SIZE_M", "2.0")),
        ground_alpha=float(os.getenv("SCENE_GROUND_ALPHA", "0.08")),
    )


_SCENE_CONFIG = scene_config_from_env()


def schema_bytes(name: str) -> bytes:
    return json.dumps(JSON_SCHEMAS[name], ensure_ascii=False).encode("utf-8")


def scale_mm_to_m(value: int) -> float:
    return value / 1000.0


def spreader_size(packet: PlcPacket) -> tuple[float, float, float]:
    if packet.smh.spr_sp45:
        return 12.192, 2.438, 0.55
    if packet.smh.spr_sp40:
        return 12.192, 2.438, 0.45
    if packet.smh.spr_sp20:
        return 6.058, 2.438, 0.35
    return 6.058, 2.438, 0.35


def _new_entity(stamp_ns: int, entity_id: str, frame_id: str) -> SceneEntity:
    entity = SceneEntity()
    entity.timestamp.FromNanoseconds(stamp_ns)
    entity.frame_id = frame_id
    entity.id = entity_id
    entity.frame_locked = True
    return entity


def _set_color(
    color_obj: Any,
    r: float,
    g: float,
    b: float,
    a: float,
) -> None:
    color_obj.r = r
    color_obj.g = g
    color_obj.b = b
    color_obj.a = a


def _add_cube(
    entity: SceneEntity,
    x: float,
    y: float,
    z: float,
    sx: float,
    sy: float,
    sz: float,
    r: float,
    g: float,
    b: float,
    a: float,
) -> None:
    cube = CubePrimitive()
    cube.pose.position.x = x
    cube.pose.position.y = y
    cube.pose.position.z = z
    cube.pose.orientation.w = 1.0
    cube.size.x = sx
    cube.size.y = sy
    cube.size.z = sz
    _set_color(cube.color, r, g, b, a)
    entity.cubes.append(cube)


def _add_line_strip(
    entity: SceneEntity,
    points: list[tuple[float, float, float]],
    thickness: float,
    r: float,
    g: float,
    b: float,
    a: float,
) -> None:
    line = LinePrimitive()
    line.type = LinePrimitive.LINE_STRIP
    line.thickness = thickness
    line.scale_invariant = False
    for x, y, z in points:
        point = Point3()
        point.x = x
        point.y = y
        point.z = z
        line.points.append(point)
    _set_color(line.color, r, g, b, a)
    entity.lines.append(line)


def _add_horizontal_plane(
    entity: SceneEntity,
    center_x: float,
    center_y: float,
    z: float,
    size_x: float,
    size_y: float,
    r: float,
    g: float,
    b: float,
    a: float,
) -> None:
    half_x = size_x / 2.0
    half_y = size_y / 2.0
    corners = [
        (center_x - half_x, center_y - half_y, z),
        (center_x + half_x, center_y - half_y, z),
        (center_x + half_x, center_y + half_y, z),
        (center_x - half_x, center_y + half_y, z),
    ]
    triangles = TriangleListPrimitive()
    triangles.pose.orientation.w = 1.0
    for x, y, corner_z in corners:
        point = Point3()
        point.x = x
        point.y = y
        point.z = corner_z
        triangles.points.append(point)
    triangles.indices.extend([0, 1, 2, 0, 2, 3])
    _set_color(triangles.color, r, g, b, a)
    entity.triangles.append(triangles)


def _add_label(
    entity: SceneEntity,
    text: str,
    x: float,
    y: float,
    z: float,
    r: float,
    g: float,
    b: float,
    a: float,
    font_size: float = 1.2,
) -> None:
    label = TextPrimitive()
    label.pose.position.x = x
    label.pose.position.y = y
    label.pose.position.z = z
    label.pose.orientation.w = 1.0
    label.billboard = True
    label.font_size = font_size
    label.scale_invariant = True
    label.text = text
    _set_color(label.color, r, g, b, a)
    entity.texts.append(label)


def _build_ground_reference(
    stamp_ns: int,
    mc: float,
    mt: float,
    fixed_height: float,
    config: SceneConfig,
) -> SceneEntity:
    entity = _new_entity(stamp_ns, "ground_reference", config.frame_id)
    half = config.ground_size_m / 2.0

    _add_horizontal_plane(
        entity,
        mc,
        mt,
        0.0,
        config.ground_size_m,
        config.ground_size_m,
        0.72,
        0.78,
        0.82,
        config.ground_alpha,
    )

    grid_step = 20.0
    grid_lines: list[tuple[float, float, float]] = []
    start_x = mc - half
    end_x = mc + half
    start_y = mt - half
    end_y = mt + half
    x = start_x
    while x <= end_x + 1e-6:
        grid_lines.extend([(x, start_y, 0.01), (x, end_y, 0.01)])
        x += grid_step
    y = start_y
    while y <= end_y + 1e-6:
        grid_lines.extend([(start_x, y, 0.01), (end_x, y, 0.01)])
        y += grid_step
    for i in range(0, len(grid_lines), 2):
        _add_line_strip(entity, [grid_lines[i], grid_lines[i + 1]], 0.02, 0.55, 0.58, 0.62, 0.12)

    axis_len = 4.0
    _add_line_strip(
        entity,
        [(mc, mt, 0.02), (mc + axis_len, mt, 0.02)],
        0.03,
        0.75,
        0.45,
        0.45,
        0.35,
    )
    _add_line_strip(
        entity,
        [(mc, mt, 0.02), (mc, mt + axis_len, 0.02)],
        0.03,
        0.45,
        0.72,
        0.48,
        0.35,
    )
    _add_line_strip(
        entity,
        [(mc, mt, 0.02), (mc, mt, axis_len)],
        0.03,
        0.45,
        0.58,
        0.82,
        0.35,
    )
    _add_label(entity, "X(MC)", mc + axis_len + 0.5, mt, 0.5, 0.7, 0.5, 0.5, 0.8, 0.9)
    _add_label(entity, "Y(MT)", mc, mt + axis_len + 0.5, 0.5, 0.5, 0.75, 0.5, 0.8, 0.9)
    _add_label(entity, "Z", mc, mt, axis_len + 0.5, 0.5, 0.6, 0.85, 0.8, 0.9)
    return entity


def build_scene_update(
    packet: PlcPacket,
    stamp_ns: int,
    config: SceneConfig | None = None,
) -> SceneUpdate:
    cfg = config or _SCENE_CONFIG
    mc = scale_mm_to_m(packet.mc_pos)
    mt = scale_mm_to_m(packet.mt_pos)
    mh = scale_mm_to_m(packet.mh_pos)
    cntrh = scale_mm_to_m(packet.cntrh_pos)
    fixed_height = cfg.trolley_fixed_height_m
    spreader_x, spreader_y, spreader_z = spreader_size(packet)
    marker = cfg.marker_size_m

    scene = SceneUpdate()
    entities: list[SceneEntity] = []

    entities.append(_build_ground_reference(stamp_ns, mc, mt, fixed_height, cfg))

    gantry = _new_entity(stamp_ns, "gantry_mc", cfg.frame_id)
    _add_cube(
        gantry,
        mc,
        0.0,
        marker / 2.0,
        marker,
        marker,
        marker,
        0.35,
        0.65,
        0.95,
        0.92,
    )
    _add_label(gantry, f"MC {packet.mc_pos}mm", mc, -marker, marker + 1.0, 0.4, 0.7, 0.95, 0.9)
    entities.append(gantry)

    trolley = _new_entity(stamp_ns, "trolley_mt", cfg.frame_id)
    _add_cube(
        trolley,
        mc,
        mt,
        fixed_height,
        marker * 0.85,
        marker * 0.85,
        marker * 0.85,
        0.2,
        0.9,
        0.35,
        0.95,
    )
    _add_line_strip(
        trolley,
        [(mc, 0.0, fixed_height), (mc, mt, fixed_height)],
        0.15,
        0.15,
        0.85,
        0.3,
        0.9,
    )
    _add_line_strip(
        trolley,
        [(mc, 0.0, 0.0), (mc, 0.0, fixed_height)],
        0.1,
        0.55,
        0.55,
        0.6,
        0.7,
    )
    _add_label(
        trolley,
        f"MT {packet.mt_pos}mm @ {fixed_height:.0f}m",
        mc,
        mt,
        fixed_height + marker,
        0.2,
        0.9,
        0.35,
        1.0,
    )
    entities.append(trolley)

    spreader = _new_entity(stamp_ns, "spreader_mh", cfg.frame_id)
    if packet.smh.mh_spr_lcked:
        spr_r, spr_g, spr_b, spr_a = 0.2, 0.9, 0.3, 0.95
    elif packet.smh.mh_spr_landed:
        spr_r, spr_g, spr_b, spr_a = 0.95, 0.75, 0.1, 0.9
    else:
        spr_r, spr_g, spr_b, spr_a = 0.2, 0.7, 1.0, 0.85
    spreader_z_center = max(mh, spreader_z / 2.0)
    _add_cube(
        spreader,
        mc,
        mt,
        spreader_z_center,
        spreader_x,
        spreader_y,
        spreader_z,
        spr_r,
        spr_g,
        spr_b,
        spr_a,
    )
    _add_line_strip(
        spreader,
        [(mc, mt, fixed_height), (mc, mt, mh)],
        0.18,
        0.25,
        0.55,
        0.95,
        0.95,
    )
    _add_label(
        spreader,
        f"MH {packet.mh_pos}mm",
        mc,
        mt,
        mh + spreader_z / 2.0 + 0.8,
        spr_r,
        spr_g,
        spr_b,
        1.0,
    )
    entities.append(spreader)

    container = _new_entity(stamp_ns, "container_height_cntrh", cfg.frame_id)
    plane_size_x = max(spreader_x + 1.0, 6.0)
    plane_size_y = max(spreader_y + 1.0, 3.0)
    cntrh_z = max(cntrh, 0.05)
    _add_horizontal_plane(
        container,
        mc,
        mt,
        cntrh_z,
        plane_size_x,
        plane_size_y,
        0.55,
        0.78,
        0.88,
        0.18,
    )
    corner_marker = 0.25
    for dx, dy in (
        (-plane_size_x / 2.0, -plane_size_y / 2.0),
        (plane_size_x / 2.0, -plane_size_y / 2.0),
        (plane_size_x / 2.0, plane_size_y / 2.0),
        (-plane_size_x / 2.0, plane_size_y / 2.0),
    ):
        _add_cube(
            container,
            mc + dx,
            mt + dy,
            cntrh_z + corner_marker / 2.0,
            corner_marker,
            corner_marker,
            corner_marker,
            0.45,
            0.72,
            0.82,
            0.65,
        )
    _add_label(
        container,
        f"CNTRH {packet.cntrh_pos}mm",
        mc,
        mt,
        cntrh_z + 0.8,
        0.5,
        0.75,
        0.85,
        0.85,
    )
    entities.append(container)

    scene.entities.extend(entities)
    return scene


def build_position_json(packet: PlcPacket) -> bytes:
    return json.dumps(
        {
            "mh_pos": packet.mh_pos,
            "mt_pos": packet.mt_pos,
            "mc_pos": packet.mc_pos,
            "cntrh_pos": packet.cntrh_pos,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def build_velocity_json(
    mh_vel: float,
    mt_vel: float,
    mc_vel: float,
    cntrh_vel: float,
) -> bytes:
    return json.dumps(
        {
            "mh_vel": round(mh_vel, 2),
            "mt_vel": round(mt_vel, 2),
            "mc_vel": round(mc_vel, 2),
            "cntrh_vel": round(cntrh_vel, 2),
            "unit": "mm/s",
        },
        ensure_ascii=False,
    ).encode("utf-8")


def build_raw_json(packet: PlcPacket) -> bytes:
    return json.dumps(packet.to_dict(), ensure_ascii=False).encode("utf-8")


def build_signals_json(flags: dict[str, bool]) -> bytes:
    return json.dumps(flags, ensure_ascii=False).encode("utf-8")


def build_heartbeat_json(packet: PlcPacket) -> bytes:
    return json.dumps(
        {"heart_beat": packet.heart_beat, "source_ip": packet.source_ip},
        ensure_ascii=False,
    ).encode("utf-8")


def build_connection_json(health: dict[str, Any]) -> bytes:
    return json.dumps(health, ensure_ascii=False).encode("utf-8")


class VelocityTracker:
    def __init__(self) -> None:
        self._prev: PlcPacket | None = None

    def reset(self) -> None:
        self._prev = None

    def compute(self, packet: PlcPacket) -> tuple[float, float, float, float]:
        if self._prev is None:
            self._prev = packet
            return 0.0, 0.0, 0.0, 0.0

        dt = packet.received_at - self._prev.received_at
        if dt <= 0:
            return 0.0, 0.0, 0.0, 0.0

        mh_vel = (packet.mh_pos - self._prev.mh_pos) / dt
        mt_vel = (packet.mt_pos - self._prev.mt_pos) / dt
        mc_vel = (packet.mc_pos - self._prev.mc_pos) / dt
        cntrh_vel = (packet.cntrh_pos - self._prev.cntrh_pos) / dt
        self._prev = packet
        return mh_vel, mt_vel, mc_vel, cntrh_vel
