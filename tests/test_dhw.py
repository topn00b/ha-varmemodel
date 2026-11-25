"""Varmt vand: tankens model lært af målinger, og planen for hvornår der varmes."""

from __future__ import annotations

from custom_components.varmemodel.dhw import DhwHour, TankModel, learn_tank, plan_dhw

M = TankModel(loss=0.02, amb=20.0, draw=[0.0] * 7 + [4.0] + [0.0] * 11 + [3.0] + [0.0] * 4, heat=0.3, el=0.12, gap=3.0)


def hrs(n: int = 24, price=lambda i: 2.0, blocked=lambda i: False, start_hour: int = 0) -> list[DhwHour]:
    return [DhwHour(ts=i * 3600, price=price(i), blocked=blocked(i), hour=(start_hour + i) % 24) for i in range(n)]


def test_tank_model_is_learned_from_measurements() -> None:
    """Tab i ro, forbrug time for time, opvarmning pr. minut, el pr. grad og varmepumpens startgrænse læres af målingerne:
    ti døgn med tab 2 % af overtemperaturen pr. time, bad kl. 7 (4 °C) og kl. 19 (3 °C) og en opvarmning hver nat kl. 3."""
    temps: list[tuple[float, float]] = []
    runs: list[tuple[float, float]] = []
    el: list[tuple[float, float]] = []
    t, kwh, ts = 44.0, 100.0, 0.0
    for day in range(10):
        for hour in range(24):
            first = 0
            if hour == 3:  # opvarmning til 45 °C: 0,3 °C pr. minut, 0,12 kWh el pr. °C
                mins = (45.0 - t) / 0.3
                runs.append((ts, ts + mins * 60))
                el.append((ts, kwh))
                kwh += (45.0 - t) * 0.12
                el.append((ts + mins * 60, kwh))
                temps.append((ts, t))
                t = 45.0
                temps.append((ts + mins * 60 + 120, t))  # føleren er et par minutter om det
                first = int((mins * 60 + 120) // 300) + 1
            for k in range(first, 12):  # hvert 5. minut
                temps.append((ts + k * 300, round(t - (0.02 * (t - 20.0) + (4.0 if hour == 7 else 3.0 if hour == 19 else 0.0)) * k / 12, 3)))
            t -= 0.02 * (t - 20.0) + (4.0 if hour == 7 else 3.0 if hour == 19 else 0.0)
            ts += 3600
    temps.sort()
    m = learn_tank(temps, runs, lambda x: int(x // 3600) % 24, 20.0, targets=[(0.0, 45.0)], el=el)
    assert abs(m.loss - 0.02) < 0.002 and m.hours > 150 and m.runs == 10
    assert abs(m.draw[7] - 4.0) < 0.3 and abs(m.draw[19] - 3.0) < 0.3 and max(m.draw[h] for h in range(24) if h not in (7, 19)) < 0.2
    assert abs(m.heat - 0.3) < 0.03 and abs(m.el - 0.12) < 0.01
    assert m.gap == 2.4  # den første nats opvarmning gik i gang 2,4 °C under målet: startgrænsen er højst det
    # opvarmninger, der altid starter langt under målet, siger intet om startgrænsen – standarden står
    far = learn_tank([(0.0, 35.0), (1800.0, 35.0), (2100.0, 45.0), (2700.0, 45.0)], [(0.0, 1800.0)], lambda x: 0, 20.0, targets=[(0.0, 45.0)])
    assert far.gap == 5.0 and far.runs == 1 and abs(far.heat - 10 / 30) < 0.01
    # det, der ikke kan læres af så lidt, beholdes fra den forrige model
    m2 = learn_tank([(0.0, 42.0), (1500.0, 42.0), (1800.0, 45.0), (2400.0, 45.0)], [(0.0, 1500.0)], lambda x: 0, 20.0, targets=[(0.0, 45.0)], prev=m)
    assert m2.gap == 2.4 and m2.loss == m.loss and m2.draw == m.draw and m2.runs == 1
    assert TankModel.from_dict(m.as_dict()).as_dict() == m.as_dict()
    assert learn_tank([], [], lambda x: 0).hours == 0  # ingen målinger: standardmodellen


def test_dhw_plan_keeps_the_tank_above_minimum_cheaply() -> None:
    """Uden blokeringer: tanken holdes over »mindst« hele døgnet med så få og billige opvarmninger som muligt – kun til
    »normalt«, og kun i timer, hvor varmepumpen faktisk ville gå i gang (tanken ligger under målet minus startgrænsen)."""
    h = hrs(24, price=lambda i: 1.0 if i in (2, 3) else 3.0)
    p = plan_dhw(h, 41.0, M, 40.0, 45.0, 50.0, "economy")
    runs = [x for x in p["hours"] if x["run"]]
    assert runs and all(x["target"] == 45.0 for x in runs)
    assert runs[0]["ts"] in (2 * 3600, 3 * 3600)  # den billige nat – tanken holder dertil
    # Morgenbadet kl. 7 tager tanken under »mindst« (42,6 → 38,1 °C): det må det gerne – den varmes igen i timen efter, så
    # den ikke står kold, når den næste vil i bad. Der købes ingen ekstra opvarmning til »højt« for at undgå det.
    assert p["under_hours"] == 0 and all(x["start"] >= 40.0 for x in p["hours"]) and p["hours"][7]["tank"] < 40.0
    assert p["hours"][8]["run"] and p["hours"][8]["target"] == 45.0 and p["lowest"]["tank"] >= 40.0
    # balance må også bruge »højt«: den fylder mere på i den billige nat, når det samlet bliver billigere end at varme
    # igen i en dyr time – aldrig dyrere end økonomi, og tanken holder stadig
    bal = plan_dhw(h, 41.0, M, 40.0, 45.0, 50.0, "balance")
    assert bal["under_hours"] == 0 and bal["kr"] <= p["kr"] and any(x["target"] == 50.0 for x in bal["hours"] if x["ts"] in (2 * 3600, 3 * 3600))
    assert p["state"] == "off" and p["next_run"]["ts"] == runs[0]["ts"] and p["runs"] == len(runs)
    assert abs(runs[0]["el"] - (45.0 - 40.2) * 0.12) < 0.15 and runs[0]["min"] == round(runs[0]["el"] / 0.12 / 0.3)
    assert abs(p["kr"] - sum(x["kr"] for x in p["hours"])) < 0.02
    # varm tank: ingen opvarmning, før den er nødvendig – og aldrig, mens tanken ligger tættere på målet end startgrænsen
    warm = plan_dhw(hrs(6), 44.5, M, 40.0, 45.0, 50.0, "economy")
    assert warm["runs"] == 0 and warm["state"] == "off" and warm["next_run"] is None
    # kold tank nu: varm straks
    cold = plan_dhw(hrs(6), 36.0, M, 40.0, 45.0, 50.0, "economy")
    assert cold["state"] == "normal" and cold["target"] == 45.0 and cold["hours"][0]["run"] and cold["hours"][0]["min"] == 30


def test_dhw_plan_before_a_long_block_and_priority() -> None:
    """En lang blokering (kl. 6–21): opvarmningen lægges tæt på blokeringen, så tanken går fuld ind i den. Økonomi varmer
    kun til »normalt«; balance må bruge »højt« lige før, når tanken ellers ikke kan holde igennem; komfort holder desuden
    tanken tæt på »normalt« i de frie timer. Der varmes aldrig i en blokeret time."""
    h = hrs(24, blocked=lambda i: 6 <= i < 21)
    eco, bal, com = (plan_dhw(h, 43.0, M, 40.0, 45.0, 50.0, x) for x in ("economy", "balance", "comfort"))
    for p in (eco, bal, com):
        assert not any(x["run"] for x in p["hours"] if x["blocked"])
    last = lambda p: max(x["ts"] for x in p["hours"] if x["run"] and x["ts"] < 6 * 3600)  # noqa: E731
    assert last(eco) == 5 * 3600 and all(x["target"] == 45.0 for x in eco["hours"] if x["run"])
    assert any(x["target"] == 50.0 for x in bal["hours"] if x["run"] and x["ts"] < 6 * 3600)
    assert bal["under_hours"] < eco["under_hours"] and bal["lowest"]["tank"] > eco["lowest"]["tank"]
    assert com["runs"] >= bal["runs"] and com["under_hours"] <= bal["under_hours"]
    assert eco["under_from"] is not None and eco["under_from"] >= 6 * 3600
    # blokeret nu og kold tank: der kan ikke varmes – planen siger det
    blk = plan_dhw(hrs(4, blocked=lambda i: True), 35.0, M, 40.0, 45.0, 50.0, "balance")
    assert blk["state"] == "off" and blk["runs"] == 0 and blk["under_hours"] == 4 and blk["under_from"] == 0
    # »Bad nu«: højt med det samme, også i en blokeret time
    boost = plan_dhw(hrs(4, blocked=lambda i: True), 35.0, M, 40.0, 45.0, 50.0, "economy", boost=True)
    assert boost["state"] == "high" and boost["target"] == 50.0 and boost["hours"][0]["run"] and boost["hours"][0]["tank"] > 45
    assert plan_dhw([], 40.0, M, 40.0, 45.0, 50.0)["state"] == "off"


def test_tank_may_be_cold_outside_the_hours_of_use() -> None:
    """Brugstiden: grænsen »mindst« gælder kun, når vandet bruges. Om aftenen kl. 21 med en lunken tank varmes der ikke
    til natten – opvarmningen lægges tæt på morgenen, så varmen ikke står og taber sig natten over. Komfort holder
    tanken over »mindst« hele døgnet."""
    use = [6 <= h < 21 for h in range(24)]
    h = hrs(12, start_hour=21)  # kl. 21 → kl. 9
    eco = plan_dhw(h, 39.5, M, 40.0, 45.0, 50.0, "economy", use=use)
    runs = [x for x in eco["hours"] if x["run"]]
    # kl. 5: sidste time før brugstiden (lige priser: mindst tab) – tanken er klar kl. 6, ikke først efter en opvarmning
    # i selve timen; kl. 8 varmes der igen efter morgenbadet
    assert eco["state"] == "off" and [x["ts"] for x in runs] == [8 * 3600, 11 * 3600]
    assert eco["hours"][9]["start"] >= 44.0 and not eco["hours"][9]["run"]
    assert eco["under_hours"] == 0 and eco["use"] == list(range(6, 21))
    assert eco["hours"][0]["use"] is False and eco["hours"][9]["use"] is True
    # tankens laveste punkt tælles i brugstiden – ikke natten, hvor den gerne må være kold
    assert eco["lowest"]["ts"] >= 9 * 3600 and min(x["start"] for x in eco["hours"][:8]) < 40.0
    # uden brugstid (hele døgnet): der varmes med det samme
    allday = plan_dhw(h, 39.5, M, 40.0, 45.0, 50.0, "economy", use=[True] * 24)
    assert allday["state"] == "normal" and allday["hours"][0]["run"]
    # uden for brugstiden varmes der ikke – heller ikke i komfort – kun i timen lige før brugstiden (klar kl. 6)
    com = plan_dhw(h, 39.5, M, 40.0, 45.0, 50.0, "comfort", use=use)
    assert com["state"] == "off" and not any(x["run"] for x in com["hours"][:8])
    assert all(x["start"] >= 40.0 for x in com["hours"][9:])
    # en lang blokering næste dag (fra kl. 7:30) flytter ikke opvarmningen til aftenen: kl. 21 er uden for brugstiden
    blk = plan_dhw(hrs(12, start_hour=21, blocked=lambda i: i >= 10), 38.7, M, 40.0, 45.0, 50.0, "economy", use=use)
    assert [x["ts"] for x in blk["hours"] if x["run"]][:1] == [8 * 3600] and blk["hours"][9]["start"] >= 44.0
    # en opvarmning, der allerede er i gang uden for brugstiden, regnes færdig (ellers sidder varmen fast i varmepumpen)
    run = plan_dhw(h, 38.7, M, 40.0, 45.0, 50.0, "economy", use=use, running=45.0)
    assert run["state"] == "normal" and run["hours"][0]["run"] and not any(x["run"] for x in run["hours"][1:8])
    # »bad nu« varmer altid
    assert plan_dhw(h, 38.7, M, 40.0, 45.0, 50.0, "economy", boost=True, use=use)["state"] == "high"
    # den lærte brugstid: timerne med målbart forbrug; før noget er lært: hele døgnet
    m = TankModel(draw=[0.0] * 7 + [4.0] + [0.05] * 11 + [3.0] + [0.0] * 4, hours=200)
    assert [i for i, u in enumerate(m.use()) if u] == [7, 19] and all(TankModel().use())
    lrn = plan_dhw(hrs(12, start_hour=21), 39.5, m, 40.0, 45.0, 50.0, "economy")
    assert lrn["use"] == [7, 19] and lrn["state"] == "off"


def test_a_run_in_progress_is_counted_to_its_end() -> None:
    """En opvarmning, der er i gang, regnes færdig i den første time – ellers ville planen tro, at varmepumpen ikke går i
    gang så tæt på målet, og melde »fra« midt i den. I en blokeret time gælder det ikke."""
    off = plan_dhw(hrs(6), 43.5, M, 40.0, 45.0, 50.0, "economy")
    assert off["state"] == "off" and not off["hours"][0]["run"]
    on = plan_dhw(hrs(6), 43.5, M, 40.0, 45.0, 50.0, "economy", running=45.0)
    assert on["state"] == "normal" and on["target"] == 45.0 and on["hours"][0]["run"] and on["hours"][0]["min"] == 5
    blk = plan_dhw(hrs(6, blocked=lambda i: i == 0), 43.5, M, 40.0, 45.0, 50.0, "economy", running=45.0)
    assert blk["state"] == "off" and not blk["hours"][0]["run"]


def test_pause_after_a_run_is_learned_from_the_compressor() -> None:
    from custom_components.varmemodel.dhw import run_pause

    runs = [(0.0, 1800.0), (90000.0, 91800.0)]
    # kompressoren stopper ved skiftet tilbage og starter igen 6 og 4 minutter efter
    assert run_pause(runs, [(-600.0, 1805.0), (2160.0, 5000.0), (89000.0, 91800.0), (92040.0, 95000.0)]) == 5.0
    assert run_pause(runs[:1], [(-600.0, 5000.0)]) == 0.0  # kørte videre hen over skiftet
    assert run_pause(runs[:1], [(-600.0, 1800.0)]) is None  # ingen rumvarme bagefter: intet at lære
    assert run_pause([(0.0, 120.0)], [(-600.0, 5000.0)]) is None  # for kort til at være en opvarmning
