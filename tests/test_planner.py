"""Planlæggeren: blokerede timer, billige timer, rummene over min., pauser."""

from __future__ import annotations

from custom_components.varmemodel.planner import PlanHour, PlanRoom, SourceModel, make_plan

M = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, cop=(3.0, 0.15, -0.04, 0.0), emission_kw_k=0.25,
                flow_c0=34.5, flow_slope=-0.6)


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


def test_flow() -> None:
    p = make_plan(hours(), M, [], 20.5)
    h = next(x for x in p.hours if x["heat_kw"] > 0)
    assert h["flow"] == round(20.5 + h["heat_kw"] / M.emission_kw_k, 1)
    assert "shift" not in h


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
    # Forvarmningen er markeret time for time og samlet i et vindue, der slutter når blokeringen begynder
    w = p3.preload
    assert w and w["block_from"] == 9 * 3600 and w["block_to"] == 17 * 3600 and w["to"] == 9 * 3600 and w["kwh"] > 0
    assert all(x["preload"] in (0, x["heat_kw"]) for x in p3.hours) and all(x["preload"] == 0 for x in p3.hours[9:])
    assert f"opvarmer kl. {w['from'] // 3600:02d}–09 før blokeringen 09–17" in p3.reason


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
    # Usikkerheden vokser fra 0 nu til fuld efter 6 timer
    assert all(t >= 19.5 + 0.4 * min(1.0, (i + 1) / 6) - 0.05 for i, t in enumerate(sim)) and not p.warnings
    assert sum(q[2:5]) > 0 and "kWh el" in p.reason
    assert all(x["setpoint"] is not None for x in p.hours if x["heat_kw"] > 0)
    # Varme lagt før den røde blok for at holde rummet gennem den er forvarmning – med tidspunkt
    assert p.preload and p.preload["block_from"] == 14 * 3600 and p.preload["to"] == 14 * 3600
    assert sum(x["preload"] for x in p.hours) > 0 and "før blokeringen om 14 t–om 21 t" in p.reason


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


def test_low_and_even_caps_flow_at_learned_flow() -> None:
    """Lavt og jævnt: fremløbet holdes på det lærte fremløb, så varmen spredes over flere timer."""
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=34.5, flow_slope=-0.6)
    hs = hours(24, price=lambda i: 1.0 if i < 6 else 3.0, t_out=lambda i: 1.0)
    free = make_plan(hs, m, [], 20.0)
    even = make_plan(hs, m, [], 20.0, flow_margin=0.0)
    on = lambda p: sum(1 for x in p.hours if x["heat_kw"] > 0)
    assert max(x["flow"] or 0 for x in even.hours) <= 34.5 - 0.6 + 0.25
    assert on(even) > on(free) and abs(even.heat_kwh - free.heat_kwh) < 2.0


def test_flow_boost_steps_up_and_down() -> None:
    """Reserve: +1 °C, derefter højst ét trin pr. 6 timer (gulvet er langsomt), højst 5; ned igen 1 °C pr. 6 timer."""
    from custom_components.varmemodel.planner import FLOW_BOOST_MAX, FLOW_BOOST_STEP_S, flow_boost_step

    b = flow_boost_step(None, 0, ["Rum A"])
    assert b["k"] == 1 and b["rooms"] == ["Rum A"]
    b = flow_boost_step(b, 5 * 3600, ["Rum A"])
    assert b["k"] == 1  # ikke før der er gået 6 timer
    for i in range(1, 10):
        b = flow_boost_step(b, i * FLOW_BOOST_STEP_S, ["Rum A"])
    assert b["k"] == FLOW_BOOST_MAX
    t = 10 * FLOW_BOOST_STEP_S
    b = flow_boost_step(b, t, [])
    assert b["k"] == FLOW_BOOST_MAX - 1 and not b["rooms"]
    b = flow_boost_step(b, t + 3 * 3600, [])
    assert b["k"] == FLOW_BOOST_MAX - 1


def test_plan_raises_flow_limit_when_forecast_says_rooms_drop() -> None:
    """Forudsigende: kommer et rum under grænsen, fordi fremløbsgrænsen holder varmen nede, hæves grænsen i planen."""
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=26.0, flow_slope=-0.6)
    # 10 timers blokering ved −3 °C: med fremløbet på den lærte linje forudsiges rummet under grænsen sidst i blokeringen
    hs = hours(20, t_out=lambda i: -3.0, blocked=lambda i: 6 <= i <= 15)
    room = PlanRoom(name="Rum A", t_now=20.3, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)
    p = make_plan(hs, m, [room], 20.0, flow_margin=0.0)
    assert p.flow_raise == 2 and p.deficit <= 0.05 and "fremløbsgrænsen hævet 2 °C" in p.reason
    assert max(x["flow"] or 0 for x in p.hours) > 26.0 + 0.6 * 3 + 0.25
    # Kan en hævning ikke rette det (rummet er allerede under gulvet nu), hæves der ikke forgæves
    now_cold = PlanRoom(name="Rum A", t_now=19.0, t_min=20.0, heat_now=0.03, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)
    assert make_plan(hours(12, t_out=lambda i: 1.0), m, [now_cold], 20.0, flow_margin=0.0).flow_raise == 0
    # Holder rummene uden: ingen hævning
    ok = make_plan(hours(12, t_out=lambda i: 1.0), m, [], 20.0, flow_margin=0.0)
    assert ok.flow_raise == 0


