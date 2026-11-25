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
5. Fremløb = mål + effekt ÷ gulvets afgivelse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PlanHour:
    ts: int  # timens start, unix-sekunder (UTC)
    price: float  # kr pr. kWh el
    t_out: float  # forventet udetemperatur
    blocked: bool = False  # fx rød pris: ingen varme
    # Delvist fri time: kun ``free`` af timen kan bruges – resten er blokerede kvarterer i den ene ende. Den frie del ligger
    # først i timen (fortsætter timen før) eller sidst (``free_end``: fortsætter ind i timen efter) og bruges kun sammen med
    # den nabotime, så varmepumpen aldrig starter for at blive stoppet kort efter.
    free: float = 1.0
    free_end: bool = False


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
    accuracy: float = 0.0  # rummodellens ærlige præcision (°C) uden loft – tjekkets afvigelsesgrænse
    neighbours: list[tuple[float, float]] = field(default_factory=list)  # (°C pr. time pr. grad, naboens temperatur)
    held: bool = False  # temperaturen er ikke målt nu: den sidst kendte eller forudsagt (føleren er væk)
    estimated: bool = False  # føleren har været væk i over 30 min.: temperaturen er forudsagt fra sidste måling
    t_max: float | None = None  # loft: planen varmer aldrig så meget, at rummet kommer over (luft-luft)
    share: float | None = None  # rummets målte andel af kildens varme, når kredsen er åben (0–1); None = altid sin faste andel
    t_close: float | None = None  # rummets termostat lukker kredsen ved denne temperatur (gulv + bånd); None = altid åben


@dataclass
class SourceModel:
    heat_loss_w_k: float  # H
    gains_w: float = 0.0  # G
    p_max: float = 6.0  # kW varme
    p_min: float = 2.0  # kW varme – laveste stabile
    cop: tuple[float, float, float, float] = (3.0, 0.15, -0.04, 0.0)  # a, b, c, d
    emission_kw_k: float = 0.25  # gulvets afgivelse
    flow_c0: float = 34.0  # lært fremløb ved 0 °C ude (ren rumvarme)
    flow_slope: float = -0.6  # lært fremløb: ændring pr. °C ude
    # Rimens pris: (ude, faktor) – så meget af den målte varme huset reelt får ved den udetemperatur i forhold til
    # gennemsnittet (afrimningerne tager varme fra gulvvandet, som varmemåleren ikke trækker fra). Tom = 1 overalt.
    frost: tuple[tuple[float, float], ...] = ()


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
    preload: dict[str, Any] | None = None
    deficit: float = 0.0  # største forudsagte manko under planens grænse (°C, 0 = alle rum holder)
    # Rum, der forudsiges under grænsen (til besked forud): rum, ts (laveste), temp, grænse, under, fra_ts (første time under), blokeret
    problems: list[dict[str, Any]] = field(default_factory=list)
    trimmed_kwh: float = 0.0  # varme skåret fra husets behov, fordi rummene forudsiges at holde uden
    flow_raise: float = 0.0  # så mange °C er fremløbsgrænsen hævet, fordi rummene ellers forudsiges under grænsen  # første forvarmning: fra/til, blokeringen den dækker og mængden
    # Hvad hvert trin af prioriteten ville vælge (til visning): trin, hævet (°C), kr, kwh, el_kwh og rummet, der afgør det
    # (rum, snit = gennemsnitlig manko, under/temp/ts = det laveste punkt, timer under grænsen) – tom, når alle rum holder,
    # eller fremløbet ikke kan hæves
    choices: list[dict[str, Any]] = field(default_factory=list)


STEP = 0.25  # kWh pr. portion
PRELOAD_MAX = 1.0  # forvarmning: højst så mange °C ekstra krav sidst i en lang blokering
AIR_STEP = 0.1  # kWh el pr. portion for luft-luft
FLOW_MIN, FLOW_MAX = 25.0, 45.0  # fremløbsmål, varmepumpen kan sætte


def _mg(r: PlanRoom, i: int) -> float:
    """Rummets krav over gulvet i time i: modellens usikkerhed, der vokser fra 0 nu til den fulde (6-timers) præcision.

    Komfort er et bånd – rummene må svinge op til »komfort« over gulvet – ikke et mål planen køber varme for at nå."""
    return r.margin * min(1.0, (i + 1) / 6)


def flow_for(m: SourceModel, p: float, target: float) -> float:
    """Fremløbsmålet for p kW varme: rummål + p ÷ gulvets afgivelse, begrænset og rundet til 0,5 °C."""
    f = target + (p / m.emission_kw_k if m.emission_kw_k > 0 else 0.0)
    return round(2 * max(FLOW_MIN, min(FLOW_MAX, f))) / 2


