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
    both = make_plan(hs, M, [cold, other], 20.5)
    only_cold = make_plan(hs, M, [cold], 20.5)
    assert any("Stue" in w for w in both.warnings)
    assert both.heat_kwh > only_cold.heat_kwh


def test_room_checked_again_after_blocked_start() -> None:
    """Under grænsen i en rød start: tjekket fortsætter, så der varmes før næste røde blok."""
    room = PlanRoom("Stue", t_now=20.2, t_min=20.0, heat_now=0.03, heat_delayed=0.02, cool_per_hk=0.005, margin=0.3)
    hs = hours(n=24, price=lambda i: 1.0 if i >= 21 else 3.0, t_out=lambda i: -2.0, blocked=lambda i: i < 3 or 12 <= i < 21)
    with_room = make_plan(hs, M, [room], 20.5)
    without = make_plan(hs, M, [], 20.5)
    assert sum(h["heat_kw"] for h in with_room.hours[3:12]) > sum(h["heat_kw"] for h in without.hours[3:12])
    assert all(h["heat_kw"] == 0 for h in with_room.hours if h["blocked"])


def test_no_warning_when_only_margin_is_touched() -> None:
    room = PlanRoom("Stue", t_now=20.25, t_min=20.0, heat_now=0.0, heat_delayed=0.0, cool_per_hk=0.0, margin=0.4)
    p = make_plan(hours(n=6, blocked=lambda i: True), M, [room], 20.5)
    assert p.warnings == [] or all("Stue" not in w for w in p.warnings)


def test_red_allowance_needs_less_preheat() -> None:
    room = PlanRoom("Stue", t_now=21.0, t_min=20.0, heat_now=0.01, heat_delayed=0.01, cool_per_hk=0.006, margin=0.0)
    hs = hours(n=18, t_out=lambda i: 0.0, blocked=lambda i: i >= 6)
    strict = make_plan(hs, M, [room], 20.5)
    loose = make_plan(hs, M, [room], 20.5, red_allowance=1.0)
    assert strict.heat_kwh > loose.heat_kwh


def test_boost_only_for_given_hours() -> None:
    a = make_plan(hours(n=24), M, [], 20.5, boost=1.0, boost_hours=5)
    b = make_plan(hours(n=24), M, [], 20.5)
    assert abs((a.demand_kwh - b.demand_kwh) - 5 * M.heat_loss_w_k / 1000) < 0.02


def test_build_hours_fills_one_missing_hour_and_safety_plan() -> None:
    from custom_components.varmemodel.inputs import build_hours

    prices = {0: 1.0, 3600: 2.0, 3 * 3600: 4.0, 4 * 3600: 5.0}  # time 2 mangler (fx når uret stilles tilbage)
    hs, safety = build_hours(0, prices, {}, {}, 3.0, 48)
    assert not safety and [h.price for h in hs] == [1.0, 2.0, 3.0, 4.0, 5.0]
    hs, safety = build_hours(0, {}, {}, {7200: True}, 3.0, 48)
    assert safety and len(hs) == 24 and hs[2].blocked and all(h.price == 1.0 for h in hs)


def test_new_prices_extend_the_plan() -> None:
    """Når morgendagens priser kommer (typisk kl. 13), bliver planen længere."""
    from custom_components.varmemodel.inputs import build_hours

    today = {k * 3600: 2.0 for k in range(11)}
    both = {k * 3600: 2.0 for k in range(35)}
    assert len(build_hours(0, today, {}, {}, 3.0, 48)[0]) == 11
    assert len(build_hours(0, both, {}, {}, 3.0, 48)[0]) == 35


