"""统一命令行入口 ``lidar-sync``。"""

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
        prog="lidar-sync",
        description="RoboSense rs_driver 采集 server + 共享内存 + 可插拔客户端（无需 ROS）",
    )
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("config_pos", nargs="?", help="配置文件")
        p.add_argument("-c", "--config", default=None)
        p.add_argument("--duration", type=float, default=None)
        p.add_argument("--mock", action="store_true", help="合成点云（无需硬件）")

    p_server = sub.add_parser("server", help="运行采集 server")
    add_common(p_server)

    p_client = sub.add_parser("client", help="运行客户端")
    p_client.add_argument("kind", choices=["save", "foxglove"])
    add_common(p_client)

    p_run = sub.add_parser("run", help="server + 已启用客户端")
    add_common(p_run)

    p_info = sub.add_parser("info", help="打印配置")
    add_common(p_info)

    p_discover = sub.add_parser("discover", help="发现局域网设备")
    p_discover.add_argument("-o", "--output", default="configs/lidar.discovered.yaml")
    p_discover.add_argument("--subnet", default=None)
    p_discover.add_argument("--timeout", type=float, default=3.0, help="UDP 嗅探秒数")
    p_discover.add_argument("--arp", action="store_true", help="仅用 ARP/ping（旧模式）")
    p_discover.add_argument("--print", action="store_true")

    p_shm = sub.add_parser("shm-watch", help="调试：监视 SHM 帧序号")
    p_shm.add_argument("name", help="SHM 名，如 lidar_lidar0")
    p_shm.add_argument("--interval", type=float, default=1.0)

    p_udp = sub.add_parser("udp-watch", help="调试：监听雷达 MSOP/DIFOP UDP")
    p_udp.add_argument("--msop-port", type=int, default=6699)
    p_udp.add_argument("--difop-port", type=int, default=7788)
    p_udp.add_argument("--lidar-ip", default=None, help="用于推断本机应对雷达使用的 IP")
    p_udp.add_argument("--group-address", default="0.0.0.0", help="组播地址，如 224.0.0.205")
    p_udp.add_argument("--timeout", type=float, default=5.0)

    return ap


def _load(args, *, role: Optional[str] = None):
    cfg_path = args.config or args.config_pos
    if not cfg_path:
        print("必须提供 -c CONFIG", file=sys.stderr)
        sys.exit(1)
    try:
        from pathlib import Path
        p = Path(cfg_path)
        if p.name.startswith("sensors") or "cameras" in p.read_text(encoding="utf-8")[:500]:
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
    for l in cfg.lidars:
        log.info("  %s ip=%s mac=%s msop=%d difop=%d shm=%s enabled=%s",
                 l.name, l.lidar_ip, l.get("mac"), l.msop_port, l.difop_port,
                 cfg.shm.segment_name(l.name), l.enabled)
    log.info("clients: save=%s foxglove=%s(:%d)", cfg.clients.save.enabled,
             cfg.clients.foxglove.enabled, cfg.clients.foxglove.port)
    log.info("web: :%d", cfg.web.port)
    return 0


def _cmd_discover(args) -> int:
    from .discovery import discover_lidars, discover_lidars_udp, write_discovered_config
    from .env_config import EnvSettings
    env = EnvSettings.from_env()
    subnet = args.subnet or env.subnet or "192.168.1.*"
    log = get_logger("discover")
    if getattr(args, "arp", False):
        devs = discover_lidars(subnet=subnet)
        log.info("ARP 扫描模式")
    else:
        log.info("UDP 特征扫描（MSOP 1200B + DIFOP 256B）…")
        devs = discover_lidars_udp(
            subnet=subnet,
            timeout_sec=float(getattr(args, "timeout", 3.0)),
            bind_interface=env.local_ip,
        )
    if not devs:
        log.warning("未发现雷达（子网 %s）", subnet)
    for d in devs:
        log.info(
            "  %s mac=%s msop=%d difop=%d likely_lidar=%s",
            d.ip, d.mac or "-", d.msop_port, d.difop_port, d.is_likely_lidar,
        )
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
        t = threading.Thread(target=c.run, kwargs={"install_signals": False},
                             name=f"client-{c.client_name}", daemon=True)
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
    from .discovery import local_ip_for_peer, local_ips_for_pattern, resolve_host_address, sniff_udp_ports

    log = get_logger("udp-watch")
    grp = (args.group_address or "0.0.0.0").strip()
    bind_pat = getattr(args, "bind", None) or None
    ifaces = local_ips_for_pattern(bind_pat) if bind_pat else []
    host_hint = ""
    if args.lidar_ip:
        host_hint = resolve_host_address(args.lidar_ip, "0.0.0.0", grp)
        log.info("访问雷达 %s 时本机应使用 IP: %s", args.lidar_ip, host_hint or "(未知)")
    if ifaces:
        log.info("本机网卡: %s", ", ".join(ifaces))
    if grp not in ("", "0.0.0.0"):
        log.info("组播模式 group=%s", grp)
    sniff_host = host_hint or "0.0.0.0"
    log.info(
        "监听 UDP msop=%d difop=%d host=%s，超时 %.1fs",
        args.msop_port, args.difop_port, sniff_host, args.timeout,
    )
    msop, difop, srcs = sniff_udp_ports(
        args.msop_port,
        args.difop_port,
        host=sniff_host,
        group_address=grp,
        timeout_sec=args.timeout,
    )
    if msop or difop:
        log.info("收到 MSOP=%d DIFOP=%d，来源: %s", msop, difop, srcs)
        return 0
    log.warning(
        "未收到任何 UDP。ping 通雷达不等于有点云；请到雷达 Web 将目的 IP 设为 %s",
        host_hint or local_ip_for_peer(args.lidar_ip) if args.lidar_ip else "本机同网段 IP",
    )
    return 1


def _cmd_shm_watch(args) -> int:
    import time
    from .shm import SharedPointCloudReader

    reader = SharedPointCloudReader(args.name)
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
    if args.command == "shm-watch":
        return _cmd_shm_watch(args)
    if args.command == "udp-watch":
        return _cmd_udp_watch(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