def _preload_window(hours: list[PlanHour], q: list[float], label: Callable[[int], str],
                    red_spans: list[tuple[int, int]] | None = None) -> tuple[list[float], dict[str, Any] | None, str | None]:
    """Opvarmning før rød: de sammenhængende timer med varme lige op til den første blokering.

    Giver varmen time for time (0 uden for vinduet), vinduet (fra/til, blokeringen og mængden) og en tekst.
    ``red_spans``: de røde perioder med kvarterer [(fra, til)] – blokeringens start og slut vises så præcist (fx 06:30–10:15),
    selvom planen regner i hele timer.
    """
    n = len(hours)
    pq = [0.0] * n

    def blk(i: int) -> bool:
        """Blokeret – eller en delvist fri time uden varme: dens røde kvarterer hører til blokeringen ved siden af."""
        return hours[i].blocked or (hours[i].free < 1.0 and q[i] <= 1e-9)

    first = next((i for i in range(n) if blk(i)), None)
    if not first or q[first - 1] <= 1e-9:
        return pq, None, None
    a = first - 1
    while a > 0 and q[a - 1] > 1e-9 and not blk(a - 1):
        a -= 1
    e = first
    while e + 1 < n and blk(e + 1):
        e += 1
    for i in range(a, first):
        pq[i] = q[i]
    w = {"from": hours[a].ts, "to": hours[first].ts, "block_from": hours[first].ts, "block_to": hours[e].ts + 3600,
         "kwh": round(sum(q[a:first]), 2)}
    last = hours[first - 1]
    if last.free < 1.0 and not last.free_end:
        # vinduets sidste time er delvist fri: det røde begynder, hvor dens frie del slutter
        w["to"] = w["block_from"] = last.ts + round(last.free * 3600)
    ov = [(x, y) for x, y in red_spans or [] if x < w["block_to"] and y > w["block_from"]]
    if ov:
        w["block_from"], w["block_to"] = min(x for x, _ in ov), max(y for _, y in ov)
        # Blokeringen begynder inde i vinduets sidste time (delvist fri time): opvarmningen slutter, når den begynder
        w["to"] = max(w["from"], min(w["to"], w["block_from"]))
    clk = lambda ts: label(ts).split("kl. ")[-1]  # noqa: E731
    return pq, w, f"opvarmer {label(w['from'])}–{clk(w['to'])} før blokeringen {clk(w['block_from'])}–{clk(w['block_to'])}"


FLOW_BOOST_MAX = 5  # fremløb for lavt: højst så mange °C over grænsen
# Prioritet (økonomi – balance – komfort): hvor meget af det opnåelige planen køber, når et rum forudsiges under sin grænse,
# og et højere fremløb kan rette det (forvarmning før en blokering, genopvarmning efter). Der er ingen prisgrænser: planen
# regner alle hævninger igennem – op til det, varmepumpen kan yde – og vælger den laveste, der når trinnets andel af det, der
# kan hentes. Et rum, der kan bringes helt op til sin grænse, bringes derop på alle trin.
PRIORITIES = ("economy", "balance", "comfort")
PRIORITY_SHARE = {"economy": 0.0, "balance": 0.5, "comfort": 1.0}
PRIORITY_NAMES = {"economy": "økonomi", "balance": "balance", "comfort": "komfort"}
TEMP_RES = 0.1  # °C: rumfølernes opløsning – mindre forskelle regnes ikke som en virkning
# Gulvvarme er langsom (op mod 5–6 timer, før en ændring kan mærkes): reserven venter 6 timer og ændrer højst hver 6. time
FLOW_LOW_OPEN_S = 6 * 3600
FLOW_BOOST_STEP_S = 6 * 3600


def flow_boost_step(prev: dict[str, Any] | None, now: float, rooms_low: list[str]) -> dict[str, Any]:
    """Reserve, når modellen tager fejl: hæv fremløbsgrænsen 1 °C, når et rum er under sit gulv og under det,
    modellen ventede, mens termostaten har kaldt på varme i mindst 6 timer. Gulvet er langsomt, så der går
    mindst 6 timer mellem hvert trin – op og ned.

    ``prev``/resultat: {"k": °C over grænsen, "t": sidste ændring, "rooms": rummene der kalder}.
    """
    k, t = int((prev or {}).get("k", 0)), float((prev or {}).get("t", 0.0))
    if rooms_low:
        if k == 0:
            k, t = 1, now
        elif now - t >= FLOW_BOOST_STEP_S and k < FLOW_BOOST_MAX:
            k, t = k + 1, now
    elif k > 0 and now - t >= FLOW_BOOST_STEP_S:
        k, t = k - 1, now
    return {"k": k, "t": t, "rooms": sorted(rooms_low)}


def frost_factor(m: SourceModel, t_out: float) -> float:
    """Rimens faktor ved udetemperaturen: lineært mellem de lærte bånd, fladt uden for dem; 1 uden lærte tal."""
    pts = m.frost
    if not pts:
        return 1.0
    if t_out <= pts[0][0]:
        return pts[0][1]
    for (a, x), (b, y) in zip(pts, pts[1:]):
        if t_out <= b:
            return x + (y - x) * (t_out - a) / (b - a) if b > a else y
    return pts[-1][1]


def _cop(m: SourceModel, t_out: float, p: float, target: float) -> float:
    a, b, c, d = m.cop
    return max(1.2, a + b * t_out + c * flow_for(m, p, target) + d * p)


def _el(m: SourceModel, t_out: float, p: float, target: float) -> float:
    return p / _cop(m, t_out, p, target) if p > 0 else 0.0


def simulate_rooms(rooms: list[PlanRoom], hours: list[PlanHour], q: list[float], past_q: list[float] | None = None) -> dict[str, list[float]]:
    """Forudsagt temperatur ved udgangen af hver time for hvert rum.

    Termostater: et rum med målt varmeandel (``share``) og lukketemperatur (``t_close``) får ingen varme, mens det er
    over lukketemperaturen; dets andel fordeles på de åbne rum i forhold til deres målte andele (højst 3×). Sådan går
    vandet i virkeligheden til de kredse, der stadig kalder. Rum uden andel får altid deres faste del (som før)."""
    past = list(past_q or [0.0] * 6)[-6:]
    past = [0.0] * (6 - len(past)) + past
    temps = {r.name: r.t_now for r in rooms}
    out: dict[str, list[float]] = {r.name: [] for r in rooms}
    given: dict[str, list[float]] = {r.name: [] for r in rooms}  # den varme rummet faktisk fik pr. time (efter ventil)
    shared = [r for r in rooms if r.share and r.t_close is not None]
    total = sum(r.share for r in shared)
    for i, h in enumerate(hours):
        open_share = sum(r.share for r in shared if temps[r.name] < r.t_close)
        scale = min(3.0, total / open_share) if shared and open_share > 0 else 1.0
        for r in rooms:
            t = temps[r.name]
            if r.share and r.t_close is not None:
                qe = 0.0 if t >= r.t_close else q[i] * scale
            else:
                qe = q[i]
            q_delay = given[r.name][i - 6] if i >= 6 else past[i]
            t = (t + r.heat_now * qe + r.heat_delayed * q_delay + r.cool_per_hk * (h.t_out - t) + r.free_per_h
                 + sum(k * (tn - t) for k, tn in r.neighbours))
            given[r.name].append(qe)
            temps[r.name] = t
            out[r.name].append(round(t, 2))
    return out