def test_plan_raises_flow_when_one_room_gains_although_weakest_does_not() -> None:
    """Et rum med en svag kreds (allerede under gulvet, hævningen når det knap) må ikke spærre for de andre rum."""
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=26.0, flow_slope=-0.6)
    hs = hours(20, t_out=lambda i: -3.0, blocked=lambda i: 6 <= i <= 15)
    good = PlanRoom(name="Rum A", t_now=20.3, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)
    weak = PlanRoom(name="Rum B", t_now=19.0, t_min=20.0, heat_now=0.002, heat_delayed=0.002, cool_per_hk=0.006, margin=0.2)
    alone = make_plan(hs, m, [weak], 20.0, flow_margin=0.0)
    assert alone.flow_raise == 0  # alene: hævningen hjælper ikke mærkbart
    p = make_plan(hs, m, [good, weak], 20.0, flow_margin=0.0)
    # +2 °C får Rum A til at holde; flere grader giver kun Rum B ca. 0,01 °C pr. grad – dem betaler planen ikke for
    assert p.flow_raise == 2 and "rummene holder ikke helt, men det hjælper (Rum A" in p.reason
    u = {x["rum"]: x["under"] for x in p.problems}
    assert "Rum A" not in u and u["Rum B"] > 0.5


def test_trims_heat_when_rooms_are_warm() -> None:
    """Husets behov er kun udgangspunktet: er rummene varme nok, skæres varmen ned (lavere effekt og fremløb)."""
    hs = hours(12, t_out=lambda i: 2.0)
    warm = PlanRoom(name="Rum A", t_now=21.5, t_min=19.5, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.3)
    cold = PlanRoom(name="Rum A", t_now=19.8, t_min=19.5, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.3)
    pw = make_plan(hs, M, [warm], 20.0)
    pc = make_plan(hs, M, [cold], 20.0)
    assert pw.trimmed_kwh > 0 and pw.heat_kwh < pc.heat_kwh and "mindre end husets behov" in pw.reason
    from custom_components.varmemodel.planner import simulate_rooms
    sim = simulate_rooms([warm], hs, [x["heat_kw"] for x in pw.hours])["Rum A"]
    assert min(sim) >= 19.5 + 0.3 - 0.01
    assert make_plan(hs, M, [], 20.0).trimmed_kwh == 0  # uden rummodeller skæres intet


def test_air_plan_red_allowance_saves_heat_before_block() -> None:
    """Brugerens økonomi-valg: må rummet gå 1 °C under gulvet i røde timer, købes der mindre varme før blokeringen."""
    from custom_components.varmemodel.planner import make_air_plan

    hs = hours(24, price=lambda i: 2.0, t_out=lambda i: 2.0, blocked=lambda i: 14 <= i <= 20)
    strict = make_air_plan(hs, [_air_room()], e_min=0.3, e_max=1.0)
    loose = make_air_plan(hs, [_air_room()], e_min=0.3, e_max=1.0, red_allowance=1.0)
    assert loose.el_kwh < strict.el_kwh
    assert all(x["heat_kw"] == 0 for x in loose.hours if x["blocked"])



def test_comfort_is_a_band_not_a_target() -> None:
    """Komfort er et bånd: kravet er gulv + usikkerhed (voksende til 6 t) – komfort får ikke planen til at købe mere varme."""
    from custom_components.varmemodel.planner import make_air_plan

    hs = hours(24, price=lambda i: 2.0, t_out=lambda i: 2.0)
    a = make_air_plan(hs, [_air_room()], e_min=0.3, e_max=1.0, comfort=0.0)
    b = make_air_plan(hs, [_air_room()], e_min=0.3, e_max=1.0, comfort=1.0)
    assert a.el_kwh == b.el_kwh
    room = PlanRoom(name="Stue", t_now=20.3, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.3)
    p0 = make_plan(hours(), M, [room], 20.5, comfort=0.0)
    p1 = make_plan(hours(), M, [room], 20.5, comfort=1.0)
    assert p0.heat_kwh == p1.heat_kwh


def test_margin_grows_with_lead_time() -> None:
    """Usikkerheden er 0 i timen nu og fuld efter 6 timer (den horisont præcisionen er målt over)."""
    from custom_components.varmemodel.planner import _mg

    r = PlanRoom(name="x", t_now=20, t_min=20, heat_now=0, heat_delayed=0, cool_per_hk=0, margin=0.6)
    assert abs(_mg(r, 0) - 0.1) < 1e-9 and abs(_mg(r, 2) - 0.3) < 1e-9 and _mg(r, 5) == 0.6 and _mg(r, 20) == 0.6


