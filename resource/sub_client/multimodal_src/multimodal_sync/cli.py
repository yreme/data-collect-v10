"""CLI entry point for multimodal-sync."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .logging_setup import get_logger
from .shm_discovery import scan_shm_segments

LOG = get_logger("cli")


def _cmd_foxglove(args: argparse.Namespace) -> int:
    from .clients.foxglove_client import FoxgloveMultimodalClient

    cfg = load_config(args.config)
    return FoxgloveMultimodalClient(cfg).run()


def _cmd_mcap(args: argparse.Namespace) -> int:
    from .clients.mcap_client import McapMultimodalClient

    cfg = load_config(args.config)
    return McapMultimodalClient(cfg).run()


def _cmd_shm_watch(args: argparse.Namespace) -> int:
    import time

    prefixes = ("gige_", "imu_", "lidar_")
    while True:
        found = scan_shm_segments()
        print(
            f"cameras={found.cameras} imus={found.imus} lidars={found.lidars} "
            f"(total={found.total})",
            flush=True,
        )
        if args.once:
            return 0
        time.sleep(args.interval)


def _cmd_discover(args: argparse.Namespace) -> int:
    """Run per-sensor discovery on 192.168.1.* subnet."""
    cfg = load_config(args.config)
    subnet = args.subnet or cfg.runtime.discover_subnet
    repo_root = Path(__file__).resolve().parents[2]

    commands = []
    if not args.skip_camera:
        gige_cfg = cfg.server_configs.get("camera") or str(
            repo_root / "gige-camera-sync/configs/x64.yaml"
        )
        commands.append(
            ["gige-sync", "discover", "-o", gige_cfg, "--subnet", subnet]
        )
    if not args.skip_imu:
        commands.append(
            [
                "imu-sync",
                "discover",
                "-o",
                str(repo_root / "imu-sync/configs/imu.discovered.yaml"),
                "--subnet",
                subnet,
            ]
        )
    if not args.skip_lidar:
        commands.append(
            [
                "lidar-sync",
                "discover",
                "--subnet",
                subnet,
                "-o",
                str(repo_root / "lidar-sync/configs/lidar.discovered.yaml"),
            ]
        )

    rc = 0
    for cmd in commands:
        LOG.info("执行: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode != 0:
                rc = result.returncode
        except FileNotFoundError:
            LOG.error("命令不存在: %s（请先安装对应 sensor 包）", cmd[0])
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="multimodal-sync",
        description="多模态传感器融合：从共享内存读取相机/IMU/雷达，Foxglove 广播或 MCAP 录制",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fg = sub.add_parser("foxglove", help="Foxglove WebSocket 广播（默认端口 8765）")
    p_fg.add_argument("-c", "--config", required=True, help="配置文件路径")
    p_fg.set_defaults(func=_cmd_foxglove)

    p_mcap = sub.add_parser("mcap", help="录制多模态 MCAP 文件")
    p_mcap.add_argument("-c", "--config", required=True, help="配置文件路径")
    p_mcap.set_defaults(func=_cmd_mcap)

    p_watch = sub.add_parser("shm-watch", help="监视 /dev/shm 中的传感器段")
    p_watch.add_argument("--interval", type=float, default=2.0)
    p_watch.add_argument("--once", action="store_true")
    p_watch.set_defaults(func=_cmd_shm_watch)

    p_disc = sub.add_parser("discover", help="在子网 192.168.1.* 上发现全部传感器")
    p_disc.add_argument("-c", "--config", default="configs/multimodal.yaml")
    p_disc.add_argument("--subnet", default=None)
    p_disc.add_argument("--skip-camera", action="store_true")
    p_disc.add_argument("--skip-imu", action="store_true")
    p_disc.add_argument("--skip-lidar", action="store_true")
    p_disc.set_defaults(func=_cmd_discover)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
