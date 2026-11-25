"""Kalibreringen skal finde de tal, de kunstige data er lavet med."""

from __future__ import annotations

import math
import random

from custom_components.varmemodel.calibration import (
    Hour,
    Slot,
    calibrate,
    fit_cooling,
    fit_flow,
    fit_heat_loss,
    fit_lift,
    frost_cycles,
    frost_fade,
    guard,
    ice_watch,
    kw_at,
    lift_for,
    lift_levels,
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
                          comp=60, dhw=0, defrost=0))
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


def test_flow_learned_from_measured_flow() -> None:
    """Det lærte fremløb: målt fremløb ved ren rumvarme mod udetemperatur (medianen af de tilfældige tillæg er 1)."""
    hs = synth()
    for h in hs:
        h.t_flow = C0 + C1 * h.t_out + (1.0 if h.ts % 7200 else 0.0)
    f = fit_flow(hs)
    assert abs(f.value - (C0 + 1.0)) < 0.2 and abs(f.extra["slope"] - C1) < 0.05
    # Varmt vand og korte kørsler tæller ikke med
    for h in hs:
        h.dhw = 10
    assert fit_flow(hs).value is None


def test_flow_learned_is_not_an_echo_of_the_control() -> None:
    """Timer, hvor styringen selv satte fremløbet, tæller ikke med i husets eget fremløb: ellers lærer loftet af det fremløb,
    det selv har holdt nede, og falder for hver kalibrering (koldt vejr kom samtidig med styringen, så de timer vejer tungt)."""
    t0 = 1_700_000_000
    # Huset selv: 35 °C ved 0 °C ude, −0,5 °C pr. grad – mildt vejr (2 … 14 °C)
    hs = [Hour(ts=t0 + i * 3600, q=4.0, t_out=2 + i % 13, t_flow=35 - 0.5 * (2 + i % 13), comp=60, dhw=0) for i in range(78)]
    cut = t0 + 78 * 3600
    # Styringen: 30 °C uanset vejret – og det er blevet koldt (−2 … 2 °C)
    hs += [Hour(ts=cut + i * 3600, q=4.0, t_out=-2 + i % 5, t_flow=30.0, comp=60, dhw=0) for i in range(60)]
    echo = fit_flow(hs)
    own = fit_flow(hs, skip=[(cut + 1200, None)])  # styringen begyndte 20 min. inde i timen: hele timen tæller som styret
    assert abs(own.value - 35.0) < 0.05 and abs(own.extra["slope"] + 0.5) < 0.01
    assert own.n == 78 and own.extra["own_control_hours_left_out"] == 60
    assert echo.value < own.value - 1 and "own_control_hours_left_out" not in echo.extra
    # En afsluttet periode: kun de timer, den rører, udelades
    part = fit_flow(hs, skip=[(cut, cut + 10 * 3600)])
    assert part.n == 78 + 50 and part.extra["own_control_hours_left_out"] == 10
    # For få timer uden egen styring: ingen ny værdi (den seneste beholdes af kalibreringens vagt), og noten siger hvorfor
    few = fit_flow(hs, skip=[(t0 + 10 * 3600, None)])
    assert few.value is None and few.n == 10 and "egen styring" in few.note
    assert "egen styring" not in fit_flow(hs[:10]).note


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
    assert set(res) == {"heat_loss", "emission", "max_power", "min_power", "flow_learned", "defrost", "cooling", "wind_effect"}
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


def test_room_fit_recovers_true_coefficients() -> None:
    """Rummodellen: ændringen og tilførslen dækker de samme 6 timer – ingen systematisk underskydning (før 0.10.0: ca. 17 %)."""
    import random as _r

    from custom_components.varmemodel.calibration import RoomHour, fit_room

    rnd = _r.Random(3)
    cool, heat = 0.010, 0.050
    t, hs = 20.0, []
    for i in range(24 * 40):
        q = 3.0 if (i % 24) in range(2, 8) or rnd.random() < 0.15 else 0.0
        t_out = 3.0 + 4 * __import__("math").sin(i / 30)
        hs.append(RoomHour(ts=1_700_000_000 + i * 3600, t_room=round(t, 3), t_out=t_out, inputs={"A": q}))
        t = t + heat * q + cool * (t_out - t)
    f = fit_room(hs)
    src = f.extra["sources"]["A"]
    assert abs(f.value / 20 - cool) / cool < 0.08
    assert abs(src["now"] - heat) / heat < 0.08 and abs(src["delayed"]) < 0.25 * heat