def test_dst_prices_are_keyed_in_utc() -> None:
    """Sommertid: lokale tider med og uden forskydning giver sammenhængende UTC-timer."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from custom_components.varmemodel.inputs import build_hours, hourly_prices

    tz = ZoneInfo("Europe/Copenhagen")

    def parse_local(s: str):
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=tz)

    # 25. oktober 2026: kl. 02-03 findes to gange; med forskydning skelnes de
    items = [{"start": f"2026-10-25T0{h}:00:00{o}", "value": 1.0 + i} for i, (h, o) in enumerate([(0, "+02:00"), (1, "+02:00"), (2, "+02:00"), (2, "+01:00"), (3, "+01:00")])]
    p = hourly_prices({"prices": items}, None, parse_local)
    ks = sorted(p)
    assert len(ks) == 5 and all(b - a == 3600 for a, b in zip(ks, ks[1:]))
    # uden forskydning ('YYYY-MM-DDTHH') falder de to kl. 02 sammen – planen udfylder hullet
    items = [{"t": f"2026-10-25T{h:02d}", "el": 1.0} for h in (0, 1, 2, 2, 3, 4)]
    p = hourly_prices({"timer": items}, None, parse_local)
    hs, _ = build_hours(min(p), p, {}, {}, 3.0, 48)
    assert len(hs) == 6 and all(b.ts - a.ts == 3600 for a, b in zip(hs, hs[1:]))
    # 28. marts 2027: kl. 02 findes ikke; ingen huller i UTC
    items = [{"t": f"2027-03-28T{h:02d}", "el": 1.0} for h in (0, 1, 3, 4)]
    p = hourly_prices({"timer": items}, None, parse_local)
    hs, _ = build_hours(min(p), p, {}, {}, 3.0, 48)
    assert all(b.ts - a.ts == 3600 for a, b in zip(hs, hs[1:])) and len(hs) == 4


def test_estimate_room_matches_hourly_model() -> None:
    """Rummet regnet frem i 5-minutters trin giver det samme som planens timetrin (føleren væk)."""
    from custom_components.varmemodel.planner import estimate_room, simulate_rooms

    r = PlanRoom(name="Stue", t_now=20.0, t_min=19.0, heat_now=0.04, heat_delayed=0.03, cool_per_hk=0.006, free_per_h=0.01,
                 neighbours=[(0.02, 22.0)])
    t0 = 1_700_000_400  # på en timegrænse
    q5 = {t0 + k * 300: 0.4 for k in range(-72, 36)}  # 4,8 kWh i timen, også 6 timer før (gulvets forsinkede varme)
    out5 = {t0 + k * 300: 2.0 for k in range(-72, 36)}
    hourly = simulate_rooms([r], [PlanHour(ts=t0 + i * 3600, price=1.0, t_out=2.0) for i in range(3)], [4.8] * 3, [4.8] * 6)["Stue"]
    est = estimate_room(r, 20.0, t0, t0 + 3 * 3600, q5, out5, 5.0)
    assert abs(est - hourly[2]) < 0.01
    # Et delvist trin og trin uden måling: halvanden time, de sidste 30 min. uden tal = ingen tilførsel
    q5b = {k: v for k, v in q5.items() if k < t0 + 3600}
    est2 = estimate_room(r, 20.0, t0, t0 + 5400, q5b, out5, 5.0)
    assert hourly[0] - 0.05 < est2 < hourly[1]


def test_preload_charges_floors_before_long_block() -> None:
    """Bevidst forvarmning: før en lang blokering lades gulvene op, så rummet står højere sidst i blokeringen."""
    hs = hours(24, price=lambda i: 1.0 if i >= 18 else 2.0, t_out=lambda i: 3.0, blocked=lambda i: 9 <= i <= 16)  # 8 timers blokering

    def room():
        return PlanRoom(name="Stue", t_now=20.4, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)

    from custom_components.varmemodel.planner import simulate_rooms

    p0 = make_plan(hs, M, [room()], 20.0)
    p3 = make_plan(hs, M, [room()], 20.0, preload=0.3, label=lambda ts: f"kl. {ts // 3600:02d}")
    end0 = simulate_rooms([room()], hs, [x["heat_kw"] for x in p0.hours])["Stue"][16]
    end3 = simulate_rooms([room()], hs, [x["heat_kw"] for x in p3.hours])["Stue"][16]
    # Uden forvarmning holdes min + usikkerhed (20,2); med 0,3 °C pr. 6 t skal rummet stå 0,4 °C højere sidst i de 8 timer
    assert end0 >= 20.2 - 0.05 and end3 >= 20.2 + 0.4 - 0.05 and end3 > end0 + 0.15
    assert sum(x["heat_kw"] for x in p3.hours[:9]) > sum(x["heat_kw"] for x in p0.hours[:9])
    assert "forvarmer til blokeringen kl. 09–17 (+0.40 °C sidst i den)" in p3.reason and "forvarmer" not in p0.reason


def _air_room(name="Rum A", t_now=19.6, t_min=19.5, t_max=None, now=0.7, delayed=-0.2, cool=0.02, free=0.15):
    return PlanRoom(name=name, t_now=t_now, t_min=t_min, heat_now=now, heat_delayed=delayed, cool_per_hk=cool, free_per_h=free,
                    margin=0.4, t_max=t_max)


def test_air_plan_keeps_floor_in_cheap_hours_never_in_red() -> None:
    """Luft-luft: rummet holdes over gulv + usikkerhed, i de billigste timer og aldrig i blokerede timer."""
    from custom_components.varmemodel.planner import make_air_plan, simulate_rooms

    hs = hours(24, price=lambda i: 1.0 if i in (2, 3, 4) else 3.0, t_out=lambda i: 2.0, blocked=lambda i: 14 <= i <= 20)
    p = make_air_plan(hs, [_air_room()], e_min=0.3, e_max=1.0)
    q = [x["heat_kw"] for x in p.hours]
    sim = simulate_rooms([_air_room()], hs, q)["Rum A"]
    assert all(q[i] == 0 for i in range(14, 21))
    assert min(sim) >= 19.5 + 0.4 - 0.05 and not p.warnings
    assert sum(q[2:5]) > 0 and "kWh el" in p.reason
    assert all(x["setpoint"] is not None for x in p.hours if x["heat_kw"] > 0)


def test_air_plan_respects_cap_of_warm_room() -> None:
    """Loftet: planen varmer ikke så meget, at et rum kommer over (eller længere over) sit loft."""
    from custom_components.varmemodel.planner import make_air_plan, simulate_rooms

    hs = hours(12, price=lambda i: 2.0, t_out=lambda i: 2.0)
    cold = _air_room("Rum A", t_now=19.4)
    warm = _air_room("Rum B", t_now=22.4, t_min=-99.0, t_max=22.5, now=0.9, cool=0.01, free=0.2)
    p = make_air_plan(hs, [cold, warm], e_min=0.3, e_max=1.0)
    q = [x["heat_kw"] for x in p.hours]
    base = simulate_rooms([cold, warm], hs, [0.0] * 12)["Rum B"]
    sim = simulate_rooms([cold, warm], hs, q)["Rum B"]
    assert all(t <= 22.5 + 1e-6 or t <= b + 1e-6 for t, b in zip(sim, base))
    # Over loftet allerede nu: ingen varme, og begrundelsen siger det
    hot = _air_room("Rum B", t_now=23.8, t_min=-99.0, t_max=22.5, now=0.9, cool=0.01, free=0.0)
    p2 = make_air_plan(hs, [_air_room("Rum A", t_now=19.4), hot], e_min=0.3, e_max=1.0)
    assert "over loftet (23.8 mod 22.5 °C)" in p2.reason
    assert sum(x["heat_kw"] for x in p2.hours[:3]) == 0


def test_flow_target_is_direct_and_bounded() -> None:
    """Fremløbsmålet regnes direkte (rummål + effekt ÷ gulvets afgivelse) og holdes inden for 25–45 °C."""
    from custom_components.varmemodel.planner import flow_for

    m = SourceModel(heat_loss_w_k=90, emission_kw_k=0.283)
    assert flow_for(m, 5.2, 19.9) == 38.5  # 19,9 + 5,2 ÷ 0,283 = 38,3 → nærmeste 0,5
    assert flow_for(m, 0.5, 19.9) == 25.0 and flow_for(m, 9.0, 21.0) == 45.0
    p = make_plan(hours(12, t_out=lambda i: 2.0), m, [], 20.0)
    assert all(x["flow"] is None or 25.0 <= x["flow"] <= 45.0 for x in p.hours)
