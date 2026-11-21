"""Kalibreringen skal finde de tal, de kunstige data er lavet med."""

from __future__ import annotations

import math
import random

from custom_components.varmemodel.calibration import (
    Hour,
    calibrate,
    fit_cooling,
    fit_cop,
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


def test_cop_recovers_flow_coefficient() -> None:
    f = fit_cop(synth())
    assert f.extra["c_source"] == "fit"
    assert abs(f.extra["c"] - C) < 0.01
    assert abs(f.extra["b"] - B) < 0.01
    assert abs(f.value - (A + C * 35)) < 0.05


def test_cop_uses_prior_when_flow_follows_curve() -> None:
    hours = synth()
    for h in hours:  # fremløb følger kun udetemperaturen → c kan ikke skilles fra b
        h.t_flow = C0 + C1 * h.t_out
        h.e = h.q / (A + B * h.t_out + C * h.t_flow) if h.q else h.e
    f = fit_cop(hours)
    assert f.extra["c_source"] == "prior"


def test_cop_skips_hot_water_hours() -> None:
    hours = synth()
    for h in hours[::3]:
        h.dhw = 30
        h.e = h.q / 1.5 if h.q else h.e  # varmt vand: dårlig COP
    f = fit_cop(hours)
    assert abs(f.value - (A + C * 35)) < 0.1


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
    assert set(res) == {"heat_loss", "cop", "emission", "max_power", "min_power", "curve", "defrost", "cooling", "wind_effect"}
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