def test_room_fit_skips_block_when_hour_before_lacks_temperature() -> None:
    """Mangler timen før blokken en rumtemperatur, springes blokken over (0.10.0 fejlede på None)."""
    from custom_components.varmemodel.calibration import RoomHour, fit_room

    hs = [RoomHour(ts=1_700_000_000 + i * 3600, t_room=None if i % 7 == 0 else 20.0 + 0.1 * (i % 5), t_out=3.0, inputs={"A": 1.0})
          for i in range(24 * 20)]
    fit_room(hs)  # må ikke fejle


ICE_COP = {"a": 4.0, "b": 0.1, "c": -0.04, "d": 0.0}  # modellen ved 0,5 °C ude og 33 °C fremløb: COP 2,73


def _cycle(t0: int, ratios: list[float]) -> list[Slot]:
    """Portionerne i én driftscyklus: afrimningen slutter ved ``t0``, første portion 10 min. efter, én pr. 5 min. med den
    givne ydelse i forhold til modellen."""
    return [Slot(ts=t0 + 600 + 300 * i, q=0.4, e=0.4 / (2.73 * r), t_out=0.5, t_flow=33.0, run_min=10 + 5 * i) for i, r in enumerate(ratios)]


def _cycles(start: int, levels: list[float], minutes: int = 50) -> list[Slot]:
    """En række cyklusser efter hinanden med hver sit niveau (ydelse i forhold til modellen) – med rim, der sætter sig."""
    out: list[Slot] = []
    for i, lv in enumerate(levels):
        out += _cycle(start + i * minutes * 60, [lv, lv, lv, lv * 0.95, lv * 0.9, lv * 0.85])
    return out


def test_frost_cycles_from_steady_slots() -> None:
    """Driftscyklusser ud fra portionerne: start ved afrimningens slut, længde, og ydelsen i forhold til husets egen
    COP-model – den rene flade (første 15 min.), slutningen (sidste 10 min.) og hele cyklussen."""
    t0 = 1_795_000_200
    slots = _cycle(t0, [1.10, 1.05, 1.00, 0.95, 0.80, 0.70]) + _cycle(t0 + 3600, [0.90, 0.88, 0.86]) + _cycle(t0 + 7200, [1.0, 1.0])
    cy = frost_cycles(slots, ICE_COP)
    assert len(cy) == 2  # to portioner (10 min.) er for lidt til en cyklus
    assert cy[0]["t"] == t0 and cy[0]["min"] == 40 and cy[0]["til"] == t0 + 600 + 6 * 300 and cy[0]["n"] == 6
    assert abs(cy[0]["ren"] - 1.05) < 0.005 and abs(cy[0]["slut"] - 0.75) < 0.005 and abs(cy[0]["snit"] - 0.975) < 0.005
    assert cy[0]["ude"] == 0.5 and cy[0]["kw"] == 4.8
    assert abs(cy[1]["ren"] - 0.88) < 0.005 and cy[1]["t"] == t0 + 3600
    assert frost_cycles(slots, None) == [] and frost_cycles(slots, {}) == []  # uden husets COP-model kan intet vurderes


