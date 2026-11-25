"""Luft-luft-stigen: trinene, nat/dag, forvarmning før blokering, op/ned-regler og lært forbrug."""

from __future__ import annotations

from custom_components.varmemodel.air_control import (
    DOWN_GAP,
    DOWN_HOLD,
    UP_EVERY,
    band,
    build_ladder,
    choose_step,
    fan_options,
    is_night,
    learn_w,
    eff_per_kwh,
    eff_update,
    efficient_step,
    preload_per_room,
    room_gain,
    target_step,
)

FANS = fan_options(["Auto", "Low", "LowMid", "Mid", "HighMid", "High"])


def test_ladder_has_every_fan_on_every_temperature() -> None:
    """Stigen: for hver temperatur alle blæsere fra laveste til højeste – først den laveste temperatur."""
    lad = build_ladder(FANS, "High", 20.0, 23.0, 0.5, quiet=True)
    assert lad[0].preset == "quiet" and [x.fan for x in lad[1:6]] == FANS
    assert all(x.setpoint == 20.0 for x in lad[:6]) and lad[6].preset == "quiet" and lad[6].setpoint == 20.5
    assert lad[-1].fan == "High" and lad[-1].setpoint == 23.0 and len(lad) == 6 * 7
    night = build_ladder(FANS, "HighMid", 20.0, 23.0, 0.5, quiet=True)
    assert max(x.fan or "" for x in night if x.fan) != "High" and night[-1].fan == "HighMid" and len(night) == 5 * 7
    # ned ad stigen: alle blæsere på en temperatur, før temperaturen falder en halv grad
    day = build_ladder(FANS, "High", 20.0, 20.5, 0.5, floor="Mid")
    assert [(x.fan, x.setpoint) for x in reversed(day)] == [("High", 20.5), ("HighMid", 20.5), ("Mid", 20.5),
                                                            ("High", 20.0), ("HighMid", 20.0), ("Mid", 20.0)]


def test_up_takes_the_fan_first_and_then_the_next_temperature_on_the_top_fan() -> None:
    """Op, når et rum mangler varme: blæseren et trin op; fra højeste blæser den næste temperatur – stadig på højeste
    blæser. Ned går ét trin ad gangen (alle blæsere), så den nye temperaturs lavere blæsere prøves på vej ned."""
    from custom_components.varmemodel.air_control import top_of, up_from

    lad = build_ladder(FANS, "High", 20.0, 21.0, 0.5, floor="Mid")  # Mid, HighMid, High × 20,0 / 20,5 / 21,0
    seq, i = [], 0
    for _ in range(5):
        seq.append((lad[i].fan, lad[i].setpoint))
        i = up_from(lad, i)
    assert seq == [("Mid", 20.0), ("HighMid", 20.0), ("High", 20.0), ("High", 20.5), ("High", 21.0)]
    assert up_from(lad, len(lad) - 1) == len(lad) - 1 and top_of(lad, 3) == 5 and top_of(lad, 5) == 5
    # i stigen: et rum under gulvet tager springet; planens mål følges uden at gå forbi det
    i, ch, _ds, why = choose_step(2, len(lad), {"A": -0.3}, False, UP_EVERY, 0, None, up_to=up_from(lad, 2))
    assert i == 5 and "højeste blæser" in why
    i, _c, _d, why = choose_step(1, len(lad), {"A": -0.3}, False, UP_EVERY, 0, None, up_to=up_from(lad, 1))
    assert i == 2 and "et trin op" in why and "højeste blæser" not in why
    assert choose_step(2, len(lad), {"A": 0.2}, False, UP_EVERY, 0, None, target=3, up_to=up_from(lad, 2))[0] == 3
    assert choose_step(2, len(lad), {"A": 0.2}, False, UP_EVERY, 0, None, target=8, up_to=up_from(lad, 2))[0] == 5
    # uden op-trin (ældre kald): som før, ét trin
    assert choose_step(2, len(lad), {"A": -0.3}, False, UP_EVERY, 0, None)[0] == 3