def test_closed_valves_send_heat_to_open_rooms() -> None:
    """Termostater: et rum over sin lukketemperatur får ingen varme; de åbne rum får dets andel (målt andel)."""
    from custom_components.varmemodel.planner import simulate_rooms

    hs = hours(6, t_out=lambda i: 2.0)
    warm = PlanRoom(name="Varm", t_now=21.0, t_min=20.0, heat_now=0.05, heat_delayed=0.0, cool_per_hk=0.0, share=0.5, t_close=20.5)
    cold = PlanRoom(name="Kold", t_now=19.8, t_min=20.0, heat_now=0.05, heat_delayed=0.0, cool_per_hk=0.0, share=0.5, t_close=20.5)
    q = [2.0] * 6
    sim = simulate_rooms([warm, cold], hs, q)
    assert sim["Varm"][0] == 21.0  # lukket: ingen varme
    assert abs(sim["Kold"][0] - (19.8 + 0.05 * 2.0 * 2)) < 1e-6  # fik også den lukkede kreds' andel
    fixed = simulate_rooms([PlanRoom(name="Kold", t_now=19.8, t_min=20.0, heat_now=0.05, heat_delayed=0.0, cool_per_hk=0.0)], hs, q)
    assert sim["Kold"][0] > fixed["Kold"][0]


def test_valves_let_plan_reach_weak_room_without_overheating() -> None:
    """Et rum med lille andel løftes ved, at de varme rum lukker deres kredse: kravet nås med næsten samme varme,
    hvor faste andele ikke kan nå det (de varmer bare de andre rum op)."""
    hs = hours(24, t_out=lambda i: 8.0)

    def rooms(valves: bool):
        mk = lambda n, t, hn, sh: PlanRoom(name=n, t_now=t, t_min=20.0, heat_now=hn, heat_delayed=0.0, cool_per_hk=0.003, margin=0.2,
                                           share=sh if valves else None, t_close=20.5 if valves else None)
        return [mk("Svag", 19.9, 0.015, 0.25), mk("Stærk", 20.8, 0.04, 0.75)]
    with_valves = make_plan(hs, M, rooms(True), 20.0)
    without = make_plan(hs, M, rooms(False), 20.0)
    assert with_valves.deficit < 0.05 < without.deficit
    assert with_valves.heat_kwh <= without.heat_kwh * 1.15


def test_economy_knob_buys_less_heat_at_lower_flow() -> None:
    """Økonomi-knappen: accepterer rummene at ligge lidt under gulvet, køber planen mindre varme ved lavere fremløb."""
    hs = hours(24, t_out=lambda i: 2.0)
    room = PlanRoom(name="Stue", t_now=20.2, t_min=20.0, heat_now=0.06, heat_delayed=0.02, cool_per_hk=0.004, margin=0.2)
    base = make_plan(hs, M, [room], 20.0)
    eco = make_plan(hs, M, [room], 19.5, economy=0.5)
    assert eco.heat_kwh < base.heat_kwh
    assert max(x["flow"] or 0 for x in eco.hours) <= max(x["flow"] or 0 for x in base.hours)


def test_air_plan_spreads_when_high_steps_are_less_efficient() -> None:
    """Med lært virkning pr. trin (høje trin giver mindre varme pr. kWh el) spreder planen varmen over flere timer på
    lave trin i stedet for at køre de højeste trin i de billigste timer – og bruger mindre el."""
    from custom_components.varmemodel.planner import make_air_plan

    hs = hours(n=12, price=lambda i: 1.0 + 0.02 * i, t_out=lambda i: 5.0)
    base = make_air_plan(hs, [_air_room(t_now=19.5, t_min=19.5, cool=0.02)], e_min=0.3, e_max=1.5)
    spread = make_air_plan(hs, [_air_room(t_now=19.5, t_min=19.5, cool=0.02)], e_min=0.3, e_max=1.5,
                           eff_curve=[(0.3, 2.0), (0.8, 1.6), (1.5, 0.7)])
    assert max(x["el_kw"] for x in spread.hours) < max(x["el_kw"] for x in base.hours)
    assert sum(1 for x in spread.hours if x["el_kw"] > 0) > sum(1 for x in base.hours if x["el_kw"] > 0)
    assert spread.el_kwh < base.el_kwh


def test_preload_window_shows_red_with_quarters() -> None:
    """Rød med kvarterer: planen regner i hele timer (blokeret 9–17), men vinduet viser den røde periodes præcise start
    og slut (08:30–17:15), når de røde perioder kendes."""
    hs = hours(24, price=lambda i: 1.0 if i >= 18 else 2.0, t_out=lambda i: 3.0, blocked=lambda i: 9 <= i <= 16)
    room = PlanRoom(name="Stue", t_now=20.4, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)

    def lab(ts):
        return f"kl. {ts // 3600:02d}" + (f":{ts // 60 % 60:02d}" if ts % 3600 else "")

    p = make_plan(hs, M, [room], 20.0, preload=0.3, label=lab, red_spans=[(8 * 3600 + 1800, 17 * 3600 + 900), (20 * 3600, 21 * 3600)])
    w = p.preload
    # opvarmningen slutter, når det røde begynder (08:30) – ikke ved timens slut
    assert w and w["to"] == 8 * 3600 + 1800 and w["block_from"] == 8 * 3600 + 1800 and w["block_to"] == 17 * 3600 + 900
    assert "før blokeringen 08:30–17:15" in p.reason


