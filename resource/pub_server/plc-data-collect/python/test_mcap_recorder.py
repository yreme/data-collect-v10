import tempfile
import time
from pathlib import Path

from connection_health import ConnectionHealth, LinkState
from foxglove_messages import VelocityTracker
from mcap.reader import make_reader

from mcap_recorder import McapConfig, McapRecorder, beijing_stamp
from udp_anomaly import UdpAnomalyTracker, VariableChangeTracker
from udp_parser import OicrFlags, SmhFlags, build_packet, parse_packet


def test_mcap_dual_tier_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hourly_dir = str(Path(tmp) / "hourly")
        short_dir = str(Path(tmp) / "short")
        recorder = McapRecorder(
            McapConfig(
                enabled=True,
                hourly_dir=hourly_dir,
                short_dir=short_dir,
                hourly_rotation_seconds=3600,
                short_rotation_seconds=600,
            )
        )
        health = ConnectionHealth()
        health.reset(mock_mode=True)
        tracker = VelocityTracker()

        for i in range(5):
            smh = SmhFlags(spr_sp40=True, mh_spr_lcked=True)
            oicr = OicrFlags(outside=True)
            raw = build_packet(smh, oicr, 1000 + i, 2000, 3000, 400, i)
            packet = parse_packet(raw, "10.172.237.32", 12730, time.time())
            assert packet is not None
            health.on_packet()
            velocities = tracker.compute(packet)
            recorder.write_packet(packet, velocities, health.snapshot(True).to_dict())

        recorder.stop()
        hourly_files = list(Path(hourly_dir).glob("*.mcap"))
        short_files = list(Path(short_dir).glob("*.mcap"))
        assert len(hourly_files) == 1
        assert len(short_files) == 1
        assert "-" in hourly_files[0].name
        assert hourly_files[0].stat().st_size > 1000


def test_mcap_dedup_skips_heartbeat_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hourly_dir = str(Path(tmp) / "hourly")
        short_dir = str(Path(tmp) / "short")
        recorder = McapRecorder(
            McapConfig(
                enabled=True,
                dedup_enabled=True,
                hourly_dir=hourly_dir,
                short_dir=short_dir,
            )
        )
        health = ConnectionHealth()
        health.reset(mock_mode=True)
        tracker = VelocityTracker()

        smh = SmhFlags(spr_sp40=True, mh_spr_lcked=True)
        oicr = OicrFlags(outside=True)
        for i in range(20):
            raw = build_packet(smh, oicr, 1000, 2000, 3000, 400, i)
            packet = parse_packet(raw, "10.172.237.32", 12730, time.time() + i * 0.1)
            assert packet is not None
            health.on_packet()
            velocities = tracker.compute(packet)
            recorder.write_packet(packet, velocities, health.snapshot(True).to_dict())

        assert recorder._packets_written == 1
        assert recorder._packets_skipped == 19

        recorder.stop()
        hourly_files = list(Path(hourly_dir).glob("*.mcap"))
        assert len(hourly_files) == 1
        with open(hourly_files[0], "rb") as f:
            reader = make_reader(f)
            message_count = sum(1 for _ in reader.iter_messages())
        assert message_count == 8


def test_mcap_dedup_writes_on_value_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hourly_dir = str(Path(tmp) / "hourly")
        short_dir = str(Path(tmp) / "short")
        recorder = McapRecorder(
            McapConfig(
                enabled=True,
                dedup_enabled=True,
                hourly_dir=hourly_dir,
                short_dir=short_dir,
            )
        )
        health = ConnectionHealth()
        health.reset(mock_mode=True)
        tracker = VelocityTracker()

        for mh_pos in (1000, 1000, 1100, 1100, 1200):
            raw = build_packet(SmhFlags(), OicrFlags(), mh_pos, 2000, 3000, 400, 1)
            packet = parse_packet(raw, "10.172.237.32", 12730, time.time())
            assert packet is not None
            health.on_packet()
            velocities = tracker.compute(packet)
            recorder.write_packet(packet, velocities, health.snapshot(True).to_dict())

        assert recorder._packets_written == 3
        assert recorder._packets_skipped == 2
        recorder.stop()


def test_beijing_stamp_format() -> None:
    stamp = beijing_stamp(1726471978.0)
    assert len(stamp) == 15
    assert stamp[8] == "_"


def test_health_udp_timeout() -> None:
    health = ConnectionHealth(timeout_seconds=1.0)
    health.reset(mock_mode=False)
    snap = health.snapshot(True)
    assert snap.state == LinkState.WAITING
    assert snap.can_collect is False

    health.on_packet()
    snap = health.snapshot(True)
    assert snap.state == LinkState.CONNECTED
    assert snap.can_collect is True


def test_udp_anomaly_tracking() -> None:
    tracker = UdpAnomalyTracker()
    good = build_packet(SmhFlags(), OicrFlags(), 1, 2, 3, 4, 1)
    tracker.on_raw_packet(good, "10.0.0.1", 12730)
    packet = parse_packet(good, "10.0.0.1", 12730, time.time())
    assert packet is not None
    tracker.on_parsed_packet(packet)
    assert tracker.packets_received == 1
    assert tracker.packets_parsed == 1

    tracker.on_raw_packet(b"\x00\x01\x02", "10.0.0.1", 12730)
    assert tracker.parse_errors >= 1


def test_mcap_empty_suffix_for_static_data() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hourly_dir = str(Path(tmp) / "hourly")
        short_dir = str(Path(tmp) / "short")
        recorder = McapRecorder(
            McapConfig(
                enabled=True,
                dedup_enabled=True,
                hourly_dir=hourly_dir,
                short_dir=short_dir,
                empty_max_unique_timestamps=2,
            )
        )
        health = ConnectionHealth()
        health.reset(mock_mode=True)
        tracker = VelocityTracker()

        smh = SmhFlags(spr_sp40=True)
        oicr = OicrFlags()
        for i in range(5):
            raw = build_packet(smh, oicr, 1000, 2000, 3000, 400, i)
            packet = parse_packet(raw, "10.172.237.32", 12730, time.time() + i * 0.1)
            assert packet is not None
            health.on_packet()
            velocities = tracker.compute(packet)
            recorder.write_packet(packet, velocities, health.snapshot(True).to_dict())

        recorder.stop()
        hourly_files = list(Path(hourly_dir).glob("*.mcap"))
        assert len(hourly_files) == 1
        assert hourly_files[0].name.endswith("-empty.mcap")


def test_variable_change_tracking() -> None:
    tracker = VariableChangeTracker()
    p1 = parse_packet(build_packet(SmhFlags(), OicrFlags(), 1, 2, 3, 4, 1), "1.1.1.1", 12730, 1.0)
    p2 = parse_packet(build_packet(SmhFlags(spr_sp20=True), OicrFlags(), 10, 2, 3, 4, 2), "1.1.1.1", 12730, 2.0)
    assert p1 and p2
    tracker.update(p1)
    changes = tracker.update(p2)
    assert any(c["field"] == "mh_pos" for c in changes)
    assert any(c["field"] == "smh.spr_sp20" for c in changes)


if __name__ == "__main__":
    test_mcap_dual_tier_round_trip()
    test_mcap_dedup_skips_heartbeat_only()
    test_mcap_dedup_writes_on_value_change()
    test_beijing_stamp_format()
    test_health_udp_timeout()
    test_udp_anomaly_tracking()
    test_mcap_empty_suffix_for_static_data()
    test_variable_change_tracking()
    print("ok")
