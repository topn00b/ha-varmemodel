"""Styring af en luft-luft-varmepumpe: en stige af trin (ren Python, ingen Home Assistant).

Stigen: først blæseren op (laveste trin kan være varmepumpens »quiet«), så setpunktet op – blæseren
højst til den valgte maks. (dag eller nat). Planen siger, om der skal varmes; stigen siger, hvor hårdt:

- op ét trin pr. kvarter, når et rum er under sit gulv (+ forvarmning før en blokering)
- ellers mod planens el-mål for timen: det laveste trin, hvis lærte forbrug dækker målet (planen har valgt
  timer og mængde efter pris) – højst ét trin pr. kvarter op og pr. 45 min. ned
- uden nok lærte trin: ned ét trin, når alle rum har været over gulv + deres usikkerhed i 30 min.
- start på det trin, der sidst holdt rummene ved samme udetemperatur (lært pr. 5 °C)

Setpunkterne flyder: de regnes fra rummenes gulv og klimaentitetens grænser. Brugeren vælger, hvor meget
blæseren må larme (maks. blæser dag og nat) og hvor lavt den må gå (min. blæser – fx når et fjernt rum ikke får del i
varmen ved lav blæser). Intet forskydes kunstigt.

Det, der er lært på ét trin, deles med de andre (``w_lookup``/``eff_lookup``): nat og dag har hver sin maks. blæser og
dermed hver sine trin, men samme setpunkt over gulvet. Et trin uden egne målinger bruger de andre blæseres tal for samme
setpunkt (»delt«), og mangler også de, et skøn mellem de nærmeste lærte setpunkter (»skøn«).
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
                 quiet: bool = False, floor: str | None = None) -> list[Step]:
    """Trinene: (quiet) → blæser op til ``cap`` ved ``sp_min`` → setpunktet op til ``sp_max`` ved ``cap``.

    ``floor``: den laveste blæser, stigen må bruge (min. blæser). Trinene under den – og »quiet« – udgår. Ligger den over
    ``cap`` (fx om natten), vinder ``cap``: støjgrænsen er brugerens udtrykkelige valg, og stigen er så kun setpunkter."""
    if cap in fans:
        fans = fans[: fans.index(cap) + 1]
    if floor in fans:
        fans, quiet = fans[fans.index(floor):], False
    elif floor is not None and floor != "quiet" and fans:
        fans, quiet = fans[-1:], False  # min. blæser ligger over maks.: kun maks.
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


def target_step(ladder_w: list[float | None], target_w: float | None) -> int | None:
    """Det laveste trin, hvis lærte forbrug (W) dækker målet. None uden mål eller med under to lærte trin."""
    known = [(i, w) for i, w in enumerate(ladder_w) if w is not None]
    if target_w is None or len(known) < 2:
        return None
    if target_w <= 0:
        return 0
    hit = next((i for i, w in known if w >= target_w), None)
    return hit if hit is not None else known[-1][0]


def choose_step(cur: int | None, n: int, margins: dict[str, float], down_ok: bool, now: float,
                changed: float, down_since: float | None, start: int | None = None,
                target: int | None = None, up_every: int = UP_EVERY, rising: dict[str, float] | None = None,
                min_rise: float = 0.2, rise_txt: str = "") -> tuple[int, float, float | None, str]:
    """Næste trin. ``margins``: rum → målt − (gulv + forvarmning). ``down_ok``: alle rum over gulv + usikkerhed.
    ``target``: trinnet der dækker planens el-mål (None = ukendt). Giver (trin, ændret, ned-siden, grund)."""
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
        r = (rising or {}).get(low) if rising is not None else None
        if r is not None and r >= min_rise:
            # Rolig: trinnet virker – rummet stiger nok, så det får lov at holde
            return cur, changed, None, f"{low} er {abs(margins[low]):.1f} °C under gulvet, men stiger {r:.2f} °C/t på trin {cur}{rise_txt} – holder"
        if now - changed >= up_every:
            if r is not None:
                return cur + 1, now, None, f"{low} er {abs(margins[low]):.1f} °C under gulvet og stiger kun {r:+.2f} °C/t – et trin op"
            return cur + 1, now, None, f"{low} er {abs(margins[low]):.1f} °C under gulvet – et trin op"
        return cur, changed, None, f"{low} er under gulvet – venter (højst ét trin pr. {'kvarter' if up_every <= 900 else 'halve time'})"
    if target is not None and target > cur:
        if now - changed >= up_every:
            return cur + 1, now, None, f"mod planens el-mål (trin {target}) – et trin op"
        return cur, changed, None, f"mod planens el-mål (trin {target}) – venter"
    if down_ok and (target is None or target < cur):
        ds = down_since if down_since is not None else now
        if cur > 0 and now - ds >= DOWN_HOLD and now - changed >= DOWN_GAP:
            return cur - 1, now, None, ("mod planens el-mål – et trin ned" if target is not None
                                        else "alle rum over gulv + usikkerhed i 30 min. – et trin ned")
        return cur, changed, ds, "rummene har overskud – venter med at gå ned"
    return cur, changed, None, "rummene holder – trinnet holdes" + (f" (planens el-mål: trin {target})" if target is not None else "")


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


EFF_HOLD = 2700  # virkning pr. trin læres først, når trinnet har kørt så længe (gulv og luft er faldet til ro)
EFF_DECAY = 0.98  # ældre målinger vægter lidt mindre
EFF_MIN_N = 8  # så mange kvarterer, før et trins virkning bruges


def room_gain(t_prev: float, t_now: float, hours: float, t_out: float, cool_per_hk: float, free_per_h: float,
              neighbours: list[tuple[float, float]]) -> float:
    """Den del af rummets temperaturændring, varmepumpen står for: målt ændring minus det, rummet ville have
    gjort uden den (afkøling mod ude, gratis varme som opdrift fra etagen under, varme fra naborum)."""
    passive = hours * (free_per_h - cool_per_hk * (t_prev - t_out) + sum(k * (tn - t_prev) for k, tn in neighbours))
    return (t_now - t_prev) - passive


def eff_update(eff: dict[str, list[float]], label: str, gain: float, kwh: float) -> dict[str, list[float]]:
    """Læg et kvarters varme (°C, snit af rummene) og el (kWh) til trinnets sum. Giver en ny ordbog."""
    new = {k: list(v) for k, v in eff.items()}
    g, e, n = new.get(label, [0.0, 0.0, 0.0])
    new[label] = [round(g * EFF_DECAY + gain, 4), round(e * EFF_DECAY + kwh, 4), n + 1]
    return new


def eff_per_kwh(eff: dict[str, list[float]], label: str) -> float | None:
    """°C pr. kWh el for trinnet (None før der er nok målinger)."""
    v = eff.get(label)
    if not v or v[2] < EFF_MIN_N or v[1] <= 0:
        return None
    return v[0] / v[1]


def efficient_step(ladder_w: list[float | None], ladder_eff: list[float | None], target_w: float | None) -> int | None:
    """Det trin, der giver planens varme for mindst el: blandt trin med lært forbrug og virkning vælges det med
    lavest forbrug, hvis varme (W × virkning) dækker planens varme (mål-W × gennemsnitlig virkning).
    None, før mindst to trin har begge dele."""
    known = [(i, w, e) for i, (w, e) in enumerate(zip(ladder_w, ladder_eff)) if w is not None and e is not None and e > 0]
    if target_w is None or len(known) < 2:
        return None
    avg = sum(e for _, _, e in known) / len(known)
    need = target_w * avg
    ok = [(w, i) for i, w, e in known if w * e >= need]
    return min(ok)[1] if ok else max(known, key=lambda x: x[1] * x[2])[0]


def power_defrosts(points: list[tuple[float, float]], run_w: float, off_w: float | None = None) -> list[tuple[float, float, float]]:
    """Afrimninger for en luft-luft-varmepumpe uden afrimningsføler, læst af el-effekten: (start, slut, laveste W).

    En afrimning ses som et kort dyk midt i driften. Varmepumpen kører (mindst ``run_w`` i de ti minutter før), effekten
    falder til under halvdelen af det, den lige har trukket, i 1–12 minutter, og den kører videre inden for ti minutter
    bagefter. Et stop, fordi rummet er varmt nok, varer længere eller følges ikke af drift – det tæller ikke. Falder
    effekten helt ned til det, varmepumpen bruger slukket (``off_w``, den laveste effekt der er set), er det en kort
    slukning og ingen afrimning; dyk lige efter hinanden regnes som ét.
    ``points``: (tidspunkt, W) i tidsrækkefølge; regnes på minutgennemsnit, så enkelte udfald ikke bliver til dyk."""
    mins: dict[int, list[float]] = {}
    for t, w in points:
        mins.setdefault(int(t // 60), []).append(w)
    ks = sorted(mins)
    mean = {k: sum(v) / len(v) for k, v in mins.items()}
    low = {k: min(v) for k, v in mins.items()}
    dips: list[list[float]] = []  # [første minut, sidste minut, laveste W]
    i = 0
    while i < len(ks):
        k = ks[i]
        before = sorted(mean[x] for x in ks[max(0, i - 10):i] if k - x <= 10)
        base = before[len(before) // 2] if len(before) >= 5 else 0.0
        if base < run_w or mean[k] >= 0.5 * base:
            i += 1
            continue
        j = i
        while j < len(ks) and mean[ks[j]] < 0.5 * base and ks[j] - k <= 12:
            j += 1
        after = [mean[x] for x in ks[j:j + 10] if x - ks[j - 1] <= 10]
        if ks[j - 1] - k + 1 <= 12 and j < len(ks) and after and max(after) >= run_w:
            lo = min(low[x] for x in ks[i:j])
            if dips and k - dips[-1][1] <= 3:  # lige efter det forrige dyk: samme hændelse
                dips[-1][1], dips[-1][2] = ks[j - 1], min(dips[-1][2], lo)
            else:
                dips.append([k, ks[j - 1], lo])
        i = max(j, i + 1)
    stop = None if off_w is None else off_w + 15.0  # så tæt på slukket-effekten er varmepumpen slukket, ikke i afrimning
    return [(a * 60.0, (b + 1) * 60.0, lo) for a, b, lo in dips if stop is None or lo > stop]


def split_label(label: str) -> tuple[str, float, str, str | None] | None:
    """Et trins navn delt op: »HighMid +0.5|vent til@0« → (blæser, setpunkt over gulvet, ventilation, ude-bånd)."""
    core, _, band = label.partition("@")
    core, _, vent = core.partition("|")
    fan, sep, off = core.rpartition(" +")
    try:
        return (fan, float(off), f"|{vent}" if vent else "", band or None) if sep else None
    except ValueError:
        return None


def _between(pts: dict[float, float], x: float) -> float | None:
    """Værdien ved ``x`` mellem de nærmeste kendte punkter (lineært; uden for området den nærmeste)."""
    if not pts:
        return None
    ks = sorted(pts)
    if x <= ks[0]:
        return pts[ks[0]]
    for a, b in zip(ks, ks[1:]):
        if x <= b:
            return pts[a] + (pts[b] - pts[a]) * (x - a) / (b - a) if b > a else pts[b]
    return pts[ks[-1]]


def w_lookup(w: dict[str, float], fan: str, off: float, band: str | None = None,
             fans: set[str] | None = None) -> tuple[float | None, str | None]:
    """Et trins forbrug (W) og hvor tallet kommer fra: »trin« (egne målinger – i udebåndet, ellers alle), »delt« (samme
    setpunkt over gulvet med andre blæsere – nat og dag deler viden) eller »skøn« (mellem de nærmeste lærte setpunkter).
    ``fans``: stigens blæsere (inkl. »quiet«) – andre, fx »Auto« fra en anden styring, deles ikke."""
    own = f"{fan} +{off:.1f}"
    for k in ([f"{own}@{band}"] if band is not None else []) + [own]:
        if w.get(k) is not None:
            return w[k], "trin"
    per: dict[tuple[float, bool], list[float]] = {}
    for k, v in w.items():
        s = split_label(k)
        if s is None or v is None or (fans is not None and s[0] not in fans):
            continue
        if s[3] is None or s[3] == band:
            per.setdefault((s[1], s[3] is not None), []).append(float(v))
    for in_band in (True, False):
        if (v := per.get((round(off, 1), in_band))):
            return round(sum(v) / len(v)), "delt"
    est = _between({o: sum(v) / len(v) for (o, b), v in per.items() if not b}, round(off, 1))
    return (round(est), "skøn") if est is not None else (None, None)


def eff_lookup(eff: dict[str, list[float]], fan: str, off: float, vkey: str = "", band: str | None = None,
               fans: set[str] | None = None) -> tuple[float | None, str | None, int]:
    """Et trins virkning (°C pr. kWh el), hvor tallet kommer fra, og hvor mange kvarterer det bygger på.

    Rækkefølge: trinnets egne målinger (med ventilationens tilstand; i udebåndet, ellers alle) → »delt«: samme setpunkt
    over gulvet med andre blæsere (samme ventilation; i udebåndet, ellers alle; til sidst uanset ventilation) → »skøn«
    mellem de nærmeste lærte setpunkter. Hvert led kræver EFF_MIN_N kvarterer."""
    own = f"{fan} +{off:.1f}{vkey}"
    for k in ([f"{own}@{band}"] if band is not None else []) + [own]:
        if (v := eff_per_kwh(eff, k)) is not None:
            return v, "trin", int(eff[k][2])
    # summer pr. (setpunkt, ventilation, i udebåndet?) over blæserne
    per: dict[tuple[float, str, bool], list[float]] = {}
    for k, v in eff.items():
        s = split_label(k)
        if s is None or (fans is not None and s[0] not in fans) or not (s[3] is None or s[3] == band):
            continue
        acc = per.setdefault((s[1], s[2], s[3] is not None), [0.0, 0.0, 0.0])
        acc[0], acc[1], acc[2] = acc[0] + v[0], acc[1] + v[1], acc[2] + v[2]

    def val(keys: list[tuple[float, str, bool]]) -> tuple[float, int] | None:
        g = sum(per[k][0] for k in keys if k in per)
        e = sum(per[k][1] for k in keys if k in per)
        n = sum(per[k][2] for k in keys if k in per)
        return (g / e, int(n)) if n >= EFF_MIN_N and e > 0 else None

    o = round(off, 1)
    vents = sorted({k[1] for k in per})
    for keys in ([(o, vkey, True)], [(o, vkey, False)], [(o, v, False) for v in vents]):
        if (r := val(keys)) is not None:
            return r[0], "delt", r[1]
    pts = {x: r[0] for x in sorted({k[0] for k in per}) if (r := val([(x, v, False) for v in vents])) is not None}
    est = _between(pts, o)
    return (est, "skøn", 0) if est is not None else (None, None, 0)