def test_plan_problems_structured() -> None:
    """Rum under grænsen står også i en struktureret liste (til besked forud): laveste temperatur og tid, grænse, hvor meget
    under og første time under – samme rum som advarslerne."""
    hs = hours(24, t_out=lambda i: 8.0)
    mk = lambda n, t, hn, sh: PlanRoom(name=n, t_now=t, t_min=20.0, heat_now=hn, heat_delayed=0.0, cool_per_hk=0.003, margin=0.2,  # noqa: E731
                                       share=sh, t_close=None)
    p = make_plan(hs, M, [mk("Svag", 19.9, 0.015, None), mk("Stærk", 20.8, 0.04, None)], 20.0)
    assert p.problems and {x["rum"] for x in p.problems} == {w.split(":")[0] for w in p.warnings if "under planens grænse" in w}
    x = p.problems[0]
    assert x["under"] > 0 and x["temp"] < x["grænse"] and x["fra_ts"] <= x["ts"] and isinstance(x["blokeret"], bool)


def test_plan_input_dump_roundtrip_is_anonymous_and_replayable() -> None:
    """Planens input kan gemmes uden navne og tider og genkøres til samme plan – også med et ændret argument."""
    import json

    from custom_components.varmemodel.planner import plan_from_dump, plan_input_dump

    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=26.0, flow_slope=-0.6)
    hs = hours(20, t_out=lambda i: -3.0, blocked=lambda i: 6 <= i <= 15)
    for h in hs:
        h.ts += 1_795_000_000  # rigtige tidsstempler
    room = PlanRoom(name="Stuen hos familien", t_now=20.3, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2,
                    neighbours=[(0.01, 19.0)])
    kw = {"past_q": [0.0] * 6, "flow_margin": 0.0, "red_spans": [(hs[6].ts, hs[16].ts)], "comfort": 1.0}
    p = make_plan(hs, m, [room], 20.0, 0, **kw)
    d = json.loads(json.dumps(plan_input_dump(hs, m, [room], 20.0, max_pause=0, **kw)))
    txt = json.dumps(d)
    assert "familien" not in txt and "1795" not in txt and d["rooms"][0]["name"] == "rum_1" and d["hours"][6]["blocked"]
    q = plan_from_dump(d)
    assert (q.heat_kwh, q.deficit, q.flow_raise) == (p.heat_kwh, p.deficit, p.flow_raise)
    assert [x["heat_kw"] for x in q.hours] == [x["heat_kw"] for x in p.hours]
    assert q.rooms["rum_1"] == p.rooms["Stuen hos familien"]
    assert plan_from_dump(d, flow_raise_max=0).flow_raise == 0


def test_plan_raises_from_a_low_start_through_the_dead_zone() -> None:
    """Ligger grænsen langt under det fremløb, varmepumpens laveste ydelse alligevel giver (søgningen er lige startet lavt),
    ændrer de første grader intet i planen. De tæller ikke med i kravet – planen forvarmer stadig, når den må hæve nok."""
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=26.0, flow_slope=-0.6)
    hs = hours(20, t_out=lambda i: -3.0, blocked=lambda i: 6 <= i <= 15)
    room = PlanRoom(name="Rum A", t_now=20.3, t_min=20.0, heat_now=0.08, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)
    ref = make_plan(hs, m, [room], 20.0, flow_margin=0.0)  # hæver 2 °C fra den lærte linje
    low = make_plan(hs, m, [room], 20.0, flow_margin=-10.0, flow_raise_max=12)  # samme sted, startet 10 °C lavere
    assert ref.flow_raise == 2 and low.flow_raise == 12 and low.deficit <= 0.05
    assert [x["heat_kw"] for x in low.hours] == [x["heat_kw"] for x in ref.hours]
    # Må den ikke hæve nok til at komme ud af den døde zone, ændrer den intet
    assert make_plan(hs, m, [room], 20.0, flow_margin=-10.0, flow_raise_max=5).flow_raise == 0


def _lab(ts: int) -> str:
    return f"kl. {ts // 3600:02d}" + (f":{ts // 60 % 60:02d}" if ts % 3600 else "")


def _before_block(partial: bool, t_out: float = -8.0, price=lambda i: 2.0) -> list[PlanHour]:
    """Blokeret kl. 6–20; med ``partial`` er time 6 delvist fri (rødt først fra 06:30)."""
    out = hours(24, t_out=lambda i: t_out, blocked=lambda i: 6 <= i <= 20, price=price)
    if partial:
        out[6] = PlanHour(ts=6 * 3600, price=price(6), t_out=t_out, blocked=False, free=0.5)
    return out


