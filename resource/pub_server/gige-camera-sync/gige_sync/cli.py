"""gige-camera-sync 命令行入口。"""

from __future__ import annotations

import argparse
import sys

from multimodal_common.logging_setup import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gige-camera-sync", description="GigE 相机采集 server")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", required=True, help="统一 sensors.yaml")
        p.add_argument("--mock", action="store_true")

    add_common(sub.add_parser("server", help="运行采集 server"))
    p_disc = sub.add_parser("discover", help="发现 GigE 相机")
    p_disc.add_argument("-c", "--config", default="configs/sensors.yaml")
    p_disc.add_argument("--subnet", default=None)
    return ap


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(args.log_level)
    log = get_logger("cli")

    if args.command == "discover":
        from .discovery import discover_cameras
        from multimodal_common.unified_config import load_unified_config
        cfg = load_unified_config(args.config)
        subnet = args.subnet or cfg.discovery.subnet
        devs = discover_cameras(subnet=subnet)
        for d in devs:
            log.info("  %s serial=%s ip=%s mac=%s", d.name, d.serial, d.ip, d.mac)
        return 0 if devs else 1

    from .server import CaptureServer
    from .unified_bridge import load_from_unified
    cfg = load_from_unified(args.config)
    if args.mock:
        cfg.capture.mock = True
    return CaptureServer(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
