"""Unit tests for clock-aligned IMU rotate windows."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from imu_record.rotate import (
    format_window_filename_ts,
    next_window_start,
    window_id,
    window_start,
)


TZ = "Asia/Shanghai"


def test_window_aligns_to_0_6_12_18():
    cases = [
        ("2026-08-05 00:00:00", "2026-08-05 00:00:00"),
        ("2026-08-05 05:59:59", "2026-08-05 00:00:00"),
        ("2026-08-05 06:00:00", "2026-08-05 06:00:00"),
        ("2026-08-05 11:30:00", "2026-08-05 06:00:00"),
        ("2026-08-05 12:00:01", "2026-08-05 12:00:00"),
        ("2026-08-05 17:59:59", "2026-08-05 12:00:00"),
        ("2026-08-05 18:00:00", "2026-08-05 18:00:00"),
        ("2026-08-05 23:45:00", "2026-08-05 18:00:00"),
    ]
    tz = ZoneInfo(TZ)
    for now_s, expect_s in cases:
        now = datetime.strptime(now_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        got = window_start(now, period_hours=6, tz_name=TZ)
        expect = datetime.strptime(expect_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        assert got == expect, (now_s, got, expect)


def test_filename_ts_format():
    tz = ZoneInfo(TZ)
    now = datetime(2026, 8, 5, 14, 22, 33, tzinfo=tz)
    assert format_window_filename_ts(now, period_hours=6, tz_name=TZ) == "2026-08-05_12-00-00"


def test_next_window_crosses_midnight():
    tz = ZoneInfo(TZ)
    now = datetime(2026, 8, 5, 20, 0, 0, tzinfo=tz)
    nxt = next_window_start(now, period_hours=6, tz_name=TZ)
    assert nxt == datetime(2026, 8, 6, 0, 0, 0, tzinfo=tz)


def test_window_id_stable_within_period():
    tz = ZoneInfo(TZ)
    a = datetime(2026, 8, 5, 6, 1, 0, tzinfo=tz)
    b = datetime(2026, 8, 5, 11, 59, 0, tzinfo=tz)
    c = datetime(2026, 8, 5, 12, 0, 0, tzinfo=tz)
    assert window_id(a, period_hours=6, tz_name=TZ) == window_id(b, period_hours=6, tz_name=TZ)
    assert window_id(a, period_hours=6, tz_name=TZ) != window_id(c, period_hours=6, tz_name=TZ)