def test_partial_hour_continues_the_run_up_to_the_block() -> None:
    """Blokeringen begynder midt i en time (de frie kvarterer først): planen bruger den frie halvdel som fortsættelse af
    timen før – halv mængde ved samme effekt og fremløb – i stedet for at stoppe en halv time for tidligt. Vinduet viser,
    at opvarmningen slutter, når det røde begynder."""
    room = PlanRoom(name="Stue", t_now=19.6, t_min=20.0, heat_now=0.02, heat_delayed=0.03, cool_per_hk=0.004, margin=0.2)
    spans = [(6 * 3600 + 1800, 21 * 3600)]
    old = make_plan(_before_block(False), M, [room], 20.0, past_q=[4.0] * 6, red_spans=spans, label=_lab)
    new = make_plan(_before_block(True), M, [room], 20.0, past_q=[4.0] * 6, red_spans=spans, label=_lab)
    assert old.hours[6]["heat_kw"] == 0 and old.hours[6]["blocked"]
    h5, h6 = new.hours[5], new.hours[6]
    assert h5["heat_kw"] == M.p_max and h6["heat_kw"] == M.p_max / 2 and not h6["blocked"] and h6["free"] == 0.5
    assert h6["flow"] == h5["flow"] and h6["cop"] == h5["cop"]  # samme effekt, mens der varmes
    assert abs(h6["cost"] - h5["cost"] / 2) < 0.02
    assert "free" not in h5
    assert [x["heat_kw"] for x in new.hours[:6]] == [x["heat_kw"] for x in old.hours[:6]] and new.heat_kwh >= old.heat_kwh + 2.9
    assert new.preload["to"] == 6 * 3600 + 1800 and "opvarmer kl. 00–06:30 før blokeringen 06:30–21" in new.reason
    # også uden de røde perioder kendes blokeringens start fra timens frie del
    bare = make_plan(_before_block(True), M, [room], 20.0, past_q=[4.0] * 6, label=_lab)
    assert bare.preload["to"] == bare.preload["block_from"] == 6 * 3600 + 1800
    assert new.rooms["Stue"][20] > old.rooms["Stue"][20] + 0.1  # varmere sidst i blokeringen


def test_partial_hour_is_never_used_alone() -> None:
    """Den frie del af en delvist fri time bruges kun sammen med nabotimen på den frie side – ellers ville varmepumpen
    starte for at blive stoppet af blokeringen kort efter. Gælder begge ender af en blokering og planens første time."""
    cheap = lambda *on: (lambda i: 0.5 if i in on else 2.0)  # noqa: E731
    # før blokeringen: time 6 er billigst, men time 5 er dyr og varmer ikke
    p = make_plan(_before_block(True, 8.0, lambda i: 0.5 if i == 6 else (5.0 if i == 5 else 2.0)), M, [], 20.5)
    assert p.hours[5]["heat_kw"] == 0 and p.hours[6]["heat_kw"] == 0 and abs(p.heat_kwh - p.demand_kwh) < 2.5
    # en ubrugt delvist fri time hører til blokeringen: opvarmningen før vises stadig (til timens start)
    p = make_plan(_before_block(True, 8.0, lambda i: 5.0 if i == 6 else (0.5 if i == 5 else 2.0)), M, [], 20.5,
                  red_spans=[(6 * 3600 + 1800, 21 * 3600)], label=_lab)
    assert p.hours[5]["heat_kw"] > 0 and p.hours[6]["heat_kw"] == 0
    assert p.preload["to"] == 6 * 3600 and p.preload["block_from"] == 6 * 3600 + 1800 and "–06 før blokeringen 06:30–21" in p.reason
    p = make_plan(_before_block(True, 8.0, cheap(5, 6)), M, [], 20.5)
    assert p.hours[5]["heat_kw"] > 0 and 0 < p.hours[6]["heat_kw"] <= M.p_max / 2

    # efter blokeringen: de frie kvarterer ligger sidst i time 21 og fortsætter ind i time 22
    def after(price) -> list[PlanHour]:
        out = hours(30, blocked=lambda i: 9 <= i <= 21, price=price)
        out[21] = PlanHour(ts=21 * 3600, price=price(21), t_out=3.0, blocked=False, free=0.5, free_end=True)
        return out

    p = make_plan(after(cheap(21, 22)), M, [], 20.5)
    assert p.hours[22]["heat_kw"] > 0 and 0 < p.hours[21]["heat_kw"] <= M.p_max / 2
    p = make_plan(after(lambda i: 0.5 if i == 21 else (6.0 if i == 22 else 2.0)), M, [], 20.5)
    assert p.hours[22]["heat_kw"] == 0 and p.hours[21]["heat_kw"] == 0

    # planens første time: kun når varmen kørte i timen før (målt varme)
    def first() -> list[PlanHour]:
        out = hours(24, blocked=lambda i: 1 <= i <= 8, price=cheap(0))
        out[0] = PlanHour(ts=0, price=0.5, t_out=3.0, blocked=False, free=0.5)
        return out

    assert make_plan(first(), M, [], 20.5, past_q=[0, 0, 0, 0, 3.0, 3.0]).hours[0]["heat_kw"] > 0
    assert make_plan(first(), M, [], 20.5, past_q=[3.0, 3.0, 0, 0, 0, 0]).hours[0]["heat_kw"] == 0
    assert make_plan(first(), M, [], 20.5).hours[0]["heat_kw"] == 0
    # … eller når styringen ved, om kilden kører lige nu (går forud for den målte varme)
    assert make_plan(first(), M, [], 20.5, running=True).hours[0]["heat_kw"] > 0
    assert make_plan(first(), M, [], 20.5, past_q=[0, 0, 0, 0, 3.0, 3.0], running=False).hours[0]["heat_kw"] == 0