def test_a_step_that_was_not_enough_is_not_tried_again_at_once() -> None:
    """Var trinnet under ikke nok for lidt siden, går stigen først ned til det, når overskuddet har holdt dobbelt så længe."""
    ok = {"A": 0.6}
    t = DOWN_GAP + DOWN_HOLD
    assert choose_step(5, 9, ok, True, t, 0, 0)[0] == 4  # uden bund: ned efter 45 min.
    i, _c, _d, why = choose_step(5, 9, ok, True, t, 0, 0, floor=5)
    assert i == 5 and "ikke nok" in why
    assert choose_step(5, 9, ok, True, 2 * DOWN_GAP, 0, 0, floor=5)[0] == 4  # overskuddet bliver ved: prøv igen
    assert choose_step(6, 9, ok, True, t, 0, 0, floor=5)[0] == 5  # ned til bunden er altid tilladt


def test_a_step_is_found_again_when_the_ladder_is_rebuilt() -> None:
    """Trinnet huskes som (blæser, niveau): bygges stigen om (nat/dag, min. blæser, toppen), findes det nærmeste igen."""
    from custom_components.varmemodel.air_control import locate

    order = ["quiet", "Low", "LowMid", "Mid", "HighMid", "High"]
    day = [(f, lv) for lv in (-1.5, -1.0, -0.5) for f in ("Mid", "HighMid", "High")]
    night = [(f, lv) for lv in (-1.5, -1.0, -0.5) for f in ("Mid", "HighMid")]
    assert locate(day, "HighMid", -1.0, order) == 4 and locate(night, "HighMid", -1.0, order) == 3
    assert locate(night, "High", -1.0, order) == 3  # natten har ikke High: højeste, der ikke er højere
    assert locate(day, "Low", -0.5, order) == 6  # under stigens laveste blæser: den laveste
    assert locate(day, "High", 2.0, order) == 8 and locate(day, "Mid", -3.0, order) == 0  # uden for stigen: nærmeste niveau
    assert locate([], "High", 0.0, order) is None


def test_night_window_over_midnight() -> None:
    assert is_night(23 * 60, "22:00", "07:00") and is_night(6 * 60 + 59, "22:00", "07:00")
    assert not is_night(7 * 60, "22:00", "07:00") and not is_night(12 * 60, "22:00", "07:00")
    assert is_night(13 * 60, "12:00", "14:00") and not is_night(15 * 60, "12:00", "14:00")


def test_up_one_per_quarter_down_after_hold() -> None:
    i, ch, ds, why = choose_step(None, 12, {"A": -0.2}, False, 0, 0, None, start=4)
    assert i == 4 and "starter" in why
    i, ch, ds, why = choose_step(4, 12, {"A": -0.2}, False, 600, ch, ds)
    assert i == 4 and "venter" in why
    i, ch, ds, _ = choose_step(4, 12, {"A": -0.2}, False, UP_EVERY, ch, ds)
    assert i == 5
    # alle rum over gulv + usikkerhed: først efter 30 min. og 45 min. på trinnet
    t = UP_EVERY + 60
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, True, t, ch, ds)
    assert i == 5 and ds == t
    i, ch2, ds, _ = choose_step(5, 12, {"A": 0.6}, True, t + DOWN_HOLD, ch, ds)
    assert i == 5  # trinnet har ikke holdt 45 min.
    i, ch2, ds, why = choose_step(5, 12, {"A": 0.6}, True, ch + DOWN_GAP + DOWN_HOLD, ch, t)
    assert i == 4 and "ned" in why
    # over gulvet, men ikke over usikkerheden: holdes
    assert choose_step(4, 12, {"A": 0.3}, False, 10**6, 0, None)[0] == 4