def test_ice_watch_compares_with_the_house_itself() -> None:
    """Isvagten: lav er en cyklus, der er ringere end ni ud af ti af husets egne tidligere. Enkelte lave cyklusser er
    almindelige; tre i træk vises som »følg«, og først når det har varet seks timer uafbrudt, er det »is«."""
    now = 1_795_600_000
    day = 86400
    # 40 tidligere cyklusser over ti døgn: fire lidt lave (0,90) og resten 0,96–1,08 – husets normale spredning
    ref = _cycles(now - 11 * day, [0.90] * 4 + [0.96 + 0.01 * (i % 13) for i in range(36)], minutes=6 * 60)
    few = frost_cycles(_cycles(now - 2 * day, [1.0] * 5), ICE_COP)
    assert ice_watch(few, now)["status"] == "learning" and ice_watch(few, now)["bounds"] is None
    ok = frost_cycles(ref + _cycles(now - 4 * 3600, [1.02, 0.80, 1.01]), ICE_COP)
    w = ice_watch(ok, now)
    assert w["status"] == "ok" and w["low_run"] == 0 and w["ref_n"] == 40 and 0.89 < w["bounds"]["ren_low"] < 0.97
    assert abs(w["bounds"]["ren_normal"] - 1.01) < 0.03 and len(w["cycles"]) == 8 and abs(w["ren"] - 1.01) < 0.005
    # tre lave i træk (to driftstimer): følg – en enkelt normal cyklus bryder løbet
    watch = ice_watch(frost_cycles(ref + _cycles(now - 3 * 3600, [0.80, 0.78, 0.76]), ICE_COP), now)
    assert watch["status"] == "watch" and watch["low_run"] == 3 and watch["low_hours"] == 2.0 and watch["since"] == now - 3 * 3600
    broken = ice_watch(frost_cycles(ref + _cycles(now - 4 * 3600, [0.80, 0.78, 1.00, 0.76]), ICE_COP), now)
    assert broken["status"] == "ok" and broken["low_run"] == 1
    # lave cyklusser i træk i seks driftstimer: is – også hen over en pause (rød periode), for isen forsvinder ikke af den
    low9 = [0.85, 0.82, 0.80, 0.78, 0.76, 0.75, 0.74, 0.72, 0.72]
    ice = ice_watch(frost_cycles(ref + _cycles(now - 8 * 3600, low9), ICE_COP), now)
    assert ice["status"] == "ice" and ice["low_run"] == 9 and ice["low_hours"] == 6.0
    before = ice_watch(frost_cycles(ref + _cycles(now - 26 * 3600, low9[:5]), ICE_COP), now - 22 * 3600)  # før pausen: følg
    assert before["status"] == "watch" and before["low_run"] == 5
    split = frost_cycles(ref + _cycles(now - 26 * 3600, low9[:5]) + _cycles(now - 4 * 3600, low9[5:]), ICE_COP)
    assert ice_watch(split, now, before["bounds"])["status"] == "ice"
    assert ice_watch(split[:-1], now, before["bounds"])["status"] == "watch"


def test_ice_watch_holds_its_bounds_through_a_long_episode() -> None:
    """En lang tilisning må ikke selv flytte grænsen for, hvad der er lavt: grænserne fra løbets start holdes fast."""
    now = 1_795_600_000
    day = 86400
    ref = _cycles(now - 12 * day, [0.96 + 0.01 * ((i + 5) % 13) for i in range(40)], minutes=6 * 60)
    start = ice_watch(frost_cycles(ref + _cycles(now - 3 * 3600, [0.84, 0.83, 0.82]), ICE_COP), now)
    assert start["status"] == "watch"
    # to døgn senere: 55 lave cyklusser ligger nu blandt de »tidligere« – uden hukommelse ville mange af dem ikke være lave
    later = now + 2 * day
    long_ = frost_cycles(ref + _cycles(now - 3 * 3600, [0.84 - 0.001 * i for i in range(58)]), ICE_COP)
    free = ice_watch(long_, later)
    held = ice_watch(long_, later, start["bounds"])
    assert held["status"] == "ice" and held["low_run"] == 58 and held["bounds"] == start["bounds"]
    assert free["bounds"]["ren_low"] < start["bounds"]["ren_low"]  # grænsen ville være gledet ned


