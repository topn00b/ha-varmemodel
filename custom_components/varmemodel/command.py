"""Anbefalet styring: planens ønske omsat til kommandoer, der skåner varmepumpen (ren Python).

Reglerne:
- aldrig varme i blokerede timer (slukker straks)
- mindst 60 min. tændt og 30 min. slukket
- højst én ændring pr. 15 min.
- kurve-forskydningen ændres kun, når den er holdt mindst 1 time, og forskellen er mindst 1 K

Varmemodel styrer ikke selv noget: sensoren viser, hvad en automation bør sende, og tæller
pr. døgn, hvor mange skrivninger og tænd/sluk-skift det giver.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

MIN_ON = 3600
MIN_OFF = 1800
MIN_GAP = 900
SHIFT_HOLD = 3600
DEADBAND = 1


@dataclass
class Command:
    on: bool = False
    shift: int | None = None
    since_onoff: float = -1e12  # langt tilbage: en ny tilstand må handle med det samme
    since_shift: float = -1e12
    last_change: float = -1e12
    reason: str = ""
    day: str = ""
    writes: int = 0
    flips: int = 0
    blocked_on: int = 0

    @classmethod
    def load(cls, d: dict[str, Any] | None) -> Command:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})

    def dump(self) -> dict[str, Any]:
        return asdict(self)


def step(c: Command, now: float, day: str, want_on: bool, want_shift: int | None, blocked: bool) -> Command:
    """Næste anbefalede kommando ud fra planens ønske nu."""
    n = Command.load(c.dump())
    if n.day != day:
        n.day, n.writes, n.flips, n.blocked_on = day, 0, 0, 0
    reason = "følger planen"
    if blocked:
        want_on = False
    target = want_on
    if n.on != want_on and not blocked:
        if n.on and now - n.since_onoff < MIN_ON:
            target, reason = True, "holder mindst 60 min. tændt"
        elif not n.on and now - n.since_onoff < MIN_OFF:
            target, reason = False, "holder mindst 30 min. slukket"
        elif now - n.last_change < MIN_GAP:
            target, reason = n.on, "højst én ændring pr. 15 min."
    if blocked and n.on:
        reason = "blokeret: slukker straks"
    if target != n.on:
        n.on = target
        n.since_onoff = n.last_change = now
        n.writes += 1
        n.flips += 1
        n.shift = want_shift if target else None
        n.since_shift = now
    elif n.on and want_shift is not None and want_shift != n.shift:
        if n.shift is None or (abs(want_shift - n.shift) >= DEADBAND and now - n.since_shift >= SHIFT_HOLD and now - n.last_change >= MIN_GAP):
            n.shift, n.since_shift, n.last_change = want_shift, now, now
            n.writes += 1
            reason = "ny kurve"
        else:
            reason = "holder kurven mindst 1 time" if now - n.since_shift < SHIFT_HOLD else "højst én ændring pr. 15 min."
    if n.on and blocked:
        n.blocked_on += 1
    n.reason = reason
    return n
