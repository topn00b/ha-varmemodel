"""Planlæggeren: blokerede timer, billige timer, rummene over min., pauser."""

from __future__ import annotations

from custom_components.varmemodel.planner import PlanHour, PlanRoom, SourceModel, make_plan

M = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, cop=(3.0, 0.15, -0.04, 0.0), emission_kw_k=0.25,
                curve_c0=34.5, curve_slope=-0.6)


def hours(n=24, price=lambda i: 2.0, t_out=lambda i: 3.0, blocked=lambda i: False):
    return [PlanHour(ts=i * 3600, price=price(i), t_out=t_out(i), blocked=blocked(i)) for i in range(n)]


def test_never_heats_blocked_hours() -> None:
    hs = hours(blocked=lambda i: 8 <= i < 16)
    p = make_plan(hs, M, [], 20.5)
    assert all(h["heat_kw"] == 0 for h in p.hours if h["blocked"])
    assert abs(p.heat_kwh - p.demand_kwh) < 2.5


def test_prefers_cheap_hours() -> None:
    hs = hours(price=lambda i: 1.0 if i < 8 else 4.0)
    p = make_plan(hs, M, [], 20.5)
    cheap = sum(h["heat_kw"] for h in p.hours[:8])
    assert cheap >= 0.8 * p.heat_kwh


def test_spreads_when_prices_flat() -> None:
    """Lige priser: lav og lang drift (COP-gevinst ved lavt fremløb) frem for få hårde timer."""
    p = make_plan(hours(), M, [], 20.5)
    on = [h for h in p.hours if h["heat_kw"] > 0]
    assert len(on) >= 8 and max(h["heat_kw"] for h in on) <= M.p_max


def test_min_power_respected() -> None:
    p = make_plan(hours(price=lambda i: 1.0 + (i % 5) * 0.1), M, [], 20.5)
    assert all(h["heat_kw"] == 0 or h["heat_kw"] >= M.p_min - 1e-9 for h in p.hours)


def test_room_kept_above_min_before_long_block() -> None:
    """Et koldt rum før en lang blokering: planen lægger varme før, så rummet holder."""
    room = PlanRoom("Kontor", t_now=19.6, t_min=19.0, heat_now=0.03, heat_delayed=0.02, cool_per_hk=0.005, margin=0.3)
    hs = hours(n=24, price=lambda i: 1.0 if i >= 16 else 3.0, t_out=lambda i: -5.0, blocked=lambda i: 4 <= i < 16)
    p = make_plan(hs, M, [room], 20.5)
    assert min(p.rooms["Kontor"]) >= room.t_min + room.margin - 0.05 or p.warnings
    assert sum(h["heat_kw"] for h in p.hours[:4]) > 0


def test_max_pause_breaks_long_gap() -> None:
    hs = hours(price=lambda i: 1.0 if i < 4 else 5.0, t_out=lambda i: 10.0)
    p = make_plan(hs, M, [], 20.5, max_pause=6)
    run = best = 0
    for h in p.hours:
        run = 0 if (h["heat_kw"] > 0 or h["blocked"]) else run + 1
        best = max(best, run)
    assert best <= 6


def test_shift_and_flow() -> None:
    p = make_plan(hours(), M, [], 20.5)
    h = next(x for x in p.hours if x["heat_kw"] > 0)
    assert h["flow"] == round(20.5 + h["heat_kw"] / M.emission_kw_k, 1)
    assert M.shift_min <= h["shift"] <= M.shift_max


def test_deterministic() -> None:
    hs = hours(price=lambda i: 1.0 + (i % 7) * 0.13)
    assert make_plan(hs, M, [], 20.5).hours == make_plan(hs, M, [], 20.5).hours


def test_unfixable_room_does_not_stop_other_rooms() -> None:
    """Et rum der allerede er for koldt i en blokeret time må ikke forhindre, at de andre rum tjekkes."""
    cold = PlanRoom("Stue", t_now=19.0, t_min=20.0, heat_now=0.03, heat_delayed=0.02, cool_per_hk=0.005, margin=0.3)
    other = PlanRoom("Gang", t_now=20.4, t_min=20.0, heat_now=0.03, heat_delayed=0.02, cool_per_hk=0.01, margin=0.3)
    hs = hours(n=24, t_out=lambda i: -2.0, blocked=lambda i: i < 3)
    p = make_plan(hs, M, [cold, other], 20.5)
    assert any("Stue" in w for w in p.warnings)
    late = p.rooms["Gang"][6:]
    assert min(late) >= other.t_min + other.margin - 0.05 or any("Gang" in w for w in p.warnings)