def test_what_a_defrost_takes_from_the_floor_water() -> None:
    """Under en afrimning er fremløbet koldere end returen: varmen til at smelte rimen tages fra gulvvandet, og
    varmemåleren tæller ikke baglæns. Den regnes af vandets flow × (frem − retur) – flowet skønnet af varmeeffekten ÷
    (frem − retur) lige før afrimningen. 15 l/min og 5 K = 5,23 kW før; under afrimningen −8 K i 3 min. = −0,42 kWh."""
    from custom_components.varmemodel.calibration import defrost_cost

    t0 = 1_000_000 * 300  # en afrimning på en 5-min.-grænse
    flow = [(t0 - 3600.0, 35.0), (t0 + 0.0, 22.0), (t0 + 180.0, 31.0), (t0 + 240.0, 35.0)]
    ret = [(t0 - 3600.0, 30.0), (t0 + 200.0, 30.0), (t0 + 400.0, 30.0)]
    lpm, c = 15.0, 4.186 / 60
    five = {t0 - 300: {"q": lpm * c * 5.0 / 12}, t0 - 600: {"q": lpm * c * 5.0 / 12}}
    res = defrost_cost([(t0, t0 + 180.0)], flow, ret, five)
    assert len(res) == 1 and res[0]["min"] == 3.0 and abs(res[0]["flow"] - 15.0) < 0.1
    assert abs(res[0]["q"] - lpm * c * 8.0 * 180 / 3600) < 0.01 and res[0]["til"] == t0 + 180  # fremløbet er varmest igen ved afrimningens slut
    # fremløbet er stadig koldere end returen et minut efter: halen tælles med, til det vender
    slow = [(t0 - 3600.0, 35.0), (t0 + 0.0, 22.0), (t0 + 180.0, 28.0), (t0 + 240.0, 35.0)]
    r2 = defrost_cost([(t0, t0 + 180.0)], slow, ret, five)
    assert abs(r2[0]["q"] - lpm * c * (8.0 * 180 + 2.0 * 60) / 3600) < 0.01 and r2[0]["til"] == t0 + 240
    # uden drift lige før (fx efter varmt vand) kan flowet ikke skønnes: afrimningen springes over
    assert defrost_cost([(t0, t0 + 180.0)], flow, ret, {}) == []
    assert defrost_cost([(t0, t0 + 180.0)], flow, ret, {t0 - 300: {"q": 0.01}}) == []
    # for få målinger under afrimningen: springes over
    assert defrost_cost([(t0, t0 + 180.0)], [(t0 - 3600.0, 35.0)], [(t0 - 3600.0, 30.0)], five) == []


