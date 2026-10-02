#!/usr/bin/env python3
"""Mock 727R PLC UDP sender for local testing."""

from __future__ import annotations

import argparse
import math
import socket
import time

from udp_parser import OicrFlags, SmhFlags, build_packet


def simulate_frame(t: float, heart_beat: int) -> bytes:
    cycle = t % 120.0
    mh_pos = int(8000 + 3500 * math.sin(t * 0.35))
    mt_pos = int(12000 + 5000 * math.sin(t * 0.22 + 1.2))
    mc_pos = int(45000 + 8000 * math.sin(t * 0.08))
    cntrh_pos = int(2500 + 1200 * abs(math.sin(t * 0.5)))

    smh = SmhFlags(
        spr_sp20=cycle < 40,
        spr_sp40=40 <= cycle < 80,
        spr_sp45=cycle >= 80,
        mh_spr_lcked=cycle > 15,
        mh_spr_unlcked=cycle < 10,
        mh_spr_landed=(cycle % 30) < 8,
        hoist_up=math.sin(t * 0.35) > 0,
        hoist_down=math.sin(t * 0.35) <= 0,
    )
    oicr = OicrFlags(
        outside=(int(t) % 20) < 10,
        inside=(int(t) % 20) >= 10,
        crane_maintain=False,
        crane_left=math.sin(t * 0.08) > 0.2,
        crane_right=math.sin(t * 0.08) < -0.2,
        crane_street=(int(t) % 60) > 50,
    )

    return build_packet(smh, oicr, mh_pos, mt_pos, mc_pos, cntrh_pos, heart_beat)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock 727R crane PLC UDP sender")
    parser.add_argument("--host", default="127.0.0.1", help="Target host")
    parser.add_argument("--port", type=int, default=12730, help="Target UDP port")
    parser.add_argument("--hz", type=float, default=2.0, help="Send frequency (Hz)")
    parser.add_argument(
        "--targets",
        nargs="*",
        default=[],
        help="Additional host:port targets (e.g. 10.172.241.102:12730)",
    )
    args = parser.parse_args()

    targets: list[tuple[str, int]] = [(args.host, args.port)]
    for item in args.targets:
        if ":" in item:
            host, port_str = item.rsplit(":", 1)
            targets.append((host, int(port_str)))
        else:
            targets.append((item, args.port))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / max(args.hz, 0.1)
    heart_beat = 0
    start = time.time()

    print(f"Sending mock PLC data at {args.hz} Hz to:")
    for host, port in targets:
        print(f"  - {host}:{port}")

    try:
        while True:
            now = time.time()
            packet = simulate_frame(now - start, heart_beat)
            for host, port in targets:
                sock.sendto(packet, (host, port))
            heart_beat = (heart_beat + 1) & 0xFFFF
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
