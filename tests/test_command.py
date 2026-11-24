"""Anbefalet styring: mindste tid tændt/slukket, blokerede timer, fremløb holdt, tællere pr. døgn."""

from __future__ import annotations

from custom_components.varmemodel.command import Command, step

M = 60


def test_min_on_then_off() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    assert c.on and c.flow == 2 and c.writes == 1 and c.flips == 1
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


def test_flow_held_a_quarter() -> None:
    """Fremløbet må justeres hvert kvarter (≥ 1 K) – det er tænd/sluk, der skånes."""
    c = step(Command(), 0, "d", True, 2, False)
    c = step(c, 10 * M, "d", True, 4, False)
    assert c.flow == 2 and "kvarter" in c.reason
    c = step(c, 16 * M, "d", True, 4, False)
    assert c.flow == 4 and c.writes == 2


def test_load_renames_old_fields() -> None:
    """0.8.0: gemte kommandoer med »shift« læses som fremløbsmål."""
    c = Command.load({"on": True, "shift": 33, "since_shift": 5.0})
    assert c.flow == 33 and c.since_flow == 5.0


def test_counters_reset_each_day() -> None:
    c = step(Command(), 0, "d1", True, 2, False)
    c = step(c, 90 * M, "d2", True, 2, False)
    assert c.writes == 0 and c.flips == 0 and c.day == "d2"


def test_roundtrip() -> None:
    c = step(Command(), 0, "d", True, 2, False)
    assert Command.load(c.dump()) == c


def test_hold_has_slack_for_replan_jitter() -> None:
    """24/11: fremløbet sat 13:32:55 (kørsel lige efter en genstart); planlægningen 13:47:00 må hæve til forvarmning –
    ellers blev et kvarters hold til en halv time, og forvarmningen nåede aldrig frem før rød kl. 14."""
    c = step(Command(), 0, "d", True, 30, False)
    c = step(c, 845, "d", True, 35, False)
    assert c.flow == 35 and c.reason == "nyt fremløb"
    c = step(c, 845 + 600, "d", True, 30, False)
    assert c.flow == 35 and c.reason == "holder fremløbet mindst et kvarter"
