"""Kalibreringen skal finde de tal, de kunstige data er lavet med."""

from __future__ import annotations

import math
import random

from custom_components.varmemodel.calibration import (
    Hour,
    calibrate,
    fit_cooling,
    fit_curve,
    fit_heat_loss,
    guard,
    quantile,
    theil_sen,
)

H_TRUE = 90.0  # W/K
G_TRUE = 300.0  # W
A, B, C = 6.0, 0.09, -0.10  # COP = A + B·ude + C·fremløb
K_TRUE = 0.25
C0, C1 = 34.5, -0.58


def synth(days: int = 50, seed: int = 1) -> list[Hour]:
    rnd = random.Random(seed)
    hours = []
    for i in range(days * 24):
        day = i // 24
        t_out = 6 + 5 * math.sin(day / 6) + 2 * math.sin(i / 24 * 2 * math.pi) + rnd.gauss(0, 0.3)
        t_in = 20.5 + rnd.gauss(0, 0.1)
        q = max(0.0, (H_TRUE * (t_in - t_out) - G_TRUE) / 1000 + rnd.gauss(0, 0.05))
        offset = rnd.choice([-1, 0, 2, 5])
        t_flow = C0 + C1 * t_out + offset
        cop = A + B * t_out + C * t_flow + rnd.gauss(0, 0.05)
        hours.append(Hour(ts=1_700_000_000 + i * 3600, q=q, e=q / cop, t_out=t_out, t_in=t_in, t_flow=t_flow,
                          comp=60, dhw=0, defrost=0, setpoint=t_flow, offset=offset))
    return hours


def test_theil_sen_and_quantile() -> None:
    a, b = theil_sen([0, 1, 2, 3, 100], [1, 3, 5, 7, -50])  # én vild måling påvirker ikke
    assert abs(b - 2) < 1e-9 and abs(a - 1) < 1e-9
    assert quantile([1, 2, 3, 4, 5], 0.5) == 3


def test_heat_loss_recovers_h_and_gains() -> None:
    f = fit_heat_loss(synth())
    assert f.source == "fit"
    assert abs(f.value - H_TRUE) < 5
    assert abs(f.extra["gains_w"] - G_TRUE) < 80


def test_heat_loss_needs_spread() -> None:
    flat = [Hour(ts=i * 3600, q=1.5, t_out=5.0, t_in=20.5) for i in range(24 * 40)]
    f = fit_heat_loss(flat)
    assert f.value is None and f.source == "fallback"


