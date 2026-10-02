#!/usr/bin/env python3
"""多路 GigE 相机聚合带宽测试 — 自动发现相机，阶梯验证主机上行是否达到目标 Gbps。

原理：
  1. 自动扫描网段内 GigE 相机（arv-tool），无需写死 IP
  2. 同时打开所有相机，自由运行（Continuous + FrameRateEnable）
  3. 监控网卡 RX 字节增量 + 各相机实际收帧数
  4. 按 1G→2G→…→10G 阈值逐级提升帧率，验证是否达标
  5. 达到带宽极限（掉帧/饱和）后自动停止，不再测更高档位

用法:
  export MVCAM_COMMON_RUNENV=/opt/MVS/lib
  export LD_LIBRARY_PATH=/opt/MVS/lib/64:$LD_LIBRARY_PATH
  python3 scripts/bandwidth_test.py --iface enp5s0
  python3 scripts/bandwidth_test.py --iface enp5s0 --subnet 192.168.1.0/24
  python3 scripts/bandwidth_test.py --iface enp5s0 --duration 8 --fps 15 30 60 90
  python3 scripts/bandwidth_test.py --iface enp5s0 --thresholds 2 3 4 5
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import byref, memset, sizeof
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gige_arv_util import (
    enrich_camera,
    filter_subnet,
    discover_devices,
    find_arv_tool,
    read_camera_info,
)
from gige_mvs_util import MVS_PYTHON, setup_mvs_env

DEFAULT_LOCAL_IP = "192.168.1.102"
DEFAULT_IFACE = "enp5s0"
DEFAULT_SUBNET = "192.168.1.0/24"

# 运行时由 discover_cameras() 填充
CAMERAS: List[dict] = []

_sdk_lock = threading.Lock()
_sdk_refcount = 0

BYTES_PER_PIXEL = {
    "BayerGB8": 1,
    "RGB8Packed": 3,
    "Bgr8": 3,
}


def _ip_to_u32(s: str) -> int:
    a = [int(x) for x in s.split(".")]
    return (a[0] << 24) | (a[1] << 16) | (a[2] << 8) | a[3]


def read_rx_bytes(iface: str) -> int:
    path = Path(f"/sys/class/net/{iface}/statistics/rx_bytes")
    return int(path.read_text().strip())


def read_link_speed_mbps(iface: str) -> int:
    path = Path(f"/sys/class/net/{iface}/speed")
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def read_camera_link_speed_mbps(ip: str) -> int:
    """通过 arv-tool 读取相机 GevLinkSpeed（100/1000 Mbps）。"""
    arv = shutil.which("arv-tool-0.8")
    if not arv:
        info = read_camera_info(ip)
        return info.link_speed_mbps
    try:
        out = subprocess.check_output(
            [arv, "-a", ip, "control", "GevLinkSpeed"],
            text=True,
            timeout=5,
            stderr=subprocess.STDOUT,
        )
        m = re.search(r"GevLinkSpeed\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    except (subprocess.SubprocessError, ValueError):
        return 0


def discover_cameras(subnet: str) -> List[dict]:
    """扫描网段内 GigE 相机，返回 [{name, ip, serial, model, link_speed_mbps}, ...]。"""
    find_arv_tool()
    devices = filter_subnet(discover_devices(), subnet)
    if not devices:
        raise RuntimeError(f"未在 {subnet} 内发现 GigE 相机")

    specs: List[dict] = []
    for i, (name, ip) in enumerate(sorted(devices, key=lambda x: x[1])):
        info = enrich_camera(name, ip)
        specs.append({
            "name": f"cam{i}",
            "ip": ip,
            "serial": info.serial or "",
            "model": info.model or name,
            "link_speed_mbps": info.link_speed_mbps,
            "width": info.width,
            "height": info.height,
        })
    return specs


def estimate_bytes_per_frame(width: int, height: int, pixel_format: str) -> int:
    bpp = BYTES_PER_PIXEL.get(pixel_format, 1)
    if width > 0 and height > 0:
        return width * height * bpp
    # MV-CU013-A0GC 默认分辨率
    return 1280 * 1024 * bpp


def estimate_fps_for_threshold(
    threshold_mbps: float,
    num_cameras: int,
    bytes_per_frame: int,
    margin: float = 1.08,
) -> float:
    """根据目标聚合带宽估算每路相机所需帧率。"""
    if num_cameras <= 0 or bytes_per_frame <= 0:
        return 15.0
    fps = threshold_mbps * 1e6 / (num_cameras * bytes_per_frame * 8)
    return max(1.0, min(120.0, fps * margin))


def max_achievable_mbps(host_link_mbps: int, cameras: List[dict]) -> float:
    """理论聚合上限 = min(主机链路, 各相机链路之和)。"""
    cam_sum = sum(c.get("link_speed_mbps", 1000) or 1000 for c in cameras)
    host = host_link_mbps if host_link_mbps > 0 else 10000
    return float(min(host, cam_sum))


def build_threshold_plan(
    thresholds_gbps: List[int],
    host_link_mbps: int,
    cameras: List[dict],
    bytes_per_frame: int,
) -> List[tuple[int, float]]:
    """返回 [(threshold_mbps, estimated_fps), ...]，跳过物理上不可能达到的档位。"""
    cap = max_achievable_mbps(host_link_mbps, cameras)
    plan: List[tuple[int, float]] = []
    for g in sorted(thresholds_gbps):
        mbps = g * 1000
        if mbps > cap * 1.05:
            continue
        fps = estimate_fps_for_threshold(mbps, len(cameras), bytes_per_frame)
        plan.append((mbps, fps))
    return plan


def ensure_cameras_available() -> None:
    """若 MVS 客户端占用相机，提示并尝试结束进程。"""
    try:
        out = subprocess.check_output(["pgrep", "-af", "MVS"], text=True)
    except subprocess.CalledProcessError:
        return
    for line in out.splitlines():
        if "/opt/MVS/bin/MVS" in line:
            print("检测到 MVS 客户端占用相机，正在结束进程以释放设备...", file=sys.stderr)
            subprocess.run(["pkill", "-f", "/opt/MVS/bin/MVS"], check=False)
            time.sleep(2)
            return


def sdk_initialize(mv_cls) -> None:
    global _sdk_refcount
    with _sdk_lock:
        if _sdk_refcount == 0:
            mv_cls.MV_CC_Initialize()
        _sdk_refcount += 1


def sdk_finalize(mv_cls) -> None:
    global _sdk_refcount
    with _sdk_lock:
        _sdk_refcount = max(0, _sdk_refcount - 1)
        if _sdk_refcount == 0:
            try:
                mv_cls.MV_CC_Finalize()
            except Exception:
                pass


@dataclass
class CamStats:
    name: str
    ip: str
    frames: int = 0
    bytes_raw: int = 0
    errors: int = 0
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    link_speed_mbps: int = 0
    opened: bool = False


@dataclass
class TestResult:
    target_fps: float
    target_threshold_mbps: int
    duration_sec: float
    rx_mbps: float
    rx_gbps: float
    total_frames: int
    expected_frames: int
    frame_delivery_pct: float
    per_cam: Dict[str, CamStats] = field(default_factory=dict)
    saturated: bool = False
    threshold_passed: bool = False


class CameraWorker:
    """单相机采集线程：自由运行模式，统计收帧。"""

    def __init__(self, spec: dict, target_fps: float, local_ip: str, pixel_format: str = "BayerGB8") -> None:
        self.spec = spec
        self.target_fps = target_fps
        self.local_ip = local_ip
        self.pixel_format = pixel_format
        self.stats = CamStats(
            name=spec["name"],
            ip=spec["ip"],
            link_speed_mbps=spec.get("link_speed_mbps") or read_camera_link_speed_mbps(spec["ip"]),
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cam = None
        self._mv = None
        self._frame_out_cls = None
        self._sdk_held = False

    def start(self, delay: float = 0.0) -> None:
        self._start_delay = delay
        self._thread = threading.Thread(target=self._run, name=self.spec["name"], daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def snapshot_frames(self) -> int:
        return self.stats.frames

    def _run(self) -> None:
        delay = getattr(self, "_start_delay", 0.0)
        if delay > 0:
            time.sleep(delay)
        for attempt in range(3):
            try:
                self._open_and_grab()
                return
            except Exception as exc:
                print(f"[{self.spec['name']}] attempt {attempt+1} ERROR: {exc}", file=sys.stderr)
                self.stats.errors += 1
                self._cleanup()
                time.sleep(1.0 * (attempt + 1))

    def _open_and_grab(self) -> None:
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

        self._mv = MvCamera
        self._frame_out_cls = MV_FRAME_OUT
        sdk_initialize(MvCamera)
        self._sdk_held = True

        g = MV_GIGE_DEVICE_INFO()
        g.nCurrentIp = _ip_to_u32(self.spec["ip"])
        g.nNetExport = _ip_to_u32(self.local_ip)
        dev = MV_CC_DEVICE_INFO()
        dev.nTLayerType = MV_GIGE_DEVICE
        dev.SpecialInfo.stGigEInfo = g

        cam = MvCamera()
        self._cam = cam
        rh = int(cam.MV_CC_CreateHandle(dev))
        if rh != 0:
            raise RuntimeError(f"CreateHandle 0x{rh & 0xFFFFFFFF:08x}")

        ro = int(cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0))
        if ro != 0:
            hint = "occupied?" if ro == 0x80000203 else "see MvErrorDefine"
            raise RuntimeError(f"OpenDevice 0x{ro & 0xFFFFFFFF:08x} ({hint})")

        self.stats.opened = True
        ps = cam.MV_CC_GetOptimalPacketSize()
        if int(ps) > 0:
            cam.MV_CC_SetIntValue("GevSCPSPacketSize", ps)

        cam.MV_CC_SetEnumValueByString("AcquisitionMode", "Continuous")
        cam.MV_CC_SetEnumValueByString("TriggerMode", "Off")
        cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
        cam.MV_CC_SetFloatValue("AcquisitionFrameRate", float(self.target_fps))

        try:
            cam.MV_CC_SetEnumValueByString("PixelFormat", self.pixel_format)
        except Exception:
            pass

        st_w = MVCC_INTVALUE()
        st_h = MVCC_INTVALUE()
        memset(byref(st_w), 0, sizeof(st_w))
        memset(byref(st_h), 0, sizeof(st_h))
        cam.MV_CC_GetIntValue("Width", st_w)
        cam.MV_CC_GetIntValue("Height", st_h)
        self.stats.width = int(st_w.nCurValue)
        self.stats.height = int(st_h.nCurValue)
        self.stats.pixel_format = self.pixel_format

        if int(cam.MV_CC_StartGrabbing()) != 0:
            raise RuntimeError("StartGrabbing failed")

        self._ready.set()
        st_out = MV_FRAME_OUT()
        memset(byref(st_out), 0, sizeof(st_out))
        while not self._stop.is_set():
            gr = int(cam.MV_CC_GetImageBuffer(st_out, 200))
            if gr == 0 and st_out.pBufAddr:
                inf = st_out.stFrameInfo
                self.stats.frames += 1
                self.stats.bytes_raw += int(inf.nFrameLen)
                cam.MV_CC_FreeImageBuffer(st_out)
            elif gr != 0:
                self.stats.errors += 1

        self._cleanup()

    def _cleanup(self) -> None:
        cam = self._cam
        if cam is not None:
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
        self._cam = None
        if self._sdk_held and self._mv is not None:
            sdk_finalize(self._mv)
            self._sdk_held = False


def run_test(
    target_fps: float,
    duration: float,
    iface: str,
    local_ip: str,
    cameras: List[dict],
    pixel_format: str = "BayerGB8",
    threshold_mbps: int = 0,
    delivery_min_pct: float = 90.0,
) -> TestResult:
    workers = [CameraWorker(c, target_fps, local_ip, pixel_format) for c in cameras]

    for i, w in enumerate(workers):
        w.start(delay=i * 0.3)

    if not all(w.wait_ready(15.0) for w in workers):
        for w in workers:
            if not w.stats.opened:
                print(f"  ⚠ {w.spec['name']} 未能开流", file=sys.stderr)
        for w in workers:
            w.stop()
        raise RuntimeError("部分相机未能成功开流")

    time.sleep(2.0)

    frame0 = {w.spec["name"]: w.snapshot_frames() for w in workers}
    rx0 = read_rx_bytes(iface)
    t0 = time.monotonic()
    time.sleep(duration)
    t1 = time.monotonic()
    rx1 = read_rx_bytes(iface)
    frame1 = {w.spec["name"]: w.snapshot_frames() for w in workers}

    for w in workers:
        w.stop()

    elapsed = t1 - t0
    rx_bytes = rx1 - rx0
    rx_mbps = rx_bytes * 8 / elapsed / 1e6
    rx_gbps = rx_mbps / 1000

    total_frames = sum(frame1[n] - frame0[n] for n in frame0)
    expected = int(target_fps * elapsed * len(cameras))
    delivery = (total_frames / expected * 100) if expected > 0 else 0

    per_cam: Dict[str, CamStats] = {}
    for w in workers:
        st = CamStats(
            name=w.stats.name,
            ip=w.stats.ip,
            frames=frame1[w.spec["name"]] - frame0[w.spec["name"]],
            width=w.stats.width,
            height=w.stats.height,
            pixel_format=w.stats.pixel_format,
            errors=w.stats.errors,
            link_speed_mbps=w.stats.link_speed_mbps,
            opened=w.stats.opened,
        )
        per_cam[w.spec["name"]] = st

    saturated = delivery < delivery_min_pct
    threshold_passed = threshold_mbps > 0 and rx_mbps >= threshold_mbps and not saturated

    result = TestResult(
        target_fps=target_fps,
        target_threshold_mbps=threshold_mbps,
        duration_sec=elapsed,
        rx_mbps=rx_mbps,
        rx_gbps=rx_gbps,
        total_frames=total_frames,
        expected_frames=expected,
        frame_delivery_pct=delivery,
        per_cam=per_cam,
        saturated=saturated,
        threshold_passed=threshold_passed,
    )

    thresh_label = f" | 目标 >= {threshold_mbps/1000:.0f}Gbps" if threshold_mbps else ""
    print(f"\n{'='*70}")
    print(f"目标帧率: {target_fps:.1f} fps × {len(cameras)} 路 | 测量 {elapsed:.1f}s{thresh_label}")
    print(f"网卡 {iface} RX: {rx_mbps:.1f} Mbps ({rx_gbps:.3f} Gbps)")
    print(f"总收帧: {total_frames} / 期望 {expected} ({delivery:.1f}%)")
    for name, st in per_cam.items():
        cam_fps = st.frames / elapsed if elapsed > 0 else 0
        link_note = f"{st.link_speed_mbps}Mbps-link"
        warn = " ⚠百兆!" if st.link_speed_mbps and st.link_speed_mbps < 1000 else ""
        print(
            f"  {name} ({st.ip}): {st.frames} frames, {cam_fps:.1f} fps, "
            f"{st.width}x{st.height} {st.pixel_format}, {link_note}{warn}"
        )
    if threshold_mbps:
        mark = "✓ 通过" if threshold_passed else "✗ 未通过"
        print(f"阈值 {threshold_mbps/1000:.0f} Gbps: {mark}")
    if saturated:
        print(f"⚠ 检测到带宽/帧率饱和（交付率 < {delivery_min_pct:.0f}%）")
    return result


def print_discovered_cameras(cameras: List[dict]) -> None:
    print("\n已发现相机:")
    for c in cameras:
        link = c.get("link_speed_mbps", 0)
        res = ""
        if c.get("width") and c.get("height"):
            res = f" {c['width']}x{c['height']}"
        print(f"  [{c['name']}] {c.get('model', '')} ip={c['ip']} serial={c.get('serial', '')}{res} link={link}Mbps")


def print_link_audit(cameras: List[dict]) -> None:
    print("\n相机链路协商状态 (GevLinkSpeed):")
    slow = 0
    for c in cameras:
        speed = c.get("link_speed_mbps") or read_camera_link_speed_mbps(c["ip"])
        c["link_speed_mbps"] = speed
        status = "OK" if speed >= 1000 else "⚠ 百兆协商 — 需检查网线/交换机端口"
        if speed and speed < 1000:
            slow += 1
        print(f"  {c['name']} {c['ip']}: {speed} Mbps  [{status}]")
    if slow:
        print(
            f"\n⚠ {slow} 台相机仅协商到 100Mbps，四路聚合带宽上限约 "
            f"{slow * 80 + (len(cameras) - slow) * 800} Mbps，无法验证更高档位。"
        )


def run_adaptive_threshold_tests(
    iface: str,
    local_ip: str,
    cameras: List[dict],
    host_link_mbps: int,
    thresholds_gbps: List[int],
    duration: float,
    pixel_format: str,
    delivery_min_pct: float,
    cooldown_sec: float,
) -> tuple[List[TestResult], int, float]:
    """按阈值逐级测试，达标继续，饱和或失败则停止。"""
    width = cameras[0].get("width", 0) if cameras else 0
    height = cameras[0].get("height", 0) if cameras else 0
    bpf = estimate_bytes_per_frame(width, height, pixel_format)
    cap = max_achievable_mbps(host_link_mbps, cameras)

    plan = build_threshold_plan(thresholds_gbps, host_link_mbps, cameras, bpf)
    skipped = [g for g in thresholds_gbps if g * 1000 > cap * 1.05]

    print(f"\n理论聚合上限: {cap/1000:.2f} Gbps (主机 {host_link_mbps}Mbps, "
          f"{len(cameras)} 路相机)")
    print(f"单帧约 {bpf/1024:.1f} KiB ({pixel_format})")
    if plan:
        print("自适应测试计划:")
        for mbps, fps in plan:
            print(f"  >= {mbps/1000:.0f} Gbps → 约 {fps:.1f} fps/路")
    if skipped:
        print(f"跳过不可达档位: {', '.join(f'{g}G' for g in skipped)} "
              f"(超过相机聚合上限 {cap/1000:.2f}G)")

    results: List[TestResult] = []
    max_passed_gbps = 0
    peak_mbps = 0.0
    stop_reason = ""

    for mbps, fps in plan:
        print(f"\n>>> 验证 >= {mbps/1000:.0f} Gbps (目标 {fps:.1f} fps/路) ...")
        try:
            r = run_test(
                fps, duration, iface, local_ip, cameras, pixel_format,
                threshold_mbps=mbps, delivery_min_pct=delivery_min_pct,
            )
            results.append(r)
            peak_mbps = max(peak_mbps, r.rx_mbps)
        except Exception as exc:
            print(f"测试失败: {exc}", file=sys.stderr)
            stop_reason = f"开流失败: {exc}"
            break

        time.sleep(cooldown_sec)

        if r.threshold_passed:
            max_passed_gbps = mbps // 1000
            continue

        if r.saturated:
            stop_reason = f"带宽饱和 (交付率 {r.frame_delivery_pct:.1f}%)"
        else:
            stop_reason = f"实测 {r.rx_mbps:.0f} Mbps < 目标 {mbps} Mbps"
        print(f"\n已达极限，停止后续测试: {stop_reason}")
        break

    if not stop_reason and plan and results and results[-1].threshold_passed:
        stop_reason = "所有计划档位均已通过"

    return results, max_passed_gbps, peak_mbps


def run_manual_fps_tests(
    fps_list: List[float],
    iface: str,
    local_ip: str,
    cameras: List[dict],
    duration: float,
    pixel_format: str,
    thresholds_gbps: List[int],
    cooldown_sec: float,
) -> tuple[List[TestResult], float]:
    results: List[TestResult] = []
    peak_mbps = 0.0
    for fps in fps_list:
        print(f"\n>>> 开始测试 {fps} fps ...")
        try:
            r = run_test(fps, duration, iface, local_ip, cameras, pixel_format)
            results.append(r)
            peak_mbps = max(peak_mbps, r.rx_mbps)
        except Exception as exc:
            print(f"测试 {fps} fps 失败: {exc}", file=sys.stderr)
        time.sleep(cooldown_sec)
    return results, peak_mbps


def print_summary(
    results: List[TestResult],
    thresholds_gbps: List[int],
    peak_mbps: float,
    max_passed_gbps: int,
    host_link_mbps: int,
    iface: str,
    adaptive: bool,
) -> None:
    print("\n" + "=" * 70)
    print("汇总")
    hdr = f"{'目标fps':>8} {'RX Mbps':>10} {'RX Gbps':>8} {'交付率':>8}"
    for g in thresholds_gbps:
        hdr += f" {'>'+str(g)+'G':>5}"
    print(hdr)
    print("-" * 70)
    for r in results:
        row = (
            f"{r.target_fps:>8.1f} {r.rx_mbps:>10.1f} {r.rx_gbps:>8.3f} "
            f"{r.frame_delivery_pct:>7.1f}%"
        )
        for g in thresholds_gbps:
            row += f" {'✓' if r.rx_mbps >= g * 1000 else '✗':>5}"
        print(row)

    print("-" * 70)
    print(f"峰值 RX 带宽: {peak_mbps:.1f} Mbps ({peak_mbps/1000:.3f} Gbps)")
    for g in thresholds_gbps:
        ok = peak_mbps >= g * 1000
        if adaptive and g <= max_passed_gbps:
            ok = True
        elif adaptive and g > max_passed_gbps and peak_mbps < g * 1000:
            ok = False
        print(f"  验证 >= {g} Gbps: {'通过 ✓' if ok else '未通过 ✗'}")

    if adaptive:
        print(f"\n自适应验证最高通过档位: >= {max_passed_gbps} Gbps")
    if host_link_mbps >= 10000:
        print(f"\n主机网卡 {iface} 已协商 10Gbps — 上行物理层无瓶颈。")
    elif host_link_mbps >= 1000:
        print(f"\n主机网卡 {iface} 协商 {host_link_mbps} Mbps。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GigE 多路聚合带宽测试（自动发现相机 + 自适应阈值验证）",
    )
    parser.add_argument("--iface", default=DEFAULT_IFACE, help="监测 RX 的网卡名")
    parser.add_argument("--local-ip", default=DEFAULT_LOCAL_IP, help="本机 GigE 出口 IP")
    parser.add_argument(
        "--subnet", default=DEFAULT_SUBNET,
        help="自动发现相机的网段 (默认 192.168.1.0/24)",
    )
    parser.add_argument("--duration", type=float, default=8.0, help="每档测量秒数")
    parser.add_argument("--cooldown", type=float, default=3.0, help="每档间隔秒数")
    parser.add_argument(
        "--thresholds", type=int, nargs="+",
        default=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        help="要验证的 Gbps 阈值列表 (默认 1..10)",
    )
    parser.add_argument(
        "--fps", type=float, nargs="*",
        help="手动指定帧率阶梯；若省略则按阈值自动估算帧率",
    )
    parser.add_argument(
        "--pixel-format", default="BayerGB8",
        choices=["BayerGB8", "RGB8Packed", "Bgr8"],
        help="像素格式；BayerGB8 单帧更小、可测更高帧率",
    )
    parser.add_argument(
        "--delivery-min", type=float, default=90.0,
        help="帧交付率下限 %%，低于此视为饱和",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出最终结果")
    args = parser.parse_args()

    setup_mvs_env()
    if not Path(MVS_PYTHON).is_dir():
        print(f"MVS SDK 未找到: {MVS_PYTHON}", file=sys.stderr)
        return 1

    try:
        find_arv_tool()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    ensure_cameras_available()

    try:
        cameras = discover_cameras(args.subnet)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    global CAMERAS
    CAMERAS = cameras

    host_speed = read_link_speed_mbps(args.iface)
    thresholds = sorted(set(args.thresholds))

    print("=" * 70)
    print("GigE 多路聚合带宽测试（自动发现）")
    print(f"主机: {args.local_ip} | 网卡: {args.iface} ({host_speed} Mbps link)")
    print(f"网段: {args.subnet} | 像素格式: {args.pixel_format}")
    print_discovered_cameras(cameras)
    print_link_audit(cameras)
    print("=" * 70)

    rx0 = read_rx_bytes(args.iface)
    time.sleep(2)
    rx1 = read_rx_bytes(args.iface)
    baseline_mbps = (rx1 - rx0) * 8 / 2 / 1e6
    print(f"\n基线流量（无采集）: {baseline_mbps:.1f} Mbps")

    adaptive = not args.fps
    max_passed_gbps = 0

    if adaptive:
        results, max_passed_gbps, peak_mbps = run_adaptive_threshold_tests(
            args.iface, args.local_ip, cameras, host_speed,
            thresholds, args.duration, args.pixel_format,
            args.delivery_min, args.cooldown,
        )
    else:
        results, peak_mbps = run_manual_fps_tests(
            args.fps, args.iface, args.local_ip, cameras,
            args.duration, args.pixel_format, thresholds, args.cooldown,
        )
        for g in reversed(thresholds):
            if peak_mbps >= g * 1000:
                max_passed_gbps = g
                break

    if args.json:
        payload = {
            "iface": args.iface,
            "local_ip": args.local_ip,
            "subnet": args.subnet,
            "host_link_mbps": host_speed,
            "camera_count": len(cameras),
            "cameras": cameras,
            "pixel_format": args.pixel_format,
            "thresholds_gbps": thresholds,
            "max_passed_gbps": max_passed_gbps,
            "peak_rx_mbps": round(peak_mbps, 1),
            "adaptive": adaptive,
            "results": [
                {
                    "target_fps": r.target_fps,
                    "target_threshold_mbps": r.target_threshold_mbps,
                    "rx_mbps": round(r.rx_mbps, 1),
                    "rx_gbps": round(r.rx_gbps, 3),
                    "frame_delivery_pct": round(r.frame_delivery_pct, 1),
                    "saturated": r.saturated,
                    "threshold_passed": r.threshold_passed,
                }
                for r in results
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_summary(
            results, thresholds, peak_mbps, max_passed_gbps,
            host_speed, args.iface, adaptive,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
