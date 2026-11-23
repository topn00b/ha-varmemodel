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
    eff_per_kwh,
    eff_update,
    efficient_step,
    preload_per_room,
    room_gain,
    target_step,
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
    i, ch, ds, why = choose_step(None, 12, {"A": -0.2}, False, 0, 0, None, start=4)
    assert i == 4 and "starter" in why
    i, ch, ds, why = choose_step(4, 12, {"A": -0.2}, False, 600, ch, ds)
    assert i == 4 and "venter" in why
    i, ch, ds, _ = choose_step(4, 12, {"A": -0.2}, False, UP_EVERY, ch, ds)
    assert i == 5
    # alle rum over gulv + usikkerhed: først efter 30 min. og 45 min. på trinnet
    t = UP_EVERY + 60
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, True, t, ch, ds)
    assert i == 5 and ds == t
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, True, t + DOWN_HOLD, ch, ds)
    assert i == 5  # trinnet har ikke holdt 45 min.
    i, ch2, ds, why = choose_step(5, 12, {"A": 0.6}, True, ch + DOWN_GAP + DOWN_HOLD, ch, t)
    assert i == 4 and "ned" in why
    # over gulvet, men ikke over usikkerheden: holdes
    assert choose_step(4, 12, {"A": 0.3}, False, 10**6, 0, None)[0] == 4


def test_plan_target_drives_step() -> None:
    """Planens el-mål: laveste trin, hvis lærte forbrug dækker målet – op pr. kvarter, ned først når rummene har overskud."""
    w = [180, None, 300, None, 450, 600]
    assert target_step(w, 400) == 4 and target_step(w, 100) == 0 and target_step(w, 900) == 5
    assert target_step([None, 300, None], 200) is None and target_step(w, None) is None
    i, ch, _, why = choose_step(2, 6, {"A": 0.4}, False, UP_EVERY, 0, None, target=4)
    assert i == 3 and "el-mål" in why
    # målet er lavere, men rummene har ikke overskud: holdes
    assert choose_step(4, 6, {"A": 0.1}, False, 10**6, 0, None, target=1)[0] == 4
    # rum under gulvet går forud for målet
    assert choose_step(2, 6, {"A": -0.1}, False, UP_EVERY, 0, None, target=0)[0] == 3


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


def test_room_gain_removes_cooling_and_free_heat() -> None:
    """Varmepumpens andel: målt ændring minus afkøling mod ude og opdrift (gratis varme) i perioden."""
    g = room_gain(20.0, 20.1, 0.25, 0.0, 0.02, 0.1, [])
    # uden varme: 0,25 t × (0,1 − 0,02·20) = −0,075 °C → varmepumpen stod for 0,175 °C
    assert abs(g - 0.175) < 1e-9


def test_efficient_step_prefers_more_heat_per_kwh() -> None:
    """Lavt trin med bedre virkning vælges, når det dækker planens varme – ellers et højere."""
    eff: dict = {}
    for _ in range(10):
        eff = eff_update(eff, "Low 20", 0.06, 0.08)   # 0,75 °C/kWh el
        eff = eff_update(eff, "High 23", 0.10, 0.20)  # 0,50 °C/kWh el
    e_low, e_high = eff_per_kwh(eff, "Low 20"), eff_per_kwh(eff, "High 23")
    assert abs(e_low - 0.75) < 0.01 and abs(e_high - 0.5) < 0.01
    w, e = [320.0, 800.0], [e_low, e_high]
    # plan 300 W ved gennemsnitlig virkning (0,625) = 187 varme-enheder: Low giver 240 → Low
    assert efficient_step(w, e, 300) == 0
    # plan 600 W = 375: kun High (400) dækker
    assert efficient_step(w, e, 600) == 1
    assert eff_per_kwh(eff_update({}, "x", 0.1, 0.1), "x") is None  # for få målinger


def test_ventilation_effect_compares_same_steps() -> None:
    """Ventilationens effekt: virkning med og uden på de samme trin."""
    from custom_components.varmemodel.coordinator import VarmemodelCoordinator

    eff: dict = {}
    for _ in range(10):
        eff = eff_update(eff, "Low 20|vent til", 0.12, 0.1)
        eff = eff_update(eff, "Low 20|vent fra", 0.10, 0.1)
    assert VarmemodelCoordinator._vent_effect(eff) == 20
    assert VarmemodelCoordinator._vent_effect({}) is None


def test_choose_step_calm_up_every() -> None:
    """Rolig opstart: højst ét trin op pr. halve time."""
    from custom_components.varmemodel.air_control import choose_step

    i, ch, _ds, why = choose_step(3, 8, {"A": -0.2}, False, 10_000.0, 10_000.0 - 1000, None, up_every=1800)
    assert i == 3 and "halve time" in why
    i, ch, _ds, why = choose_step(3, 8, {"A": -0.2}, False, 10_000.0, 10_000.0 - 1900, None, up_every=1800)
    assert i == 4


def test_air_soft_cap_efficient_steps() -> None:
    """Rolig opstart: loft ved det højeste trin med mindst 85 % af den bedste virkning – ellers 70 % af stigen."""
    from custom_components.varmemodel.coordinator import VarmemodelCoordinator as C

    lw = [300, 450, 600, 800, 1000, 1300]
    le = [2.0, 2.1, 1.9, 1.8, 1.5, 1.2]  # bedst 2,1 → 85 % = 1,785 → trin 3
    assert C._air_soft_cap(lw, le, 6) == 3
    assert C._air_soft_cap([None] * 12, [None] * 12, 12) == 8  # round(11 × 0,7)
