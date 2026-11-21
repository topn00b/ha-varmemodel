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
    assert set(res) == {"heat_loss", "cop", "emission", "max_power", "min_power", "curve", "defrost", "cooling"}
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
