"""统一命令行入口 ``imu-sync``。"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, load_config
from .logging_setup import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="imu-sync",
        description="Yesense IMU 采集 server + 共享内存 + Foxglove/MCAP 客户端（无需 ROS）",
    )
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("config_pos", nargs="?", help="配置文件")
        p.add_argument("-c", "--config", default=None)
        p.add_argument("--duration", type=float, default=None)
        p.add_argument("--mock", action="store_true", help="合成 IMU 数据（无需硬件）")

    p_server = sub.add_parser("server", help="运行采集 server")
    add_common(p_server)

    p_client = sub.add_parser("client", help="运行客户端")
    p_client.add_argument("kind", choices=["foxglove", "mcap"])
    add_common(p_client)

    p_run = sub.add_parser("run", help="server + 已启用客户端")
    add_common(p_run)

    p_info = sub.add_parser("info", help="打印配置")
    add_common(p_info)

    p_discover = sub.add_parser("discover", help="发现 IMU 设备")
    p_discover.add_argument("-o", "--output", default="configs/imu.discovered.yaml")
    p_discover.add_argument("--subnet", default=None)
    p_discover.add_argument("--timeout", type=float, default=5.0)
    p_discover.add_argument("--print", action="store_true")

    p_udp = sub.add_parser("udp-watch", help="调试：监听 IMU UDP 并打印包长度")
    p_udp.add_argument("--port", type=int, default=2368, help="本机监听端口")
    p_udp.add_argument("--host", default="0.0.0.0", help="绑定地址")
    p_udp.add_argument("--timeout", type=float, default=10.0)

    p_shm = sub.add_parser("shm-watch", help="调试：监视 SHM 帧序号")
    p_shm.add_argument("name", help="SHM 名，如 imu_imu0")
    p_shm.add_argument("--interval", type=float, default=1.0)

    return ap


def _load(args, *, role: Optional[str] = None):
    cfg_path = args.config or args.config_pos
    if not cfg_path:
        print("必须提供 -c CONFIG", file=sys.stderr)
        sys.exit(1)
    try:
        from pathlib import Path as _Path
        p = _Path(cfg_path)
        try:
            text = p.read_text(encoding="utf-8")[:800]
        except Exception:
            text = ""
        if p.name.startswith("sensors") or ("cameras:" in text and "imus:" in text):
            from .unified_bridge import load_from_unified
            cfg = load_from_unified(cfg_path)
        else:
            cfg = load_config(cfg_path, role=role)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "mock", False):
        cfg.capture.mock = True
    if getattr(args, "duration", None) is not None:
        cfg.runtime.duration_sec = args.duration
    return cfg


def _cmd_server(args) -> int:
    from .server.server import CaptureServer
    cfg_path = args.config or args.config_pos
    cfg = _load(args)
    server = CaptureServer(cfg)
    if cfg_path:
        server.set_config_path(Path(cfg_path))
    return server.run()


def _cmd_client(args) -> int:
    from .clients.factory import build_client
    return build_client(args.kind, _load(args, role="client")).run()


def _cmd_info(args) -> int:
    cfg = _load(args)
    log = get_logger("info")
    log.info("capture: hz=%d mock=%s", cfg.capture.hz, cfg.capture.mock)
    log.info("shm: prefix=%s slots=%d cap=%dB", cfg.shm.name_prefix,
             cfg.shm.slot_count, cfg.shm.slot_capacity_bytes)
    for i in cfg.imus:
        log.info(
            "  %s transport=%s ip=%s port=%d serial=%s mac=%s shm=%s enabled=%s",
            i.name, i.transport, i.imu_ip, i.port, i.serial_port, i.mac,
            cfg.shm.segment_name(i.name), i.enabled,
        )
    log.info(
        "clients: foxglove=%s(:%d) mcap=%s",
        cfg.clients.foxglove.enabled, cfg.clients.foxglove.port,
        cfg.clients.mcap.enabled,
    )
    log.info("web: :%d", cfg.web.port)
    return 0


def _cmd_discover(args) -> int:
    from .config import AppConfig, CaptureConfig, ClientsConfig, ImuConfig, RuntimeConfig, ShmConfig, WebConfig, apply_env_overrides
    from .discovery import discover_all, write_discovered_config
    from .discovery.udp_discover import normalize_packet_sizes
    from .env_config import EnvSettings

    env = EnvSettings.from_env()
    subnet = args.subnet or env.subnet or "192.168.1.*"
    log = get_logger("discover")
    cfg = apply_env_overrides(
        AppConfig(
            capture=CaptureConfig(
                auto_discover=True,
                discover_subnet=subnet,
                discover_rescan_sec=0,
            ),
            shm=ShmConfig(),
            imus=[ImuConfig(name="imu0", enabled=True)],
            clients=ClientsConfig(),
            runtime=RuntimeConfig(),
            config_dir=Path("."),
            web=WebConfig(),
        )
    )
    sizes = normalize_packet_sizes(cfg.capture.discover_packet_size, cfg.capture.discover_packet_sizes)
    timeout = float(args.timeout)
    log.info(
        "扫描 IMU（UDP payload %s @ 本机端口 %s，子网 %s，超时 %.1fs）…",
        list(sizes), cfg.capture.discover_ports, subnet, timeout,
    )
    devs = discover_all(cfg, timeout_sec=timeout)
    if not devs:
        from .discovery.net_util import discovery_failure_hint
        log.warning(discovery_failure_hint(subnet))
    for d in devs:
        log.info("  %s", d.to_dict())
    out = write_discovered_config(devs, Path(args.output), env=env)
    log.info("已写入 %s", out)
    if args.print:
        print(out.read_text(encoding="utf-8"))
    return 0 if devs else 1


def _cmd_run(args) -> int:
    from .clients.factory import build_enabled_clients
    from .server.server import CaptureServer

    cfg = _load(args)
    log = get_logger("run")
    stop = threading.Event()
    server = CaptureServer(cfg)
    server._stop = stop
    clients = build_enabled_clients(cfg)
    for c in clients:
        c.stop = stop

    def on_sig(signum, _frame):
        log.info("信号 %s", signum)
        stop.set()

    signal.signal(signal.SIGINT, on_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_sig)

    server.start()
    threads: List[threading.Thread] = []
    for c in clients:
        t = threading.Thread(
            target=c.run, kwargs={"install_signals": False},
            name=f"client-{c.client_name}", daemon=True,
        )
        t.start()
        threads.append(t)

    if cfg.runtime.duration_sec and cfg.runtime.duration_sec > 0:
        threading.Timer(float(cfg.runtime.duration_sec), stop.set).start()

    while not stop.is_set():
        stop.wait(timeout=0.5)
    for t in threads:
        t.join(timeout=8.0)
    server.join(timeout=15.0)
    server.close()
    return 0


def _cmd_udp_watch(args) -> int:
    import socket
    import time
    from collections import Counter

    from .discovery.udp_discover import IMU_PAYLOAD_SIZES, is_imu_payload_size

    log = get_logger("udp-watch")
    host = args.host or "0.0.0.0"
    port = int(args.port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError as exc:
        log.error("绑定 %s:%d 失败: %s", host, port, exc)
        return 1
    s.settimeout(0.2)
    log.info("监听 %s:%d，超时 %.1fs（Ctrl+C 退出）", host, port, args.timeout)
    sizes: Counter = Counter()
    imu_hits = 0
    deadline = time.monotonic() + float(args.timeout)
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            n = len(data)
            sizes[n] += 1
            if is_imu_payload_size(n, IMU_PAYLOAD_SIZES):
                imu_hits += 1
                log.info("IMU? %s:%d len=%d head=%s", addr[0], addr[1], n, data[:4].hex())
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    log.info("长度分布: %s", dict(sorted(sizes.items())))
    log.info("匹配 IMU 特征 %s 的包: %d", list(IMU_PAYLOAD_SIZES), imu_hits)
    return 0 if imu_hits else 1


def _cmd_shm_watch(args) -> int:
    import time
    from .shm import SharedImuReader

    reader = SharedImuReader(args.name)
    last = -2
    log = get_logger("shm-watch")
    log.info("监视 %s（Ctrl+C 退出）", args.name)
    try:
        while True:
            seq = reader.latest_seq()
            if seq != last:
                log.info("seq=%s", seq)
                last = seq
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        reader.close()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(argv)
    setup_logging(args.log_level)
    if args.command == "server":
        return _cmd_server(args)
    if args.command == "client":
        return _cmd_client(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "info":
        return _cmd_info(args)
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "udp-watch":
        return _cmd_udp_watch(args)
    if args.command == "shm-watch":
        return _cmd_shm_watch(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