def test_heat_loss_assumes_typical_indoor_when_missing() -> None:
    hours = synth()
    for h in hours[: len(hours) // 2]:
        h.t_in = None
    f = fit_heat_loss(hours)
    assert f.source == "fit" and f.extra["hours_with_assumed_indoor"] > 0


def test_curve_removes_offset() -> None:
    f = fit_curve(synth())
    assert abs(f.value - C0) < 0.1 and abs(f.extra["slope"] - C1) < 0.02


def test_cooling_time_constant() -> None:
    tau = 400.0
    hours, t_in = [], 21.0
    for i in range(24 * 10):
        heating = (i % 24) < 10
        t_out = 3.0
        t_in = t_in + (0.06 if heating else -(t_in - t_out) / tau)
        hours.append(Hour(ts=i * 3600, q=3.0 if heating else 0.0, t_out=t_out, t_in=t_in))
    f = fit_cooling(hours)
    assert f.source == "fit" and abs(f.value - tau) < 40


def test_calibrate_returns_all_keys() -> None:
    res = calibrate(synth())
    assert set(res) == {"heat_loss", "emission", "max_power", "min_power", "curve", "defrost", "cooling", "wind_effect"}
    assert res["emission"].value is not None


def test_guard_limits_and_cold_weather() -> None:
    v, note = guard(120.0, 100.0, (0.0, 10.0), (0.0, 10.0))
    assert v == 110.0 and "10" in note
    v, note = guard(105.0, 100.0, (0.0, 10.0), (0.0, 10.0))
    assert v == 105.0 and note == "ok"
    # Koldere vejr end før: må flytte sig 30 %
    v, note = guard(125.0, 100.0, (-6.0, 10.0), (0.0, 10.0))
    assert v == 125.0 and note == "koldere vejr"
    v, _ = guard(None, 100.0, (None, None), (0.0, 10.0))
    assert v == 100.0
    v, note = guard(50.0, None, (0.0, 1.0), (None, None))
    assert v == 50.0 and note == "ny"


def test_steady_slots_skip_start_and_stop_at_defrost() -> None:
    from custom_components.varmemodel.calibration import steady_slots

    t0 = 1_700_000_100  # ikke på en 5-minutters grænse
    five = {t: {"q": 0.3, "e": 0.1, "t_out": 5.0, "t_flow": 33.0} for t in range(1_699_999_800, t0 + 7200, 300)}
    comp = [(t0, t0 + 3600)]
    defrost = [(t0 + 2400, t0 + 2700)]
    sl = steady_slots(five, comp, defrost, [])
    # Første portion starter tidligst 10 min efter start, og ingen portion overlapper afrimningen
    assert sl[0].ts >= t0 + 600
    assert all(not (s.ts < t0 + 2700 and s.ts + 300 > t0 + 2400) for s in sl)
    # Efter afrimningen ventes igen 10 minutter
    after = [s for s in sl if s.ts >= t0 + 2700]
    assert after and after[0].ts >= t0 + 2700 + 600


def test_cop_steady_recovers_coefficients() -> None:
    from custom_components.varmemodel.calibration import Slot, fit_cop_steady

    rnd = random.Random(3)
    slots = []
    for i in range(600):
        u = rnd.uniform(-3, 10)
        f = 30 + rnd.uniform(-3, 6) - 0.5 * u
        cop = 4.0 + 0.15 * u - 0.04 * f + rnd.gauss(0, 0.05)
        q = rnd.uniform(0.25, 0.45)  # effekt uafhængig af COP
        slots.append(Slot(i * 300, q, q / cop, u, f, 20.0 + (i % 6) * 5))
    fit = fit_cop_steady(slots)
    assert fit.extra["c_source"] == "fit"
    assert abs(fit.extra["c"] + 0.04) < 0.01
    assert abs(fit.extra["d"]) < 0.02
    assert abs(fit.value - (4.0 - 0.04 * 35)) < 0.08
    assert fit.extra["c_ci95"][0] < -0.04 < fit.extra["c_ci95"][1]


def test_daily_cop_and_expected_steady() -> None:
    from custom_components.varmemodel.calibration import Fit, daily_cop

    hours = [Hour(ts=i * 3600, q=3.0, e=1.0, t_out=5.0, t_flow=33.0, dhw=0) for i in range(48)]
    hours[5].dhw, hours[5].e = 30, 2.0  # én time varmt vand med dårlig COP
    day_of = lambda ts: "d1" if ts < 24 * 3600 else "d2"  # noqa: E731
    steady = Fit(3.0, 100, extra={"a": 3.0, "b": 0.0, "c": 0.0})
    d = daily_cop(hours, day_of, steady, ["d1"])
    assert d["heat_kwh"] == 72.0 and d["el_kwh"] == 25.0
    assert d["cop"] == round(72 / 25, 2) and d["cop_without_dhw"] == 3.0
    assert d["expected_steady"] == 3.0 and d["vs_steady_pct"] == 0.0
    assert daily_cop(hours, day_of, steady, ["d3"]) is None


def test_weather_recovers_wind_and_sun() -> None:
    from custom_components.varmemodel.calibration import fit_weather

    rnd = random.Random(7)
    hours = []
    w_true, s_true = 0.03, 0.8  # 3 % pr. m/s, 0,8 kW sol ved klar himmel
    for i in range(24 * 90):
        day = i // 24
        t_out = 5 + 4 * math.sin(day / 5) + rnd.gauss(0, 0.3)
        wind = max(0.0, 4 + 3 * math.sin(day / 3.3) + rnd.gauss(0, 0.5))
        cloud = rnd.choice([10, 50, 95])
        uv = 1.0 if 8 <= i % 24 < 16 else 0.0
        sun = (1 - cloud / 100) if uv else 0.0
        q = max(0.0, H_TRUE / 1000 * (20.5 - t_out) * (1 + w_true * wind) - 0.2 - s_true * sun + rnd.gauss(0, 0.03))
        hours.append(Hour(ts=i * 3600, q=q, t_out=t_out, t_in=20.5, wind=wind, cloud=cloud, uv=uv, wind_u=0.0, wind_v=wind))
    f = fit_weather(hours)
    assert f.source == "fit"
    assert abs(f.value - w_true * 100) < 0.6
    assert abs(f.extra["sun_kw_clear"] - s_true) < 0.15


def test_weather_waits_for_data() -> None:
    from custom_components.varmemodel.calibration import fit_weather

    f = fit_weather([Hour(ts=i * 3600, q=1.0, t_out=5.0, t_in=20.0) for i in range(24 * 60)])
    assert f.value is None and "vejrdata" in f.note


def test_wind_sector_from_vector() -> None:
    from custom_components.varmemodel.calibration import wind_sector

    assert wind_sector(0.0, 5.0) == "N"  # vind fra nord
    assert wind_sector(5.0, 0.0) == "Ø"
    assert wind_sector(-3.5, -3.5) == "SV"
    assert wind_sector(0.1, 0.1) is None


def test_room_model_recovers_cooling_and_skips_airing() -> None:
    from custom_components.varmemodel.calibration import RoomHour, fit_room

    rnd = random.Random(11)
    b_true, a_true = 0.005, 0.03  # 1/time og °C pr. kWh
    t = 20.5
    hours = []
    for i in range(24 * 40):
        t_out = 4 + 4 * math.sin(i / 70)
        q = 4.0 if (i % 24) < 8 else 0.0
        aired = (i % 97) == 0
        t = t + a_true * q + b_true * (t_out - t) + rnd.gauss(0, 0.01) - (0.8 if aired else 0.0)
        hours.append(RoomHour(ts=i * 3600, t_room=t, t_out=t_out, q=q, aired=aired))
    f = fit_room(hours)
    assert f.source == "fit"
    assert abs(f.value - b_true * 20) < 0.02
    assert f.extra["aired_blocks_skipped"] > 0


def test_detect_airing() -> None:
    from custom_components.varmemodel.calibration import detect_airing

    pts = [(i * 300, 21.0) for i in range(12)] + [(12 * 300, 20.6), (13 * 300, 20.5)]
    hours = detect_airing(pts)
    assert {0, 3600, 7200, 10800} <= hours and 14400 not in hours
    assert not detect_airing([(i * 300, 21.0 - i * 0.01) for i in range(24)])


def test_room_model_adds_recent_source_when_it_matters() -> None:
    """En ny varmekilde med kun få dages data tages med for det rum, den tydeligt varmer."""
    from custom_components.varmemodel.calibration import RoomHour, fit_room

    rnd = random.Random(5)
    t = 20.0
    hours = []
    n = 24 * 50
    for i in range(n):
        t_out = 4 + 3 * math.sin(i / 60)
        floor = 4.0 if (i % 24) < 8 else 0.0
        new = 1.0 if (i % 17) < 5 else 0.0  # ny kilde (uregelmæssig), kun data de sidste 8 døgn
        known = i >= n - 24 * 8
        t = t + 0.01 * floor + 0.08 * new + 0.005 * (t_out - t) + rnd.gauss(0, 0.01)
        hours.append(RoomHour(ts=i * 3600, t_room=t, t_out=t_out, inputs={"Gulv": floor, "Ny": new if known else None}))
    f = fit_room(hours)
    assert f.source == "fit"
    assert "Ny" in f.extra["sources"] and f.extra["sources_added_recent"] == ["Ny"]
    assert abs(f.extra["sources"]["Ny"]["total"] - 0.08) < 0.03


def test_room_model_adds_recent_neighbour() -> None:
    """En nabozone med kort historik tages med, når den tydeligt påvirker rummet."""
    from custom_components.varmemodel.calibration import RoomHour, fit_room

    rnd = random.Random(9)
    t, hours, n = 20.0, [], 24 * 50
    for i in range(n):
        t_out = 4 + 3 * math.sin(i / 60)
        nb = 23 + 3 * math.sin(i / 7)
        q = 4.0 if (i % 24) < 8 else 0.0
        t = t + 0.01 * q + 0.004 * (t_out - t) + 0.05 * (nb - t) + rnd.gauss(0, 0.01)
        hours.append(RoomHour(ts=i * 3600, t_room=t, t_out=t_out, q=q, neighbours={"Trappe": nb if i >= n - 24 * 8 else None}))
    f = fit_room(hours)
    assert f.source == "fit" and "Trappe" in (f.extra["neighbours"] or {})


def test_validate_room_gives_honest_precision() -> None:
    """Afprøvningen kører modellen 6 timer frem med målt tilførsel: lille fejl for en god model, skævhed for en forkert."""
    from dataclasses import replace

    from custom_components.varmemodel.calibration import RoomHour, fit_room, validate_room

    rnd = random.Random(5)
    b_true, a_true = 0.005, 0.03
    t = 20.5
    hours = []
    for i in range(24 * 40):
        t_out = 4 + 4 * math.sin(i / 70)
        q = 4.0 if (i % 24) < 8 else 0.0
        t = t + a_true * q + b_true * (t_out - t) + rnd.gauss(0, 0.01)
        hours.append(RoomHour(ts=i * 3600, t_room=t, t_out=t_out, q=q))
    f = fit_room(hours)
    v = validate_room(hours, f)
    assert v is not None and v["n"] > 100
    assert v["rmse"] < 0.1 and abs(v["bias"]) < 0.05
    # En model med for lidt varme pr. kWh ventes for kold: rummet bliver »over«, og fejlen vokser
    weak = replace(f, extra={**f.extra, "sources": {k: {**s, "now": s["now"] / 2, "delayed": s["delayed"] / 2} for k, s in f.extra["sources"].items()}})
    w = validate_room(hours, weak)
    assert w["bias"] > 0.05 and w["rmse"] > v["rmse"]


def test_plant_room_loss_is_subtracted_before_house_fit() -> None:
    """Varmecentralens faste tab trækkes fra varmen, så husets eget varmetab bliver mindre."""
    hs = []
    for i in range(48 * 12):
        t_out = 2 + (i // 48) % 6 * 1.5
        t_plant = 22.0 + ((i // 48) % 3) * 0.5
        q = (70 * (21 - t_out) + 30 * (t_plant - 9) - 200) / 1000
        hs.append(Hour(ts=i * 3600, q=q, t_out=t_out, t_in=21.0, t_plant=t_plant))
    plain = fit_heat_loss(hs)
    split = fit_heat_loss(hs, plant_w_k=30, ground=9)
    assert abs(split.value - 70) < 1.5
    assert plain.extra["gains_w"] < split.extra["gains_w"]  # uden leddet ender tabet i den gratis varme
    ex = split.extra
    assert ex["plant_room_w_k"] == 30 and ex["plant_room_loss_kwh_day"] > 9
    assert ex["plant_room_free_fit"]["usable"] is False


def test_cop_today_breakdown():
    """Dagens COP: samlet, uden varmt vand, stabil drift og tab ved opstart (ekspertmetoden)."""
    from custom_components.varmemodel.calibration import cop_today

    t0 = 1_700_000_100 // 300 * 300
    five = {}
    # 2 timers drift: første 10 min opstart (COP 2), derefter stabilt COP 3; sidste 30 min varmt vand (COP 2)
    for i in range(24):
        t = t0 + i * 300
        if i < 2:
            q, e = 0.2, 0.1
        elif i >= 18:
            q, e = 0.2, 0.1
        else:
            q, e = 0.3, 0.1
        five[t] = {"q": q, "e": e, "t_out": 2.0, "t_flow": 33.0}
    comp = [(t0, t0 + 24 * 300)]
    dhw = [(t0 + 18 * 300, t0 + 24 * 300)]
    d = cop_today(five, comp, [], dhw, None)
    assert d is not None
    assert d["cop_steady"] == 3.0
    assert d["dhw_el_kwh"] == 0.6
    # uden varmt vand: (2·0,2 + 16·0,3) / 1,8 = 2,89 → tab mod stabil ≈ 3,7 %
    assert abs(d["cop_without_dhw"] - 2.89) < 0.01
    assert 3 < d["loss_start_defrost_pct"] < 4.5
    assert d["dhw_effect_pct"] > 0
    assert cop_today({}, [], [], [], None) is None


def test_cop_loss_same_method_as_today():
    """14-dages-tallet og COP i dag regnes ens: rumvarme mod målt stabil drift."""
    from custom_components.varmemodel.calibration import Fit, cop_today, fit_cop_loss

    t0 = 1_700_000_100 // 300 * 300
    five = {t0 + i * 300: {"q": 0.2 if i < 2 else 0.3, "e": 0.1, "t_out": 2.0, "t_flow": 33.0} for i in range(24)}
    comp = [(t0, t0 + 24 * 300)]
    f = fit_cop_loss(Fit(None), five, comp, [], [])
    assert f.value == cop_today(five, comp, [], [], None)["loss_start_defrost_pct"]
    assert f.value > 0
