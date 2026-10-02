"""UDP 解析器单元测试（迁移自 resource/pub_server/plc-data-collect）。"""

from __future__ import annotations

import struct

from udp_parser import OicrFlags, SmhFlags, build_packet, flatten_state, parse_packet, parse_payload


def test_round_trip() -> None:
    smh = SmhFlags(spr_sp20=True, mh_spr_lcked=True, hoist_up=True)
    oicr = OicrFlags(outside=True, crane_left=True)
    raw = build_packet(smh, oicr, 100, 200, 300, 400, 42)
    packet = parse_packet(raw, "10.172.237.32", 12730, 1.0)
    assert packet is not None
    assert packet.mh_pos == 100
    assert packet.mt_pos == 200
    assert packet.mc_pos == 300
    assert packet.cntrh_pos == 400
    assert packet.heart_beat == 42
    assert packet.smh.spr_sp20 is True
    assert packet.oicr.outside is True


def test_raw_payload_without_frame() -> None:
    payload = struct.pack("<BBiiiiH", 0x01, 0x02, 100, 200, 300, 400, 7)
    packet = parse_packet(payload, "10.172.241.56", 18001, 1.0)
    assert packet is not None
    assert packet.mh_pos == 100
    assert packet.heart_beat == 7

    direct = parse_payload(payload)
    assert direct is not None
    assert direct.mt_pos == 200


def test_flatten_state() -> None:
    smh = SmhFlags(spr_sp20=True)
    oicr = OicrFlags(outside=True)
    raw = build_packet(smh, oicr, 1, 2, 3, 4, 5)
    pkt = parse_packet(raw)
    assert pkt is not None
    flat = flatten_state(pkt)
    assert flat["mh_pos"] == 1
    assert flat["smh_spr_sp20"] is True
    assert flat["oicr_outside"] is True