def test_plan_target_drives_step() -> None:
    """Planens el-mål: laveste trin, hvis lærte forbrug dækker målet – op pr. kvarter, ned først når rummene har overskud."""
    w = [180, None, 300, None, 450, 600]
    assert target_step(w, 400) == 4 and target_step(w, 100) == 0 and target_step(w, 900) == 5
    assert target_step([None, 300, None], 200) is None and target_step(w, None) is None
    i, ch, _, why = choose_step(2, 6, {"A": 0.4}, False, UP_EVERY, 0, None, target=4)
    assert i == 3 and "el-mål" in why
    # målet er lavere, men rummene har ikke overskud: holdes
    assert choose_step(4, 6, {"A": 0.1}, False, 10**6, 0, None, target=1)[0] == 4
    # rum under gulvet går forud for målet
    assert choose_step(2, 6, {"A": -0.1}, False, UP_EVERY, 0, None, target=0)[0] == 3


def test_preload_is_planned_drop_during_block() -> None:
    plan = [{"blocked": False}, {"blocked": False}, {"blocked": True}, {"blocked": True}, {"blocked": True}, {"blocked": False}]
    fc = {"A": [21.0, 21.4, 21.0, 20.6, 20.2, 20.5], "B": [20.0, 20.1, 20.1, 20.1, 20.1, 20.2]}
    pre, blk = preload_per_room(plan, fc, 1.5)
    assert blk == "2–5" and pre["A"] == 1.2 and pre["B"] == 0.0
    assert preload_per_room(plan, fc, 0.5)[0]["A"] == 0.5
    # blokering kun 1 time eller for langt fremme: ingen
    assert preload_per_room([{"blocked": False}, {"blocked": True}, {"blocked": False}], {"A": [1, 2, 3]}, 1.5)[0] == {}
    assert preload_per_room([{"blocked": False}] * 6 + [{"blocked": True}] * 3, {"A": [20.0] * 9}, 1.5)[0] == {}


def test_learned_watt_is_moving_average() -> None:
    w = learn_w({}, "High 23", 440)
    assert w == {"High 23": 440}
    assert learn_w(w, "High 23", 540)["High 23"] == 450


def test_band() -> None:
    assert band(2.3) == "0" and band(-0.1) == "-5" and band(7.0) == "5" and band(None) is None


def test_room_gain_removes_cooling_and_free_heat() -> None:
    """Varmepumpens andel: målt ændring minus afkøling mod ude og opdrift (gratis varme) i perioden."""
    g = room_gain(20.0, 20.1, 0.25, 0.0, 0.02, 0.1, [])
    # uden varme: 0,25 t × (0,1 − 0,02·20) = −0,075 °C → varmepumpen stod for 0,175 °C
    assert abs(g - 0.175) < 1e-9


def test_efficient_step_prefers_more_heat_per_kwh() -> None:
    """Lavt trin med bedre virkning vælges, når det dækker planens varme – ellers et højere."""
    eff: dict = {}
    for _ in range(10):
        eff = eff_update(eff, "Low 20", 0.06, 0.08)   # 0,75 °C/kWh el
        eff = eff_update(eff, "High 23", 0.10, 0.20)  # 0,50 °C/kWh el
    e_low, e_high = eff_per_kwh(eff, "Low 20"), eff_per_kwh(eff, "High 23")
    assert abs(e_low - 0.75) < 0.01 and abs(e_high - 0.5) < 0.01
    w, e = [320.0, 800.0], [e_low, e_high]
    # plan 300 W ved gennemsnitlig virkning (0,625) = 187 varme-enheder: Low giver 240 → Low
    assert efficient_step(w, e, 300) == 0
    # plan 600 W = 375: kun High (400) dækker
    assert efficient_step(w, e, 600) == 1
    assert eff_per_kwh(eff_update({}, "x", 0.1, 0.1), "x") is None  # for få målinger


