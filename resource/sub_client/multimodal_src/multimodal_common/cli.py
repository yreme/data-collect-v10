"""multimodal 统一客户端 CLI。"""

from __future__ import annotations

import argparse
import sys

from .logging_setup import setup_logging
from .unified_config import ConfigError, load_unified_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="multimodal-client", description="多模态客户端")
    parser.add_argument("-c", "--config", required=True, help="统一 sensors.yaml")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("foxglove", help="Foxglove WebSocket 可视化")
    sub.add_parser("mcap", help="MCAP 录制")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    try:
        cfg = load_unified_config(args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    if args.command == "foxglove":
        from .clients.foxglove_bridge import FoxgloveBridge
        return FoxgloveBridge(cfg).run()
    if args.command == "mcap":
        from .clients.mcap_recorder import McapRecorder
        return McapRecorder(cfg).run()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
