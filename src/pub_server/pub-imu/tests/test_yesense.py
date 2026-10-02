"""Yesense 解码器单元测试。"""

from __future__ import annotations

import struct

import pytest

from yesense import YesenseDecoder, calc_crc16, default_output, parse_tlv


def _build_frame(tlv_payload: bytes, tid: int = 1) -> bytes:
    """构造最小有效 Yesense 帧。"""
    payload = tlv_payload
    msg_len = len(payload)
    header = b"\x59\x53" + struct.pack("<HB", tid, msg_len)
    crc_data = header[2:] + payload
    crc = calc_crc16(crc_data, len(crc_data))
    return header + payload + struct.pack("<H", crc)


def test_parse_accel_tlv() -> None:
    info = default_output()
    raw = struct.pack("<iii", 1000000, -2000000, 9810000)
    assert parse_tlv(0x10, 0x0C, raw, info)
    assert abs(info["acc_z"] - 9.81) < 0.01


def test_decoder_single_frame() -> None:
    acc = struct.pack("<iii", 0, 0, int(9.81 / 0.000001))
    gyro = struct.pack("<iii", 100000, 0, 0)
    tlv = bytes([0x10, 0x0C]) + acc + bytes([0x20, 0x0C]) + gyro
    frame = _build_frame(tlv)
    dec = YesenseDecoder()
    result = dec.feed(frame)
    assert result is not None
    assert abs(result["acc_z"] - 9.81) < 0.01
    assert result["gyro_x"] == pytest.approx(0.1, rel=0.01)


def test_decoder_streaming() -> None:
    acc = struct.pack("<iii", 0, 0, int(9.81 / 0.000001))
    tlv = bytes([0x10, 0x0C]) + acc
    frame = _build_frame(tlv)
    dec = YesenseDecoder()
    assert dec.feed(frame[:5]) is None
    result = dec.feed(frame[5:])
    assert result is not None
    assert abs(result["acc_z"] - 9.81) < 0.01
