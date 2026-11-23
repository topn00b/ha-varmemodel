"""Luft-luft-stigen: trinene, nat/dag, forvarmning før blokering, op/ned-regler og lært forbrug."""

from __future__ import annotations

from custom_components.varmemodel.air_control import (
    DOWN_GAP,
    DOWN_HOLD,
    UP_EVERY,
    band,
    build_ladder,
    choose_step,
    fan_options,
    is_night,
    learn_w,
    preload_per_room,
)

FANS = fan_options(["Auto", "Low", "LowMid", "Mid", "HighMid", "High"])


def test_ladder_fan_first_then_setpoint() -> None:
    lad = build_ladder(FANS, "High", 20.0, 23.0, 0.5, quiet=True)
    assert lad[0].preset == "quiet" and [x.fan for x in lad[1:6]] == FANS
    assert all(x.setpoint == 20.0 for x in lad[:6]) and lad[-1].fan == "High" and lad[-1].setpoint == 23.0
    assert len(lad) == 12
    night = build_ladder(FANS, "HighMid", 20.0, 23.0, 0.5, quiet=True)
    assert max(x.fan or "" for x in night if x.fan) != "High" and night[-1].fan == "HighMid" and len(night) == 11


def test_night_window_over_midnight() -> None:
    assert is_night(23 * 60, "22:00", "07:00") and is_night(6 * 60 + 59, "22:00", "07:00")
    assert not is_night(7 * 60, "22:00", "07:00") and not is_night(12 * 60, "22:00", "07:00")
    assert is_night(13 * 60, "12:00", "14:00") and not is_night(15 * 60, "12:00", "14:00")


def test_up_one_per_quarter_down_after_hold() -> None:
    i, ch, ds, why = choose_step(None, 12, {"A": -0.2}, 0.5, 0, 0, None, start=4)
    assert i == 4 and "starter" in why
    i, ch, ds, why = choose_step(4, 12, {"A": -0.2}, 0.5, 600, ch, ds)
    assert i == 4 and "venter" in why
    i, ch, ds, _ = choose_step(4, 12, {"A": -0.2}, 0.5, UP_EVERY, ch, ds)
    assert i == 5
    # alle rum over gulv + margen: først efter 30 min. og 45 min. på trinnet
    t = UP_EVERY + 60
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, 0.5, t, ch, ds)
    assert i == 5 and ds == t
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, 0.5, t + DOWN_HOLD, ch, ds)
    assert i == 5  # trinnet har ikke holdt 45 min.
    i, ch2, ds, why = choose_step(5, 12, {"A": 0.6}, 0.5, ch + DOWN_GAP + DOWN_HOLD, ch, t)
    assert i == 4 and "ned" in why
    # mellem gulv og margen: holdes
    assert choose_step(4, 12, {"A": 0.3}, 0.5, 10**6, 0, None)[0] == 4


def test_preload_is_planned_drop_during_block() -> None:
    plan = [{"blocked": False}, {"blocked": False}, {"blocked": True}, {"blocked": True}, {"blocked": True}, {"blocked": False}]
    fc = {"A": [21.0, 21.4, 21.0, 20.6, 20.2, 20.5], "B": [20.0, 20.1, 20.1, 20.1, 20.1, 20.2]}
    pre, blk = preload_per_room(plan, fc, 1.5)
    assert blk == "2–5" and pre["A"] == 1.2 and pre["B"] == 0.0
    assert preload_per_room(plan, fc, 0.5)[0]["A"] == 0.5
    # blokering kun 1 time eller for langt fremme: ingen
    assert preload_per_room([{"blocked": False}, {"blocked": True}, {"blocked": False}], {"A": [1, 2, 3]}, 1.5)[0] == {}
    assert preload_per_room([{"blocked": False}] * 6 + [{"blocked": True}] * 3, {"A": [20.0] * 9}, 1.5)[0] == {}


def test_learned_watt_is_moving_average() -> None:
    w = learn_w({}, "High 23", 440)
    assert w == {"High 23": 440}
    assert learn_w(w, "High 23", 540)["High 23"] == 450


def test_band() -> None:
    assert band(2.3) == "0" and band(-0.1) == "-5" and band(7.0) == "5" and band(None) is None
