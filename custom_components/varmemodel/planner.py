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
    neighbours: list[tuple[float, float]] = field(default_factory=list)  # (°C pr. time pr. grad, naboens temperatur)


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


def make_plan(hours: list[PlanHour], model: SourceModel, rooms: list[PlanRoom], target: float,
              max_pause: int = 0, past_q: list[float] | None = None) -> Plan:
    """Planen for de givne timer. ``target`` er den indetemperatur behovet regnes ud fra."""
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan
    demand = [max(0.0, (model.heat_loss_w_k * (target - h.t_out) - model.gains_w) / 1000) for h in hours]
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

    def add_cheapest(cands: list[int], amount_left: float) -> bool:
        best, best_c, best_a = None, float("inf"), 0.0
        for i in cands:
            a = max(STEP, model.p_min) if q[i] == 0 else STEP
            c = add_cost(i, a) / a
            if c < best_c:
                best, best_c, best_a = i, c, a
        if best is None:
            return False
        q[best] += best_a
        return True

    # 2) vandfyldning til husets behov
    need = sum(demand)
    guard = 0
    while sum(q) < need - 1e-6 and guard < 2000:
        guard += 1
        if not add_cheapest(free, need - sum(q)):
            plan.warnings.append("behovet kan ikke dækkes i de frie timer")
            break

    # 3) rummene: kommer et rum under min + usikkerhed, lægges varme i den billigste time før
    for _ in range(200):
        sim = simulate_rooms(rooms, hours, q, past_q)
        worst = None
        for r in rooms:
            for i, t in enumerate(sim[r.name]):
                if t < r.t_min + r.margin:
                    if worst is None or i < worst[0]:
                        worst = (i, r.name)
                    break
        if worst is None:
            break
        before = [j for j in free if j <= worst[0]]
        if not before or not add_cheapest(before, STEP):
            plan.warnings.append(f"{worst[1]} kan ikke holdes over min. (time {worst[0]})")
            break

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

    # 5) resultat time for time
    plan.rooms = simulate_rooms(rooms, hours, q, past_q)
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