def test_ventilation_effect_compares_same_steps() -> None:
    """Ventilationens effekt: virkning med og uden på de samme trin."""
    from custom_components.varmemodel.coordinator import VarmemodelCoordinator

    eff: dict = {}
    for _ in range(10):
        eff = eff_update(eff, "Low 20|vent til", 0.12, 0.1)
        eff = eff_update(eff, "Low 20|vent fra", 0.10, 0.1)
    assert VarmemodelCoordinator._vent_effect(eff) == 20
    assert VarmemodelCoordinator._vent_effect({}) is None


def test_choose_step_calm_up_every() -> None:
    """Rolig opstart: højst ét trin op pr. halve time."""
    from custom_components.varmemodel.air_control import choose_step

    i, ch, _ds, why = choose_step(3, 8, {"A": -0.2}, False, 10_000.0, 10_000.0 - 1000, None, up_every=1800)
    assert i == 3 and "halve time" in why
    i, ch, _ds, why = choose_step(3, 8, {"A": -0.2}, False, 10_000.0, 10_000.0 - 1900, None, up_every=1800)
    assert i == 4


def test_choose_step_calm_holds_when_room_rises() -> None:
    """Rolig opstart: et rum under gulvet, der stiger nok på trinnet, får ikke et trin mere; ellers ét op pr. halve time."""
    from custom_components.varmemodel.air_control import choose_step

    i, _c, _d, why = choose_step(6, 12, {"A": -1.0}, False, 10_000.0, 10_000.0 - 1900, None, up_every=1800, rising={"A": 0.3}, min_rise=0.2)
    assert i == 6 and "stiger" in why and "holder" in why
    i, _c, _d, why = choose_step(6, 12, {"A": -1.0}, False, 10_000.0, 10_000.0 - 1900, None, up_every=1800, rising={"A": 0.05}, min_rise=0.2)
    assert i == 7 and "stiger kun" in why


def test_defrosts_are_read_from_the_power_curve() -> None:
    """Luft-luft uden afrimningsføler: en afrimning er et kort dyk midt i driften – effekten falder til under halvdelen i
    nogle minutter, og varmepumpen kører videre. En kort slukning (helt ned på slukket-effekten), et langt stop og et dyk
    uden drift bagefter tæller ikke; to dyk lige efter hinanden er én hændelse."""
    from custom_components.varmemodel.air_control import power_defrosts

    def run(minutes: list[tuple[int, float]]) -> list[tuple[float, float]]:
        """(antal minutter, W) i forlængelse af hinanden → et punkt hvert 10. sekund."""
        out, t = [], 0.0
        for n, w in minutes:
            for _ in range(n * 6):
                out.append((t, w))
                t += 10
        return out

    # 20 min. drift ved 700 W, dyk til 105 W i 3 min., videre ved 600 W og et løft til 1800 W: én afrimning
    pts = run([(20, 700.0), (3, 105.0), (8, 600.0), (6, 1800.0), (20, 700.0)])
    assert power_defrosts(pts, 150.0, 5.0) == [(20 * 60.0, 23 * 60.0, 105.0)]
    assert power_defrosts(pts, 150.0) == [(20 * 60.0, 23 * 60.0, 105.0)]  # slukket-effekten er ikke kendt endnu
    # helt ned på slukket-effekten: en kort slukning, ingen afrimning
    assert power_defrosts(run([(20, 700.0), (4, 6.0), (20, 700.0)]), 150.0, 5.0) == []
    # for langt (15 min.) eller ingen drift bagefter: et stop
    assert power_defrosts(run([(20, 700.0), (15, 105.0), (20, 700.0)]), 150.0, 5.0) == []
    assert power_defrosts(run([(20, 700.0), (3, 105.0), (30, 8.0)]), 150.0, 5.0) == []
    # varmepumpen kørte ikke før (under »kører«): intet dyk at tale om
    assert power_defrosts(run([(20, 100.0), (3, 20.0), (20, 100.0)]), 150.0, 5.0) == []
    # to dyk med to minutter imellem er samme hændelse (det laveste gælder) – her ender det på slukket: ingen afrimning
    two = run([(20, 700.0), (1, 200.0), (2, 500.0), (4, 6.0), (20, 700.0)])
    assert power_defrosts(two, 150.0, 5.0) == [] and len(power_defrosts(two, 150.0)) == 1
    # et enkelt udfald på 10 sekunder er ikke et dyk (der regnes på minutgennemsnit) – heller ikke uden kendt slukket-effekt
    assert power_defrosts(run([(30, 700.0)])[:100] + [(1000.0, 0.0)] + run([(30, 700.0)])[101:], 150.0) == []


