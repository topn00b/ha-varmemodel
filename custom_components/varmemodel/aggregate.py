"""Time-for-time opsummering af tilstandsændringer (ren Python).

Bruges både til at fylde opsamleren op fra historikken ved første opstart og
til den løbende opsamling. Tider er unix-sekunder; timer starter på hele
UTC-timer (Danmark ligger på hele timer, så det passer også lokalt).
"""

from __future__ import annotations

HOUR = 3600


def hour_start(ts: float) -> int:
    return int(ts // HOUR * HOUR)


def on_minutes(changes: list[tuple[float, bool]], start: float, end: float) -> dict[int, tuple[float, int]]:
    """Minutter tændt og antal starter pr. time for en on/off-serie.

    ``changes`` er (tid, tændt) sorteret efter tid; tilstanden før første
    ændring regnes som slukket, medmindre første punkt ligger før ``start``.
    """
    out: dict[int, list[float]] = {}
    for h in range(hour_start(start), int(end), HOUR):
        out[h] = [0.0, 0]
    state = False
    t_prev = start
    for t, on in sorted(changes):
        if t <= start:
            state = on
            continue
        if t >= end:
            break
        if state:
            _add_on(out, t_prev, t)
        if on and not state:
            h = hour_start(t)
            if h in out:
                out[h][1] += 1
        state, t_prev = on, t
    if state:
        _add_on(out, t_prev, end)
    return {h: (round(v[0] / 60, 2), int(v[1])) for h, v in out.items()}


def _add_on(out: dict[int, list[float]], a: float, b: float) -> None:
    t = a
    while t < b:
        h = hour_start(t)
        nxt = min(b, h + HOUR)
        if h in out:
            out[h][0] += nxt - t
        t = nxt


def step_mean(changes: list[tuple[float, float | None]], start: float, end: float) -> dict[int, float]:
    """Tidsvægtet middel pr. time for en trinvis værdi (fx et setpunkt)."""
    acc: dict[int, list[float]] = {}
    val: float | None = None
    t_prev = start
    pts = sorted(changes)
    for t, v in pts:
        if t <= start:
            val = v
            continue
        if t >= end:
            break
        _add_val(acc, t_prev, t, val)
        val, t_prev = v, t
    _add_val(acc, t_prev, end, val)
    return {h: round(s / w, 3) for h, (s, w) in acc.items() if w >= HOUR * 0.5}


def _add_val(acc: dict[int, list[float]], a: float, b: float, v: float | None) -> None:
    if v is None:
        return
    t = a
    while t < b:
        h = hour_start(t)
        nxt = min(b, h + HOUR)
        s = acc.setdefault(h, [0.0, 0.0])
        s[0] += v * (nxt - t)
        s[1] += nxt - t
        t = nxt


def intervals(changes: list[tuple[float, bool]], end: float) -> list[tuple[float, float]]:
    """Perioder hvor en on/off-serie var tændt."""
    out: list[tuple[float, float]] = []
    cur: float | None = None
    for t, on in sorted(changes):
        if on and cur is None:
            cur = t
        elif not on and cur is not None:
            out.append((cur, t))
            cur = None
    if cur is not None:
        out.append((cur, end))
    return out
