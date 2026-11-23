"""Planlægger: hvornår og hvor meget en vandbåren varmekilde skal varme de næste timer.

Rene funktioner uden Home Assistant. Alle tal kommer fra kalibreringen:

1. Behov pr. time = (H·(mål − ude) − G) / 1000 kWh  (husets varmetab H, gratis varme G).
2. Varmen fordeles som "vandfyldning": hver portion lægges i den time, hvor den
   koster mindst el (pris ÷ COP). COP = a + b·ude + c·fremløb + d·effekt, så høj
   effekt i én time bliver dyrere end jævn effekt i flere – det giver lav og lang
   drift, når prisen tillader det. Blokerede timer (fx rød pris) bruges aldrig.
   Første portion i en time er mindst den laveste effekt, varmepumpen kan holde.
3. Hvert rum forudsiges time for time med sin rummodel. Kommer et rum under
   sin min. + usikkerhed, lægges der varme i den billigste time før.
4. Pauser længere end den valgte grænse brydes med den billigste time i pausen.
5. Fremløb = mål + effekt ÷ gulvets afgivelse; kurve-forskydning = fremløb − kurven.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanHour:
    ts: int  # timens start, unix-sekunder (UTC)
    price: float  # kr pr. kWh el
    t_out: float  # forventet udetemperatur
    blocked: bool = False  # fx rød pris: ingen varme


@dataclass
class PlanRoom:
    name: str
    t_now: float
    t_min: float
    heat_now: float  # °C pr. kWh i samme time (fra rummodellen, 6-timers blokke)
    heat_delayed: float  # °C pr. kWh 6 timer senere (gulvets forsinkelse)
    cool_per_hk: float  # °C pr. time pr. grad (ude − rum)
    free_per_h: float = 0.0  # gratis varme °C pr. time
    margin: float = 0.3  # usikkerhed (°C) – rummet skal holdes over min + margin
    accuracy: float = 0.0  # rummodellens ærlige præcision (°C) uden loft – tjekkets afvigelsesgrænse
    neighbours: list[tuple[float, float]] = field(default_factory=list)  # (°C pr. time pr. grad, naboens temperatur)
    held: bool = False  # temperaturen er ikke målt nu: den sidst kendte eller forudsagt (føleren er væk)
    estimated: bool = False  # føleren har været væk i over 30 min.: temperaturen er forudsagt fra sidste måling
    t_max: float | None = None  # loft: planen varmer aldrig så meget, at rummet kommer over (luft-luft)


@dataclass
class SourceModel:
    heat_loss_w_k: float  # H
    gains_w: float = 0.0  # G
    p_max: float = 6.0  # kW varme
    p_min: float = 2.0  # kW varme – laveste stabile
    cop: tuple[float, float, float, float] = (3.0, 0.15, -0.04, 0.0)  # a, b, c, d
    emission_kw_k: float = 0.25  # gulvets afgivelse
    curve_c0: float = 34.0  # kurven ved 0 °C uden forskydning
    curve_slope: float = -0.6
    shift_min: int = -5
    shift_max: int = 5


@dataclass
class Plan:
    hours: list[dict[str, Any]] = field(default_factory=list)
    rooms: dict[str, list[float]] = field(default_factory=dict)
    demand_kwh: float = 0.0
    heat_kwh: float = 0.0
    el_kwh: float = 0.0
    cost_kr: float = 0.0
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    preload: dict[str, Any] | None = None  # første forvarmning: fra/til, blokeringen den dækker og mængden


STEP = 0.25  # kWh pr. portion
PRELOAD_MAX = 1.0  # forvarmning: højst så mange °C ekstra krav sidst i en lang blokering
AIR_STEP = 0.1  # kWh el pr. portion for luft-luft
FLOW_MIN, FLOW_MAX = 25.0, 45.0  # fremløbsmål, varmepumpen kan sætte


def flow_for(m: SourceModel, p: float, target: float) -> float:
    """Fremløbsmålet for p kW varme: rummål + p ÷ gulvets afgivelse, begrænset og rundet til 0,5 °C."""
    f = target + (p / m.emission_kw_k if m.emission_kw_k > 0 else 0.0)
    return round(2 * max(FLOW_MIN, min(FLOW_MAX, f))) / 2


def _next_block(hours: list[PlanHour]) -> list[int | None]:
    """For hver time: indeks på første blokerede time efter den (None hvis ingen)."""
    nb: list[int | None] = [None] * len(hours)
    nxt = None
    for i in range(len(hours) - 1, -1, -1):
        nb[i] = nxt
        if hours[i].blocked:
            nxt = i
    return nb


def _preload_window(hours: list[PlanHour], pq: list[float], label: Callable[[int], str]) -> tuple[dict[str, Any] | None, str | None]:
    """Første sammenhængende forvarmning (timer med varme lagt for at dække en senere blokering)."""
    idx = [i for i, x in enumerate(pq) if x > 1e-9]
    if not idx:
        return None, None
    a = idx[0]
    b = a
    while b + 1 < len(pq) and (pq[b + 1] > 1e-9 or (not hours[b + 1].blocked and b + 2 < len(pq) and pq[b + 2] > 1e-9)):
        b += 1
    k = b + 1
    while k < len(hours) and not hours[k].blocked:
        k += 1
    e = k
    while e + 1 < len(hours) and hours[e + 1].blocked:
        e += 1
    w = {"from": hours[a].ts, "to": hours[b].ts + 3600, "block_from": hours[k].ts if k < len(hours) else None,
         "block_to": hours[e].ts + 3600 if k < len(hours) else None, "kwh": round(sum(pq[a:b + 1]), 2)}
    clk = lambda ts: label(ts).split("kl. ")[-1]  # noqa: E731
    txt = f"forvarmer {label(w['from'])}–{clk(w['to'])}"
    if w["block_from"] is not None:
        txt += f" til blokeringen {clk(w['block_from'])}–{clk(w['block_to'])}"
    return w, txt


def _cop(m: SourceModel, t_out: float, p: float, target: float) -> float:
    a, b, c, d = m.cop
    return max(1.2, a + b * t_out + c * flow_for(m, p, target) + d * p)


def _el(m: SourceModel, t_out: float, p: float, target: float) -> float:
    return p / _cop(m, t_out, p, target) if p > 0 else 0.0


def simulate_rooms(rooms: list[PlanRoom], hours: list[PlanHour], q: list[float], past_q: list[float] | None = None) -> dict[str, list[float]]:
    """Forudsagt temperatur ved udgangen af hver time for hvert rum."""
    past = list(past_q or [0.0] * 6)[-6:]
    past = [0.0] * (6 - len(past)) + past
    out: dict[str, list[float]] = {}
    for r in rooms:
        t, series = r.t_now, []
        for i, h in enumerate(hours):
            q_delay = q[i - 6] if i >= 6 else past[i]
            t = (t + r.heat_now * q[i] + r.heat_delayed * q_delay + r.cool_per_hk * (h.t_out - t) + r.free_per_h
                 + sum(k * (tn - t) for k, tn in r.neighbours))
            series.append(round(t, 2))
        out[r.name] = series
    return out


def estimate_room(r: PlanRoom, t0: float, ts0: float, end: float, q5: dict[int, float], out5: dict[int, float],
                  t_out: float) -> float:
    """Rummets temperatur ved ``end``, regnet frem fra sidste måling ``t0`` (kl. ``ts0``) i 5-minutters trin.

    Samme rummodel som planen: varme nu pr. kWh i trinnet, gulvets forsinkede varme fra 6 timer før, afkøling mod
    ude, gratis varme og naboer (timeleddene × 5/60). ``q5`` er kWh pr. 5-minutters trin (trinnets start, sekunder),
    ``out5`` udetemperaturen pr. trin (ellers ``t_out``). Et trin uden måling af tilførslen tæller som 0 kWh."""
    step = 300
    t, ts = t0, ts0
    while ts < end - 1:
        b = int(ts // step * step)
        nxt = min(b + step, end)
        f = (nxt - ts) / step  # andel af trinnet: det første og det sidste kan være delvise
        q = max(q5.get(b, 0.0), 0.0) * f
        qd = max(q5.get(b - 6 * 3600, 0.0), 0.0) * f
        h = f * step / 3600
        t = (t + r.heat_now * q + r.heat_delayed * qd
             + (r.cool_per_hk * (out5.get(b, t_out) - t) + r.free_per_h + sum(k * (tn - t) for k, tn in r.neighbours)) * h)
        ts = nxt
    return t


def make_plan(hours: list[PlanHour], model: SourceModel, rooms: list[PlanRoom], target: float,
              max_pause: int = 0, past_q: list[float] | None = None,
              label: Callable[[int], str] | None = None, boost: float = 0.0, boost_hours: int = 0,
              red_allowance: float = 0.0, preload: float = 0.0, flow_over_curve: float | None = None) -> Plan:
    """Planen for de givne timer. ``target`` er den indetemperatur behovet regnes ud fra.

    ``boost`` (»jeg fryser«) hæver målet og rummenes min. i de første ``boost_hours`` timer.
    ``red_allowance``: så meget må et rum falde under sit min. i blokerede timer.
    ``preload``: bevidst forvarmning – i blokerede timer hæves rummenes krav med så mange °C pr. 6 timer inde
    i blokeringen (højst ``PRELOAD_MAX``). Jo længere gulvene skal være slukket, jo mere lades de op før.
    """
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan

    def limit(r: PlanRoom, i: int) -> float:
        """Laveste tilladte temperatur for rummet ved udgangen af time i."""
        return r.t_min + (boost if i < boost_hours else 0.0) - (red_allowance if hours[i].blocked else 0.0)

    # Forvarmning: kravet stiger med antal timer inde i en blokering (time 1 i en blokering: preload/6 osv.)
    pre: list[float] = []
    run = 0
    for h in hours:
        run = run + 1 if h.blocked else 0
        pre.append(min(PRELOAD_MAX, max(preload, 0.0) * run / 6))

    demand = [max(0.0, (model.heat_loss_w_k * (target + (boost if i < boost_hours else 0.0) - h.t_out) - model.gains_w) / 1000)
              for i, h in enumerate(hours)]
    plan.demand_kwh = round(sum(demand), 2)
    q = [0.0] * n
    free = [i for i, h in enumerate(hours) if not h.blocked]

    # Lavt og jævnt: fremløbet må højst ligge ``flow_over_curve`` °C over varmepumpens egen kurve ved timens
    # udetemperatur. Det begrænser effekten pr. time, så varmen spredes over flere timer (højere COP, færre afrimninger).
    pcap = [model.p_max] * n
    if flow_over_curve is not None and model.emission_kw_k > 0:
        for i, h in enumerate(hours):
            fmax = model.curve_c0 + model.curve_slope * h.t_out + flow_over_curve
            pcap[i] = min(model.p_max, max(model.p_min, (fmax - target) * model.emission_kw_k))

    def add_cost(i: int, amount: float) -> float:
        """Ekstra el-pris (kr) for at lægge ``amount`` kWh mere i time i."""
        p0, p1 = q[i], q[i] + amount
        if p1 > pcap[i] + 1e-9:
            return float("inf")
        h = hours[i]
        return h.price * (_el(model, h.t_out, p1, target) - _el(model, h.t_out, p0, target))

    def add_cheapest(cands: list[int], amount_left: float) -> float:
        """Læg en portion i den billigste kandidat-time. Giver mængden tilbage (0 = ingen plads)."""
        best, best_c, best_a = None, float("inf"), 0.0
        for i in cands:
            a = max(STEP, model.p_min) if q[i] == 0 else STEP
            c = add_cost(i, a) / a
            if c < best_c:
                best, best_c, best_a = i, c, a
        if best is None:
            return 0.0
        q[best] += best_a
        return best_a

    # 2) vandfyldning til husets behov
    need = sum(demand)
    guard = 0
    while sum(q) < need - 1e-6 and guard < 2000:
        guard += 1
        if not add_cheapest(free, need - sum(q)):
            plan.warnings.append("behovet kan ikke dækkes i de frie timer")
            break

    # 3) rummene: kommer et rum under min + usikkerhed, lægges varme i den billigste frie time før.
    #    Kan en time ikke rettes (fx rød lige nu, eller timerne før er fulde), springes netop den time
    #    over, og tjekket fortsætter – så der stadig varmes op før næste blokering. Husets behov i de
    #    blokerede timer må flyttes frem; derudover kan hvert rum højst kræve 10 kWh ekstra, så ét svagt
    #    rum ikke trækker hele planen. Ved samme pris lægges varmen så sent som muligt før behovet
    #    (mindre går tabt, og gulvets forsinkede varme når ind i blokeringen).
    skip: set[tuple[str, int]] = set()
    extra: dict[str, float] = {}
    nb = _next_block(hours)
    pq = [0.0] * n  # forvarmning: varme lagt i time j for at holde et rum gennem en senere blokering
    cap = 10.0 + sum(d for d, h in zip(demand, hours) if h.blocked)
    for _ in range(1500):
        sim = simulate_rooms(rooms, hours, q, past_q)
        worst: tuple[int, PlanRoom] | None = None
        for r in rooms:
            for i, t in enumerate(sim[r.name]):
                if t < limit(r, i) + r.margin + pre[i] and (r.name, i) not in skip:
                    if worst is None or i < worst[0]:
                        worst = (i, r)
                    break
        if worst is None:
            break
        i_bad, r = worst
        if extra.get(r.name, 0.0) >= cap:
            skip.update((r.name, k) for k in range(i_bad, n))
            continue
        before = q[:]
        added = add_cheapest([j for j in reversed(free) if j <= i_bad], STEP)
        if added:
            extra[r.name] = extra.get(r.name, 0.0) + added
            j = next(k for k in range(n) if q[k] != before[k])
            if nb[j] is not None and nb[j] <= i_bad:
                pq[j] += added
        else:
            skip.add((r.name, i_bad))

    # 4) for lange pauser brydes med pausens billigste frie time
    if max_pause > 0:
        for _ in range(50):
            run, hit = 0, None
            for i in range(n):
                if q[i] > 0 or hours[i].blocked:
                    run = 0
                    continue
                run += 1
                if run > max_pause:
                    gap = [j for j in range(i - run + 1, i + 1) if not hours[j].blocked]
                    hit = min(gap, key=lambda j: hours[j].price / _cop(model, hours[j].t_out, model.p_min, target))
                    break
            if hit is None:
                break
            q[hit] = max(q[hit], model.p_min)

    # 5) resultat time for time. Der advares kun, når et rum ventes under selve min. (ikke min. + usikkerhed).
    plan.rooms = simulate_rooms(rooms, hours, q, past_q)
    lab = label or (lambda ts: f"om {(ts - hours[0].ts) // 3600} t")
    for r in rooms:
        s = plan.rooms[r.name]
        k = min(range(n), key=lambda j: s[j] - limit(r, j))
        if s[k] < limit(r, k):
            plan.warnings.append(f"{r.name}: {s[k]:.1f} °C {lab(hours[k].ts)} er under planens grænse ({limit(r, k):.1f})")
    for i, h in enumerate(hours):
        p = round(q[i], 2)
        flow = flow_for(model, p, target) if p > 0 else None
        shift = None
        if p > 0:
            shift = round(flow - (model.curve_c0 + model.curve_slope * h.t_out))
            shift = max(model.shift_min, min(model.shift_max, shift))
        cop = _cop(model, h.t_out, p, target) if p > 0 else None
        el = p / cop if cop else 0.0
        plan.hours.append({"ts": h.ts, "heat_kw": p, "flow": flow, "shift": shift,
                           "price": round(h.price, 3), "t_out": round(h.t_out, 1), "blocked": h.blocked,
                           "cop": round(cop, 2) if cop else None, "cost": round(el * h.price, 2),
                           "preload": round(min(pq[i], p), 2)})
        plan.heat_kwh += p
        plan.el_kwh += el
        plan.cost_kr += el * h.price
    plan.heat_kwh, plan.el_kwh, plan.cost_kr = round(plan.heat_kwh, 1), round(plan.el_kwh, 1), round(plan.cost_kr, 1)
    on = sum(1 for x in plan.hours if x["heat_kw"] > 0)
    # Forvarmning: hvornår der varmes op til en senere blokering (de timer, rummene ellers ville komme under kravet)
    notes: list[str] = []
    plan.preload, txt = _preload_window(hours, [min(a, b) for a, b in zip(pq, q)], lab)
    if txt:
        notes.append(txt)
    plan.reason = (f"{on} timer, {plan.heat_kwh} kWh varme for ca. {plan.cost_kr} kr" + "".join(" – " + x for x in notes)
                   + (" – " + "; ".join(plan.warnings) if plan.warnings else ""))
    return plan


def make_air_plan(hours: list[PlanHour], rooms: list[PlanRoom], e_min: float = 0.3, e_max: float = 1.0,
                  past_q: list[float] | None = None, label: Callable[[int], str] | None = None,
                  preload: float = 0.0, comfort: float = 0.0) -> Plan:
    """Plan for en luft-luft-varmepumpe i kWh el pr. time (rummodellerne lærer grader pr. kWh el).

    Varmen måles ikke, så der er intet husbehov at fordele. Planen varmer kun så meget, at rummene holder
    gulv + komfort + usikkerhed (+ forvarmning i blokerede timer), i de billigste frie timer før behovet
    (ved samme pris så sent som muligt), og aldrig så meget, at et rum kommer over sit loft (``t_max``).
    Rum uden gulv (``t_min`` −99, ikke med i planen) har kun loft. Der varmes aldrig i blokerede timer.
    ``e_min``/``e_max``: laveste og højeste el pr. time, når varmepumpen kører.
    """
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan
    pre: list[float] = []
    run = 0
    for h in hours:
        run = run + 1 if h.blocked else 0
        pre.append(min(PRELOAD_MAX, max(preload, 0.0) * run / 6))
    q = [0.0] * n
    free = [i for i, h in enumerate(hours) if not h.blocked]
    planned = [r for r in rooms if r.t_min > -50]
    capped = [r for r in rooms if r.t_max is not None]

    def need(r: PlanRoom, i: int) -> float:
        return r.t_min + comfort + r.margin + pre[i]

    def new_over_cap(sim: dict[str, list[float]], base: dict[str, list[float]]) -> bool:
        """Kommer et rum over loftet (eller længere over) end uden den nye portion?"""
        return any(t > r.t_max + 1e-6 and t > b + 1e-6 for r in capped for t, b in zip(sim[r.name], base[r.name]))

    skip: set[tuple[str, int]] = set()
    full: set[int] = set()
    nb = _next_block(hours)
    pq = [0.0] * n  # forvarmning: el lagt i time j for at holde et rum gennem en senere blokering
    sim = simulate_rooms(rooms, hours, q, past_q)
    for _ in range(800):
        worst: tuple[int, PlanRoom] | None = None
        for r in planned:
            for i, t in enumerate(sim[r.name]):
                if t < need(r, i) and (r.name, i) not in skip:
                    if worst is None or i < worst[0]:
                        worst = (i, r)
                    break
        if worst is None:
            break
        i_bad, r = worst
        # Billigste frie time før behovet; ved samme pris den seneste (stabil sortering af en omvendt liste)
        cands = sorted((j for j in reversed(free) if j <= i_bad and j not in full and q[j] < e_max - 1e-9), key=lambda j: hours[j].price)
        added = False
        for j in cands:
            a = e_min if q[j] == 0 else min(AIR_STEP, e_max - q[j])
            q[j] += a
            sim2 = simulate_rooms(rooms, hours, q, past_q)
            if capped and new_over_cap(sim2, sim):
                q[j] -= a
                full.add(j)
                continue
            sim, added = sim2, True
            if nb[j] is not None and nb[j] <= i_bad:
                pq[j] += a
            break
        if not added:
            skip.add((r.name, i_bad))

    plan.rooms = sim
    lab = label or (lambda ts: f"om {(ts - hours[0].ts) // 3600} t")
    for r in planned:
        s = sim[r.name]
        k = min(range(n), key=lambda j: s[j] - r.t_min)
        if s[k] < r.t_min:
            plan.warnings.append(f"{r.name}: {s[k]:.1f} °C {lab(hours[k].ts)} er under gulvet ({r.t_min:.1f})")
    notes: list[str] = []
    for r in capped:
        if r.t_now > r.t_max:
            notes.append(f"{r.name} er over loftet ({r.t_now:.1f} mod {r.t_max:.1f} °C) – varmer ikke, før det er faldet")
    sp = (min(r.t_max for r in capped) - 0.5) if capped else None
    for i, h in enumerate(hours):
        e = round(q[i], 2)
        setpoint = None
        if e > 0:
            setpoint = sp if sp is not None else round(2 * (max((need(r, i) for r in planned), default=21.0) + 1.0)) / 2
        plan.hours.append({"ts": h.ts, "heat_kw": e, "el_kw": e, "flow": None, "shift": None, "setpoint": setpoint,
                           "price": round(h.price, 3), "t_out": round(h.t_out, 1), "blocked": h.blocked, "cop": None,
                           "cost": round(e * h.price, 2), "preload": round(min(pq[i], e), 2)})
        plan.el_kwh += e
        plan.cost_kr += e * h.price
    plan.el_kwh = round(plan.el_kwh, 1)
    plan.heat_kwh = plan.el_kwh  # luft-luft: varmen måles ikke – tallene er el
    plan.cost_kr = round(plan.cost_kr, 1)
    on = sum(1 for x in plan.hours if x["heat_kw"] > 0)
    plan.preload, txt = _preload_window(hours, [min(a, b) for a, b in zip(pq, q)], lab)
    if txt:
        notes.insert(0, txt)
    plan.reason = (f"{on} timer, {plan.el_kwh} kWh el for ca. {plan.cost_kr} kr" + "".join(" – " + x for x in notes)
                   + (" – " + "; ".join(plan.warnings) if plan.warnings else ""))
    return plan

