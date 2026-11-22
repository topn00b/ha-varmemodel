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


STEP = 0.25  # kWh pr. portion


def _cop(m: SourceModel, t_out: float, p: float, target: float) -> float:
    a, b, c, d = m.cop
    flow = target + (p / m.emission_kw_k if m.emission_kw_k > 0 else 0.0)
    return max(1.2, a + b * t_out + c * flow + d * p)


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
              red_allowance: float = 0.0) -> Plan:
    """Planen for de givne timer. ``target`` er den indetemperatur behovet regnes ud fra.

    ``boost`` (»jeg fryser«) hæver målet og rummenes min. i de første ``boost_hours`` timer.
    ``red_allowance``: så meget må et rum falde under sit min. i blokerede timer.
    """
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan

    def limit(r: PlanRoom, i: int) -> float:
        """Laveste tilladte temperatur for rummet ved udgangen af time i."""
        return r.t_min + (boost if i < boost_hours else 0.0) - (red_allowance if hours[i].blocked else 0.0)

    demand = [max(0.0, (model.heat_loss_w_k * (target + (boost if i < boost_hours else 0.0) - h.t_out) - model.gains_w) / 1000)
              for i, h in enumerate(hours)]
    plan.demand_kwh = round(sum(demand), 2)
    q = [0.0] * n
    free = [i for i, h in enumerate(hours) if not h.blocked]

    def add_cost(i: int, amount: float) -> float:
        """Ekstra el-pris (kr) for at lægge ``amount`` kWh mere i time i."""
        p0, p1 = q[i], q[i] + amount
        if p1 > model.p_max:
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
    #    over, og tjekket fortsætter – så der stadig varmes op før næste blokering. Hvert rum kan højst
    #    kræve 10 kWh ekstra, så ét svagt rum ikke trækker hele planen.
    skip: set[tuple[str, int]] = set()
    extra: dict[str, float] = {}
    for _ in range(1500):
        sim = simulate_rooms(rooms, hours, q, past_q)
        worst: tuple[int, PlanRoom] | None = None
        for r in rooms:
            for i, t in enumerate(sim[r.name]):
                if t < limit(r, i) + r.margin and (r.name, i) not in skip:
                    if worst is None or i < worst[0]:
                        worst = (i, r)
                    break
        if worst is None:
            break
        i_bad, r = worst
        if extra.get(r.name, 0.0) >= 10.0:
            skip.update((r.name, k) for k in range(i_bad, n))
            continue
        added = add_cheapest([j for j in free if j <= i_bad], STEP)
        if added:
            extra[r.name] = extra.get(r.name, 0.0) + added
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
        flow = target + (p / model.emission_kw_k if p > 0 and model.emission_kw_k > 0 else 0.0)
        shift = None
        if p > 0:
            shift = round(flow - (model.curve_c0 + model.curve_slope * h.t_out))
            shift = max(model.shift_min, min(model.shift_max, shift))
        cop = _cop(model, h.t_out, p, target) if p > 0 else None
        el = p / cop if cop else 0.0
        plan.hours.append({"ts": h.ts, "heat_kw": p, "flow": round(flow, 1) if p > 0 else None, "shift": shift,
                           "price": round(h.price, 3), "t_out": round(h.t_out, 1), "blocked": h.blocked,
                           "cop": round(cop, 2) if cop else None, "cost": round(el * h.price, 2)})
        plan.heat_kwh += p
        plan.el_kwh += el
        plan.cost_kr += el * h.price
    plan.heat_kwh, plan.el_kwh, plan.cost_kr = round(plan.heat_kwh, 1), round(plan.el_kwh, 1), round(plan.cost_kr, 1)
    on = sum(1 for x in plan.hours if x["heat_kw"] > 0)
    plan.reason = f"{on} timer, {plan.heat_kwh} kWh varme for ca. {plan.cost_kr} kr" + (" – " + "; ".join(plan.warnings) if plan.warnings else "")
    return plan
