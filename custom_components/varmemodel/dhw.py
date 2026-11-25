"""Varmt vand: tankens model lært af målinger, og en plan for hvornår der skal varmes.

Ren regning uden Home Assistant, så den kan afprøves alene. Tanken beskrives med få tal, der alle læres af husets egne
målinger (``learn_tank``): tabet i ro, forbruget time for time på døgnet, hvor hurtigt varmepumpen varmer tanken, hvad en
grad koster i el, og hvor langt under målet tanken skal være, før varmepumpen går i gang. Planen (``plan_dhw``) lægger
opvarmningerne i de billigste frie timer, så tanken holder brugerens mindste-temperatur – også gennem en lang blokering,
så godt det kan lade sig gøre.

De eneste faste tal er brugerens egne tre temperaturer (mindst, normalt, højt); alt andet er lært.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

DHW_STATES = ("off", "normal", "high")
GRID = 0.1  # °C: planens opløsning for tanktemperaturen (følerens)
USE_MIN = 0.1  # °C: en time på døgnet, hvor forbruget i gennemsnit tager mindst så meget (følerens opløsning), er brugstid
LOSS_WIN, LOSS_MIN = 6, 4  # timer: vinduet, tabet i ro måles over, og det korteste rolige stræk, der tæller
T_LO, T_HI = 5.0, 70.0  # planens temperaturområde


@dataclass
class TankModel:
    """Tanken, som målingerne viser den. Standardværdierne bruges kun, til der er data nok (``learned`` siger, hvad der er lært)."""

    loss: float = 0.02  # andel af (tank − omgivelser), tanken taber pr. time i ro
    amb: float = 20.0  # omgivelsernes temperatur (°C)
    draw: list[float] = field(default_factory=lambda: [0.0] * 24)  # forventet fald fra forbrug pr. time på døgnet (°C)
    heat: float = 0.3  # °C pr. minut under en opvarmning
    el: float = 0.12  # kWh el pr. °C
    gap: float = 5.0  # varmepumpen starter en opvarmning, når tanken ligger mindst så meget under målet (°C)
    pause: float = 10.0  # minutter uden rumvarme omkring en opvarmning (kompressoren står ved skiftet til og fra varmt vand)
    hours: int = 0  # timer i ro, tab og forbrug er lært af
    runs: int = 0  # opvarmninger, opvarmning, el og startgrænse er lært af

    def use(self) -> list[bool]:
        """Brugstiden: de timer på døgnet, hvor der bruges varmt vand (lært af tankens fald). Før noget er lært: alle."""
        return [d >= USE_MIN for d in self.draw] if self.hours else [True] * 24

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["draw"] = [round(x, 2) for x in self.draw]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TankModel:
        d = d or {}
        m = cls(**{k: d[k] for k in ("loss", "amb", "heat", "el", "gap", "pause", "hours", "runs") if d.get(k) is not None})
        if isinstance(d.get("draw"), list) and len(d["draw"]) == 24:
            m.draw = [float(x) for x in d["draw"]]
        return m


def _at(series: list[tuple[float, float]], t: float) -> float | None:
    """Seriens seneste værdi på tidspunktet ``t`` (serien er i tidsrækkefølge)."""
    v = None
    for ts, x in series:
        if ts > t:
            break
        v = x
    return v


def learn_tank(temps: list[tuple[float, float]], runs: list[tuple[float, float]], hour_of: Any, amb: float = 20.0,
               targets: list[tuple[float, float]] | None = None, el: list[tuple[float, float]] | None = None,
               prev: TankModel | None = None) -> TankModel:
    """Tankens model ud fra målinger.

    ``temps``: (tidspunkt, tanktemperatur) i tidsrækkefølge. ``runs``: (start, slut) for varmepumpens opvarmninger.
    ``hour_of``: funktion fra tidspunkt til lokal time på døgnet (0–23). ``targets``: (tidspunkt, mål) når målet ændres.
    ``el``: (tidspunkt, kWh) fra en elmåler, der tæller op. ``prev``: den forrige model – det, der ikke kan læres nu, beholdes.

    - Tab og forbrug: for hver hel time uden opvarmning (og uden for timen efter en – føleren er forsinket) ses på
      faldet. Tabet i ro læres af vinduer på seks sammenhængende rolige timer (glidende, en time ad gangen; et kortere
      stræk på mindst fire timer tæller som ét vindue) – over så lang tid udlignes det, at en føler står stille i timevis
      og så falder et hak: faldet over vinduet som andel af tankens overtemperatur. Vinduer uden forbrug falder mindst, så
      tabet er den fjerdedel af vinduerne, der falder mindst. Det, en time på døgnet i gennemsnit falder mere end tabet, er
      forbruget i den time – og timer med målbart forbrug er brugstiden (``TankModel.use``).
    - Opvarmning: stigningen fra start til det højeste inden for ti minutter efter slut (føleren er forsinket), pr. minut
      og pr. kWh el. Startgrænsen er det mindste, tanken har ligget under målet, da en opvarmning gik i gang."""
    m = TankModel(**{**asdict(prev), "amb": amb}) if prev else TankModel(amb=amb)
    if len(temps) < 2:
        return m
    busy = [(a - 300, b + 3600) for a, b in runs]  # opvarmningen og timen efter (føleren er forsinket, tanken blander sig)
    t0 = int(temps[0][0] // 3600 + 1) * 3600
    rows: list[tuple[int, int, float, float]] = []  # (timens start, lokal time, fald, overtemperatur)
    h = t0
    while h + 3600 <= temps[-1][0]:
        if not any(a < h + 3600 and b > h for a, b in busy):
            x, y = _at(temps, h), _at(temps, h + 3600)
            if x is not None and y is not None and x - amb > 2:
                rows.append((h, int(hour_of(h)), x - y, (x + y) / 2 - amb))
        h += 3600
    # Vinduer på LOSS_WIN sammenhængende rolige timer (glidende; et kortere stræk på mindst LOSS_MIN timer er ét vindue):
    # fald pr. overtemperatur over vinduet
    wins: list[float] = []
    cur: list[tuple[int, int, float, float]] = []
    for r in [*rows, None]:
        if r is not None and (not cur or r[0] - cur[-1][0] == 3600):
            cur.append(r)
            continue
        if len(cur) >= LOSS_MIN:
            n_ = min(LOSS_WIN, len(cur))
            for j in range(len(cur) - n_ + 1):
                w_ = cur[j:j + n_]
                if (o_ := sum(x[3] for x in w_)) > 0:
                    wins.append(sum(x[2] for x in w_) / o_)
        cur = [r] if r is not None else []
    if len(rows) >= 12 and wins:
        wins.sort()
        m.loss = round(max(0.0, wins[(len(wins) - 1) // 4]), 4)  # den fjerdedel af vinduerne, der falder mindst
        by: dict[int, list[float]] = {}
        for _ts, hr, d, over in rows:
            by.setdefault(hr, []).append(d - m.loss * over)
        m.draw = [round(max(0.0, sum(by[k]) / len(by[k])), 2) if by.get(k) else 0.0 for k in range(24)]
        m.hours = len(rows)
    rates, els, gaps = [], [], []
    for a, b in runs:
        if b - a < 300:
            continue
        x = _at(temps, a)
        after = [v for ts, v in temps if b <= ts <= b + 600]
        y = max(after) if after else _at(temps, b)
        if x is not None and targets and (g := _at(targets, a + 60)) is not None and g - x >= 1:
            gaps.append(g - x)  # også når tanken ikke steg (nogen brugte vand imens): opvarmningen gik i gang ved den afstand
        if x is None or y is None or y - x < 2:
            continue
        rates.append((y - x) / ((b - a) / 60))
        if el:
            e0, e1 = _at(el, a), _at(el, b + 300)
            if e0 is not None and e1 is not None and e1 > e0:
                els.append((e1 - e0) / (y - x))
    if rates:
        m.heat = round(median(rates), 3)
        m.runs = len(rates)
    if els:
        m.el = round(median(els), 3)
    if gaps:
        # Opvarmninger, der lægges om natten, starter altid langt under målet – det mindste, der er set, er derfor kun en
        # øvre grænse for varmepumpens egen startgrænse: den sættes aldrig op af den grund (hverken over den forrige eller
        # over standarden)
        m.gap = round(min(min(gaps), m.gap), 1)
    return m


def run_pause(runs: list[tuple[float, float]], comp_on: list[tuple[float, float]]) -> float | None:
    """Minutter, kompressoren står, når en opvarmning af varmt vand slutter, og rumvarmen skal i gang igen – medianen over
    opvarmningerne (kun dem, hvor kompressoren kom i gang igen inden for et kvarter; ellers var der ingen rumvarme at
    vende tilbage til). ``comp_on``: (start, slut) for kompressorens driftsperioder. None = intet at lære af."""
    out: list[float] = []
    for a, b in runs:
        if b - a < 300:
            continue
        if any(x < b - 30 and y > b + 30 for x, y in comp_on):
            out.append(0.0)  # kompressoren kørte videre hen over skiftet
            continue
        nxt = min((x for x, _y in comp_on if b - 30 <= x <= b + 900), default=None)
        if nxt is not None:
            out.append(max(0.0, nxt - b) / 60)
    return round(median(out), 1) if out else None


@dataclass
class DhwHour:
    ts: int  # timens start, unix-sekunder
    price: float  # kr pr. kWh el
    blocked: bool  # ingen opvarmning i timen (fx rød)
    hour: int  # lokal time på døgnet (til forbruget)


def plan_dhw(hours: list[DhwHour], tank: float, m: TankModel, t_min: float, t_normal: float, t_high: float,
             priority: str = "balance", boost: bool = False, frac0: float = 1.0, use: list[bool] | None = None,
             running: float | None = None) -> dict[str, Any]:
    """Planen for varmt vand: hvilke timer der varmes og til hvilket mål, så tanken holder ``t_min`` billigst muligt.

    Varmepumpen varmer selv tanken, når varmt vand er slået til, og tanken ligger mindst ``m.gap`` under målet – planen
    vælger derfor for hver time: fra, »normalt« (``t_normal``) eller »højt« (``t_high``). Først holdes tanken over
    grænsen så godt, det kan lade sig gøre (færrest gradtimer under), dernæst billigst – aldrig i blokerede timer.
    Det giver af sig selv en opvarmning tæt på en lang blokering, så tanken går fuld ind i den. Grænsen gælder det, der
    er i tanken, når timen begynder (efter en eventuel opvarmning): et bad må gerne tage tanken under grænsen – så varmes
    den i næste frie time – men den skal ikke stå kold, når nogen vil i bad.

    Grænsen gælder kun i brugstiden – ``use`` (24 timer på døgnet, fra brugerens indstilling), ellers den lærte
    (``m.use()``): om natten, hvor ingen bruger vand, må tanken gerne være kold – så varmes den tæt på morgenen i stedet
    for om aftenen. Når brugstiden begynder (en time i brugstiden efter en uden for), skal tanken være klar, før timen
    begynder – en opvarmning i selve timen kommer for sent til den, der står først op.

    ``priority``: økonomi varmer kun til »normalt«; balance må også bruge »højt« (fx lige før en blokering, tanken ellers
    ikke kan holde igennem); komfort holder desuden tanken tæt på »normalt« i brugstiden (grænsen er »normalt« minus
    varmepumpens startgrænse) og på ``t_min`` resten af døgnet, så der altid er varmt vand.
    ``boost``: »højt« nu, også i en blokeret time.
    ``frac0``: den del af den første time, der er tilbage.
    Uden for brugstiden varmes der ikke, undtagen i timen lige før brugstiden begynder (og med ``boost``).
    ``running``: målet for en opvarmning, der er i gang lige nu – den regnes færdig i den første time (medmindre timen er
    blokeret eller uden for brugstiden), i stedet for at planen tror, at varmepumpen ikke ville gå i gang så tæt på målet.

    Resultat: ``state`` (fra/normalt/højt nu), ``hours`` (pr. time: mål, om der varmes, minutter, kWh el, kr, tanken ved
    timens start – efter en eventuel opvarmning – og ved timens slut), næste opvarmning, tankens laveste punkt ved en
    times start og hvor mange timer den begynder under ``t_min``."""
    n = len(hours)
    learned_use = use is None
    use = m.use() if use is None else use
    # Grænsen time for time: i brugstiden »mindst« (komfort: tæt på »normalt«); uden for brugstiden ingen (komfort: »mindst«)
    bars = [(max(t_min, t_normal - m.gap) if priority == "comfort" else t_min) if use[h.hour] else (t_min if priority == "comfort" else T_LO)
            for h in hours]
    # Brugstiden begynder: tanken skal være klar ved timens start (før en opvarmning i timen) – ikke for den første time,
    # den er allerede i gang
    ready = [i > 0 and use[h.hour] and not use[(h.hour - 1) % 24] for i, h in enumerate(hours)]
    # Uden for brugstiden varmes der ikke – kun i timen lige før brugstiden begynder, så tanken er klar til den, der står
    # først op (og med »bad nu«). Ellers kunne planen varme om aftenen for at ramme varmepumpens startgrænse senere.
    may = [use[h.hour] or use[(h.hour + 1) % 24] or not any(use) for h in hours]
    bar = t_min  # det, der står i tanken over »mindst« ved periodens slutning, har værdi
    allowed = [t_normal] if priority == "economy" else [t_normal, t_high]
    k = lambda t: int(round((min(max(t, T_LO), T_HI) - T_LO) / GRID))  # noqa: E731
    val = lambda i: T_LO + i * GRID  # noqa: E731
    size = k(T_HI) + 1

    def step(i: int, t: float, g: float | None) -> tuple[float, float, float, float]:
        """Tanken gennem time ``i`` med målet ``g``: (temperatur ved timens slut, minutter opvarmning, kWh el, temperaturen
        til rådighed i timen – efter en eventuel opvarmning, før tab og forbrug)."""
        mins = kwh = 0.0
        if g is not None and t < g and (t <= g - m.gap + 1e-9 or (i == 0 and going)):
            mins, kwh, t = (g - t) / m.heat if m.heat > 0 else 0.0, (g - t) * m.el, g
        f = frac0 if i == 0 else 1.0
        return t - f * (m.loss * max(0.0, t - m.amb) + m.draw[hours[i].hour]), mins, kwh, t

    # en opvarmning i gang regnes færdig – men ikke uden for brugstiden: dér slås varmt vand fra, og rumvarmen rider på
    # den varme, varmepumpen allerede har lavet (en opvarmning, der ikke skulle være startet)
    going = running is not None and bool(hours) and not hours[0].blocked and may[0] and not boost

    def acts(i: int) -> list[float | None]:
        if i == 0 and boost:
            return [t_high]
        if i == 0 and going:
            return [running]
        return [None] if hours[i].blocked or not may[i] else [None, *allowed]

    # Baglæns: for hver time og tanktemperatur den bedste fortsættelse – først færrest gradtimer under grænsen, så billigst
    INF = (float("inf"), float("inf"))
    # Det, der står i tanken, når perioden slutter, er ikke spildt – og det, der mangler, skal varmes bagefter: begge dele
    # regnes til periodens billigste pris. Ellers ville planen varme så tidligt som muligt (mindst løft nu) i stedet for
    # så sent som muligt (mindst tab), og skubbe regningen ud over periodens kant
    cheap = min((h.price for h in hours if not h.blocked), default=0.0)
    # Det, der står over grænsen, er kun det værd, der er tilbage af det, når vandet næste gang bruges (tabet i ro i timerne
    # frem til næste brugstime efter perioden) – ellers stod en opvarmning i periodens sidste billige time lige med ingen
    end_h = (hours[-1].hour + 1) % 24 if hours else 0
    wait = next((j for j in range(24) if use[(end_h + j) % 24]), 0)
    keep = (1 - min(max(m.loss, 0.0), 0.5)) ** wait
    nxt: list[tuple[float, float]] = [(0.0, round(-(val(s) - bar) * m.el * cheap * (keep if val(s) > bar else 1.0), 3)) for s in range(size)]
    choice: list[list[float | None]] = []
    for i in range(n - 1, -1, -1):
        cur: list[tuple[float, float]] = [INF] * size
        pick: list[float | None] = [None] * size
        for s in range(size):
            t = val(s)
            for g in acts(i):
                if g is not None and t > g - m.gap + 1e-9 and not (i == 0 and (boost or going)):
                    continue  # varmepumpen ville ikke gå i gang
                t1, _mins, kwh, avail = step(i, t, g)
                tail = nxt[k(t1)]
                # manko i tiendedele grader (følerens opløsning): en tank, der mangler mindre end det, mangler intet
                c = (round(round(max(0.0, bars[i] - (t if ready[i] else avail)), 1) + tail[0], 3), round(kwh * hours[i].price + tail[1], 3))
                if c < cur[s]:
                    cur[s], pick[s] = c, g
        nxt = cur
        choice.append(pick)
    choice.reverse()
    # Forlæns fra tankens temperatur nu – på samme opløsning som baglæns, så planen er den, der blev regnet som den bedste
    t = tank
    rows: list[dict[str, Any]] = []
    for i in range(n):
        t = val(k(t))
        g = choice[i][k(t)]
        before = t
        t, mins, kwh, avail = step(i, t, g)
        rows.append({"ts": hours[i].ts, "target": g, "run": kwh > 0, "min": round(mins), "el": round(kwh, 2),
                     "kr": round(kwh * hours[i].price, 2), "start": round(before if ready[i] else avail, 1), "tank": round(t, 1),
                     "blocked": hours[i].blocked, "use": use[hours[i].hour]})
    first = rows[0] if rows else None
    state = "off" if not first or first["target"] is None else ("high" if first["target"] >= t_high - 1e-9 and t_high > t_normal else "normal")
    used = [x for x in rows if x["use"]]
    low = min(used or rows, key=lambda x: x["start"]) if rows else None  # det laveste, tanken er, når vandet bruges
    under = [x for x in used if x["start"] < t_min - 1e-9]
    nxt_run = next((x for x in rows if x["run"]), None)
    return {
        "state": state, "target": first["target"] if first else None, "hours": rows,
        "next_run": {"ts": nxt_run["ts"], "target": nxt_run["target"], "min": nxt_run["min"], "el": nxt_run["el"], "kr": nxt_run["kr"]} if nxt_run else None,
        "lowest": {"ts": low["ts"], "tank": low["start"]} if low else None,
        "under_hours": len(under), "under_from": under[0]["ts"] if under else None,
        "el": round(sum(x["el"] for x in rows), 2), "kr": round(sum(x["kr"] for x in rows), 2), "runs": sum(1 for x in rows if x["run"]),
        "use": [h for h in range(24) if use[h]] if (m.hours or not learned_use) else None,
    }
