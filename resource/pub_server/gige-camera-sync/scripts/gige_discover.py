#!/usr/bin/env python3
"""扫描网段内所有 GigE 相机，并读取每台相机的链路协商速率。

依赖 arv-tool-0.8 进行设备发现与 GenICam 参数读取。

用法:
  python3 scripts/gige_discover.py
  python3 scripts/gige_discover.py --subnet 192.168.1.0/24
  python3 scripts/gige_discover.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从 gige-camera-sync 根目录或 scripts/ 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gige_arv_util import enrich_camera, filter_subnet, discover_devices, find_arv_tool


def main() -> int:
    parser = argparse.ArgumentParser(description="发现 GigE 相机并检测链路协商速率")
    parser.add_argument(
        "--subnet", default="192.168.1.0/24",
        help="仅保留该网段内的相机 (默认 192.168.1.0/24)",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args()

    try:
        find_arv_tool()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"正在扫描 GigE 设备 (过滤网段 {args.subnet})...", file=sys.stderr)
    devices = filter_subnet(discover_devices(), args.subnet)

    if not devices:
        print(f"未在 {args.subnet} 内发现 GigE 相机", file=sys.stderr)
        return 1

    cameras = [enrich_camera(name, ip) for name, ip in devices]

    if args.json:
        payload = [
            {
                "name": c.name,
                "ip": c.ip,
                "vendor": c.vendor,
                "model": c.model,
                "serial": c.serial,
                "link_speed_mbps": c.link_speed_mbps,
                "device_link_speed_mbps": c.device_link_speed_mbps,
                "max_throughput_bps": c.max_throughput_bps,
                "resolution": f"{c.width}x{c.height}" if c.width else "",
                "pixel_format": c.pixel_format,
                "link_status": c.link_status,
                "error": c.error,
            }
            for c in cameras
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"\n发现 {len(cameras)} 台 GigE 相机 (网段 {args.subnet}):\n")
    print(
        f"{'IP':<16} {'协商速率':<18} {'型号':<24} {'序列号':<12} {'分辨率':<12} 状态"
    )
    print("-" * 95)
    slow = 0
    for c in cameras:
        if c.link_speed_mbps and c.link_speed_mbps < 1000:
            slow += 1
        res = f"{c.width}x{c.height}" if c.width else "-"
        model = c.model or c.name
        status = c.error if c.error else c.link_status
        speed = f"{c.link_speed_mbps} Mbps" if c.link_speed_mbps else "N/A"
        print(f"{c.ip:<16} {speed:<18} {model:<24} {c.serial:<12} {res:<12} {status}")

    print("-" * 95)
    gige_count = sum(1 for c in cameras if c.link_speed_mbps >= 1000)
    print(f"千兆协商: {gige_count}/{len(cameras)} 台")
    if slow:
        print(f"⚠ {slow} 台相机仅协商到 100Mbps，请检查网线或交换机端口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
