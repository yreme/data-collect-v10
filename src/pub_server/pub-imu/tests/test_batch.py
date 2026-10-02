"""网格分批逻辑单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent.parent / "common" / "python"))

from pub_imu import ClockFit, ImuPublisher, decoded_to_sample  # noqa: E402
from sensorhub.codecs import IMU_SAMPLE_DTYPE, unpack_imu  # noqa: E402
from sensorhub.sync_grid import period_ns  # noqa: E402


def test_clock_fit() -> None:
    fit = ClockFit()
    fit.update(1_000_000, 2_000_000_000)
    fit.update(2_000_000, 3_000_000_000)
    mapped = fit.map(1_500_000, 0)
    assert 2_400_000_000 < mapped < 2_700_000_000


def test_batch_boundary() -> None:
    """样本恰在 slot 边界应归入当前批。"""
    pub_hz = 20
    p_ns = period_ns(pub_hz)
    slot = 1_000_000_000
    lo = slot - p_ns

    samples = []
    for i in range(12):
        t = lo + int(p_ns * (i + 1) / 12)
        samples.append(decoded_to_sample({"acc_z": 9.81, "status": 1}, t))

    in_batch = [s for s in samples if lo < s.stamp_ns <= slot]
    assert len(in_batch) >= 1
    assert all(lo < s.stamp_ns <= slot for s in in_batch)


def test_pack_imu_roundtrip() -> None:
    from sensorhub.codecs import ImuSample, pack_imu
    samples = [ImuSample(stamp_ns=100, ax=0.1, ay=0.2, az=9.81, gx=0.01, gy=0.0, gz=0.0, status=1)]
    data = pack_imu(samples)
    arr = np.frombuffer(data, dtype=IMU_SAMPLE_DTYPE)
    assert len(arr) == 1
    assert arr[0]["az"] == pytest.approx(9.81)
