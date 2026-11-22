"""Læsning af priser, blokerede timer og vejrudsigt fra entiteter (ren Python, kan testes)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .planner import PlanHour

TIME_KEYS = ("start", "t", "time", "hour", "datetime", "start_time")
VALUE_KEYS = ("el", "value", "price", "total", "price_kwh", "kr")
LIST_ATTRS = ("timer", "prices", "forecast", "data", "raw_today", "raw_tomorrow", "today", "tomorrow")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def parse_time(v: Any, parse_local: Callable[[str], datetime | None]) -> int | None:
    """Unix-sekunder for et tidspunkt: datetime, ISO-tekst eller 'YYYY-MM-DDTHH' (lokal tid)."""
    if isinstance(v, datetime):
        return int(v.timestamp())
    if isinstance(v, (int, float)):
        return int(v / 1000 if v > 1e11 else v)
    if isinstance(v, str):
        s = v.strip()
        if len(s) == 13 and s[10] == "T":
            s += ":00"
        d = parse_local(s)
        return int(d.timestamp()) if d else None
    return None


def hourly_prices(attrs: dict[str, Any], attr: str | None, parse_local: Callable[[str], datetime | None]) -> dict[int, float]:
    """Pris pr. hel time (snit af kvartersværdier). Finder selv listen og nøglerne."""
    lists: list[Any] = []
    if attr:
        lists = [attrs.get(attr)]
    else:
        lists = [attrs.get(k) for k in LIST_ATTRS if isinstance(attrs.get(k), list)]
    acc: dict[int, list[float]] = {}
    for lst in lists:
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            tk = next((k for k in TIME_KEYS if k in item), None)
            vk = next((k for k in VALUE_KEYS if k in item and _num(item[k]) is not None), None)
            if tk is None or vk is None:
                continue
            ts = parse_time(item[tk], parse_local)
            if ts is None:
                continue
            acc.setdefault(ts // 3600 * 3600, []).append(_num(item[vk]))
    return {h: sum(v) / len(v) for h, v in acc.items()}


def blocked_hours(state: str, attrs: dict[str, Any], codes: list[str], parse_local: Callable[[str], datetime | None]) -> dict[int, bool]:
    """Blokerede timer fra en tidsplan-entitet.

    To formater: tilstanden 'TIMEINDEKS|koder' (timer siden 1970 i UTC, én kode pr.
    time, fx 'ggyrr…'), eller en attributliste med tid og værdi (værdien i ``codes``
    = blokeret)."""
    out: dict[int, bool] = {}
    if "|" in (state or ""):
        a, c = state.split("|", 1)
        try:
            start = int(a)
        except ValueError:
            start = None
        if start is not None:
            for i, ch in enumerate(c):
                out[(start + i) * 3600] = ch in codes
            return out
    for k in LIST_ATTRS:
        lst = attrs.get(k)
        if not isinstance(lst, list):
            continue
        for item in lst:
            if not isinstance(item, dict):
                continue
            tk = next((x for x in TIME_KEYS if x in item), None)
            vk = next((x for x in ("zone", "state", "value", "blocked") if x in item), None)
            if tk is None or vk is None:
                continue
            ts = parse_time(item[tk], parse_local)
            if ts is not None:
                out[ts // 3600 * 3600] = str(item[vk]) in codes or item[vk] is True
    return out


def hourly_forecast(items: list[dict[str, Any]], parse_local: Callable[[str], datetime | None]) -> dict[int, float]:
    """Udetemperatur pr. time fra weather.get_forecasts (type hourly)."""
    out: dict[int, float] = {}
    for f in items or []:
        ts = parse_time(f.get("datetime"), parse_local)
        t = _num(f.get("temperature"))
        if ts is not None and t is not None:
            out[ts // 3600 * 3600] = t
    return out


def build_hours(h0: int, prices: dict[int, float], forecast: dict[int, float], blocks: dict[int, bool], t_now: float | None,
                horizon: int, safety_hours: int = 24) -> tuple[list[PlanHour], bool]:
    """Planens timer fra nu (UTC-timer).

    Mangler prisen for én enkelt time (fx når uret stilles tilbage, og to lokale timer
    hedder det samme), bruges snittet af nabotimerne. Mangler alle priser, laves en nødplan
    med flad pris, så rummene stadig holdes varme (anden værdi = True)."""
    t_def = t_now if t_now is not None else 5.0
    hours: list[PlanHour] = []
    for k in range(horizon):
        ts = h0 + k * 3600
        p = prices.get(ts)
        if p is None:
            prev = hours[-1].price if hours else prices.get(ts - 3600)
            nxt = prices.get(ts + 3600)
            if prev is not None and nxt is not None:
                p = (prev + nxt) / 2
            elif not hours and nxt is not None:
                p = nxt
            else:
                break
        hours.append(PlanHour(ts=ts, price=p, t_out=forecast.get(ts, t_def), blocked=bool(blocks.get(ts, False))))
    if hours:
        return hours, False
    return [PlanHour(ts=h0 + k * 3600, price=1.0, t_out=forecast.get(h0 + k * 3600, t_def), blocked=bool(blocks.get(h0 + k * 3600, False)))
            for k in range(safety_hours)], True
