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
    assert w and w["to"] == 9 * 3600 and w["block_from"] == 8 * 3600 + 1800 and w["block_to"] == 17 * 3600 + 900
    assert "før blokeringen 08:30–17:15" in p.reason