def test_min_fan_shapes_the_ladder() -> None:
    """Min. blæser: blæserne under den – og »quiet« – udgår (et fjernt rum får ikke del i varmen ved lav blæser). Ligger
    den over maks. blæser (fx om natten), vinder maks.: stigen er så kun temperaturer."""
    fans = ["Low", "LowMid", "Mid", "HighMid", "High"]
    full = build_ladder(fans, "High", 19.5, 22.5, 0.5, quiet=True)
    assert [(x.preset or x.fan) for x in full[:6]] == ["quiet", "Low", "LowMid", "Mid", "HighMid", "High"] and len(full) == 6 * 7
    mid = build_ladder(fans, "High", 19.5, 22.5, 0.5, quiet=True, floor="Mid")
    assert [(x.preset or x.fan, x.setpoint) for x in mid[:4]] == [("Mid", 19.5), ("HighMid", 19.5), ("High", 19.5), ("Mid", 20.0)] and len(mid) == 3 * 7
    assert all(x.fan in ("Mid", "HighMid", "High") and x.preset is None for x in mid)
    # min. = maks.: kun temperaturer
    top = build_ladder(fans, "High", 19.5, 22.5, 0.5, quiet=True, floor="High")
    assert {x.fan for x in top} == {"High"} and [x.setpoint for x in top] == [19.5, 20.0, 20.5, 21.0, 21.5, 22.0, 22.5]
    # natten har lavere maks. end min.: støjgrænsen vinder
    night = build_ladder(fans, "LowMid", 19.5, 22.5, 0.5, quiet=True, floor="High")
    assert {x.fan for x in night} == {"LowMid"} and len(night) == 7
    # »quiet« som min. (eller ingen): som før
    assert build_ladder(fans, "High", 19.5, 22.5, 0.5, quiet=True, floor="quiet") == full
    assert build_ladder(fans, "High", 19.5, 22.5, 0.5, quiet=True, floor=None) == full
    # en varmepumpe uden blæservalg: ét trin pr. temperatur
    assert [(x.fan, x.preset, x.setpoint) for x in build_ladder([], None, 20.0, 20.5, 0.5)] == [(None, None, 20.0), (None, None, 20.5)]