def test_partial_hour_rules_hold_when_heat_is_trimmed() -> None:
    """Også når overskud skæres væk (varme rum) og pauser brydes: en delvist fri time har aldrig varme uden nabotimen, højst
    sin del af effekten og mindst sin del af den laveste ydelse."""
    for t_now in (19.8, 20.3, 20.8, 21.5):
        for k, price in enumerate((lambda i: 2.0, lambda i: 1.0 + (i % 7) * 0.3, lambda i: 3.0 - (i % 5) * 0.4, lambda i: 0.5 if i == 6 else 2.0)):
            for t_out in (-6.0, 2.0, 9.0):
                room = PlanRoom(name="Stue", t_now=t_now, t_min=20.0, heat_now=0.05, heat_delayed=0.05, cool_per_hk=0.006, margin=0.2)
                p = make_plan(_before_block(True, t_out, price), M, [room], 20.5, max_pause=3 if k % 2 else 0)
                h5, h6 = p.hours[5]["heat_kw"], p.hours[6]["heat_kw"]
                assert h6 == 0 or (h5 > 0 and M.p_min / 2 - 1e-9 <= h6 <= M.p_max / 2 + 1e-9), (t_now, k, t_out, h5, h6)
                assert all(x["heat_kw"] == 0 for x in p.hours if x["blocked"])


def test_plan_input_dump_keeps_partial_hours() -> None:
    """Diagnostikkens plan-input har de delvist frie timer med, så genkørslen giver samme plan."""
    from custom_components.varmemodel.planner import plan_from_dump, plan_input_dump

    hs = _before_block(True)
    room = PlanRoom(name="Stue", t_now=19.6, t_min=20.0, heat_now=0.02, heat_delayed=0.03, cool_per_hk=0.004, margin=0.2)
    d = plan_input_dump(hs, M, [room], 20.0, past_q=[4.0] * 6, max_pause=0)
    assert d["hours"][6]["free"] == 0.5 and d["hours"][6]["free_end"] is False and "free" not in d["hours"][5]
    a, b = make_plan(hs, M, [room], 20.0, past_q=[4.0] * 6), plan_from_dump(d)
    assert [x["heat_kw"] for x in a.hours] == [x["heat_kw"] for x in b.hours] and b.hours[6]["heat_kw"] == 3.0


def _fake_plans(rows: list[dict[str, tuple[float, float, int]]], costs: list[float] | None = None):
    """Planer med kendte mankoer: pr. plan {rum: (største manko, gradtimer, timer under)} – til at afprøve valget alene."""
    from custom_components.varmemodel.planner import Plan

    out = []
    for i, x in enumerate(rows):
        p = Plan(cost_kr=(costs[i] if costs else 40.0 + 4 * i), heat_kwh=30.0 + 2 * i, el_kwh=10.0 + i)
        p.problems = [{"rum": r, "ts": 21 * 3600, "temp": round(20.0 - u, 1), "grænse": 20.0, "under": u, "fra_ts": 0, "blokeret": False,
                       "gh": gh, "timer": n} for r, (u, gh, n) in x.items() if u > 0]
        p.deficit = max((u for u, _gh, _n in x.values()), default=0.0)
        out.append(p)
    return out


