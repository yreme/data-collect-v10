from pathlib import Path

import pytest

from sensorhub import keys as K
from sensorhub import config as C
from sensorhub.codecs import ImuSample, XYZIRT_DTYPE, pack_imu, unpack_imu
from sensorhub.header import HEADER_SIZE, Encoding, FrameHeader, Kind, image_header
from sensorhub.params import ParamError, ParamSet, ParamSpec
from sensorhub.stats import StreamStats
from sensorhub.sub import Decimator, SyncAssembler
from sensorhub.sync_grid import (
    GridError, GridTimer, align_up, is_valid_hz, nearest_slot, next_second_boundary, period_ns,
)

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "sensors.yaml"
S = 1_000_000_000


def test_header_roundtrip():
    h = image_header(encoding="bayer_rggb8", width=1920, height=1080, seq=7,
                     stamp_ns=123, trigger_ns=100, flags=3)
    raw = h.pack()
    assert len(raw) == HEADER_SIZE == 64
    h2 = FrameHeader.unpack(raw)
    assert h2 == h
    assert h2.kind == Kind.CAMERA and h2.encoding == Encoding.BAYER_RGGB8
    assert h2.step == 1920 and h2.expected_payload_size() == 1920 * 1080
    assert h2.sync_error_ns == 23


def test_header_rejects_bad_magic():
    with pytest.raises(ValueError):
        FrameHeader.unpack(b"XXXX" + bytes(60))


def test_keys():
    k = K.sensor_keys("camera", "cam_corner_0", prefix="rig")
    assert k.data("image") == "rig/camera/cam_corner_0/image"
    assert k.status == "rig/camera/cam_corner_0/status"
    assert K.parse_key("rig/lidar/lidar0/points", "rig").name == "lidar0"
    assert K.parse_key("other/lidar/lidar0/points", "rig") is None
    with pytest.raises(ValueError):
        K.sensor_keys("camera", "Cam-0", prefix="rig")
    with pytest.raises(ValueError):
        k.data("status")


@pytest.mark.parametrize("hz,ok", [(1, True), (2, True), (5, True), (10, True), (20, True), (25, True),
                                   (3, False), (7, False), (0.5, True), (0.1, True), (0.3, False)])
def test_valid_hz(hz, ok):
    assert is_valid_hz(hz) is ok


def test_grid_alignment_across_rates():
    t = 1_700_000_000_123_456_789
    p20, p10 = period_ns(20), period_ns(10)
    a20, a10 = align_up(t, p20), align_up(t, p10)
    assert a20 % p20 == 0 and a10 % p10 == 0
    assert a10 % p20 == 0
    assert nearest_slot(10 * S + 49_000_000, p20) == 10 * S + 50_000_000
    assert next_second_boundary(10 * S + 900_000_000) == 12 * S


def test_grid_timer_hz_change_applies_on_second():
    now = [10 * S + 10_000_000]
    t = GridTimer(hz=20, clock=lambda: now[0], spin_ns=0)
    assert t.peek_next() == 10 * S + 50_000_000
    eff = t.set_hz(5)
    assert eff == 11 * S
    slots = []
    for _ in range(25):
        now[0] = t.peek_next()
        slots.append(t.wait_next())
    assert all(s % period_ns(20) == 0 for s in slots if s < eff)
    after = [s for s in slots if s >= eff]
    assert after[0] == eff and all(b - a == period_ns(5) for a, b in zip(after, after[1:]))


def test_grid_rejects_bad_hz():
    with pytest.raises(GridError):
        period_ns(3)


def test_params():
    ps = ParamSet.of([
        ParamSpec("hz", "enum", 20, choices=[1, 2, 5, 10, 20]),
        ParamSpec("exposure_us", "int", 1000, min=10, max=100000),
        ParamSpec("color", "bool", True),
    ])
    assert ps.validate({"hz": "5", "color": "false"}) == {"hz": 5, "color": False}
    with pytest.raises(ParamError):
        ps.validate({"hz": 3})
    with pytest.raises(ParamError):
        ps.validate({"exposure_us": 1})
    with pytest.raises(ParamError):
        ps.validate({"nope": 1})


def test_imu_codec():
    s = [ImuSample(1, 0.1, 0.2, 9.8, 0.01, 0.02, 0.03), ImuSample(2, 1, 2, 3, 4, 5, 6, temp_c=30.0)]
    raw = pack_imu(s)
    assert len(raw) == 2 * 56
    back = unpack_imu(raw)
    assert back[0].stamp_ns == 1 and abs(back[1].temp_c - 30.0) < 1e-6
    assert XYZIRT_DTYPE.itemsize == 24


def test_stats():
    st = StreamStats(window_s=5)
    for i in range(11):
        st.record(100, seq=i if i != 5 else 6, t_ns=i * 100_000_000)
    snap = st.snapshot(now_ns=1_000_000_000)
    assert snap["rate_hz"] == pytest.approx(10, rel=0.01)
    assert snap["msg_bytes"] == 100 and snap["gaps"] == 1


def test_decimator_picks_same_slot_for_all_keys():
    d = Decimator(1)
    ts = [S * 5 + i * 50_000_000 for i in range(40)]
    picked_a = [t for t in ts if d.accept("a", t)]
    picked_b = [t for t in ts if d.accept("b", t)]
    assert picked_a == picked_b == [5 * S, 6 * S]


class _F:
    def __init__(self, key, t):
        self.key, self.time_ns = key, t


def test_sync_assembler():
    groups = []
    asm = SyncAssembler(["a", "b"], lambda s, g: groups.append((s, sorted(g))), base_hz=10, timeout_s=10)
    asm.push(_F("a", 100_000_000 + 2_000_000))
    asm.push(_F("b", 200_000_000))
    asm.push(_F("b", 100_000_000 - 3_000_000))
    assert groups == [(100_000_000, ["a", "b"])]


def test_sample_config_valid():
    cfg = C.load(CONFIG)
    cams = C.devices(cfg, "cameras")
    assert len(cams) == 6 and cams[0]["hz"] == 20 and cams[0]["serial"] == "DA6567920"
    assert cams[0]["local_ip"] == "192.168.1.102"
    assert C.devices(cfg, "lidars")[0]["msop_port"] == 6699


def test_config_validation_errors():
    cfg = C.parse("cameras:\n  defaults: {hz: 3}\n  devices:\n    - {name: Bad-Name}\n    - {name: ok}\n    - {name: ok}\n")
    errs = C.validate(cfg)
    assert any("Bad-Name" in e for e in errs)
    assert any("重复" in e for e in errs)
    assert any("hz=3" in e for e in errs)
