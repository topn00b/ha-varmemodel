"""Time-for-time opsummering af tilstande."""

from __future__ import annotations

from custom_components.varmemodel.aggregate import on_minutes, step_mean

T0 = 1_700_002_800  # hel time


def test_on_minutes_across_hours_and_starts() -> None:
    ch = [(T0 + 1800, True), (T0 + 3600 + 900, False), (T0 + 3600 + 1800, True)]
    res = on_minutes(ch, T0, T0 + 3 * 3600)
    assert res[T0] == (30.0, 1)
    assert res[T0 + 3600] == (45.0, 1)  # 15 min før slukket + 30 min efter ny start
    assert res[T0 + 7200] == (60.0, 0)


def test_on_minutes_state_before_start() -> None:
    res = on_minutes([(T0 - 100, True)], T0, T0 + 3600)
    assert res[T0] == (60.0, 0)


def test_step_mean_time_weighted() -> None:
    res = step_mean([(T0 - 10, 30.0), (T0 + 1800, 40.0)], T0, T0 + 3600)
    assert res[T0] == 35.0


def test_step_mean_ignores_none_and_short_coverage() -> None:
    res = step_mean([(T0, None), (T0 + 3000, 30.0)], T0, T0 + 3600)
    assert T0 not in res  # under en halv time med værdi