def test_frost_price_per_outdoor_band() -> None:
    """Rimens pris til planen: afrimninger pr. kWh varme for hvert udebånd × det, en afrimning tager fra vandet – som
    forskel til gennemsnittet, husets modeller allerede har i sig. Under 1 i rimvejr, over 1 i mildt vejr."""
    from custom_components.varmemodel.calibration import fit_defrost, frost_points

    hours = []
    for i in range(60):  # 20 timer ved −1 °C (hver time en afrimning), 20 ved 1,5 °C (hver anden), 20 ved 8 °C (ingen)
        t_out, df = ((-1.0, 1), (1.5, i % 2), (8.0, 0))[i // 20]
        hours.append(Hour(ts=i * 3600, q=4.0, e=1.5, t_out=t_out, t_in=21.0, t_flow=34.0, comp=60, dhw=0, defrost=df))
    f = fit_defrost(hours)
    pk = f.extra["per_kwh"]
    assert [(b["t_out"], b["per_kwh"], b["kwh"]) for b in pk] == [(-1.0, 0.25, 80.0), (1.5, 0.125, 80.0), (8.0, 0.0, 80.0)]
    assert f.extra["per_kwh_all"] == 0.125
    pts = frost_points(pk, f.extra["per_kwh_all"], 0.3)
    base = 1 - 0.125 * 0.3
    assert [a for a, _ in pts] == [-1.0, 1.5, 8.0]
    assert abs(pts[0][1] - (1 - 0.25 * 0.3) / base) < 1e-3 and abs(pts[1][1] - 1.0) < 1e-3 and abs(pts[2][1] - 1 / base) < 1e-3
    assert pts[0][1] < 1 < pts[2][1]
    # før tallene er lært: ingen faktor
    assert frost_points(None, 0.1, 0.3) == [] and frost_points(pk, f.extra["per_kwh_all"], None) == [] and frost_points(pk, None, 0.3) == []
    # et bånd med for lidt varme tæller ikke med
    few = fit_defrost(hours + [Hour(ts=99 * 3600, q=4.0, e=1.5, t_out=4.0, t_in=21.0, t_flow=34.0, comp=60, dhw=0, defrost=1)])
    assert [b["t_out"] for b in few.extra["per_kwh"]] == [-1.0, 1.5, 8.0]


def _lift_slots(pairs: list[tuple[float, float]], n: int = 10, t0: int = 1_800_000_000) -> tuple[list[Slot], dict[int, float], list[tuple[float, float]]]:
    """Portioner til løft-tabellen: for hvert (løft, kW) ``n`` portioner med retur 25 °C og målet 25 + løft."""
    slots, ret, target, t = [], {}, [], t0
    for lift, kw in pairs:
        target.append((t, 25.0 + lift))
        for _ in range(n):
            slots.append(Slot(t, kw / 12, kw / 36, 1.0, 25.0 + lift, 30.0))
            ret[t] = 25.0
            t += 300
    return slots, ret, target


def test_lift_table_is_learned_from_the_house() -> None:
    """Løftet (fremløbsmål − retur) → effekt: median pr. hel grad, voksende, og først når nok grader er målt. En portion, hvor
    målet blev ændret undervejs, tæller ikke, og en portion uden retur heller ikke."""
    slots, ret, target = _lift_slots([(3, 3.2), (4, 3.6), (5, 3.5), (6, 4.3), (7, 5.2), (9, 7.0)])
    tab = fit_lift(slots, ret, target)
    assert [r[0] for r in tab] == [3.0, 4.0, 5.0, 6.0, 7.0, 9.0] and all(r[2] == 10 for r in tab)
    kws = [r[1] for r in tab]
    assert kws == sorted(kws) and kws[1] == kws[2] == 3.55  # 4 og 5 °C brød rækkefølgen: de får deres fælles effekt
    assert kws[0] == 3.2 and kws[-1] == 7.0
    # for få grader målt: ingen tabel (så kører alle trin som før)
    assert fit_lift(*_lift_slots([(3, 3.2), (4, 3.6), (5, 3.9)])) == []
    # en grad med for få portioner tæller ikke med
    few = _lift_slots([(3, 3.2), (4, 3.6), (5, 3.9), (6, 4.3)], n=10)
    extra = _lift_slots([(8, 6.5)], n=3, t0=1_800_100_000)
    assert [r[0] for r in fit_lift(few[0] + extra[0], {**few[1], **extra[1]}, few[2] + extra[2])] == [3.0, 4.0, 5.0, 6.0]
    # målet ændret midt i en portion: den portion tæller ikke
    slots, ret, target = _lift_slots([(3, 3.2), (4, 3.6), (5, 3.9), (6, 4.3)], n=9)
    target.insert(1, (slots[0].ts + 100, 31.0))
    target.insert(2, (slots[1].ts, 28.0))
    assert fit_lift(slots, ret, sorted(target))[0][2] == 8
    # uden retur i en portion: den tæller ikke
    del ret[slots[-1].ts]
    assert fit_lift(slots, ret, sorted(target))[-1][2] == 8


def test_lift_levels_follow_the_heat_pumps_calm_range() -> None:
    """Prioritetens løft: økonomi = løftet, der giver 15 % over den laveste stabile effekt (det rolige område), balance = midt
    imellem det og den største effekt, komfort intet loft. Hele grader; uden tabel eller uden forskel: intet."""
    tab = [[2.0, 3.18, 10], [3.0, 3.18, 55], [4.0, 3.59, 69], [5.0, 3.74, 138], [6.0, 4.25, 112], [7.0, 5.21, 92], [8.0, 6.52, 33],
           [9.0, 7.1, 23], [10.0, 7.6, 13], [11.0, 7.6, 10]]  # husets tabel 25/11-2026
    assert lift_for(tab, 3.63) is not None and 4.0 < lift_for(tab, 3.63) < 4.5 and lift_for(tab, 1.0) == 2.0 and lift_for(tab, 9.0) is None
    assert kw_at(tab, 4.0) == 3.59 and kw_at(tab, 1.0) == 3.18 and kw_at(tab, 20.0) == 7.6 and abs(kw_at(tab, 6.5) - 4.73) < 0.01
    lv = lift_levels(tab, 3.16, 5.7)
    assert lv["economy"] == {"løft": 4.0, "kw": 3.59} and lv["balance"] == {"løft": 6.0, "kw": 4.25}
    assert lv["fuld"] == {"løft": 10.0, "kw": 7.6} and "comfort" not in lv
    assert lift_levels([], 3.16, 5.7) is None and lift_levels(tab, None, 5.7) is None and lift_levels(tab, 3.16, None) is None
    # varmepumpens laveste stabile effekt ligger over tabellens top: intet roligt område at holde den i
    assert lift_levels(tab, 7.5, 8.0) is None
    # en flad tabel (løftet ændrer ikke effekten): intet at styre efter
    assert lift_levels([[3.0, 4.0, 20], [4.0, 4.0, 20], [5.0, 4.0, 20], [6.0, 4.0, 20]], 3.5, 6.0) is None
    # balance, der falder sammen med »alt, hvad den kan«, udgår (balance kører så som komfort)
    steep = [[3.0, 3.0, 20], [4.0, 3.7, 20], [5.0, 6.9, 20], [6.0, 7.0, 20]]
    lv = lift_levels(steep, 3.2, 7.0)
    assert lv["economy"]["løft"] == 4.0 and "balance" not in lv


def test_frost_fade_shows_the_drop_towards_a_defrost() -> None:
    """Faldet frem mod afrimningen: rå COP de første 15 min. mod de sidste 10 min., delt i husets rolige og hårde cyklusser –
    kun i rimvejr og kun cyklusser på mindst 30 min.; først når hver gruppe har nok."""
    def cyc(kw: float, start: float, end: float, ude: float = 1.0, minutes: int = 60) -> dict:
        return {"t": 0, "til": minutes * 60, "min": minutes, "ude": ude, "kw": kw, "ren": 1.0, "slut": 0.9, "snit": 0.95, "n": minutes // 5,
                "cop_ren": start, "cop_slut": end}
    calm = [cyc(3.5, 2.9, 2.6, minutes=100) for _ in range(6)]
    hard = [cyc(6.5, 2.6, 2.0, minutes=40) for _ in range(6)]
    f = frost_fade(calm + hard)
    assert f["rolig"] == {"n": 6, "kw": 3.5, "min": 100, "cop_ren": 2.9, "cop_slut": 2.6, "fald_pct": 10.3}
    assert f["hård"]["fald_pct"] == 23.1 and f["hård"]["min"] == 40 and f["grænse_kw"] == 5.0
    assert frost_fade(calm + hard[:5]) is None  # for få hårde
    assert frost_fade(calm + [cyc(6.5, 2.6, 2.0, ude=5.0) for _ in range(6)]) is None  # mildt vejr tæller ikke
    assert frost_fade(calm + [cyc(6.5, 2.6, 2.0, minutes=20) for _ in range(6)]) is None  # korte cyklusser tæller ikke
    # frost_cycles giver de rå tal, faldet regnes af
    slots = [Slot(1_800_000_000 + 300 * i, 0.5, 0.5 / (3.0 - 0.1 * i), 1.0, 33.0, 10.0 + 5 * i) for i in range(8)]
    c = frost_cycles(slots, {"a": 4.0, "b": 0.1, "c": -0.03, "d": 0.0})[0]
    assert c["cop_ren"] == 2.9 and c["cop_slut"] == 2.35