def test_priority_buys_a_share_of_what_can_be_gained() -> None:
    """Prioritet uden prisgrænser: for et rum, der ikke kan holde sin grænse gennem en blokering, køber økonomi intet,
    balance halvdelen og komfort det hele af det, et højere fremløb kan hente – målt som gennemsnitlig manko i de timer,
    rummet er under (dybde og varighed), og kun når det er mere end følernes 0,1 °C."""
    from custom_components.varmemodel.planner import raise_choices

    # Stue: 9 timer under; gennemsnitlig manko 0,28 → 0,18 → 0,11 → 0,09 → 0,07 → 0,06 (tal fra et rigtigt hus 25/11-2026)
    stue = [(0.38, 2.48, 9), (0.34, 1.65, 9), (0.33, 1.00, 9), (0.32, 0.83, 9), (0.31, 0.64, 9), (0.28, 0.51, 9)]
    # Gang: svag kreds – hævningerne flytter den næsten ikke (0,37 → 0,30 i snit over 7 timer er under 0,1 °C)
    gang = [(0.54, 2.58, 7), (0.54, 2.51, 7), (0.53, 2.40, 7), (0.53, 2.32, 7), (0.53, 2.15, 7), (0.52, 2.07, 7)]
    plans = _fake_plans([{"Stue": s, "Gang": g} for s, g in zip(stue, gang)])
    assert raise_choices(plans) == {"economy": 0, "balance": 1, "comfort": 3}
    # Gang alene: intet, følerne kan måle – ingen hævning på noget trin
    assert raise_choices(_fake_plans([{"Gang": g} for g in gang])) == {"economy": 0, "balance": 0, "comfort": 0}
    # Kun én hævning at vælge: komfort tager den, når den flytter rummet mere end opløsningen
    assert raise_choices(plans[:2])["comfort"] == 0 and raise_choices(plans[:3]) == {"economy": 0, "balance": 1, "comfort": 2}


def test_priority_always_saves_a_room_that_can_hold_its_limit() -> None:
    """Et rum, en hævning kan bringe helt op til grænsen, bringes derop på alle trin – også økonomi (grænsen er brugerens
    krav). Et andet rum, der ikke kan reddes, får derudover sin andel efter trinnet."""
    from custom_components.varmemodel.planner import raise_choices

    a = [(0.30, 0.9, 4), (0.12, 0.3, 4), (0.0, 0.0, 0), (0.0, 0.0, 0), (0.0, 0.0, 0)]  # holder fra +2 °C
    b = [(0.80, 6.4, 8), (0.75, 5.6, 8), (0.70, 4.8, 8), (0.60, 3.2, 8), (0.50, 1.6, 8)]  # kan ikke holde: 0,8 → 0,2 i snit
    assert raise_choices(_fake_plans([{"A": x} for x in a])) == {"economy": 2, "balance": 2, "comfort": 2}
    assert raise_choices(_fake_plans([{"A": x, "B": y} for x, y in zip(a, b)])) == {"economy": 2, "balance": 3, "comfort": 4}


def test_priority_never_picks_a_raise_that_makes_the_house_colder() -> None:
    """En hævning, der gør det værste rum koldere end uden hævning (planlæggerens støj), vælges aldrig – heller ikke når et
    andet rum vinder på den. De første ``free`` grader er almindelig drift op til loftet: dem bruger alle trin."""
    from custom_components.varmemodel.planner import raise_choices

    stue = [(0.50, 4.0, 8), (0.45, 3.2, 8), (0.40, 2.4, 8), (0.20, 0.8, 8)]
    kold = [(0.60, 1.2, 2), (0.60, 1.2, 2), (0.60, 1.2, 2), (0.75, 1.5, 2)]  # +3 °C gør det værste rum 0,15 °C koldere
    plans = _fake_plans([{"Stue": s, "Kold": k} for s, k in zip(stue, kold)])
    assert raise_choices(plans) == {"economy": 0, "balance": 1, "comfort": 2}
    # Loftet ligger 2 °C over planens udgangspunkt (søgningen står lavt): også økonomi går derop, når det hjælper
    assert raise_choices(plans, free=2) == {"economy": 2, "balance": 2, "comfort": 2}
    assert raise_choices(plans, free=1) == {"economy": 1, "balance": 1, "comfort": 2}


def test_priority_in_the_plan() -> None:
    """Hele planen: rummet kan ikke holde sin grænse gennem 14 timers blokering uanset fremløb. Økonomi lader fremløbet stå,
    balance og komfort hæver – komfort mindst lige så meget som balance – og de tre valg står i planen med pris og virkning."""
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=2, emission_kw_k=0.283, flow_c0=30.0, flow_slope=-0.3)
    hs = hours(26, price=lambda i: 2.0, t_out=lambda i: 0.0, blocked=lambda i: 9 <= i <= 22)
    room = PlanRoom(name="Rum A", t_now=19.6, t_min=20.0, heat_now=0.04, heat_delayed=0.04, cool_per_hk=0.0067, margin=0.2)
    kw = {"flow_margin": 0.0, "red_allowance": 1.0, "flow_raise_max": 10}
    eco, bal, com = (make_plan(hs, m, [room], 20.5, priority=x, **kw) for x in ("economy", "balance", "comfort"))
    assert eco.flow_raise == 0 and 0 < bal.flow_raise <= com.flow_raise
    assert eco.cost_kr < bal.cost_kr <= com.cost_kr and eco.heat_kwh < com.heat_kwh
    assert [c["trin"] for c in com.choices] == ["economy", "balance", "comfort"]
    assert [c["hævet"] for c in com.choices] == [0, int(bal.flow_raise), int(com.flow_raise)] and com.choices == eco.choices
    assert com.choices[0]["kr"] == eco.cost_kr and com.choices[2]["kr"] == com.cost_kr and com.choices[0]["rum"] == "Rum A"
    assert com.choices[2]["timer"] <= com.choices[0]["timer"] and com.choices[2]["snit"] < com.choices[1]["snit"] <= com.choices[0]["snit"]
    assert "det hjælper (Rum A i snit 0." in com.reason
    assert f"fremløbsgrænsen hævet {com.flow_raise:.0f} °C (komfort, +" in com.reason and "fremløbsgrænsen hævet" not in eco.reason
    # Uden trin (ældre kald) vælges som økonomi; holder rummene, er der intet at vælge imellem
    assert make_plan(hs, m, [room], 20.5, **kw).flow_raise == 0
    warm = PlanRoom(name="Rum A", t_now=21.5, t_min=19.0, heat_now=0.04, heat_delayed=0.04, cool_per_hk=0.0067, margin=0.2)
    assert make_plan(hs, m, [warm], 20.5, priority="comfort", **kw).choices == []


