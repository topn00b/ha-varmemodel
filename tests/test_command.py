"""Anbefalet styring: mindste tid tændt/slukket, blokerede timer, kurve holdt, tællere pr. døgn."""

from __future__ import annotations

from custom_components.varmemodel.command import Command, step

M = 60


def test_min_on_then_off() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    assert c.on and c.shift == 2 and c.writes == 1 and c.flips == 1
    c = step(c, 30 * M, "d", False, None, False)
    assert c.on and "60 min" in c.reason
    c = step(c, 61 * M, "d", False, None, False)
    assert not c.on and c.flips == 2


def test_min_off_then_on() -> None:
    c = step(Command(on=True, since_onoff=-7200, last_change=-7200), 0, "d", False, None, False)
    assert not c.on
    c = step(c, 10 * M, "d", True, 1, False)
    assert not c.on and "30 min" in c.reason
    c = step(c, 31 * M, "d", True, 1, False)
    assert c.on


def test_blocked_turns_off_at_once() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    c = step(c, 5 * M, "d", True, 2, True)
    assert not c.on and c.blocked_on == 0 and "blokeret" in c.reason


def test_shift_held_one_hour() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    c = step(c, 30 * M, "d", True, 4, False)
    assert c.shift == 2 and "1 time" in c.reason
    c = step(c, 61 * M, "d", True, 4, False)
    assert c.shift == 4 and c.writes == 2


def test_counters_reset_each_day() -> None:
    c = step(Command(), 0, "d1", True, 2, False)
    c = step(c, 90 * M, "d2", True, 2, False)
    assert c.writes == 0 and c.flips == 0 and c.day == "d2"


def test_roundtrip() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    assert Command.load(c.dump()) == c
