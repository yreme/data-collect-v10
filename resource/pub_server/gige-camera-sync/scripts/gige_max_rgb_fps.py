#!/usr/bin/env python3
"""探测单台 GigE 相机在当前网络条件下可持续传输的最大 RGB 帧率。

通过 MVS SDK 开流，以二分搜索找到「目标帧率 ≈ 实际收帧率」的上限。
判定标准：实际 fps >= 目标 fps × delivery_threshold（默认 95%）。

用法:
  python3 scripts/gige_max_rgb_fps.py --ip 192.168.1.250
  python3 scripts/gige_max_rgb_fps.py --ip 192.168.1.248 --local-ip 192.168.1.102
  python3 scripts/gige_max_rgb_fps.py --ip 192.168.1.250 --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from ctypes import byref, memset, sizeof
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gige_arv_util import read_camera_info
from gige_mvs_util import MVS_PYTHON, setup_mvs_env

DEFAULT_LOCAL_IP = "192.168.1.102"
DEFAULT_PIXEL_FORMAT = "RGB8Packed"


def _ip_to_u32(s: str) -> int:
    a = [int(x) for x in s.split(".")]
    return (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]


def ensure_cameras_available() -> None:
    try:
        out = subprocess.check_output(["pgrep", "-af", "MVS"], text=True)
    except subprocess.CalledProcessError:
        return
    for line in out.splitlines():
        if "/opt/MVS/bin/MVS" in line:
            print("检测到 MVS 客户端占用相机，正在结束进程...", file=sys.stderr)
            subprocess.run(["pkill", "-f", "/opt/MVS/bin/MVS"], check=False)
            time.sleep(2)
            return


@dataclass
class FpsProbeResult:
    target_fps: float
    actual_fps: float
    frames: int
    duration_sec: float
    width: int
    height: int
    pixel_format: str


@dataclass
class MaxFpsResult:
    ip: str
    local_ip: str
    pixel_format: str
    link_speed_mbps: int
    width: int
    height: int
    max_rgb_fps: float
    saturated_at_fps: float
    probes: list[FpsProbeResult]


def measure_fps(
    ip: str,
    local_ip: str,
    target_fps: float,
    pixel_format: str,
    warmup_sec: float,
    measure_sec: float,
) -> FpsProbeResult:
    setup_mvs_env()
    from MvImport.MvCameraControl_class import (
        MV_ACCESS_Exclusive,
        MV_CC_DEVICE_INFO,
        MV_FRAME_OUT,
        MV_GIGE_DEVICE,
        MV_GIGE_DEVICE_INFO,
        MVCC_INTVALUE,
        MvCamera,
    )

    MvCamera.MV_CC_Initialize()
    g = MV_GIGE_DEVICE_INFO()
    g.nCurrentIp = _ip_to_u32(ip)
    g.nNetExport = _ip_to_u32(local_ip)
    dev = MV_CC_DEVICE_INFO()
    dev.nTLayerType = MV_GIGE_DEVICE
    dev.SpecialInfo.stGigEInfo = g

    cam = MvCamera()
    rh = int(cam.MV_CC_CreateHandle(dev))
    if rh != 0:
        MvCamera.MV_CC_Finalize()
        raise RuntimeError(f"CreateHandle 0x{rh & 0xFFFFFFFF:08x}")

    ro = int(cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0))
    if ro != 0:
        cam.MV_CC_DestroyHandle()
        MvCamera.MV_CC_Finalize()
        hint = "相机被占用 (0x80000203)，请关闭 MVS 客户端" if ro == 0x80000203 else ""
        raise RuntimeError(f"OpenDevice 0x{ro & 0xFFFFFFFF:08x} {hint}")

    try:
        ps = cam.MV_CC_GetOptimalPacketSize()
        if int(ps) > 0:
            cam.MV_CC_SetIntValue("GevSCPSPacketSize", ps)

        cam.MV_CC_SetEnumValueByString("AcquisitionMode", "Continuous")
        cam.MV_CC_SetEnumValueByString("TriggerMode", "Off")
        cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
        cam.MV_CC_SetFloatValue("AcquisitionFrameRate", float(target_fps))
        cam.MV_CC_SetEnumValueByString("PixelFormat", pixel_format)

        st_w = MVCC_INTVALUE()
        st_h = MVCC_INTVALUE()
        memset(byref(st_w), 0, sizeof(st_w))
        memset(byref(st_h), 0, sizeof(st_h))
        cam.MV_CC_GetIntValue("Width", st_w)
        cam.MV_CC_GetIntValue("Height", st_h)
        width, height = int(st_w.nCurValue), int(st_h.nCurValue)

        if int(cam.MV_CC_StartGrabbing()) != 0:
            raise RuntimeError("StartGrabbing failed")

        st_out = MV_FRAME_OUT()
        memset(byref(st_out), 0, sizeof(st_out))
        time.sleep(warmup_sec)

        frames = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < measure_sec:
            if int(cam.MV_CC_GetImageBuffer(st_out, 200)) == 0 and st_out.pBufAddr:
                frames += 1
                cam.MV_CC_FreeImageBuffer(st_out)

        elapsed = time.monotonic() - t0
        actual = frames / elapsed if elapsed > 0 else 0.0
        return FpsProbeResult(
            target_fps=target_fps,
            actual_fps=actual,
            frames=frames,
            duration_sec=elapsed,
            width=width,
            height=height,
            pixel_format=pixel_format,
        )
    finally:
        try:
            cam.MV_CC_StopGrabbing()
        except Exception:
            pass
        try:
            cam.MV_CC_CloseDevice()
        except Exception:
            pass
        try:
            cam.MV_CC_DestroyHandle()
        except Exception:
            pass
        MvCamera.MV_CC_Finalize()


def find_max_rgb_fps(
    ip: str,
    local_ip: str,
    pixel_format: str = DEFAULT_PIXEL_FORMAT,
    fps_min: float = 1.0,
    fps_max: float = 120.0,
    delivery_threshold: float = 0.95,
    warmup_sec: float = 1.0,
    measure_sec: float = 3.0,
    tolerance: float = 1.0,
) -> MaxFpsResult:
    """二分搜索最大可持续 RGB 帧率。"""
    info = read_camera_info(ip)
    probes: list[FpsProbeResult] = []

    lo, hi = fps_min, fps_max
    best_target = fps_min
    best_probe: Optional[FpsProbeResult] = None
    saturated_at = fps_max

    while hi - lo > tolerance:
        mid = (lo + hi) / 2.0
        probe = measure_fps(ip, local_ip, mid, pixel_format, warmup_sec, measure_sec)
        probes.append(probe)
        ratio = probe.actual_fps / probe.target_fps if probe.target_fps > 0 else 0

        if ratio >= delivery_threshold:
            best_target = mid
            best_probe = probe
            lo = mid
        else:
            saturated_at = mid
            hi = mid

    if best_probe is None:
        best_probe = measure_fps(ip, local_ip, fps_min, pixel_format, warmup_sec, measure_sec)
        probes.append(best_probe)
        best_target = fps_min

    return MaxFpsResult(
        ip=ip,
        local_ip=local_ip,
        pixel_format=pixel_format,
        link_speed_mbps=info.link_speed_mbps,
        width=best_probe.width,
        height=best_probe.height,
        max_rgb_fps=round(best_probe.actual_fps, 2),
        saturated_at_fps=round(saturated_at, 2),
        probes=probes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 GigE 相机最大可持续 RGB 帧率")
    parser.add_argument("--ip", required=True, help="相机 IP 地址")
    parser.add_argument("--local-ip", default=DEFAULT_LOCAL_IP, help="本机网卡 IP（GigE 出口）")
    parser.add_argument("--pixel-format", default=DEFAULT_PIXEL_FORMAT,
                        choices=["RGB8Packed", "Bgr8"], help="RGB 像素格式")
    parser.add_argument("--fps-min", type=float, default=1.0)
    parser.add_argument("--fps-max", type=float, default=120.0)
    parser.add_argument("--delivery-threshold", type=float, default=0.95,
                        help="交付率阈值：actual/target >= 此值视为可达")
    parser.add_argument("--warmup", type=float, default=1.0, help="每次探测预热秒数")
    parser.add_argument("--measure", type=float, default=3.0, help="每次探测测量秒数")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    setup_mvs_env()
    if not Path(MVS_PYTHON).is_dir():
        print(f"MVS SDK 未找到: {MVS_PYTHON}", file=sys.stderr)
        return 1

    ensure_cameras_available()
    disp = os.environ.get("DISPLAY", "")
    if disp:
        print(f"MVS 显示环境: DISPLAY={disp}", file=sys.stderr)

    print(f"正在探测 {args.ip} 的最大 RGB 帧率 ({args.pixel_format})...", file=sys.stderr)
    try:
        result = find_max_rgb_fps(
            ip=args.ip,
            local_ip=args.local_ip,
            pixel_format=args.pixel_format,
            fps_min=args.fps_min,
            fps_max=args.fps_max,
            delivery_threshold=args.delivery_threshold,
            warmup_sec=args.warmup,
            measure_sec=args.measure,
        )
    except Exception as exc:
        print(f"探测失败: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "ip": result.ip,
            "local_ip": result.local_ip,
            "pixel_format": result.pixel_format,
            "link_speed_mbps": result.link_speed_mbps,
            "resolution": f"{result.width}x{result.height}",
            "max_rgb_fps": result.max_rgb_fps,
            "saturated_at_fps": result.saturated_at_fps,
            "probes": [
                {
                    "target_fps": p.target_fps,
                    "actual_fps": round(p.actual_fps, 2),
                    "frames": p.frames,
                    "duration_sec": round(p.duration_sec, 2),
                }
                for p in result.probes
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    link_note = f"{result.link_speed_mbps} Mbps"
    if result.link_speed_mbps and result.link_speed_mbps < 1000:
        link_note += " (百兆协商 ⚠)"

    print()
    print(f"相机 IP       : {result.ip}")
    print(f"本机出口 IP   : {result.local_ip}")
    print(f"链路协商速率  : {link_note}")
    print(f"分辨率        : {result.width} x {result.height}")
    print(f"像素格式      : {result.pixel_format}")
    print(f"最大 RGB 帧率 : {result.max_rgb_fps:.2f} Hz")
    print(f"饱和起始帧率  : ~{result.saturated_at_fps:.1f} Hz (目标超过此值开始掉帧)")
    print()
    print("探测过程:")
    print(f"  {'目标fps':>8}  {'实际fps':>8}  {'交付率':>8}")
    for p in result.probes:
        ratio = p.actual_fps / p.target_fps * 100 if p.target_fps > 0 else 0
        mark = "✓" if ratio >= args.delivery_threshold * 100 else "✗"
        print(f"  {p.target_fps:>8.1f}  {p.actual_fps:>8.2f}  {ratio:>7.1f}% {mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
