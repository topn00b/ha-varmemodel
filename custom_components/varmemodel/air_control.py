"""Styring af en luft-luft-varmepumpe: en stige af trin (ren Python, ingen Home Assistant).

Stigen: først blæseren op (laveste trin kan være varmepumpens »quiet«), så setpunktet op – blæseren
højst til den valgte maks. (dag eller nat). Planen siger, om der skal varmes; stigen siger, hvor hårdt:

- op ét trin pr. kvarter, når et rum er under sit gulv (+ forvarmning før en blokering)
- ned ét trin, når alle rum har været mindst »ned-margen« over i 30 min., og trinnet har holdt i 45 min.
- start på det trin, der sidst holdt rummene ved samme udetemperatur (lært pr. 5 °C)

Alt regnes fra målinger og planens forudsigelse; intet forskydes kunstigt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UP_EVERY = 900  # højst ét trin op pr. kvarter
DOWN_HOLD = 1800  # alle rum over ned-margenen så længe, før et trin ned
DOWN_GAP = 2700  # og trinnet skal have holdt så længe
LEARN_HOLD = 3600  # et trin, der har holdt rummene så længe, huskes for udetemperaturen
W_SETTLE = 900  # forbrug pr. trin læres først, når trinnet har holdt så længe
W_WEIGHT = 0.1  # ny måling vægter 10 % (glidende middel)


@dataclass
class Step:
    fan: str | None  # blæserhastighed (None = varmepumpens »quiet«)
    setpoint: float
    preset: str | None = None


def fan_options(fan_modes: list[str] | None) -> list[str]:
    """Blæserhastighederne fra laveste til højeste (uden »auto«)."""
    return [f for f in (fan_modes or []) if f.lower() != "auto"]


def build_ladder(fans: list[str], cap: str | None, sp_min: float, sp_max: float, sp_step: float,
                 quiet: bool = False) -> list[Step]:
    """Trinene: (quiet) → blæser op til ``cap`` ved ``sp_min`` → setpunktet op til ``sp_max`` ved ``cap``."""
    if cap in fans:
        fans = fans[: fans.index(cap) + 1]
    steps: list[Step] = []
    if quiet:
        steps.append(Step(None, sp_min, "quiet"))
    steps += [Step(f, sp_min) for f in fans]
    top = fans[-1] if fans else None
    sp, step = sp_min, max(0.1, sp_step)
    while sp + step <= sp_max + 1e-6:
        sp = round(sp + step, 2)
        steps.append(Step(top, sp))
    return steps or [Step(None, sp_min)]


def is_night(minute_of_day: int, start: str, end: str) -> bool:
    """Ligger klokken (minutter efter midnat) i natten fra ``start`` til ``end`` (»HH:MM«, kan gå over midnat)?"""
    def m(s: str) -> int:
        h, mm = (s or "0:0").split(":")[:2]
        return int(h) * 60 + int(mm)
    a, b = m(start), m(end)
    return a <= minute_of_day < b if a < b else (minute_of_day >= a or minute_of_day < b)


def preload_per_room(plan: list[dict[str, Any]], forecast: dict[str, list[float]], max_k: float,
                     ahead: int = 4, min_len: int = 2) -> tuple[dict[str, float], str | None]:
    """Forvarmning før en blokering: planens forventede fald i hvert rum under blokeringen (højst ``max_k``).

    Gælder en blokering på mindst ``min_len`` timer, der begynder 1–``ahead`` timer fremme. Indeks i ``plan`` og
    ``forecast`` følger hinanden (time 0 = denne time; forudsigelsen er ved udgangen af hver time).
    """
    n = len(plan)
    s = next((i for i in range(1, min(ahead, n - 1) + 1) if plan[i].get("blocked")), None)
    if s is None:
        return {}, None
    e = s
    while e + 1 < n and plan[e + 1].get("blocked"):
        e += 1
    if e - s + 1 < min_len:
        return {}, None
    out: dict[str, float] = {}
    for room, fc in forecast.items():
        if len(fc) > e and s >= 1:
            out[room] = round(min(max_k, max(0.0, fc[s - 1] - min(fc[s:e + 1]))), 2)
    return out, f"{s}–{e + 1}"


def choose_step(cur: int | None, n: int, margins: dict[str, float], down_margin: float, now: float,
                changed: float, down_since: float | None, start: int | None = None) -> tuple[int, float, float | None, str]:
    """Næste trin. ``margins``: rum → målt temperatur − (gulv + forvarmning). Giver (trin, ændret, ned-siden, grund)."""
    top = max(0, n - 1)
    if cur is None:
        cur = min(top, start if start is not None else 0)
        return cur, now, None, f"starter på trin {cur}" + (" (lært for udetemperaturen)" if start is not None else "")
    cur = min(cur, top)
    if not margins:
        return cur, changed, None, "ingen rum med gulv – holder trinnet"
    low = min(margins, key=lambda r: margins[r])
    if margins[low] < 0:
        if cur >= top:
            return cur, changed, None, f"{low} er under gulvet – øverste trin"
        if now - changed >= UP_EVERY:
            return cur + 1, now, None, f"{low} er {abs(margins[low]):.1f} °C under gulvet – et trin op"
        return cur, changed, None, f"{low} er under gulvet – venter (højst ét trin pr. kvarter)"
    if all(m >= down_margin for m in margins.values()):
        ds = down_since if down_since is not None else now
        if cur > 0 and now - ds >= DOWN_HOLD and now - changed >= DOWN_GAP:
            return cur - 1, now, None, f"alle rum mindst {down_margin:.1f} °C over gulvet i 30 min. – et trin ned"
        return cur, changed, ds, f"alle rum over gulvet + {down_margin:.1f} – venter med at gå ned"
    return cur, changed, None, "rummene holder – trinnet holdes"


def learn_w(learned: dict[str, float], step: int, watt: float) -> dict[str, float]:
    """Forbrug pr. trin (W) som glidende middel – målt, ikke forskudt."""
    k = str(step)
    old = learned.get(k)
    new = dict(learned)
    new[k] = round(watt if old is None else old * (1 - W_WEIGHT) + watt * W_WEIGHT)
    return new


def band(t_out: float | None) -> str | None:
    """Udetemperaturens 5-graders bånd (fx 0 for 0–4,9 °C, −5 for −5–−0,1 °C)."""
    if t_out is None:
        return None
    return str(int(t_out // 5 * 5))
