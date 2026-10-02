"""Parse 727R crane PLC UDP packets (AA BB len payload CC DD)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, asdict
from typing import Any


HEADER = bytes([0xAA, 0xBB])
FOOTER = bytes([0xCC, 0xDD])
PAYLOAD_SIZE = 20


@dataclass
class SmhFlags:
    spr_sp20: bool = False
    spr_sp40: bool = False
    spr_sp45: bool = False
    mh_spr_lcked: bool = False
    mh_spr_unlcked: bool = False
    mh_spr_landed: bool = False
    hoist_up: bool = False
    hoist_down: bool = False


@dataclass
class OicrFlags:
    outside: bool = False
    inside: bool = False
    crane_maintain: bool = False
    crane_left: bool = False
    crane_right: bool = False
    crane_street: bool = False
    reserved1: bool = False
    reserved2: bool = False


@dataclass
class PlcPacket:
    smh: SmhFlags
    oicr: OicrFlags
    mh_pos: int
    mt_pos: int
    mc_pos: int
    cntrh_pos: int
    heart_beat: int
    source_ip: str = ""
    source_port: int = 0
    received_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "smh": asdict(self.smh),
            "oicr": asdict(self.oicr),
            "mh_pos": self.mh_pos,
            "mt_pos": self.mt_pos,
            "mc_pos": self.mc_pos,
            "cntrh_pos": self.cntrh_pos,
            "heart_beat": self.heart_beat,
            "source_ip": self.source_ip,
            "source_port": self.source_port,
            "received_at": self.received_at,
        }


def packet_value_signature(packet: PlcPacket) -> tuple[Any, ...]:
    """Stable PLC payload signature for MCAP dedup (excludes heart_beat and received_at)."""
    smh = asdict(packet.smh)
    oicr = asdict(packet.oicr)
    return (
        tuple(smh[k] for k in sorted(smh)),
        tuple(oicr[k] for k in sorted(oicr)),
        packet.mh_pos,
        packet.mt_pos,
        packet.mc_pos,
        packet.cntrh_pos,
    )


def _parse_flags(byte_val: int) -> list[bool]:
    return [bool(byte_val & (1 << i)) for i in range(8)]


def parse_smh(byte_val: int) -> SmhFlags:
    bits = _parse_flags(byte_val)
    return SmhFlags(
        spr_sp20=bits[0],
        spr_sp40=bits[1],
        spr_sp45=bits[2],
        mh_spr_lcked=bits[3],
        mh_spr_unlcked=bits[4],
        mh_spr_landed=bits[5],
        hoist_up=bits[6],
        hoist_down=bits[7],
    )


def parse_oicr(byte_val: int) -> OicrFlags:
    bits = _parse_flags(byte_val)
    return OicrFlags(
        outside=bits[0],
        inside=bits[1],
        crane_maintain=bits[2],
        crane_left=bits[3],
        crane_right=bits[4],
        crane_street=bits[5],
        reserved1=bits[6],
        reserved2=bits[7],
    )


def build_packet(
    smh: SmhFlags,
    oicr: OicrFlags,
    mh_pos: int,
    mt_pos: int,
    mc_pos: int,
    cntrh_pos: int,
    heart_beat: int,
) -> bytes:
    smh_byte = sum(
        1 << i
        for i, name in enumerate(
            [
                "spr_sp20",
                "spr_sp40",
                "spr_sp45",
                "mh_spr_lcked",
                "mh_spr_unlcked",
                "mh_spr_landed",
                "hoist_up",
                "hoist_down",
            ]
        )
        if getattr(smh, name)
    )
    oicr_byte = sum(
        1 << i
        for i, name in enumerate(
            [
                "outside",
                "inside",
                "crane_maintain",
                "crane_left",
                "crane_right",
                "crane_street",
                "reserved1",
                "reserved2",
            ]
        )
        if getattr(oicr, name)
    )
    payload = struct.pack(
        "<BBiiiiH",
        smh_byte,
        oicr_byte,
        mh_pos,
        mt_pos,
        mc_pos,
        cntrh_pos,
        heart_beat & 0xFFFF,
    )
    return HEADER + bytes([len(payload)]) + payload + FOOTER


def parse_payload(
    payload: bytes,
    source_ip: str = "",
    source_port: int = 0,
    received_at: float = 0.0,
) -> PlcPacket | None:
    """Parse raw UDP_ClientSend payload (20 bytes, no AA BB / CC DD wrapper)."""
    if len(payload) < PAYLOAD_SIZE:
        return None

    smh_byte, oicr_byte, mh_pos, mt_pos, mc_pos, cntrh_pos, heart_beat = struct.unpack(
        "<BBiiiiH", payload[:PAYLOAD_SIZE]
    )

    return PlcPacket(
        smh=parse_smh(smh_byte),
        oicr=parse_oicr(oicr_byte),
        mh_pos=mh_pos,
        mt_pos=mt_pos,
        mc_pos=mc_pos,
        cntrh_pos=cntrh_pos,
        heart_beat=heart_beat,
        source_ip=source_ip,
        source_port=source_port,
        received_at=received_at,
    )


def _extract_payload(data: bytes) -> bytes | None:
    """Accept framed (AA BB len payload CC DD) or bare 20-byte UDP_ClientSend."""
    if len(data) == PAYLOAD_SIZE:
        return data

    if len(data) >= 5 and data[0:2] == HEADER and data[-2:] == FOOTER:
        payload_len = data[2]
        payload = data[3 : 3 + payload_len]
        if len(payload) >= PAYLOAD_SIZE:
            return payload[:PAYLOAD_SIZE]

    if len(data) >= PAYLOAD_SIZE:
        return data[:PAYLOAD_SIZE]

    return None


def parse_packet(
    data: bytes,
    source_ip: str = "",
    source_port: int = 0,
    received_at: float = 0.0,
) -> PlcPacket | None:
    payload = _extract_payload(data)
    if payload is None:
        return None
    return parse_payload(payload, source_ip, source_port, received_at)


def packet_from_dict(data: dict[str, Any]) -> PlcPacket:
    smh_raw = data.get("smh") or {}
    oicr_raw = data.get("oicr") or {}
    return PlcPacket(
        smh=SmhFlags(**{k: bool(v) for k, v in smh_raw.items()}),
        oicr=OicrFlags(**{k: bool(v) for k, v in oicr_raw.items()}),
        mh_pos=int(data.get("mh_pos") or 0),
        mt_pos=int(data.get("mt_pos") or 0),
        mc_pos=int(data.get("mc_pos") or 0),
        cntrh_pos=int(data.get("cntrh_pos") or 0),
        heart_beat=int(data.get("heart_beat") or 0),
        source_ip=str(data.get("source_ip") or ""),
        source_port=int(data.get("source_port") or 0),
        received_at=float(data.get("received_at") or 0.0),
    )
