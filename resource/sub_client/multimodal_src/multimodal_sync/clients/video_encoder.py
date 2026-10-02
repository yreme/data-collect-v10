"""Encode frames to H.264/H.265 for Foxglove CompressedVideo."""

from __future__ import annotations

import fractions
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


@dataclass
class EncodedPacket:
    data: bytes
    keyframe: bool


class H264Encoder:
    """Per-camera encoder. Not thread-safe."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        fps: float = 25.0,
        gop: int = 25,
        preset: str = "ultrafast",
        tune: str = "zerolatency",
        codec: str = "libx264",
        bitrate: Optional[int] = None,
        pixel_format: str = "yuv420p",
    ) -> None:
        import av

        self.width = width - (width % 2)
        self.height = height - (height % 2)
        self._fmt = "h265" if codec in ("libx265", "hevc_nvenc") else "h264"
        self._cc = av.CodecContext.create(codec, "w")
        self._cc.width = self.width
        self._cc.height = self.height
        self._cc.pix_fmt = pixel_format
        self._cc.framerate = fractions.Fraction(max(1, int(round(fps))), 1)
        self._cc.time_base = fractions.Fraction(1, max(1, int(round(fps))))
        if bitrate:
            self._cc.bit_rate = int(bitrate)
        gop = max(1, int(gop))
        opts = {
            "tune": tune,
            "preset": preset,
            "bf": "0",
        }
        if codec == "libx264":
            opts["x264-params"] = (
                f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0"
            )
        elif codec == "libx265":
            opts["x265-params"] = (
                f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0"
            )
        self._cc.options = opts
        self._av = av
        self._pts = 0

    @property
    def video_format(self) -> str:
        return self._fmt

    def encode_at_index(
        self,
        rgb: np.ndarray,
        pts_index: int,
        *,
        force_keyframe: bool = False,
    ) -> List[EncodedPacket]:
        out: List[EncodedPacket] = []
        h, w = rgb.shape[:2]
        if w != self.width or h != self.height:
            rgb = rgb[: self.height, : self.width]
        frame = self._av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        frame.pts = int(pts_index)
        if force_keyframe:
            frame.pict_type = self._av.video.frame.PictureType.I
        self._pts = max(self._pts, int(pts_index) + 1)
        for p in self._cc.encode(frame):
            out.append(EncodedPacket(data=bytes(p), keyframe=bool(p.is_keyframe)))
        return out

    def flush(self) -> List[EncodedPacket]:
        out: List[EncodedPacket] = []
        try:
            for p in self._cc.encode(None):
                out.append(EncodedPacket(data=bytes(p), keyframe=bool(p.is_keyframe)))
        except Exception:  # noqa: BLE001
            pass
        return out

    def close(self) -> None:
        try:
            self._cc.close()
        except Exception:  # noqa: BLE001
            pass


def decode_jpeg_to_rgb(data: bytes) -> Optional[np.ndarray]:
    try:
        import cv2

        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return None
        return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    except Exception:  # noqa: BLE001
        pass
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(data)).convert("RGB")
        return np.asarray(im)
    except Exception:  # noqa: BLE001
        return None


def decode_bgr8_to_rgb(data: bytes, width: int, height: int) -> Optional[np.ndarray]:
    if width <= 0 or height <= 0:
        return None
    expected = width * height * 3
    if len(data) < expected:
        return None
    arr = np.frombuffer(data[:expected], dtype=np.uint8).reshape(height, width, 3)
    try:
        import cv2

        return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    except Exception:  # noqa: BLE001
        return arr[:, :, ::-1].copy()


def decode_camera_to_rgb(
    data: bytes,
    encoding: str,
    *,
    width: int = 0,
    height: int = 0,
) -> Optional[np.ndarray]:
    enc = (encoding or "").strip().lower()
    if enc in {"jpeg", "jpg"}:
        return decode_jpeg_to_rgb(data)
    if enc == "bgr8":
        return decode_bgr8_to_rgb(data, width, height)
    if enc == "mono8" and width > 0 and height > 0:
        expected = width * height
        if len(data) >= expected:
            gray = np.frombuffer(data[:expected], dtype=np.uint8).reshape(height, width)
            return np.stack([gray, gray, gray], axis=-1)
    return decode_jpeg_to_rgb(data)
