#!/usr/bin/env python3
"""Replay camera/IMU/lidar messages from an MCAP file into mock-prefixed SHM segments.

Uses mock_* SHM prefixes by default so replay data does not collide with live sensors.
Also publishes synthetic PLC packets to a mock PLC SHM segment for end-to-end verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import av
import numpy as np
from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
from foxglove_schemas_protobuf.PointCloud_pb2 import PointCloud
from mcap.reader import make_reader

REPO_ROOT = Path(__file__).resolve().parents[2]
for sub in ("multimodal_src", "imu-sync", "lidar-sync", "client-mcap-plc-web-src"):
    p = REPO_ROOT / sub
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from imu_sync.imu_codec import ImuSample  # noqa: E402
from imu_sync.shm import SharedImuWriter  # noqa: E402
from lidar_sync.shm import SharedPointCloudWriter  # noqa: E402
from multimodal_common.shm.image_ring import SharedImageWriter  # noqa: E402
from plc.shm.plc_ring import SharedPlcWriter, pack_plc_dict  # noqa: E402

LOG = logging.getLogger("mcap_mock_shm_replay")

CAMERA_TOPIC_RE = re.compile(r"^/gige/(?P<name>[^/]+)/image$")
IMU_TOPIC_RE = re.compile(r"^/imu/(?P<name>[^/]+)/imu$")
LIDAR_TOPIC_RE = re.compile(r"^/lidar/(?P<name>[^/]+)/points$")


@dataclass(frozen=True)
class CameraFrame:
    name: str
    log_ns: int
    rgb: np.ndarray


@dataclass(frozen=True)
class ImuFrame:
    name: str
    log_ns: int
    sample: ImuSample


@dataclass(frozen=True)
class LidarFrame:
    name: str
    log_ns: int
    data: bytes
    point_count: int


class _H264StreamDecoder:
    def __init__(self) -> None:
        self._codec = av.CodecContext.create("h264", "r")

    def decode_frame(self, packets: list[bytes]) -> np.ndarray | None:
        last: np.ndarray | None = None
        for blob in packets:
            try:
                for decoded in self._codec.decode(av.Packet(blob)):
                    last = decoded.to_ndarray(format="rgb24")
            except (av.error.FFmpegError, OSError, ValueError):
                continue
        return last


def _camera_topics(summary) -> list[str]:
    topics: list[str] = []
    for ch in summary.channels.values():
        match = CAMERA_TOPIC_RE.match(ch.topic)
        if not match:
            continue
        schema = summary.schemas.get(ch.schema_id)
        if schema and "CompressedVideo" in schema.name:
            topics.append(ch.topic)
    return sorted(topics)


def _imu_json_to_sample(msg: dict[str, Any], log_ns: int) -> ImuSample:
    ori = msg.get("orientation") or {}
    gyro = msg.get("angular_velocity") or {}
    acc = msg.get("linear_acceleration") or {}
    return ImuSample(
        tid=int(log_ns // 1_000_000) & 0xFFFF,
        smp_timestamp=int(log_ns // 1_000_000) & 0xFFFFFFFF,
        ready_timestamp=int(log_ns // 1_000_000) & 0xFFFFFFFF,
        acc_x=float(acc.get("x", 0.0)),
        acc_y=float(acc.get("y", 0.0)),
        acc_z=float(acc.get("z", 9.81)),
        gyro_x=math.degrees(float(gyro.get("x", 0.0))),
        gyro_y=math.degrees(float(gyro.get("y", 0.0))),
        gyro_z=math.degrees(float(gyro.get("z", 0.0))),
        q0=float(ori.get("w", 1.0)),
        q1=float(ori.get("x", 0.0)),
        q2=float(ori.get("y", 0.0)),
        q3=float(ori.get("z", 0.0)),
        sensor_temp=25.0,
        status=1,
    )


def load_replay_frames(
    mcap_path: Path,
    *,
    max_frames_per_sensor: int | None = 120,
) -> tuple[list[CameraFrame], list[ImuFrame], list[LidarFrame]]:
    cameras: list[CameraFrame] = []
    imus: list[ImuFrame] = []
    lidars: list[LidarFrame] = []

    with mcap_path.open("rb") as fh:
        reader = make_reader(fh)
        summary = reader.get_summary()
        if summary is None:
            raise RuntimeError(f"MCAP summary missing: {mcap_path}")

        for topic in _camera_topics(summary):
            match = CAMERA_TOPIC_RE.match(topic)
            if not match:
                continue
            name = match.group("name")
            grouped: dict[int, list[bytes]] = defaultdict(list)
            count = 0
            with mcap_path.open("rb") as fh2:
                r2 = make_reader(fh2)
                for _schema, _channel, message in r2.iter_messages(topics=[topic]):
                    cv = CompressedVideo()
                    cv.ParseFromString(message.data)
                    grouped[message.log_time].append(bytes(cv.data))
                    if max_frames_per_sensor is not None and len(grouped) >= max_frames_per_sensor:
                        break
            decoder = _H264StreamDecoder()
            for log_ns in sorted(grouped):
                rgb = decoder.decode_frame(grouped[log_ns])
                if rgb is None:
                    continue
                cameras.append(CameraFrame(name=name, log_ns=log_ns, rgb=rgb))
                count += 1
                if max_frames_per_sensor is not None and count >= max_frames_per_sensor:
                    break
            LOG.info("Loaded %d camera frames for %s", count, name)

        with mcap_path.open("rb") as fh2:
            r2 = make_reader(fh2)
            imu_counts: dict[str, int] = defaultdict(int)
            lidar_counts: dict[str, int] = defaultdict(int)
            for _schema, channel, message in r2.iter_messages():
                imu_match = IMU_TOPIC_RE.match(channel.topic)
                if imu_match and channel.message_encoding == "json":
                    name = imu_match.group("name")
                    if max_frames_per_sensor is not None and imu_counts[name] >= max_frames_per_sensor:
                        continue
                    payload = json.loads(message.data.decode("utf-8"))
                    imus.append(
                        ImuFrame(
                            name=name,
                            log_ns=message.log_time,
                            sample=_imu_json_to_sample(payload, message.log_time),
                        )
                    )
                    imu_counts[name] += 1
                    continue
                lidar_match = LIDAR_TOPIC_RE.match(channel.topic)
                if lidar_match:
                    name = lidar_match.group("name")
                    if max_frames_per_sensor is not None and lidar_counts[name] >= max_frames_per_sensor:
                        continue
                    pc = PointCloud()
                    pc.ParseFromString(message.data)
                    if not pc.data:
                        continue
                    point_count = len(pc.data) // max(pc.point_stride, 16)
                    lidars.append(
                        LidarFrame(
                            name=name,
                            log_ns=message.log_time,
                            data=bytes(pc.data),
                            point_count=point_count,
                        )
                    )
                    lidar_counts[name] += 1

    for name in sorted({f.name for f in imus}):
        LOG.info("Loaded %d IMU frames for %s", sum(1 for f in imus if f.name == name), name)
    for name in sorted({f.name for f in lidars}):
        LOG.info("Loaded %d lidar frames for %s", sum(1 for f in lidars if f.name == name), name)

    return cameras, imus, lidars


@dataclass
class MockWriters:
    cameras: dict[str, SharedImageWriter]
    imus: dict[str, SharedImuWriter]
    lidars: dict[str, SharedPointCloudWriter]
    plc: SharedPlcWriter

    def close(self) -> None:
        for writer in (*self.cameras.values(), *self.imus.values(), *self.lidars.values()):
            writer.close()
        self.plc.close()


def _rgb_to_bgr_bytes(rgb: np.ndarray) -> tuple[bytes, int, int]:
    h, w = rgb.shape[:2]
    try:
        import cv2

        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        return bgr.tobytes(), w, h
    except Exception:  # noqa: BLE001
        bgr = rgb[:, :, ::-1]
        return bgr.tobytes(), w, h


def open_writers(
    camera_names: set[str],
    imu_names: set[str],
    lidar_names: set[str],
    *,
    camera_prefix: str,
    imu_prefix: str,
    lidar_prefix: str,
    plc_segment: str,
) -> MockWriters:
    cameras: dict[str, SharedImageWriter] = {}
    for name in sorted(camera_names):
        segment = f"{camera_prefix}{name}"
        cameras[name] = SharedImageWriter(
            segment,
            slot_count=8,
            slot_capacity=8 * 1024 * 1024,
            create=True,
        )
        LOG.info("SHM camera writer: %s", segment)

    imus: dict[str, SharedImuWriter] = {}
    for name in sorted(imu_names):
        segment = f"{imu_prefix}{name}"
        imus[name] = SharedImuWriter(segment, slot_count=16, slot_capacity=4096, create=True)
        LOG.info("SHM IMU writer: %s", segment)

    lidars: dict[str, SharedPointCloudWriter] = {}
    for name in sorted(lidar_names):
        segment = f"{lidar_prefix}{name}"
        lidars[name] = SharedPointCloudWriter(
            segment,
            slot_count=8,
            slot_capacity=16 * 1024 * 1024,
            create=True,
        )
        LOG.info("SHM lidar writer: %s", segment)

    plc = SharedPlcWriter(plc_segment, create=True)
    LOG.info("SHM PLC writer: %s", plc_segment)
    return MockWriters(cameras=cameras, imus=imus, lidars=lidars, plc=plc)


def _mock_plc_payload(tick: int) -> dict[str, Any]:
    return {
        "mh_pos": tick * 10,
        "mt_pos": (tick * 7) % 1000,
        "mc_pos": tick % 100,
        "cntrh_pos": tick % 500,
        "heart_beat": tick % 256,
        "smh": {"spr_sp20": bool(tick % 2), "spr_sp40": False, "hoist_up": bool(tick % 5)},
        "oicr": {"crane_left": bool(tick % 3), "crane_right": False},
        "received_at": time.time(),
        "source_ip": "127.0.0.1",
        "source_port": 12730,
    }


def replay_loop(
    writers: MockWriters,
    cameras: list[CameraFrame],
    imus: list[ImuFrame],
    lidars: list[LidarFrame],
    *,
    hz: float,
    loop: bool,
    plc_every: int,
) -> None:
    cam_buckets: dict[str, list[CameraFrame]] = defaultdict(list)
    imu_buckets: dict[str, list[ImuFrame]] = defaultdict(list)
    lidar_buckets: dict[str, list[LidarFrame]] = defaultdict(list)
    for frame in cameras:
        cam_buckets[frame.name].append(frame)
    for frame in imus:
        imu_buckets[frame.name].append(frame)
    for frame in lidars:
        lidar_buckets[frame.name].append(frame)

    if not cam_buckets and not imu_buckets and not lidar_buckets:
        raise RuntimeError("No replay events loaded from MCAP")

    cam_idx = {name: 0 for name in cam_buckets}
    imu_idx = {name: 0 for name in imu_buckets}
    lidar_idx = {name: 0 for name in lidar_buckets}
    period_s = 1.0 / max(hz, 0.1)
    tick = 0
    plc_tick = 0

    LOG.info(
        "Replay start: cameras=%s imus=%s lidars=%s, %.2f Hz, loop=%s",
        {k: len(v) for k, v in cam_buckets.items()},
        {k: len(v) for k, v in imu_buckets.items()},
        {k: len(v) for k, v in lidar_buckets.items()},
        hz,
        loop,
    )

    while True:
        now_ms = int(time.time() * 1000)
        for name, frames in cam_buckets.items():
            frame = frames[cam_idx[name] % len(frames)]
            data, width, height = _rgb_to_bgr_bytes(frame.rgb)
            writers.cameras[name].publish(
                data,
                width=width,
                height=height,
                encoding="bgr8",
                trigger_ms=now_ms,
                recv_ms=now_ms,
                frame_num=tick,
            )
            cam_idx[name] += 1

        for name, frames in imu_buckets.items():
            imu_frame = frames[imu_idx[name] % len(frames)]
            writers.imus[name].publish(
                imu_frame.sample.pack(),
                tid=imu_frame.sample.tid,
                trigger_ms=now_ms,
                recv_ms=now_ms,
                frame_num=tick,
            )
            imu_idx[name] += 1

        for name, frames in lidar_buckets.items():
            lidar_frame = frames[lidar_idx[name] % len(frames)]
            writers.lidars[name].publish(
                lidar_frame.data,
                point_count=lidar_frame.point_count,
                width=lidar_frame.point_count,
                trigger_ms=now_ms,
                recv_ms=now_ms,
                frame_num=tick,
            )
            lidar_idx[name] += 1

        tick += 1
        if tick % plc_every == 0:
            plc_tick += 1
            payload_dict = _mock_plc_payload(plc_tick)
            writers.plc.publish(
                pack_plc_dict(payload_dict),
                trigger_ms=now_ms,
                recv_ms=now_ms,
                heart_beat=payload_dict["heart_beat"],
            )

        if not loop and tick >= max(
            max((len(v) for v in cam_buckets.values()), default=1),
            max((len(v) for v in imu_buckets.values()), default=1),
            max((len(v) for v in lidar_buckets.values()), default=1),
        ):
            break
        time.sleep(period_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mcap",
        type=Path,
        help="Source MCAP with 6 cameras + IMU + lidar topics",
    )
    parser.add_argument("--hz", type=float, default=2.0, help="Replay speed factor (default: 2 Hz)")
    parser.add_argument("--loop", action="store_true", help="Loop replay continuously")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=120,
        help="Max frames per sensor to load (default: 120)",
    )
    parser.add_argument("--camera-prefix", default=os.getenv("MOCK_SHM_CAMERA_PREFIX", "mock_gige_"))
    parser.add_argument("--imu-prefix", default=os.getenv("MOCK_SHM_IMU_PREFIX", "mock_imu_"))
    parser.add_argument("--lidar-prefix", default=os.getenv("MOCK_SHM_LIDAR_PREFIX", "mock_lidar_"))
    parser.add_argument(
        "--plc-segment",
        default=os.getenv("MOCK_PLC_SHM_SEGMENT", "mock_plc_crane727r"),
        help="Mock PLC SHM segment name",
    )
    parser.add_argument("--plc-every", type=int, default=1, help="Publish mock PLC every N replay events")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if not args.mcap.is_file():
        LOG.error("MCAP not found: %s", args.mcap)
        return 1

    cameras, imus, lidars = load_replay_frames(
        args.mcap,
        max_frames_per_sensor=args.max_frames if args.max_frames > 0 else None,
    )
    writers = open_writers(
        {f.name for f in cameras},
        {f.name for f in imus},
        {f.name for f in lidars},
        camera_prefix=args.camera_prefix,
        imu_prefix=args.imu_prefix,
        lidar_prefix=args.lidar_prefix,
        plc_segment=args.plc_segment,
    )
    try:
        replay_loop(
            writers,
            cameras,
            imus,
            lidars,
            hz=args.hz,
            loop=args.loop,
            plc_every=max(1, args.plc_every),
        )
    except KeyboardInterrupt:
        LOG.info("Replay stopped")
    finally:
        writers.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