def test_hot_water_takes_its_share_of_the_hour() -> None:
    """Varmt vand i en time (``busy``): timen rummer tilsvarende mindre rumvarme – ved samme effekt, i kortere tid – og
    varmen, der mangler, lægges i andre timer. Uden ``busy`` er planen uændret."""
    hs = hours(n=12, price=lambda i: 1.0 if i == 2 else 3.0, t_out=lambda i: -6.0, blocked=lambda i: i >= 6)
    base = make_plan(hs, M, [], 20.5)
    assert base.hours[2]["heat_kw"] > 0.7 * M.p_max  # den billige time bruges fuldt
    half = make_plan(hs, M, [], 20.5, busy=[0.5 if i == 2 else 0.0 for i in range(12)])
    h2 = half.hours[2]
    assert h2["heat_kw"] <= 0.5 * M.p_max + 1e-6 and h2["dhw"] == 0.5
    # effekten, mens der varmes, er den samme som før – det er tiden, der er kortere
    assert h2["heat_kw"] > 0 and abs(half.heat_kwh - base.heat_kwh) < 1.0
    assert sum(x["heat_kw"] for i, x in enumerate(half.hours) if i != 2) > sum(x["heat_kw"] for i, x in enumerate(base.hours) if i != 2)
    assert "dhw" not in base.hours[2] and all("dhw" not in x for i, x in enumerate(half.hours) if i != 2)
    same = make_plan(hs, M, [], 20.5, busy=[0.0] * 12)
    assert [x["heat_kw"] for x in same.hours] == [x["heat_kw"] for x in base.hours]


def test_frost_price_makes_cold_hours_hold_less_and_cost_more() -> None:
    """Rimens pris: i rimvejr får huset mindre af den målte varme (afrimningerne tager varme fra gulvvandet). Timen rummer
    tilsvarende mindre og koster mere pr. kWh, huset får; i mildt vejr omvendt. Uden lærte tal er planen uændret."""
    from dataclasses import replace

    from custom_components.varmemodel.planner import frost_factor

    m = replace(M, frost=((-1.0, 0.94), (1.5, 1.0), (8.0, 1.04)))
    assert frost_factor(M, -5.0) == 1.0 and frost_factor(m, -5.0) == 0.94 and frost_factor(m, 20.0) == 1.04
    assert abs(frost_factor(m, 0.25) - 0.97) < 1e-9 and frost_factor(m, 1.5) == 1.0
    hs = hours(n=12, t_out=lambda i: -1.0, blocked=lambda i: i >= 4)  # få frie timer i frost: der køres fuldt
    base, frost = make_plan(hs, M, [], 20.5), make_plan(hs, m, [], 20.5)
    full = [i for i, x in enumerate(base.hours) if x["heat_kw"] >= M.p_max - 1e-6]
    assert full and all(abs(frost.hours[i]["heat_kw"] - M.p_max * 0.94) < 0.06 for i in full)  # timen rummer 6 % mindre
    assert all(x["frost"] == 0.94 for x in frost.hours if x["heat_kw"]) and all("frost" not in x for x in base.hours)
    # samme varme til huset koster mere el
    i = full[0]
    assert frost.hours[i]["cop"] < base.hours[i]["cop"] * 0.95 and frost.hours[i]["cost"] / frost.hours[i]["heat_kw"] > base.hours[i]["cost"] / base.hours[i]["heat_kw"] * 1.05
    # mildt vejr: timen giver mere end gennemsnittet
    mild = make_plan(hours(n=12, t_out=lambda i: 8.0), m, [], 20.5)
    ref = make_plan(hours(n=12, t_out=lambda i: 8.0), M, [], 20.5)
    on = next(i for i, x in enumerate(ref.hours) if x["heat_kw"])
    assert mild.hours[on]["cop"] > ref.hours[on]["cop"] * 1.03 and mild.el_kwh <= ref.el_kwh and mild.hours[on]["frost"] == 1.04