def estimate_room(r: PlanRoom, t0: float, ts0: float, end: float, q5: dict[int, float], out5: dict[int, float],
                  t_out: float) -> float:
    """Rummets temperatur ved ``end``, regnet frem fra sidste måling ``t0`` (kl. ``ts0``) i 5-minutters trin.

    Samme rummodel som planen: varme nu pr. kWh i trinnet, gulvets forsinkede varme fra 6 timer før, afkøling mod
    ude, gratis varme og naboer (timeleddene × 5/60). ``q5`` er kWh pr. 5-minutters trin (trinnets start, sekunder),
    ``out5`` udetemperaturen pr. trin (ellers ``t_out``). Et trin uden måling af tilførslen tæller som 0 kWh."""
    step = 300
    t, ts = t0, ts0
    while ts < end - 1:
        b = int(ts // step * step)
        nxt = min(b + step, end)
        f = (nxt - ts) / step  # andel af trinnet: det første og det sidste kan være delvise
        q = max(q5.get(b, 0.0), 0.0) * f
        qd = max(q5.get(b - 6 * 3600, 0.0), 0.0) * f
        h = f * step / 3600
        t = (t + r.heat_now * q + r.heat_delayed * qd
             + (r.cool_per_hk * (out5.get(b, t_out) - t) + r.free_per_h + sum(k * (tn - t) for k, tn in r.neighbours)) * h)
        ts = nxt
    return t


def make_plan(hours: list[PlanHour], model: SourceModel, rooms: list[PlanRoom], target: float,
              max_pause: int = 0, past_q: list[float] | None = None,
              label: Callable[[int], str] | None = None, boost: float = 0.0, boost_hours: int = 0,
              red_allowance: float = 0.0, preload: float = 0.0, flow_margin: float | None = None, comfort: float = 0.0,
              economy: float = 0.0, flow_raise_max: int = FLOW_BOOST_MAX,
              red_spans: list[tuple[int, int]] | None = None, running: bool | None = None,
              priority: str = "economy", flow_raise_free: int = 0, busy: list[float] | None = None) -> Plan:
    """Planen. Forudsiger modellen, at et rum kommer under planens grænse, fordi fremløbsgrænsen holder varmen nede,
    regnes planen igennem med grænsen hævet 1 °C ad gangen (højst ``flow_raise_max``, som kalderen sætter efter det,
    varmepumpen kan yde) – i god tid, da gulvet er langsomt. ``priority`` (økonomi – balance – komfort) afgør, hvilken
    hævning der vælges (se ``raise_choices``); de tre trins valg står i ``choices``, så prisen og virkningen kan vises.

    ``flow_raise_free``: så mange af graderne hører til almindelig drift (op til fremløbsloftet, når planen regner med et
    lavere mål fra en søgning efter laveste fremløb): dem bruger alle trin, når de hjælper.
    ``running``: varmekilden kører lige nu (afgør, om en delvist fri første time kan fortsætte kørslen); None = ukendt, så
    bruges den målte varme i timen før.
    ``busy``: den del af hver time (0–1), varmekilden bruger på andet end rumvarme – varmt vand. Timen rummer så tilsvarende
    mindre rumvarme (ved samme effekt), men er ellers en almindelig time."""
    args = (hours, model, rooms, target, max_pause, past_q, label, boost, boost_hours, red_allowance, preload, comfort, economy)
    _once = _plan_once
    if busy and any(busy):
        def _once(*a: Any, **k: Any) -> Plan:  # noqa: ANN401
            return _plan_once(*a, busy=busy, **k)
    base = _once(*args, flow_margin, red_spans=red_spans, running=running)
    if flow_margin is None or base.deficit <= 0.05 or flow_raise_max <= 0:
        return base
    plans = [base]
    for k in range(1, flow_raise_max + 1):
        p = _once(*args, flow_margin + k, red_spans=red_spans, running=running)
        plans.append(p)
        if p.deficit <= 0.05:
            break  # alle rum holder: flere grader giver intet
        if model.emission_kw_k > 0 and all((model.flow_c0 + model.flow_slope * h.t_out + flow_margin + k - target)
                                           * model.emission_kw_k >= model.p_max for h in hours):
            break  # grænsen ligger over det fremløb, varmepumpens største effekt giver: flere grader ændrer intet
    ks = raise_choices(plans, flow_raise_free)
    k = ks.get(priority, ks["economy"])
    best = plans[k]
    best.flow_raise = float(k)
    best.choices = _choice_rows(plans, ks)
    if k:
        # Rummet, der får mest ud af hævningen – målt som gennemsnitlig manko i de timer, det er under grænsen
        names, _worst, mean, _hrs = _short([base, best])
        rum = max(names, key=lambda r: mean[0][r] - mean[1][r])
        kr = best.cost_kr - base.cost_kr
        # Trinnet nævnes, når det har betydning (de tre trin vælger ikke det samme)
        why = f"{PRIORITY_NAMES.get(priority, priority)}, " if len(set(ks.values())) > 1 else ""
        best.reason += (f" – fremløbsgrænsen hævet {k} °C ({why}{kr:+.1f} kr)"
                        + (", så rummene forudsiges at holde" if best.deficit <= 0.05
                           else f": rummene holder ikke helt, men det hjælper ({rum} i snit {mean[0][rum] - mean[1][rum]:.2f} °C"
                                " mindre under grænsen)"))
    return best


def _under(p: Plan) -> dict[str, float]:
    """Hvert rums største manko under grænsen (°C)."""
    return {x["rum"]: float(x["under"]) for x in p.problems}


def _short(plans: list[Plan]) -> tuple[list[str], list[dict[str, float]], list[dict[str, float]], dict[str, int]]:
    """Rummenes manko i hver plan: navne, største manko, gennemsnitlig manko og timerne, gennemsnittet er taget over.

    Gennemsnittet er gradtimerne under grænsen delt med det største antal timer, rummet er under i nogen af planerne –
    samme nævner for alle planer, så de kan sammenlignes. Det måler både dybde og varighed: et rum, der er under sit gulv
    lige nu, kan ingen plan rette i den første time, men en plan, der henter det hurtigere ind, har færre gradtimer."""
    names = sorted({x["rum"] for p in plans for x in p.problems})
    rows = [{x["rum"]: x for x in p.problems} for p in plans]
    hrs = {r: max(1, max(int(x[r].get("timer", 1)) for x in rows if r in x)) for r in names}
    worst = [{r: (float(x[r]["under"]) if r in x else 0.0) for r in names} for x in rows]
    mean = [{r: (float(x[r].get("gh", x[r]["under"])) / hrs[r] if r in x else 0.0) for r in names} for x in rows]
    return names, worst, mean, hrs


def raise_choices(plans: list[Plan], free: int = 0) -> dict[str, int]:
    """Hvilken hævning af fremløbsgrænsen hvert trin af prioriteten vælger (indeks i ``plans``: 0 = ingen, 1 = +1 °C …).

    Der er ingen prisgrænser. For hvert rum, der forudsiges under sin grænse, ses på den gennemsnitlige manko i de timer,
    rummet er under (se ``_short``): uden hævning og det bedste, nogen hævning kan give.
    - Et rum, hævningerne ikke flytter mindst følernes opløsning (fx en svag kreds, eller et rum der er koldt lige nu og
      hentes lige hurtigt ind uanset), bestemmer ikke.
    - Et rum, en hævning kan bringe helt op til grænsen, bringes derop på alle trin.
    - Ellers køber trinnet sin andel af det, der kan hentes: økonomi intet, balance halvdelen, komfort det hele.
    Der vælges den laveste hævning, der når det for alle rum inden for en halv opløsning (ellers den, der kommer tættest
    på) – aldrig en, der gør huset koldere end uden hævning (det værste rum eller summen). De første ``free`` grader er
    almindelig drift op til loftet: dem bruger alle trin, så langt de hjælper."""
    names, worst, mean, _hrs = _short(plans)
    tol = TEMP_RES / 2
    tot0 = sum(mean[0].values())
    ok = [k for k in range(len(plans))
          if k == 0 or (plans[k].deficit <= plans[0].deficit + 1e-9 and sum(mean[k].values()) <= tot0 + 1e-9)]

    def pick(cands: list[int], start: int, share: float) -> int:
        """Den laveste hævning blandt ``cands``, der fra ``start`` henter ``share`` af det, de kan give hvert rum."""
        goal: dict[str, float | None] = {}
        for r in names:
            gain = mean[start][r] - min(mean[k][r] for k in cands)
            if gain < TEMP_RES - 1e-9:
                continue  # ingen målbar virkning for rummet
            # None: rummet kan holde sin grænse – så skal det; ellers trinnets andel af det, der kan hentes
            goal[r] = None if any(worst[k][r] <= tol for k in cands) else mean[start][r] - share * gain

        def miss(k: int) -> float:
            return round(sum(max(0.0, worst[k][r] - tol) if g is None else max(0.0, mean[k][r] - g - tol)
                             for r, g in goal.items()), 6)

        return min(cands, key=lambda k: (miss(k), k))

    k_e = pick([k for k in ok if k <= free], 0, 1.0)  # almindelig drift: det bedste, loftet kan give
    beyond = [k for k in ok if k >= k_e]
    return {name: pick(beyond, k_e, share) for name, share in PRIORITY_SHARE.items()}


def _choice_rows(plans: list[Plan], ks: dict[str, int]) -> list[dict[str, Any]]:
    """De tre trins valg til visning: hævning, pris, varme og el – og rummet, der afgør det (det, der vinder mest mellem
    økonomi og komfort; ellers det koldeste i forhold til sin grænse) med temperatur og tidspunkt i hver plan."""
    names, _worst, mean, _hrs = _short(plans)
    if not names:
        return []
    lo, hi = mean[ks["economy"]], mean[ks["comfort"]]
    rum = max(names, key=lambda r: (round(lo[r] - hi[r], 2), lo[r]))
    rows = []
    for name in PRIORITIES:
        p = plans[ks[name]]
        x = next((y for y in p.problems if y["rum"] == rum), None)
        rows.append({"trin": name, "hævet": ks[name], "kr": p.cost_kr, "kwh": p.heat_kwh, "el_kwh": p.el_kwh, "rum": rum,
                     "snit": round(mean[ks[name]][rum], 2),  # gennemsnitlig manko i de timer, rummet er under (det, der vælges efter)
                     "under": x["under"] if x else 0.0, "temp": x["temp"] if x else None, "ts": x["ts"] if x else None,
                     "timer": int(x.get("timer", 1)) if x else 0})
    return rows


def _plan_once(hours: list[PlanHour], model: SourceModel, rooms: list[PlanRoom], target: float,
              max_pause: int = 0, past_q: list[float] | None = None,
              label: Callable[[int], str] | None = None, boost: float = 0.0, boost_hours: int = 0,
              red_allowance: float = 0.0, preload: float = 0.0, comfort: float = 0.0, economy: float = 0.0,
              flow_margin: float | None = None, red_spans: list[tuple[int, int]] | None = None,
              running: bool | None = None, busy: list[float] | None = None) -> Plan:
    """Planen for de givne timer. ``target`` er den indetemperatur behovet regnes ud fra.

    ``boost`` (»jeg fryser«) hæver målet og rummenes min. i de første ``boost_hours`` timer.
    ``red_allowance``: så meget må et rum falde under sit min. i blokerede timer.
    ``preload``: bevidst forvarmning – i blokerede timer hæves rummenes krav med så mange °C pr. 6 timer inde
    i blokeringen (højst ``PRELOAD_MAX``). Jo længere gulvene skal være slukket, jo mere lades de op før.
    """
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan

    def limit(r: PlanRoom, i: int) -> float:
        """Laveste tilladte temperatur for rummet ved udgangen af time i."""
        return r.t_min + (boost if i < boost_hours else 0.0) - economy - (red_allowance if hours[i].blocked else 0.0)

    # Forvarmning: kravet stiger med antal timer inde i en blokering (time 1 i en blokering: preload/6 osv.)
    pre: list[float] = []
    run = 0
    for h in hours:
        run = run + 1 if h.blocked else 0
        pre.append(min(PRELOAD_MAX, max(preload, 0.0) * run / 6))

    demand = [max(0.0, (model.heat_loss_w_k * (target + (boost if i < boost_hours else 0.0) - h.t_out) - model.gains_w) / 1000)
              for i, h in enumerate(hours)]
    plan.demand_kwh = round(sum(demand), 2)
    q = [0.0] * n
    # Timens frie del (0 = blokeret, 1 = hel time). En delvist fri time rummer kun den del af effekten og kan kun bruges
    # som fortsættelse af nabotimen på den frie side (se ``usable``).
    fr = [0.0 if h.blocked else min(1.0, max(0.0, h.free)) for h in hours]
    # Varmt vand tager en del af timen (``busy``): den del af den frie tid, der er tilbage til rumvarme, bærer energien –
    # samme effekt i kortere tid. Timen er stadig en almindelig time (ingen krav om nabotime som for delvist frie timer).
    av = [fr[i] * (1.0 - min(0.9, max(0.0, busy[i]))) if busy and i < len(busy) else fr[i] for i in range(n)]
    # Rimens pris: i rimvejr får huset mindre af den målte varme (afrimningerne tager varme fra gulvvandet) – timen
    # rummer tilsvarende mindre og koster mere pr. kWh, huset får. Planens varme (``q``) er den, huset får.
    fg = [frost_factor(model, h.t_out) for h in hours]
    free = [i for i in range(n) if fr[i] > 0]
    # Kører varmen lige nu? Det afgør, om en delvist fri første time er en fortsættelse (ukendt: målt varme i timen før)
    ran_before = running if running is not None else (bool(past_q) and past_q[-1] > 1e-9)

    def usable(i: int) -> bool:
        """En delvist fri time kan kun bruges, når nabotimen på den frie side varmer (samme kørsel)."""
        if fr[i] >= 1.0:
            return True
        j = i + 1 if hours[i].free_end else i - 1
        return ran_before if j < 0 else (j < n and q[j] > 1e-9)

    def orphans() -> list[int]:
        """Delvist frie timer med varme, hvis nabotime ikke (længere) varmer."""
        return [i for i in free if fr[i] < 1.0 and q[i] > 0 and not usable(i)]

    def el(i: int, e: float) -> float:
        """El (kWh) for ``e`` kWh varme i time i. I en delvist fri time leveres varmen på den frie del – ved højere effekt."""
        return av[i] * _el(model, hours[i].t_out, e / (av[i] * fg[i]), target) if e > 0 else 0.0

    # Lavt og jævnt: fremløbet må højst ligge ``flow_margin`` °C over det lærte fremløb ved timens
    # udetemperatur. Det begrænser effekten pr. time, så varmen spredes over flere timer (højere COP, færre afrimninger).
    pcap = [model.p_max] * n
    if flow_margin is not None and model.emission_kw_k > 0:
        for i, h in enumerate(hours):
            fmax = model.flow_c0 + model.flow_slope * h.t_out + flow_margin
            pcap[i] = min(model.p_max, max(model.p_min, (fmax - target) * model.emission_kw_k))

    def add_cost(i: int, amount: float) -> float:
        """Ekstra el-pris (kr) for at lægge ``amount`` kWh mere i time i."""
        p0, p1 = q[i], q[i] + amount
        if p1 > pcap[i] * av[i] * fg[i] + 1e-9:
            return float("inf")
        return hours[i].price * (el(i, p1) - el(i, p0))

    def add_cheapest(cands: list[int], amount_left: float) -> float:
        """Læg en portion i den billigste kandidat-time. Giver mængden tilbage (0 = ingen plads)."""
        best, best_c, best_a = None, float("inf"), 0.0
        for i in cands:
            if not usable(i):
                continue
            a = max(STEP, model.p_min * av[i] * fg[i]) if q[i] == 0 else STEP
            c = add_cost(i, a) / a
            if c < best_c:
                best, best_c, best_a = i, c, a
        if best is None:
            return 0.0
        q[best] += best_a
        return best_a

    # 2) vandfyldning til husets behov
    need = sum(demand)
    guard = 0
    while sum(q) < need - 1e-6 and guard < 2000:
        guard += 1
        if not add_cheapest(free, need - sum(q)):
            plan.warnings.append("behovet kan ikke dækkes i de frie timer")
            break

    # 3) rummene: kommer et rum under min + usikkerhed, lægges varme i den billigste frie time før.
    #    Kan en time ikke rettes (fx rød lige nu, eller timerne før er fulde), springes netop den time
    #    over, og tjekket fortsætter – så der stadig varmes op før næste blokering. Husets behov i de
    #    blokerede timer må flyttes frem; derudover kan hvert rum højst kræve 10 kWh ekstra, så ét svagt
    #    rum ikke trækker hele planen. Ved samme pris lægges varmen så sent som muligt før behovet
    #    (mindre går tabt, og gulvets forsinkede varme når ind i blokeringen).
    skip: set[tuple[str, int]] = set()
    extra: dict[str, float] = {}
    cap = 10.0 + sum(d for d, h in zip(demand, hours) if h.blocked)
    for _ in range(1500):
        sim = simulate_rooms(rooms, hours, q, past_q)
        worst: tuple[int, PlanRoom] | None = None
        for r in rooms:
            for i, t in enumerate(sim[r.name]):
                if t < limit(r, i) + _mg(r, i) + pre[i] and (r.name, i) not in skip:
                    if worst is None or i < worst[0]:
                        worst = (i, r)
                    break
        if worst is None:
            break
        i_bad, r = worst
        if extra.get(r.name, 0.0) >= cap:
            skip.update((r.name, k) for k in range(i_bad, n))
            continue
        added = add_cheapest([j for j in reversed(free) if j <= i_bad], STEP)
        if added:
            extra[r.name] = extra.get(r.name, 0.0) + added
        else:
            skip.add((r.name, i_bad))

    # 3b) skær overskud væk: husets behov er kun et udgangspunkt. Holder rummene sig over kravet uden en portion
    #     (fordi de er varme nu, eller gulvet har varme gemt), fjernes den – dyreste først. Så sænkes effekten og
    #     dermed fremløbet, når modellen forudser, at rummene klarer sig med mindre. Uden rummodeller: intet skæres.
    if rooms:
        def need_ok(sim2: dict[str, list[float]], base: dict[str, list[float]]) -> bool:
            return all(t >= limit(r, i) + _mg(r, i) + pre[i] - 1e-6 or t >= b - 1e-6
                       for r in rooms for i, (t, b) in enumerate(zip(sim2[r.name], base[r.name])))

        sim = simulate_rooms(rooms, hours, q, past_q)
        for _ in range(400):
            # Dyreste sidste portion først (pris × ekstra el for den, inkl. lavere COP ved højere fremløb)
            cands = sorted((i for i in free if q[i] > 0), key=lambda i: -hours[i].price * (el(i, q[i]) - el(i, max(q[i] - STEP, 0.0))))
            cut = False
            for i in cands:
                old = q[i]
                q[i] = old - STEP if old - STEP >= model.p_min * av[i] * fg[i] - 1e-9 else 0.0
                # Falder nabotimen væk, falder den delvist frie time med (den kan ikke stå alene)
                gone = {j: q[j] for j in orphans()}
                for j in gone:
                    q[j] = 0.0
                sim2 = simulate_rooms(rooms, hours, q, past_q)
                if need_ok(sim2, sim):
                    sim, cut = sim2, True
                    plan.trimmed_kwh = round(plan.trimmed_kwh + old - q[i] + sum(gone.values()), 2)
                    break
                q[i] = old
                for j, v in gone.items():
                    q[j] = v
            if not cut:
                break

    # 4) for lange pauser brydes med pausens billigste frie time
    if max_pause > 0:
        for _ in range(50):
            run, hit = 0, None
            for i in range(n):
                if q[i] > 0 or fr[i] < 1.0:  # blokeret eller delvist fri: tæller ikke som pause og brydes ikke
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

    for i in orphans():  # sikkerhedsnet: en delvist fri time står aldrig alene
        q[i] = 0.0

    # 5) resultat time for time. Der advares kun, når et rum ventes under selve min. (ikke min. + usikkerhed).
    plan.rooms = simulate_rooms(rooms, hours, q, past_q)
    lab = label or (lambda ts: f"om {(ts - hours[0].ts) // 3600} t")
    for r in rooms:
        s = plan.rooms[r.name]
        k = min(range(n), key=lambda j: s[j] - limit(r, j))
        if s[k] < limit(r, k):
            plan.warnings.append(f"{r.name}: {s[k]:.1f} °C {lab(hours[k].ts)} er under planens grænse ({limit(r, k):.1f})")
            plan.deficit = max(plan.deficit, round(limit(r, k) - s[k], 2))
            first = next((j for j in range(n) if s[j] < limit(r, j)), k)
            short = [max(0.0, limit(r, j) - s[j]) for j in range(n)]
            # gh: gradtimer under grænsen i hele perioden, timer: antal timer under – dybde og varighed (til prioriteten)
            plan.problems.append({"rum": r.name, "ts": hours[k].ts, "temp": round(s[k], 1), "grænse": round(limit(r, k), 1),
                                  "under": round(limit(r, k) - s[k], 2), "fra_ts": hours[first].ts, "blokeret": bool(hours[k].blocked),
                                  "gh": round(sum(short), 2), "timer": sum(1 for v in short if v > 0)})
    pq, plan.preload, pre_txt = _preload_window(hours, q, lab, red_spans)
    for i, h in enumerate(hours):
        p = round(q[i], 2)  # kWh varme i timen (= kW i en hel time)
        # effekten, mens der varmes (højere i en delvist fri time, en time med varmt vand og i rimvejr)
        pw = p / (av[i] * fg[i]) if p > 0 else 0.0
        flow = flow_for(model, pw, target) if p > 0 else None
        # COP for den varme, huset får: i rimvejr skal varmepumpen lave mere, end huset får (faktoren)
        cop = _cop(model, h.t_out, pw, target) * fg[i] if p > 0 else None
        e_el = p / cop if cop else 0.0
        plan.hours.append({"ts": h.ts, "heat_kw": p, "flow": flow,
                           "price": round(h.price, 3), "t_out": round(h.t_out, 1), "blocked": h.blocked,
                           "cop": round(cop, 2) if cop else None, "cost": round(e_el * h.price, 2),
                           "preload": round(pq[i], 2), **({"free": round(fr[i], 2)} if 0 < fr[i] < 1 else {}),
                           **({"dhw": round(1 - av[i] / fr[i], 2)} if fr[i] > 0 and av[i] < fr[i] - 1e-9 else {}),
                           **({"frost": round(fg[i], 3)} if abs(fg[i] - 1) >= 0.005 else {})})
        plan.heat_kwh += p
        plan.el_kwh += e_el
        plan.cost_kr += e_el * h.price
    plan.heat_kwh, plan.el_kwh, plan.cost_kr = round(plan.heat_kwh, 1), round(plan.el_kwh, 1), round(plan.cost_kr, 1)
    on = sum(1 for x in plan.hours if x["heat_kw"] > 0)
    # Forvarmning: hvornår der varmes op til en senere blokering (de timer, rummene ellers ville komme under kravet)
    notes: list[str] = []
    if pre_txt:
        notes.append(pre_txt)
    if plan.trimmed_kwh >= 0.5:
        notes.append(f"{plan.trimmed_kwh:.1f} kWh varme mindre end husets behov – rummene forudsiges at holde uden")
    plan.reason = (f"{on} timer, {plan.heat_kwh} kWh varme for ca. {plan.cost_kr} kr" + "".join(" – " + x for x in notes)
                   + (" – " + "; ".join(plan.warnings) if plan.warnings else ""))
    return plan


def make_air_plan(hours: list[PlanHour], rooms: list[PlanRoom], e_min: float = 0.3, e_max: float = 1.0,
                  past_q: list[float] | None = None, label: Callable[[int], str] | None = None,
                  preload: float = 0.0, comfort: float = 0.0, red_allowance: float = 0.0, economy: float = 0.0,
                  eff_curve: list[tuple[float, float]] | None = None, red_spans: list[tuple[int, int]] | None = None) -> Plan:
    """Plan for en luft-luft-varmepumpe i kWh el pr. time (rummodellerne lærer grader pr. kWh el).

    ``eff_curve``: stigens lærte virkning [(kW el, °C pr. kWh el)] pr. trin. Med mindst tre punkter regner planen med, at
    de høje trin giver mindre varme pr. kWh el: rummene får el × virkning (i forhold til virkningen midt i området), og en
    portion lægges i den time, hvor den giver mest varme pr. krone – så spredes varmen over flere timer på lavere trin,
    når det kan betale sig (lært, ikke en fast regel).

    Varmen måles ikke, så der er intet husbehov at fordele. Planen varmer kun så meget, at rummene holder
    gulv + komfort + usikkerhed (+ forvarmning i blokerede timer), i de billigste frie timer før behovet
    (ved samme pris så sent som muligt), og aldrig så meget, at et rum kommer over sit loft (``t_max``).
    Rum uden gulv (``t_min`` −99, ikke med i planen) har kun loft. Der varmes aldrig i blokerede timer.
    ``e_min``/``e_max``: laveste og højeste el pr. time, når varmepumpen kører.
    """
    n = len(hours)
    plan = Plan()
    if n == 0:
        plan.reason = "ingen timer at planlægge"
        return plan
    pre: list[float] = []
    run = 0
    for h in hours:
        run = run + 1 if h.blocked else 0
        pre.append(min(PRELOAD_MAX, max(preload, 0.0) * run / 6))
    q = [0.0] * n
    free = [i for i, h in enumerate(hours) if not h.blocked]
    planned = [r for r in rooms if r.t_min > -50]
    capped = [r for r in rooms if r.t_max is not None]
    pts = sorted((float(k), float(e)) for k, e in (eff_curve or []) if k and e and k > 0 and e > 0)
    use_eff = len(pts) >= 3

    def eff(x: float) -> float:
        """Lært virkning (°C pr. kWh el) ved x kW el – lineært mellem trinene, fladt udenfor."""
        if x <= pts[0][0]:
            return pts[0][1]
        for (k0, e0), (k1, e1) in zip(pts, pts[1:]):
            if x <= k1:
                return e0 + (e1 - e0) * (x - k0) / (k1 - k0) if k1 > k0 else e1
        return pts[-1][1]

    e_ref = eff((e_min + e_max) / 2) if use_eff else 1.0

    def qe(x: float) -> float:
        """El omsat til rummenes enhed: el × virkning i forhold til virkningen midt i området."""
        return x * eff(x) / e_ref if use_eff and x > 0 else x

    def sim_q() -> dict[str, list[float]]:
        return simulate_rooms(rooms, hours, [qe(x) for x in q], past_q)

    def need(r: PlanRoom, i: int) -> float:
        # Brugerens valg: i blokerede (røde) timer må rummet gå op til ``red_allowance`` under gulvet for at spare
        return r.t_min + _mg(r, i) + pre[i] - economy - (red_allowance if hours[i].blocked else 0.0)

    def floor_at(r: PlanRoom, i: int) -> float:
        return r.t_min - economy - (red_allowance if hours[i].blocked else 0.0)

    def new_over_cap(sim: dict[str, list[float]], base: dict[str, list[float]]) -> bool:
        """Kommer et rum over loftet (eller længere over) end uden den nye portion?"""
        return any(t > r.t_max + 1e-6 and t > b + 1e-6 for r in capped for t, b in zip(sim[r.name], base[r.name]))

    skip: set[tuple[str, int]] = set()
    full: set[int] = set()
    sim = sim_q()

    def mcost(j: int) -> float:
        """Pris pr. varmeenhed for den næste portion i time j (uden lært virkning: bare prisen)."""
        if not use_eff:
            return hours[j].price
        a = e_min if q[j] == 0 else min(AIR_STEP, e_max - q[j])
        gain = (qe(q[j] + a) - qe(q[j])) / a if a > 0 else 0.0
        return hours[j].price / max(gain, 0.05)

    for _ in range(800):
        worst: tuple[int, PlanRoom] | None = None
        for r in planned:
            for i, t in enumerate(sim[r.name]):
                if t < need(r, i) and (r.name, i) not in skip:
                    if worst is None or i < worst[0]:
                        worst = (i, r)
                    break
        if worst is None:
            break
        i_bad, r = worst
        # Billigste frie time før behovet; ved samme pris den seneste (stabil sortering af en omvendt liste)
        cands = sorted((j for j in reversed(free) if j <= i_bad and j not in full and q[j] < e_max - 1e-9), key=mcost)
        added = False
        for j in cands:
            a = e_min if q[j] == 0 else min(AIR_STEP, e_max - q[j])
            q[j] += a
            sim2 = sim_q()
            if capped and new_over_cap(sim2, sim):
                q[j] -= a
                full.add(j)
                continue
            sim, added = sim2, True
            break
        if not added:
            skip.add((r.name, i_bad))

    plan.rooms = sim
    lab = label or (lambda ts: f"om {(ts - hours[0].ts) // 3600} t")
    for r in planned:
        s = sim[r.name]
        k = min(range(n), key=lambda j: s[j] - floor_at(r, j))
        if s[k] < floor_at(r, k):
            plan.warnings.append(f"{r.name}: {s[k]:.1f} °C {lab(hours[k].ts)} er under gulvet ({r.t_min:.1f}"
                                 + (f", må gå {red_allowance:.1f} under i rød)" if red_allowance and hours[k].blocked else ")"))
            first = next((j for j in range(n) if s[j] < floor_at(r, j)), k)
            plan.problems.append({"rum": r.name, "ts": hours[k].ts, "temp": round(s[k], 1), "grænse": round(floor_at(r, k), 1),
                                  "under": round(floor_at(r, k) - s[k], 2), "fra_ts": hours[first].ts, "blokeret": bool(hours[k].blocked)})
    notes: list[str] = []
    for r in capped:
        if r.t_now > r.t_max:
            notes.append(f"{r.name} er over loftet ({r.t_now:.1f} mod {r.t_max:.1f} °C) – varmer ikke, før det er faldet")
    sp = (min(r.t_max for r in capped) - 0.5) if capped else None
    pq, plan.preload, pre_txt = _preload_window(hours, q, lab, red_spans)
    for i, h in enumerate(hours):
        e = round(q[i], 2)
        setpoint = None
        if e > 0:
            setpoint = sp if sp is not None else round(2 * (max((need(r, i) for r in planned), default=21.0) + 1.0)) / 2
        plan.hours.append({"ts": h.ts, "heat_kw": e, "el_kw": e, "flow": None, "setpoint": setpoint,
                           "price": round(h.price, 3), "t_out": round(h.t_out, 1), "blocked": h.blocked, "cop": None,
                           "cost": round(e * h.price, 2), "preload": round(pq[i], 2)})
        plan.el_kwh += e
        plan.cost_kr += e * h.price
    plan.el_kwh = round(plan.el_kwh, 1)
    plan.heat_kwh = plan.el_kwh  # luft-luft: varmen måles ikke – tallene er el
    plan.cost_kr = round(plan.cost_kr, 1)
    on = sum(1 for x in plan.hours if x["heat_kw"] > 0)
    if pre_txt:
        notes.insert(0, pre_txt)
    plan.reason = (f"{on} timer, {plan.el_kwh} kWh el for ca. {plan.cost_kr} kr" + "".join(" – " + x for x in notes)
                   + (" – " + "; ".join(plan.warnings) if plan.warnings else ""))
    return plan


def plan_input_dump(hours: list[PlanHour], model: SourceModel, rooms: list[PlanRoom], target: float, **kw: Any) -> dict[str, Any]:
    """Planens input som JSON uden personlige oplysninger: timerne som »timer fra nu«, rummene som rum_1, rum_2 …
    Bruges i diagnostikken, så en plan kan genkøres uden for huset (``plan_from_dump``, tools/replay_plan.py)."""
    t0 = hours[0].ts if hours else 0
    spans = kw.get("red_spans")
    return {
        "hours": [{"h": (h.ts - t0) // 3600, "price": h.price, "t_out": h.t_out, "blocked": h.blocked,
                   **({"free": h.free, "free_end": h.free_end} if h.free < 1 else {})} for h in hours],
        "model": asdict(model),
        "rooms": [{**asdict(r), "name": f"rum_{i + 1}"} for i, r in enumerate(rooms)],
        "target": target,
        "kw": {**{k: v for k, v in kw.items() if k not in ("label", "red_spans")},
               "red_spans": [[a - t0, b - t0] for a, b in spans] if spans else None},
    }


def plan_from_dump(d: dict[str, Any], **override: Any) -> Plan:
    """Genkør en plan fra ``plan_input_dump`` (tiderne starter ved 0). ``override`` erstatter enkelte argumenter,
    fx ``flow_margin`` eller ``flow_raise_max`` – til at afprøve en ændring mod en rigtig situation."""
    hours = [PlanHour(ts=int(x["h"]) * 3600, price=x["price"], t_out=x["t_out"], blocked=bool(x["blocked"]),
                      free=float(x.get("free", 1.0)), free_end=bool(x.get("free_end", False))) for x in d["hours"]]
    model = SourceModel(**{**d["model"], "cop": tuple(d["model"]["cop"])})
    rooms = [PlanRoom(**{**r, "neighbours": [tuple(n) for n in r.get("neighbours") or []]}) for r in d["rooms"]]
    kw = dict(d.get("kw") or {})
    spans = kw.pop("red_spans", None)
    kw["red_spans"] = [(a, b) for a, b in spans] if spans else None
    max_pause = int(kw.pop("max_pause", 0) or 0)
    kw.update(override)
    return make_plan(hours, model, rooms, d["target"], max_pause, **kw)
