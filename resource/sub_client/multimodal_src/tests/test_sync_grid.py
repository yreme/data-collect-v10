"""网格同步算法单元测试。"""

from multimodal_common.sync_grid import (
    advance_sync_trigger_ms,
    align_up_sync_ms,
    hz_to_period_ms,
    next_sync_trigger_ms,
    slots_in_second,
    validate_hz,
)


def test_10hz_slots():
    assert slots_in_second(100) == [0, 100, 200, 300, 400, 500, 600, 700, 800, 900]


def test_next_trigger():
    # 在 1050ms 时，下一个 10Hz 触发点是 1100ms
    assert next_sync_trigger_ms(1050, 100) == 1100
    # 在 990ms 时，下一个是 1000ms（下一秒 0ms）
    assert next_sync_trigger_ms(990, 100) == 1000


def test_advance():
    assert advance_sync_trigger_ms(1000, 100) == 1100
    assert advance_sync_trigger_ms(1900, 100) == 2000


def test_align():
    assert align_up_sync_ms(1050, 100) == 1100
    assert align_up_sync_ms(1000, 100) == 1000


def test_validate_hz():
    assert validate_hz(10) == 10
    try:
        validate_hz(15)
        assert False
    except ValueError:
        pass