def test_what_one_step_learns_is_shared_with_the_others() -> None:
    """Nat og dag har hver sin maks. blæser og dermed hver sine trin, men samme setpunkt over gulvet: et trin uden egne
    målinger bruger de andre blæseres tal for samme setpunkt (»delt«) og ellers et skøn mellem de nærmeste lærte
    setpunkter (»skøn«). Egne målinger går altid forud; blæsere uden for stigen (»Auto«) deles ikke."""
    from custom_components.varmemodel.air_control import eff_lookup, split_label, w_lookup

    assert split_label("HighMid +0.5|vent til@0") == ("HighMid", 0.5, "|vent til", "0")
    assert split_label("quiet +0.0") == ("quiet", 0.0, "", None) and split_label("High +3.0@-5") == ("High", 3.0, "", "-5")
    assert split_label("noget andet") is None
    fans = {"quiet", "Low", "Mid", "HighMid", "High"}
    w = {"HighMid +0.5": 312, "HighMid +0.5@0": 313, "HighMid +3.0": 625, "HighMid +3.0@0": 611, "Auto +2.0": 9999, "High +3.0": 438}
    assert w_lookup(w, "HighMid", 0.5, "0", fans) == (313, "trin")  # egne målinger i udebåndet
    assert w_lookup(w, "HighMid", 0.5, "-10", fans) == (312, "trin")  # egne målinger uden for båndet
    assert w_lookup(w, "High", 0.5, "0", fans) == (313, "delt")  # dagens trin låner nattens tal (udebåndet først)
    assert w_lookup(w, "High", 0.5, "-10", fans) == (312, "delt")
    assert w_lookup(w, "Mid", 3.0, None, fans) == (round((625 + 438) / 2), "delt")
    est, src = w_lookup(w, "High", 1.5, "0", fans)  # mellem +0,5 (312) og +3,0 (snit 531,5)
    assert src == "skøn" and abs(est - (312 + (531.5 - 312) * 1.0 / 2.5)) <= 1
    assert w_lookup(w, "High", 0.0, None, fans) == (312, "skøn")  # under det laveste lærte: det nærmeste
    assert w_lookup({}, "High", 0.5) == (None, None) and w_lookup({"Auto +2.0": 400}, "High", 2.0, None, fans) == (None, None)

    eff = {"HighMid +0.5|vent til": [0.254, 0.52, 7], "High +0.5|vent til": [0.197, 0.24, 3],
           "HighMid +3.0|vent fra": [0.414, 0.74, 5], "HighMid +3.0|vent til": [0.580, 0.87, 6], "HighMid +2.0|vent til": [0.9, 1.0, 9]}
    assert eff_lookup(eff, "HighMid", 2.0, "|vent til", None, fans)[:2] == (0.9, "trin")
    v, src, n = eff_lookup(eff, "High", 0.5, "|vent til", "0", fans)  # hverken nat eller dag har nok alene – sammen har de
    assert src == "delt" and n == 10 and abs(v - (0.254 + 0.197) / (0.52 + 0.24)) < 1e-9
    v, src, n = eff_lookup(eff, "High", 3.0, "|vent til", None, fans)  # samme ventilation har ikke nok: uanset ventilation
    assert src == "delt" and n == 11 and abs(v - (0.414 + 0.580) / (0.74 + 0.87)) < 1e-9
    v, src, n = eff_lookup(eff, "High", 1.0, "|vent fra", None, fans)  # intet på +1,0: mellem +0,5 og +2,0
    lo, hi = (0.254 + 0.197) / 0.76, 0.9
    assert src == "skøn" and n == 0 and abs(v - (lo + (hi - lo) * 0.5 / 1.5)) < 1e-9
    assert eff_lookup({"HighMid +0.5": [0.2, 0.5, 3]}, "High", 0.5) == (None, None, 0)  # for få kvarterer: ikke lært


def test_powerful_is_the_top_fan_on_the_way_up_only() -> None:
    """»powerful« som maks. blæser: øverst på hver temperatur; op går stigen på powerful, ned springer den over den
    (den larmer)."""
    from custom_components.varmemodel.air_control import POWERFUL, build_ladder, down_from, up_from

    lad = build_ladder(["Low", "Mid", "High"], POWERFUL, 20.0, 21.0, 0.5, floor="Mid")
    names = [(x.preset or x.fan, x.setpoint) for x in lad]
    assert names == [("Mid", 20.0), ("High", 20.0), (POWERFUL, 20.0), ("Mid", 20.5), ("High", 20.5), (POWERFUL, 20.5),
                     ("Mid", 21.0), ("High", 21.0), (POWERFUL, 21.0)]
    assert lad[2].fan == "High"
    # op: High 20,0 → powerful 20,0 → powerful 20,5
    assert up_from(lad, 1) == 2 and up_from(lad, 2) == 5
    # ned: powerful 21,0 → High 21,0 → … Mid 21,0 → High 20,5 (powerful 20,5 springes over)
    assert down_from(lad, 8) == 7 and down_from(lad, 6) == 4 and down_from(lad, 3) == 1
    # uden powerful som maks.: som før
    assert all(x.preset is None for x in build_ladder(["Low", "Mid", "High"], "High", 20.0, 21.0, 0.5))
