"""Yesense 标准二进制协议解析（迁移自 resource/pub_server/imu-sync）。"""

from __future__ import annotations

import struct
from typing import Dict, MutableSequence, Optional

YIS_HEADER = b"\x59\x53"
PROTOCOL_MIN_LEN = 7
PROTOCOL_TID_POS = 2
PROTOCOL_PAYLOAD_LEN_POS = 4
CRC_CALC_START_POS = 2
PAYLOAD_POS = 5
TLV_HEADER_LEN = 2

DATA_FACTOR = 0.000001
DATA_FACTOR_RAW_MAG = 0.001
DATA_FACTOR_SENSOR_TEMP = 0.01
DATA_FACTOR_HIGH_RES_LOC = 0.0000000001
DATA_FACTOR_ALT = 0.001
DATA_FACTOR_SPEED = 0.001


def default_output() -> Dict:
    return {
        "tid": 0,
        "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
        "q0": 1.0, "q1": 0.0, "q2": 0.0, "q3": 0.0,
        "sensor_temp": 25.0,
        "acc_x": 0.0, "acc_y": 0.0, "acc_z": 1.0,
        "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
        "norm_mag_x": 0.0, "norm_mag_y": 0.0, "norm_mag_z": 0.0,
        "raw_mag_x": 0.0, "raw_mag_y": 0.0, "raw_mag_z": 0.0,
        "lat": 0.0, "longt": 0.0, "alt": 0.0,
        "vel_e": 0.0, "vel_n": 0.0, "vel_u": 0.0,
        "ms": 0, "year": 2022, "month": 1, "day": 1,
        "hour": 0, "minute": 0, "second": 0,
        "smp_timestamp": 0, "ready_timestamp": 0, "status": 0,
    }


def calc_crc16(data: bytes, length: int) -> int:
    check_a = 0
    check_b = 0
    for i in range(length):
        check_a += data[i]
        check_b += check_a
    return ((check_b % 256) << 8) + (check_a % 256)


def _crc_calc_len(payload_len: int) -> int:
    return payload_len + 3


def _crc_pos(payload_len: int) -> int:
    return CRC_CALC_START_POS + _crc_calc_len(payload_len)


def _clear_front(buf: MutableSequence[int], n: int) -> None:
    del buf[:n]


def parse_tlv(tlv_id: int, tlv_len: int, payload: bytes, info: Dict) -> bool:
    if tlv_id == 0x01 and tlv_len == 0x02:
        info["sensor_temp"] = struct.unpack_from("<h", payload)[0] * DATA_FACTOR_SENSOR_TEMP
    elif tlv_id == 0x10 and tlv_len == 0x0C:
        info["acc_x"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR
        info["acc_y"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR
        info["acc_z"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR
    elif tlv_id == 0x20 and tlv_len == 0x0C:
        info["gyro_x"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR
        info["gyro_y"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR
        info["gyro_z"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR
    elif tlv_id == 0x40 and tlv_len == 0x0C:
        info["pitch"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR
        info["roll"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR
        info["yaw"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR
    elif tlv_id == 0x41 and tlv_len == 0x10:
        info["q0"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR
        info["q1"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR
        info["q2"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR
        info["q3"] = struct.unpack_from("<i", payload, 12)[0] * DATA_FACTOR
    elif tlv_id == 0x30 and tlv_len == 0x0C:
        info["norm_mag_x"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR
        info["norm_mag_y"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR
        info["norm_mag_z"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR
    elif tlv_id == 0x31 and tlv_len == 0x0C:
        info["raw_mag_x"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR_RAW_MAG
        info["raw_mag_y"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR_RAW_MAG
        info["raw_mag_z"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR_RAW_MAG
    elif tlv_id == 0x68 and tlv_len == 0x14:
        info["lat"] = struct.unpack_from("<q", payload, 0)[0] * DATA_FACTOR_HIGH_RES_LOC
        info["longt"] = struct.unpack_from("<q", payload, 8)[0] * DATA_FACTOR_HIGH_RES_LOC
        info["alt"] = struct.unpack_from("<i", payload, 16)[0] * DATA_FACTOR_ALT
        info["pos_x"] = info["longt"]
        info["pos_y"] = info["lat"]
        info["pos_z"] = info["alt"]
    elif tlv_id == 0x50 and tlv_len == 0x0B:
        info["ms"] = struct.unpack_from("<I", payload, 0)[0]
        info["year"] = struct.unpack_from("<H", payload, 4)[0]
        info["month"] = struct.unpack_from("<B", payload, 6)[0]
        info["day"] = struct.unpack_from("<B", payload, 7)[0]
        info["hour"] = struct.unpack_from("<B", payload, 8)[0]
        info["minute"] = struct.unpack_from("<B", payload, 9)[0]
        info["second"] = struct.unpack_from("<B", payload, 10)[0]
    elif tlv_id == 0x51 and tlv_len == 0x04:
        info["smp_timestamp"] = struct.unpack_from("<I", payload)[0]
    elif tlv_id == 0x52 and tlv_len == 0x04:
        info["ready_timestamp"] = struct.unpack_from("<I", payload)[0]
    elif tlv_id == 0x70 and tlv_len == 0x0C:
        info["vel_e"] = struct.unpack_from("<i", payload, 0)[0] * DATA_FACTOR_SPEED
        info["vel_n"] = struct.unpack_from("<i", payload, 4)[0] * DATA_FACTOR_SPEED
        info["vel_u"] = struct.unpack_from("<i", payload, 8)[0] * DATA_FACTOR_SPEED
    elif tlv_id == 0x80 and tlv_len == 0x01:
        info["status"] = payload[0]
    else:
        return False
    return True


class YesenseDecoder:
    """流式 Yesense 帧解析器。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Optional[Dict]:
        if chunk:
            self._buf.extend(chunk)
        while True:
            frame = self._try_one()
            if frame is None:
                return None
            return frame

    def _try_one(self) -> Optional[Dict]:
        if len(self._buf) < PROTOCOL_MIN_LEN:
            return None
        ret = self._buf.find(YIS_HEADER)
        if ret < 0:
            self._buf.clear()
            return None
        if ret > 0:
            _clear_front(self._buf, ret)
        if len(self._buf) < PROTOCOL_MIN_LEN:
            return None
        msg_len = self._buf[PROTOCOL_PAYLOAD_LEN_POS]
        total = PROTOCOL_MIN_LEN + msg_len
        if len(self._buf) < total:
            return None
        crc_len = _crc_calc_len(msg_len)
        crc_data = bytes(self._buf[CRC_CALC_START_POS : CRC_CALC_START_POS + crc_len])
        check_sum = calc_crc16(crc_data, crc_len)
        crc_received = struct.unpack_from("<H", self._buf, _crc_pos(msg_len))[0]
        if check_sum != crc_received:
            _clear_front(self._buf, 2)
            return None
        result = default_output()
        result["tid"] = struct.unpack_from("<H", self._buf, PROTOCOL_TID_POS)[0]
        pos = PAYLOAD_POS
        payload_last = PAYLOAD_POS + msg_len
        while pos < payload_last:
            tlv_id = self._buf[pos]
            tlv_len = self._buf[pos + 1]
            tlv_payload = bytes(self._buf[pos + TLV_HEADER_LEN : pos + TLV_HEADER_LEN + tlv_len])
            if parse_tlv(tlv_id, tlv_len, tlv_payload, result):
                pos += tlv_len + TLV_HEADER_LEN
            else:
                pos += 1
        _clear_front(self._buf, total)
        return result


def has_yesense_header(data: bytes) -> bool:
    return YIS_HEADER in data
