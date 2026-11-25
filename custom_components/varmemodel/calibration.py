"""Kalibrering: regner husets og varmepumpens tal ud af målte timer.

Rene funktioner uden Home Assistant, så de kan testes med kunstige data.

En time (``Hour``) indeholder det, der er målt i den time:
  q        varme leveret, kWh (= gennemsnitlig kW)
  e        el brugt, kWh
  t_out    udetemperatur, middel
  t_in     indetemperatur (snit af rummene), middel
  t_flow   fremløb, middel (valgfri)
  t_ret    retur, middel (valgfri)
  comp     minutter kompressoren kørte (valgfri, fra opsamleren)
  dhw      minutter med varmt vand (valgfri)
  defrost  antal afrimninger der startede (valgfri)

Alle tal kommer ud som ``Fit`` med værdi, datagrundlag (n), hvor godt det
passer (r2), og hvilket udetemperatur-område det er målt i. Er der for få
data, bruges ``source="fallback"`` og værdien er None – så må kalderen bruge
sidste gode værdi eller en standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from statistics import median
from typing import Any


DEFAULT_GROUND = 9.0  # jordtemperatur i ca. 1–2 m dybde, °C


@dataclass
class Hour:
    ts: int  # unix-sekunder, timens start (UTC)
    q: float | None = None
    e: float | None = None
    t_out: float | None = None
    t_in: float | None = None
    t_flow: float | None = None
    t_ret: float | None = None
    comp: float | None = None
    dhw: float | None = None
    defrost: float | None = None
    wind: float | None = None  # m/s, middel
    wind_u: float | None = None  # vindvektor øst-vest (m/s), til middelretning
    wind_v: float | None = None  # vindvektor nord-syd (m/s)
    cloud: float | None = None  # skydække %, middel
    uv: float | None = None  # UV-indeks, middel (> 0 = dagslys)
    humidity: float | None = None  # relativ luftfugtighed ude, % (styrer afrimning)
    t_plant: float | None = None  # varmecentralens rumtemperatur (uopvarmet kælder/teknikrum), °C


@dataclass
class Fit:
    value: float | None
    n: int = 0
    r2: float | None = None
    rmse: float | None = None
    t_out_min: float | None = None
    t_out_max: float | None = None
    source: str = "fallback"  # fit | fallback
    extra: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "n": self.n,
            "r2": self.r2,
            "rmse": self.rmse,
            "t_out_min": self.t_out_min,
            "t_out_max": self.t_out_max,
            "source": self.source,
            "extra": self.extra,
            "note": self.note,
        }


# Grænser for hvornår en måling er brugbar
MIN_COMP_FULL = 45.0  # minutter kompressor for "fuld driftstime"
MAX_FLOW_SPACE_HEATING = 40.0  # over det er det typisk varmt vand
COP_LIMITS = (1.0, 7.5)  # timer udenfor er målefejl
MIN_Q_NO_COMP = 2.5  # kWh: fuld driftstime, når kompressordata mangler
PRIOR_COP_PER_K_FLOW = -0.08  # ca. −2,5 % pr. grad fremløb ved COP ~3,2


# ---------------------------------------------------------------- hjælpere
def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = (len(s) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def theil_sen(x: list[float], y: list[float]) -> tuple[float, float] | None:
    """Robust linje y = a + b·x (median af alle hældninger)."""
    n = len(x)
    if n < 3:
        return None
    slopes = [
        (y[j] - y[i]) / (x[j] - x[i])
        for i in range(n)
        for j in range(i + 1, n)
        if abs(x[j] - x[i]) > 1e-9
    ]
    if not slopes:
        return None
    b = median(slopes)
    a = median(yi - b * xi for xi, yi in zip(x, y))
    return a, b


def _r2_rmse(y: list[float], pred: list[float]) -> tuple[float | None, float | None]:
    if len(y) < 2:
        return None, None
    my = sum(y) / len(y)
    ss = sum((v - my) ** 2 for v in y)
    se = sum((v - p) ** 2 for v, p in zip(y, pred))
    return (1 - se / ss if ss > 0 else None), sqrt(se / len(y))


def ols(rows: list[list[float]], y: list[float]) -> tuple[list[float], list[float]] | None:
    """Mindste kvadraters metode med standardfejl. rows = [[1, x1, x2..], ...]."""
    n, k = len(rows), len(rows[0]) if rows else 0
    if n <= k:
        return None
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * v for r, v in zip(rows, y)) for i in range(k)]
    inv = _invert(xtx)
    if inv is None:
        return None
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    res = [v - sum(b * x for b, x in zip(beta, r)) for r, v in zip(rows, y)]
    s2 = sum(r * r for r in res) / (n - k)
    se = [sqrt(max(inv[i][i] * s2, 0.0)) for i in range(k)]
    return beta, se


def wls_cluster(rows: list[list[float]], y: list[float], w: list[float], cluster: list[Any]) -> tuple[list[float], list[float]] | None:
    """Vægtet mindste kvadrater med standardfejl, der tager højde for at målinger
    i samme klynge (fx samme kompressorkørsel) ikke er uafhængige."""
    k = len(rows[0]) if rows else 0
    if len(rows) <= k:
        return None
    xtwx = [[sum(wi * r[i] * r[j] for r, wi in zip(rows, w)) for j in range(k)] for i in range(k)]
    xtwy = [sum(wi * r[i] * v for r, v, wi in zip(rows, y, w)) for i in range(k)]
    inv = _invert(xtwx)
    if inv is None:
        return None
    beta = [sum(inv[i][j] * xtwy[j] for j in range(k)) for i in range(k)]
    groups: dict[Any, list[float]] = {}
    for r, v, wi, g in zip(rows, y, w, cluster):
        e = v - sum(b * x for b, x in zip(beta, r))
        acc = groups.setdefault(g, [0.0] * k)
        for i in range(k):
            acc[i] += wi * r[i] * e
    meat = [[sum(u[i] * u[j] for u in groups.values()) for j in range(k)] for i in range(k)]
    g = len(groups)
    corr = g / (g - 1) if g > 1 else 1.0
    cov = [[corr * sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)] for i in range(k)]
    return beta, [sqrt(max(cov[i][i], 0.0)) for i in range(k)]


def _invert(m: list[list[float]]) -> list[list[float]] | None:
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        pv = a[c][c]
        a[c] = [v / pv for v in a[c]]
        for r in range(n):
            if r != c:
                f = a[r][c]
                a[r] = [v - f * w for v, w in zip(a[r], a[c])]
    return [row[n:] for row in a]


def _range(vals: list[float]) -> tuple[float | None, float | None]:
    return (round(min(vals), 1), round(max(vals), 1)) if vals else (None, None)


def _space_heating(h: Hour, need_full: bool = True) -> bool:
    """Ren rumopvarmning: kompressoren kørte (næsten) hele timen, ikke varmt vand."""
    if h.dhw is not None and h.dhw > 0:
        return False
    if h.dhw is None and h.t_flow is not None and h.t_flow > MAX_FLOW_SPACE_HEATING:
        return False
    if need_full and h.comp is not None and h.comp < MIN_COMP_FULL:
        return False
    # Uden kompressordata (fx før opsamlingen startede) kan opstartstimer ikke
    # skilles fra: kræv så så meget varme, at det må have været en fuld time.
    if need_full and h.comp is None and (h.q is None or h.q < MIN_Q_NO_COMP):
        return False
    return True


# ---------------------------------------------------------------- varmetab
def fit_heat_loss(hours: list[Hour], block_hours: int = 48, min_blocks: int = 8, min_spread: float = 4.0,
                  plant_w_k: float = 0.0, ground: float = DEFAULT_GROUND) -> Fit:
    """Husets varmetab H (W/K) og gratis varme G (W) fra blokke af 48 timer.

    Gennemsnitlig varmeeffekt = H·(inde − ude) − G. Blokke på 48 timer,
    fordi gulvet gemmer varme fra dag til dag. Timer med varmt vand trækkes
    fra, når opsamleren ved hvornår der blev lavet varmt vand.

    Varmecentral (uopvarmet rum med installationen): dens tab til jorden,
    ``plant_w_k``·(rum − jord), er spildvarme fra installationen. Varmemåleren
    tæller det med, så det trækkes fra, før husets eget varmetab fittes.
    Tabet er fast (fra rummets opbygning). Med få data kan det ikke skilles
    fra husets tab, fordi begge følger udetemperaturen.
    """
    plant_known = [h.t_plant for h in hours if h.t_plant is not None]
    t_plant_typical = median(plant_known) if plant_known else None
    # Mangler indetemperaturen (fx før rumfølerne fik statistik), bruges husets
    # typiske indetemperatur – huset holdes tæt på den samme temperatur.
    known = [h.t_in for h in hours if h.t_in is not None]
    t_in_typical = median(known) if known else None
    blocks: dict[int, list[Hour]] = {}
    assumed = 0
    for h in hours:
        if h.q is None or h.t_out is None:
            continue
        if h.t_in is None:
            if t_in_typical is None:
                continue
            h = Hour(**{**h.__dict__, "t_in": t_in_typical})
            assumed += 1
        blocks.setdefault(h.ts // (block_hours * 3600), []).append(h)
    x: list[float] = []
    y: list[float] = []
    touts: list[float] = []
    plant_kw: list[float] = []
    plant_t: list[float] = []
    dhw_corrected = 0
    for hs in blocks.values():
        if len(hs) < block_hours * 0.9:
            continue
        clean = [h for h in hs if not (h.dhw is not None and h.dhw > 0)]
        dhw_corrected += len(hs) - len(clean)
        if len(clean) < len(hs) * 0.7:
            continue
        tp = [h.t_plant if h.t_plant is not None else t_plant_typical for h in hs]
        tp_mean = sum(tp) / len(tp) if plant_w_k > 0 and None not in tp else None
        loss_kw = plant_w_k / 1000 * (tp_mean - ground) if tp_mean is not None else 0.0
        x.append(sum(h.t_in - h.t_out for h in hs) / len(hs))
        y.append(sum(max(h.q or 0.0, 0.0) for h in clean) / len(clean) - loss_kw)
        touts.append(sum(h.t_out for h in hs) / len(hs))
        plant_kw.append(loss_kw)
        if tp_mean is not None:
            plant_t.append(tp_mean)
    lo, hi = _range(touts)
    if len(x) < min_blocks or (max(x) - min(x) if x else 0) < min_spread:
        return Fit(None, len(x), t_out_min=lo, t_out_max=hi, note="for få blokke eller for lille spredning i udetemperatur")
    ab = theil_sen(x, y)
    if ab is None or ab[1] <= 0:
        return Fit(None, len(x), t_out_min=lo, t_out_max=hi, note="ingen sammenhæng mellem varme og temperaturforskel")
    a, b = ab
    r2, rmse = _r2_rmse(y, [a + b * v for v in x])
    return Fit(
        round(b * 1000, 1), len(x), r2, rmse and round(rmse, 3), lo, hi, "fit",
        {"gains_w": round(-a * 1000, 0), "block_hours": block_hours, "dhw_hours_removed": dhw_corrected,
         "hours_with_assumed_indoor": assumed, "indoor_assumed": round(t_in_typical, 1) if t_in_typical else None,
         **({"plant_room_w_k": plant_w_k, "ground_temp": ground,
             "plant_room_temp": round(sum(plant_t) / len(plant_t), 1),
             "plant_room_loss_kwh_day": round(24 * sum(plant_kw) / len(plant_kw), 2),
             "plant_room_free_fit": _plant_free_fit(x, [yy + pk for yy, pk in zip(y, plant_kw)], plant_t, ground)}
            if plant_t else {})},
    )


def _plant_free_fit(x: list[float], y: list[float], tp: list[float], ground: float) -> dict[str, Any]:
    """Kun til diagnose: husets og varmecentralens tab fittet frit (mindste kvadraters metode).

    Bruges først, når der er nok data: mange blokke, frost og varmecentralen svinger
    uafhængigt af udetemperaturen (lav korrelation med husets temperaturforskel).
    """
    n = len(x)
    z = [t - ground for t in tp]
    if n < 4 or len(z) != n:
        return {"n": n, "usable": False, "note": "for få blokke"}
    mx, mz, my = sum(x) / n, sum(z) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    szz = sum((c - mz) ** 2 for c in z)
    sxz = sum((a - mx) * (c - mz) for a, c in zip(x, z))
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    szy = sum((c - mz) * (b - my) for c, b in zip(z, y))
    corr = sxz / (sxx * szz) ** 0.5 if sxx > 0 and szz > 0 else None
    det = sxx * szz - sxz ** 2
    spread = max(tp) - min(tp)
    if det <= 1e-9:
        return {"n": n, "usable": False, "correlation": corr and round(corr, 2), "note": "varmecentralen svinger ikke nok"}
    bh = (szz * sxy - sxz * szy) / det
    bz = (sxx * szy - sxz * sxy) / det
    usable = n >= 40 and spread >= 3 and corr is not None and abs(corr) < 0.7 and 0 <= bz * 1000 <= 40
    return {"n": n, "house_w_k": round(bh * 1000, 1), "plant_room_w_k": round(bz * 1000, 1),
            "correlation": corr and round(corr, 2), "plant_spread_k": round(spread, 1), "usable": usable,
            "note": "kan bruges" if usable else "kun diagnose: kræver ≥ 40 blokke, ≥ 3 K spredning og |korrelation| < 0,7"}


# ---------------------------------------------------------------- COP
# ---------------------------------------------------------------- gulvets afgivelse
def fit_emission(hours: list[Hour], min_n: int = 20) -> Fit:
    """K = varme ÷ (fremløb − rum) i timer med ren rumopvarmning (kW pr. grad).

    Værdien er 25 %-fraktilen (den forsigtige ende: gulvet tager varme
    lettere imod, når det er koldt efter en pause).
    """
    ks, touts, kw = [], [], []
    for h in hours:
        if None in (h.q, h.t_flow, h.t_in, h.t_out) or h.q < 1.5 or not _space_heating(h):
            continue
        d = h.t_flow - h.t_in
        if d <= 5:
            continue
        ks.append(h.q / d)
        touts.append(h.t_out)
        if h.t_ret is not None and (h.t_flow + h.t_ret) / 2 - h.t_in > 3:
            kw.append(h.q / ((h.t_flow + h.t_ret) / 2 - h.t_in))
    lo, hi = _range(touts)
    if len(ks) < min_n:
        return Fit(None, len(ks), t_out_min=lo, t_out_max=hi, note="for få driftstimer med fremløb")
    return Fit(
        round(quantile(ks, 0.25), 3), len(ks), None, None, lo, hi, "fit",
        {"median": round(median(ks), 3), "p75": round(quantile(ks, 0.75), 3),
         "median_water_mean": round(median(kw), 3) if kw else None,
         "value_is": "25 %-fraktil af varme ÷ (fremløb − rum)"},
    )


# ---------------------------------------------------------------- effekt
def fit_power(hours: list[Hour], min_n: int = 20) -> tuple[Fit, Fit]:
    """Maks. effekt (90 %-fraktil) og laveste stabile effekt (10 %-fraktil) i fulde driftstimer."""
    full = [h for h in hours if h.q is not None and h.q > 0.3 and _space_heating(h)
            and (h.comp is None or h.comp >= 55)]
    q = [h.q for h in full]
    lo, hi = _range([h.t_out for h in full if h.t_out is not None])
    if len(q) < min_n:
        f = Fit(None, len(q), t_out_min=lo, t_out_max=hi, note="for få fulde driftstimer")
        return f, Fit(None, len(q), t_out_min=lo, t_out_max=hi, note=f.note)
    pmax = Fit(round(quantile(q, 0.9), 2), len(q), None, None, lo, hi, "fit", {"value_is": "90 %-fraktil af varme i fulde driftstimer"})
    # Laveste effekt kræver kompressordata – ellers kan en halv driftstime ligne lav effekt
    known = [h for h in full if h.comp is not None]
    if len(known) < min_n:
        return pmax, Fit(None, len(known), t_out_min=lo, t_out_max=hi, note="for få timer med kompressordata")
    qk = [h.q for h in known]
    ek = [h.e for h in known if h.e is not None]
    klo, khi = _range([h.t_out for h in known if h.t_out is not None])
    pmin = Fit(round(quantile(qk, 0.1), 2), len(qk), None, None, klo, khi, "fit",
               {"el_kw_p10": round(quantile(ek, 0.1), 2) if ek else None, "value_is": "10 %-fraktil af varme i timer hvor kompressoren kørte hele timen"})
    return pmax, pmin


# ---------------------------------------------------------------- lært fremløb
def fit_flow(hours: list[Hour], min_n: int = 24, min_spread: float = 3.0,
             skip: list[tuple[float, float | None]] | None = None) -> Fit:
    """Det fremløb, huset selv har brugt til rumvarme: fremløb = værdi (ved 0 °C ude) + hældning·ude.

    Kun timer med ren rumopvarmning (kompressoren kørte hele timen, ikke varmt vand). Planen bruger det som
    loft for fremløbet (lavt og jævnt) – modellens egen linje lært af målinger.

    ``skip``: perioder (fra, til – til = None: stadig), hvor styringen selv satte fremløbet. De timer tæller ikke med:
    en linje lært af dem er styringens eget ekko, og loftet falder lidt for hver kalibrering (25/11-2026: 35,4 → 34,4 °C
    ved 0 °C ude på to døgn, mens et rum lå under sit gulv). Er der for få timer tilbage, beholdes den seneste værdi.
    """
    own = list(skip or [])
    s0 = [h for h in hours if h.t_flow is not None and h.t_out is not None and _space_heating(h)]
    s = [h for h in s0 if not any(a < h.ts + 3600 and (b is None or b > h.ts) for a, b in own)]
    left_out = len(s0) - len(s)
    x = [h.t_out for h in s]
    y = [h.t_flow for h in s]
    lo, hi = _range(x)
    if len(s) < min_n or (max(x) - min(x) if x else 0) < min_spread:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi,
                   note=("for få timer uden egen styring af fremløbet – den seneste værdi beholdes" if left_out
                         else "for få timer eller for lille spredning i udetemperatur"))
    ab = theil_sen(x, y)
    if ab is None:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
    a, b = ab
    r2, rmse = _r2_rmse(y, [a + b * v for v in x])
    extra: dict[str, Any] = {"slope": round(b, 3), "formula": "fremløb = værdi + hældning·ude (målt ved ren rumvarme)"}
    if left_out:
        extra["own_control_hours_left_out"] = left_out  # timer med styringens eget fremløb, der ikke tæller med
    return Fit(round(a, 2), len(s), r2, rmse and round(rmse, 2), lo, hi, "fit", extra)


# ---------------------------------------------------------------- afrimning
DEFROST_BANDS = (0.0, 3.0, 6.0)  # udetemperaturens bånd til afrimningsstatistikken (°C)
FROST_MIN_KWH = 20.0  # så meget varme skal et bånd have, før dets afrimninger pr. kWh bruges


def fit_defrost(hours: list[Hour], min_comp_hours: float = 10.0) -> Fit:
    """Afrimninger pr. kompressortime, i alt og fordelt på ude og effekt – og pr. kWh varme for hvert udebånd
    (``per_kwh``: båndets middel-ude, afrimninger pr. kWh varme og varmen, det bygger på), som planen bruger til rimens
    pris (``frost_points``)."""
    s = [h for h in hours if h.comp is not None and h.comp > 0 and h.defrost is not None and h.t_out is not None]
    comp_h = sum(h.comp for h in s) / 60
    lo, hi = _range([h.t_out for h in s])
    if comp_h < min_comp_hours:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="for få kompressortimer med afrimningsdata")
    bins: dict[str, list[float]] = {}
    for h in s:
        tb = "under 0" if h.t_out < 0 else "0–3" if h.t_out < 3 else "3–6" if h.t_out < 6 else "over 6"
        pb = "lav" if (h.q or 0) < 3.5 else "høj"
        b = bins.setdefault(f"{tb} °C, {pb} effekt", [0.0, 0.0])
        b[0] += h.comp / 60
        b[1] += h.defrost
    # Pr. kWh varme: rimen følger varmen, der hentes ud af luften – ikke klokken. Kun timer med målt varme.
    kb: dict[int, list[float]] = {}
    for h in s:
        if h.q is None or h.q <= 0:
            continue
        b = kb.setdefault(sum(1 for x in DEFROST_BANDS if h.t_out >= x), [0.0, 0.0, 0.0])
        b[0] += h.q
        b[1] += h.defrost
        b[2] += h.t_out * h.q
    per_kwh = [{"t_out": round(v[2] / v[0], 1), "per_kwh": round(v[1] / v[0], 3), "kwh": round(v[0], 1)}
               for _k, v in sorted(kb.items()) if v[0] >= FROST_MIN_KWH]
    q_all = sum(v[0] for v in kb.values())
    return Fit(
        round(sum(h.defrost for h in s) / comp_h, 3), len(s), None, None, lo, hi, "fit",
        {"comp_hours": round(comp_h, 1),
         "per_bin": {k: {"comp_hours": round(v[0], 1), "per_hour": round(v[1] / v[0], 2) if v[0] else None} for k, v in sorted(bins.items())},
         "per_kwh": per_kwh, "per_kwh_all": round(sum(v[1] for v in kb.values()) / q_all, 3) if q_all > 0 else None,
         "value_is": "afrimninger pr. kompressortime"},
    )


def _mean_between(series: list[tuple[float, float]], a: float, b: float) -> float | None:
    """Tidsvægtet middel af en trappeserie (tidspunkt, værdi) i [a, b). None uden en værdi ved a."""
    cur: float | None = None
    t, tot = a, 0.0
    for ts, v in series:
        if ts <= a:
            cur = v
            continue
        if ts >= b:
            break
        if cur is None:
            return None
        tot += cur * (ts - t)
        t, cur = ts, v
    if cur is None:
        return None
    return (tot + cur * (b - t)) / (b - a)


def defrost_cost(defrosts: list[tuple[float, float]], flow: list[tuple[float, float]], ret: list[tuple[float, float]],
                 five: dict[int, dict[str, float | None]], tail: int = 300) -> list[dict[str, float]]:
    """Hvad hver afrimning tager fra gulvvandet (kWh varme) – uden flowmåler.

    Under en afrimning vender varmepumpen kredsen: fremløbet er koldere end returen, og varmen til at smelte rimen tages
    fra vandet. Varmemåleren tæller ikke baglæns, så den varme ses ikke i nogen COP. Den regnes her af vandets flow ×
    (frem − retur): flowet skønnes af varmeeffekten ÷ (frem − retur) i de to 5-min.-portioner lige før afrimningen
    (``five``: portionens start → {"q": kWh varme}), og (frem − retur) lægges sammen fra afrimningens start, til fremløbet
    igen er varmere end returen (højst ``tail`` sekunder efter). ``flow``/``ret``: (tidspunkt, °C) som de er målt.
    Afrimninger uden brugbar drift lige før (fx efter varmt vand) eller med for få målinger springes over.
    Giver pr. afrimning: ``t`` (start), ``min`` (varighed), ``q`` (kWh varme taget fra vandet) og ``flow`` (l/min, skønnet)."""
    out: list[dict[str, float]] = []
    c = 4.186 / 60  # kW pr. (l/min · K)
    for t0, t1 in defrosts:
        p0 = int(t0 // 300) * 300
        est = []
        for a in (p0 - 300, p0 - 600):
            q = (five.get(a) or {}).get("q")
            f, r = _mean_between(flow, a, a + 300), _mean_between(ret, a, a + 300)
            if q is not None and f is not None and r is not None and f - r > 1.0 and q * 12 > 0.5:
                est.append(q * 12 / ((f - r) * c))
        if not est:
            continue
        lpm = median(est)
        # (frem − retur) fra afrimningens start, til fremløbet igen er varmest
        pts = sorted({t0, *(ts for ts, _ in flow if t0 < ts < t1 + tail), *(ts for ts, _ in ret if t0 < ts < t1 + tail)})
        if len(pts) < 4:
            continue
        integ, end = 0.0, t1 + tail
        for a, b in zip(pts, [*pts[1:], t1 + tail]):
            f, r = _mean_between(flow, a, b), _mean_between(ret, a, b)
            if f is None or r is None:
                continue
            if a >= t1 and f - r > 0:
                end = a
                break
            integ += (f - r) * (b - a)
        q_def = -lpm * c * integ / 3600
        if 0 < q_def < 3:
            out.append({"t": round(t0), "min": round((t1 - t0) / 60, 1), "q": round(q_def, 3), "flow": round(lpm, 1), "til": round(end)})
    return out


def frost_points(per_kwh: list[dict[str, float]] | None, per_kwh_all: float | None, def_q: float | None) -> list[tuple[float, float]]:
    """Rimens pris til planen: for hvert udebånd, hvor meget af den målte varme huset reelt får i forhold til gennemsnittet
    – (ude, faktor), sorteret efter ude.

    Hver afrimning tager ``def_q`` kWh varme fra gulvvandet, som varmemåleren ikke trækker fra. Husets og rummenes
    modeller er lært af den målte varme og har derfor gennemsnittets rim med i sig; faktoren er forskellen til det:
    (1 − afrimninger pr. kWh i båndet × def_q) ÷ (1 − afrimninger pr. kWh i alt × def_q). Under 1 i rimvejr (timen giver
    mindre end målt og koster mere pr. kWh, huset får), over 1 i mildt vejr. Tom liste, før tallene er lært."""
    if not per_kwh or per_kwh_all is None or not def_q or def_q <= 0:
        return []
    base = 1 - min(0.5, per_kwh_all * def_q)
    return sorted((float(b["t_out"]), round((1 - min(0.5, float(b["per_kwh"]) * def_q)) / base, 4)) for b in per_kwh)


# ---------------------------------------------------------------- afkøling
def fit_cooling(hours: list[Hour], min_run: int = 3, min_runs: int = 3) -> Fit:
    """Tidskonstant τ (timer): hvor hurtigt rummene falder uden varme.

    I en pause falder rummet med (inde − ude)/τ grader i timen. Pauser =
    sammenhængende timer med under 0,1 kWh varme.
    """
    runs: list[list[Hour]] = []
    cur: list[Hour] = []
    for h in sorted(hours, key=lambda h: h.ts):
        if h.q is not None and h.q < 0.1 and h.t_in is not None and h.t_out is not None and (not cur or h.ts - cur[-1].ts == 3600):
            cur.append(h)
            continue
        if len(cur) >= min_run:
            runs.append(cur)
        cur = [h] if h.q is not None and h.q < 0.1 and h.t_in is not None and h.t_out is not None else []
    if len(cur) >= min_run:
        runs.append(cur)
    rates, touts = [], []
    for r in runs:
        drop = (r[0].t_in - r[-1].t_in) / (len(r) - 1)
        dt = sum(h.t_in - h.t_out for h in r) / len(r)
        if dt > 3:
            rates.append(drop / dt)
            touts += [h.t_out for h in r]
    lo, hi = _range(touts)
    if len(rates) < min_runs:
        return Fit(None, len(rates), t_out_min=lo, t_out_max=hi, note="for få pauser uden varme")
    m = median(rates)
    if m <= 0:
        return Fit(None, len(rates), t_out_min=lo, t_out_max=hi, note="rummene faldt ikke i pauserne (sol, eftervarme)")
    return Fit(round(1 / m, 0), len(rates), None, None, lo, hi, "fit",
               {"k_per_hour_at_20k": round(20 * m, 3), "value_is": "tidskonstant i timer"})


# ---------------------------------------------------------------- samlet
def calibrate(hours: list[Hour], plant_w_k: float = 0.0, ground: float = DEFAULT_GROUND,
              flow_skip: list[tuple[float, float | None]] | None = None) -> dict[str, Fit]:
    """Alle varmekildens tal. ``flow_skip``: perioder, hvor styringen selv satte fremløbet (se ``fit_flow``)."""
    pmax, pmin = fit_power(hours)
    wind = fit_weather(hours)
    wind.extra["sectors"] = fit_wind_sectors(hours)
    return {
        "wind_effect": wind,
        "heat_loss": fit_heat_loss(hours, plant_w_k=plant_w_k, ground=ground),
        "emission": fit_emission(hours),
        "max_power": pmax,
        "min_power": pmin,
        "flow_learned": fit_flow(hours, skip=flow_skip),
        "defrost": fit_defrost(hours),
        "cooling": fit_cooling(hours),
    }


def guard(new: float | None, prev: float | None, new_range: tuple[float | None, float | None],
          prev_range: tuple[float | None, float | None], limit: float = 0.10, cold_limit: float = 0.30) -> tuple[float | None, str]:
    """Begræns hvor meget et tal må flytte sig pr. kalibrering.

    Normalt højst ±10 %. Er der kommet målinger fra koldere vejr end før
    (mindst 2 grader under det hidtil laveste), må tallet flytte sig ±30 %,
    så modellen hurtigt lærer, når det bliver rigtig koldt.
    """
    if new is None:
        return prev, "beholdt (ingen ny værdi)"
    if prev is None or prev == 0:
        return new, "ny"
    colder = new_range[0] is not None and prev_range[0] is not None and new_range[0] < prev_range[0] - 2
    lim = cold_limit if colder else limit
    lo, hi = sorted((prev * (1 - lim), prev * (1 + lim)))
    if lo <= new <= hi:
        return new, "koldere vejr" if colder else "ok"
    return round(max(lo, min(hi, new)), 6), f"begrænset til ±{round(lim * 100)} %"


# ---------------------------------------------------------------- COP i stabil drift (5-minutters portioner)
@dataclass
class Slot:
    """Fem minutters drift efter mindst 10 minutter med kompressoren i gang."""

    ts: int
    q: float
    e: float
    t_out: float
    t_flow: float
    run_min: float


def steady_slots(
    five: dict[int, dict[str, float | None]],
    comp: list[tuple[float, float]],
    defrost: list[tuple[float, float]],
    dhw: list[tuple[float, float]],
    wait: int = 600,
    min_e: float = 0.02,
) -> list[Slot]:
    """Byg portioner: start 10 min efter kompressorstart (eller efter en afrimning),
    tag 5 minutter ad gangen, indtil den afrimer, laver varmt vand eller stopper.

    ``five`` er {portionens start: {"q", "e", "t_out", "t_flow"}} fra 5-minutters statistik.
    """
    def hit(a: float, b: float, ivs: list[tuple[float, float]]) -> bool:
        return any(x < b and y > a for x, y in ivs)

    segs: list[tuple[float, float]] = []
    for a, b in comp:
        st = a
        for x, y in sorted(d for d in defrost if d[0] < b and d[1] > a):
            segs.append((st, x))
            st = y
        segs.append((st, b))
    out: list[Slot] = []
    for a, b in segs:
        t = int(-(-(a + wait) // 300) * 300)
        while t + 300 <= b:
            if hit(t, t + 300, defrost) or hit(t, t + 300, dhw):
                break
            r = five.get(t) or {}
            q, e, u, f = r.get("q"), r.get("e"), r.get("t_out"), r.get("t_flow")
            if None not in (q, e, u, f) and e >= min_e and q > 0:
                out.append(Slot(t, q, e, u, f, round((t - a) / 60, 1)))
            t += 300
    return out


def fit_cop_steady(slots: list[Slot], min_n: int = 100, min_runs: int = 20) -> Fit:
    """COP i stabil drift = a + b·ude + c·fremløb + d·effekt (uden opstart og afrimning).

    Portionerne vægtes efter varme (så små portioner ikke fylder for meget), og
    usikkerheden regnes pr. kompressorkørsel – portioner fra samme kørsel ligner
    hinanden og er ikke uafhængige målinger. Fremløbsleddet c bruges kun, hvis
    det er tydeligt negativt (mindst 2 standardfejl); ellers standardværdi.
    """
    s = [x for x in slots if COP_LIMITS[0] <= x.q / x.e <= COP_LIMITS[1]]
    lo, hi = _range([x.t_out for x in s])
    runs = {round(x.ts - x.run_min * 60, -2) for x in s}
    if len(s) < min_n or len(runs) < min_runs:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="for få kørsler i stabil drift")
    y = [x.q / x.e for x in s]
    w = [x.q for x in s]
    cl = [round(x.ts - x.run_min * 60, -2) for x in s]
    kw = [x.q * 12 for x in s]  # 5 minutter → kW
    res = wls_cluster([[1.0, x.t_out, x.t_flow, p] for x, p in zip(s, kw)], y, w, cl)
    if not res:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
    (a, b, c, d), (_, sb, sc, sd) = res
    c_source = "fit"
    if not (c < 0 and sc < abs(c) / 2):
        c_source = "prior"
        c = PRIOR_COP_PER_K_FLOW / 2
        r2 = wls_cluster([[1.0, x.t_out, p] for x, p in zip(s, kw)], [v - c * x.t_flow for v, x in zip(y, s)], w, cl)
        if r2:
            (a, b, d), (_, sb, sd) = r2
    # Niveau: fittet rammer gennemsnittet af COP-tallene, men el er varme ÷ COP, så det giver for lidt el.
    # Skalér, så modellens el for portionernes varme = den målte el (energivægtet, samme som COP i dag).
    m0 = [max(1.2, a + b * x.t_out + c * x.t_flow + d * p) for x, p in zip(s, kw)]
    level = sum(x.q / m for x, m in zip(s, m0)) / sum(x.e for x in s)
    a, b, c, d = a * level, b * level, c * level, d * level
    p_ref = 4.0
    pred = [a + b * x.t_out + c * x.t_flow + d * p for x, p in zip(s, kw)]
    r2v, rmse = _r2_rmse(y, pred)
    cop_ref = a + b * 5 + c * 33 + d * p_ref
    return Fit(
        round(a + c * 35.0 + d * p_ref, 3), len(s), r2v, rmse and round(rmse, 3), lo, hi, "fit",
        {"a": round(a, 4), "b": round(b, 4), "c": round(c, 4), "d": round(d, 4), "c_source": c_source,
         "c_se_per_run": round(sc, 4), "c_ci95": [round(c - 1.96 * sc, 3), round(c + 1.96 * sc, 3)],
         "d_se_per_run": round(sd, 4), "runs": len(runs), "level_factor": round(level, 4),
         "per_k_flow_pct": round(-c / cop_ref * 100, 1), "per_kw_pct": round(-d / cop_ref * 100, 1),
         "formula": "COP = a + b·ude + c·fremløb + d·effekt(kW)",
         "value_is": "COP i stabil drift ved 0 °C ude, 35 °C fremløb og 4 kW",
         "method": "5-minutters portioner fra 10 min efter start til afrimning, varmt vand eller stop; vægtet efter varme; usikkerhed pr. kørsel"},
    )


def fit_cop_loss(steady: Fit, five: dict[int, dict[str, float | None]], comp: list[tuple[float, float]],
                 defrost: list[tuple[float, float]], dhw: list[tuple[float, float]]) -> Fit:
    """Ekstra el ved opstart, afrimning og standby over de seneste 14 dage – samme regnestykke som
    COP i dag (``cop_today``), bare over hele perioden: rumvarmens COP mod den målte stabile drift.

    Værdien er andelen af el, der går ud over, hvad stabil drift ville have brugt til samme varme:
    1 − COP(rumvarme) ÷ COP(stabil drift). Modellens afvigelse står for sig i ``extra``.
    """
    d = cop_today(five, comp, defrost, dhw, steady)
    if d is None or d["loss_start_defrost_pct"] is None:
        return Fit(None, 0, note="for lidt drift")
    return Fit(d["loss_start_defrost_pct"], d["steady_minutes"] // 5, None, None,
               steady.t_out_min if steady else None, steady.t_out_max if steady else None, "fit",
               {"cop_space_heating": d["cop_without_dhw"], "cop_steady_measured": d["cop_steady"],
                "cop_steady_model": d["expected_steady"], "steady_vs_model_pct": d["steady_vs_model_pct"],
                "kwh_heat": d["heat_kwh"], "kwh_el": d["el_kwh"],
                "value_is": "% ekstra el mod målt stabil drift for samme varme (opstart, afrimning og standby)"})


def cop_today(five: dict[int, dict[str, float | None]], comp: list[tuple[float, float]],
              defrost: list[tuple[float, float]], dhw: list[tuple[float, float]], steady: Fit | None) -> dict[str, Any] | None:
    """COP fra 5-minutters portioner (i dag eller en hel periode): samlet, uden varmt vand, stabil drift
    (ekspertmetoden: fra 10 min efter start/afrimning til afrimning, varmt vand eller stop) og hvad tabet skyldes.

    Alle energier er kWh: ``q`` = kWh varme, ``e`` = kWh el.
    """
    def hit(a: float, b: float, ivs: list[tuple[float, float]]) -> bool:
        return any(x < b and y > a for x, y in ivs)

    q = e = qv = ev = 0.0
    for t, r in five.items():
        qq, ee = r.get("q"), r.get("e")
        if qq is None or ee is None:
            continue
        q += max(qq, 0.0)
        e += max(ee, 0.0)
        if hit(t, t + 300, dhw):
            qv += max(qq, 0.0)
            ev += max(ee, 0.0)
    if e < 0.2:
        return None
    sl = steady_slots(five, comp, defrost, dhw)
    qs, es = sum(x.q for x in sl), sum(x.e for x in sl)
    cop = q / e
    q_sh, e_sh = q - qv, e - ev
    cop_sh = q_sh / e_sh if e_sh > 0.2 else None
    cop_st = qs / es if es > 0.1 else None
    exp = None
    if steady is not None and steady.value is not None and sl:
        a, b, c, d = (steady.extra.get(k, 0.0) for k in ("a", "b", "c", "d"))
        exp = qs / sum(x.q / max(1.2, a + b * x.t_out + c * x.t_flow + d * x.q * 12) for x in sl)
    return {
        "heat_kwh": round(q, 2), "el_kwh": round(e, 2), "cop": round(cop, 2),
        "dhw_heat_kwh": round(qv, 2), "dhw_el_kwh": round(ev, 2),
        "cop_without_dhw": None if cop_sh is None else round(cop_sh, 2),
        "cop_steady": None if cop_st is None else round(cop_st, 2),
        "steady_minutes": len(sl) * 5,
        # Ekstra el ved opstart, afrimning og standby: rumvarmens COP mod målt stabil drift i samme periode
        "loss_start_defrost_pct": None if cop_sh is None or cop_st is None else round((1 - cop_sh / cop_st) * 100, 1),
        # Varmt vands træk på dagens COP
        "dhw_effect_pct": None if cop_sh is None else round((1 - cop / cop_sh) * 100, 1),
        "expected_steady": None if exp is None else round(exp, 2),
        "steady_vs_model_pct": None if exp is None or cop_st is None else round((cop_st / exp - 1) * 100, 1),
    }


# ---------------------------------------------------------------- isvagt
# Isvagten sammenligner varmepumpens seneste driftscyklusser med husets egne tidligere – ikke med faste grænser for ydelsen.
# Tallene her er de eneste valg: hvor langt tilbage »normalt« regnes, hvad »lavt« vil sige i forhold til det, og hvor længe
# det skal vare, før det vises og meldes.
ICE_REF_DAYS = 30  # husets egne cyklusser så langt tilbage er sammenligningen
ICE_RECENT_H = 12  # … bortset fra de seneste timer (dem, der vurderes)
ICE_MIN_REF = 20  # så mange tidligere cyklusser skal der til, før noget kan kaldes lavt
ICE_LOW_Q = 0.1  # lav: ringere end ni ud af ti tidligere cyklusser
ICE_WATCH_N = 3  # så mange lave cyklusser i træk vises som »følg«
ICE_ALARM_H = 6.0  # så mange driftstimer med lave cyklusser i træk giver besked (en tilisning faldt jævnt i ca. 30 timer)


def frost_cycles(slots: list[Slot], cop: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Driftscyklusser ud fra portionerne i stabil drift: fra 10 min. efter en start eller afrimning til næste afrimning,
    varmt vand eller stop.

    For hver cyklus: start (``t``, afrimningens slut eller kompressorstart), slut (``til``), længde (``min``), ude, kW og
    ydelsen i forhold til husets egen COP-model (``cop``: a, b, c, d fra ``fit_cop_steady``):
    - ``ren``: de første 15 min. – fladen lige efter afrimningen. Får afrimningen den ikke ren, er den lav fra starten.
    - ``slut``: de sidste 10 min. – så meget rim nåede at sætte sig.
    - ``snit``: hele cyklussen.
    Cyklusser med under tre portioner (15 min.) tæller ikke."""
    if not cop or cop.get("a") is None:
        return []
    a, b, c, d = (float(cop.get(k) or 0.0) for k in "abcd")
    rows = sorted((s for s in slots if s.e > 0 and COP_LIMITS[0] <= s.q / s.e <= COP_LIMITS[1]), key=lambda s: s.ts)
    out: list[dict[str, Any]] = []
    cur: list[Slot] = []

    def close() -> None:
        if len(cur) < 3:
            return
        r = [(x.q / x.e) / max(1.2, a + b * x.t_out + c * x.t_flow + d * x.q * 12) for x in cur]
        out.append({"t": int(cur[0].ts - cur[0].run_min * 60), "til": int(cur[-1].ts + 300), "min": round(cur[-1].run_min + 5),
                    "ude": round(sum(x.t_out for x in cur) / len(cur), 1), "kw": round(sum(x.q for x in cur) * 12 / len(cur), 1),
                    "ren": round(median(r[:3]), 3), "slut": round(median(r[-2:]), 3), "snit": round(median(r), 3), "n": len(cur),
                    # rå COP (varme ÷ el) i de første 15 og de sidste 10 min. – faldet frem mod afrimningen, som det betales
                    "cop_ren": round(sum(x.q for x in cur[:3]) / sum(x.e for x in cur[:3]), 2),
                    "cop_slut": round(sum(x.q for x in cur[-2:]) / sum(x.e for x in cur[-2:]), 2)})

    for s in rows:
        if cur and (s.ts - cur[-1].ts != 300 or s.run_min < cur[-1].run_min):
            close()
            cur = []
        cur.append(s)
    close()
    return out


def ice_watch(cycles: list[dict[str, Any]], now: float, hold: dict[str, Any] | None = None) -> dict[str, Any]:
    """Isvagten: er varmepumpens seneste cyklusser usædvanligt svage i forhold til husets egne tidligere?

    »Lav« er en cyklus, hvis rene flade (``ren``) eller hele forløb (``snit``) er ringere end ni ud af ti af de tidligere
    cyklusser (de seneste ``ICE_REF_DAYS`` døgn, uden de seneste ``ICE_RECENT_H`` timer). Enkelte lave cyklusser er
    almindelige – tung rim en fugtig aften klarer afrimningen selv. Det usædvanlige er, at det bliver ved:
    - ``watch``: mindst ``ICE_WATCH_N`` lave cyklusser i træk,
    - ``ice``: lave cyklusser i træk i mindst ``ICE_ALARM_H`` driftstimer – fladen bliver ikke ren. Pauser tæller ikke med:
      er varmepumpen iset til, er den det også efter en rød periode, og timerne lægges sammen hen over pausen,
    - ``ok`` ellers, og ``learning``, mens der er for få tidligere cyklusser at sammenligne med.
    ``hold``: grænserne fra et løb af lave cyklusser, der er i gang (``bounds`` fra forrige kald) – de holdes fast, til løbet
    er slut, så en lang tilisning ikke selv flytter grænsen for, hvad der er lavt."""
    ref = [c for c in cycles if now - ICE_REF_DAYS * 86400 <= c["t"] < now - ICE_RECENT_H * 3600]
    last = cycles[-1] if cycles else None
    out: dict[str, Any] = {"status": "learning", "ref_n": len(ref), "low_run": 0, "low_hours": 0.0, "since": None, "bounds": None,
                           "ren": last["ren"] if last else None, "snit": last["snit"] if last else None,
                           "slut": last["slut"] if last else None, "latest": last["til"] if last else None, "cycles": cycles[-8:]}
    bounds = hold
    if not bounds:
        if len(ref) < ICE_MIN_REF:
            return out
        ren, snit = sorted(c["ren"] for c in ref), sorted(c["snit"] for c in ref)
        bounds = {"ren_low": ren[int(len(ren) * ICE_LOW_Q)], "snit_low": snit[int(len(snit) * ICE_LOW_Q)],
                  "ren_normal": round(median(ren), 3), "snit_normal": round(median(snit), 3)}
    run: list[dict[str, Any]] = []
    for c in reversed(cycles):
        if not (c["ren"] < bounds["ren_low"] or c["snit"] < bounds["snit_low"]):
            break
        run.append(c)
    run.reverse()
    hours = sum(c["til"] - c["t"] for c in run) / 3600  # driftstimer i løbet
    status = "ice" if len(run) >= ICE_WATCH_N and hours >= ICE_ALARM_H else "watch" if len(run) >= ICE_WATCH_N else "ok"
    out.update(status=status, low_run=len(run), low_hours=round(hours, 1), since=run[0]["t"] if run else None, bounds=bounds)
    return out


def frost_fade(cycles: list[dict[str, Any]], min_n: int = 6) -> dict[str, Any] | None:
    """Faldet i ydelse frem mod afrimningen: fra de første 15 min. af en cyklus (ren flade) til de sidste 10 min. (»slut«),
    delt i husets rolige og hårde cyklusser (under/over median-effekten) – så det kan ses, hvad hård drift koster undervejs,
    ikke kun ved selve afrimningen. Kun cyklusser på mindst 30 min. i rimvejr (under ``FADE_T_OUT`` °C ude).
    None, før der er ``min_n`` cyklusser i hver gruppe."""
    cs = [c for c in cycles if c.get("min", 0) >= 30 and c.get("ude") is not None and c["ude"] < FADE_T_OUT
          and c.get("cop_ren") and c.get("cop_slut") is not None and c.get("kw")]
    if len(cs) < 2 * min_n:
        return None
    mid = median(c["kw"] for c in cs)
    calm, hard = [c for c in cs if c["kw"] <= mid], [c for c in cs if c["kw"] > mid]
    if len(calm) < min_n or len(hard) < min_n:
        return None

    def grp(g: list[dict[str, Any]]) -> dict[str, Any]:
        return {"n": len(g), "kw": round(median(c["kw"] for c in g), 1), "min": round(median(c["min"] for c in g)),
                "cop_ren": round(median(c["cop_ren"] for c in g), 2), "cop_slut": round(median(c["cop_slut"] for c in g), 2),
                "fald_pct": round(100 * median(1 - c["cop_slut"] / c["cop_ren"] for c in g), 1)}

    return {"grænse_kw": round(mid, 1), "rolig": grp(calm), "hård": grp(hard)}


# ---------------------------------------------------------------- opvarmningen: løft → effekt
# Varmepumpen regulerer selv fremløbet til målet. Hvor højt målet står over returen (løftet), er derfor det håndtag, der
# bestemmer effekten: lille løft = rolig drift, stort løft = alt, hvad den kan. Sammenhængen læres af husets egne portioner.
LIFT_MIN_N = 8  # så mange portioner skal en hel grad have, før den tæller
LIFT_MIN_BANDS = 4  # så mange grader skal være målt, før løftet bruges
ECO_OVER_MIN = 1.15  # det rolige område ligger lidt over den laveste effekt, varmepumpen kan holde (under den taber den)
LIFT_FULL = 0.95  # »alt, hvad den kan«: det laveste løft, der giver så stor en del af den største målte effekt
FADE_T_OUT = 3.0  # rimvejr: under så mange °C ude sætter rimen sig


def _step_at(series: list[tuple[float, float]], t: float) -> float | None:
    """Trinseriens værdi på tidspunktet ``t`` (den seneste ændring før eller på ``t``); None før den første."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else None


def fit_lift(slots: list[Slot], ret: dict[int, float], target: list[tuple[float, float]],
             min_n: int = LIFT_MIN_N, min_bands: int = LIFT_MIN_BANDS) -> list[list[float]]:
    """Løftet → effekt: median kW varme for hver hel grad, fremløbsmålet stod over returen, i stabil drift.

    ``ret``: returens 5-minutters snit {portionens start: °C}; ``target``: fremløbsmålet som trinserie [(tid, °C)], sorteret.
    Portioner, hvor målet blev ændret undervejs, tæller ikke. Tabellen [[løft, kW, antal], …] gøres voksende – et større
    løft giver aldrig mindre effekt; nabograder, der bryder det, får deres fælles (vægtede) effekt – så den kan læses begge
    veje. Tom, når færre end ``min_bands`` grader er målt."""
    by: dict[int, list[float]] = {}
    for s in slots:
        r, sp = ret.get(s.ts), _step_at(target, s.ts)
        if r is None or sp is None or _step_at(target, s.ts + 299) != sp:
            continue
        by.setdefault(round(sp - r), []).append(s.q * 12)
    rows = [[float(k), float(median(v)), len(v)] for k, v in sorted(by.items()) if len(v) >= min_n and k >= 1]
    if len(rows) < min_bands:
        return []
    blocks = [[i, i, r[1] * r[2], r[2]] for i, r in enumerate(rows)]  # [første række, sidste række, sum, antal]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] / blocks[i][3] > blocks[i + 1][2] / blocks[i + 1][3] + 1e-9:
            a, b = blocks[i], blocks.pop(i + 1)
            blocks[i] = [a[0], b[1], a[2] + b[2], a[3] + b[3]]
            i = max(i - 1, 0)
        else:
            i += 1
    out: list[list[float]] = []
    for a, b, tot, n in blocks:
        out += [[rows[j][0], round(tot / n, 2), int(rows[j][2])] for j in range(int(a), int(b) + 1)]
    return out


def lift_for(table: list[list[float]], kw: float) -> float | None:
    """Det laveste løft, der giver ``kw`` (lineært mellem de målte grader); None, når tabellen ikke når så højt."""
    if not table:
        return None
    if kw <= table[0][1]:
        return float(table[0][0])
    for (l0, k0, _n0), (l1, k1, _n1) in zip(table, table[1:]):
        if k1 >= kw:
            return float(l0 + (l1 - l0) * (kw - k0) / (k1 - k0)) if k1 > k0 else float(l1)
    return None


def kw_at(table: list[list[float]], lift: float) -> float | None:
    """Effekten ved et løft (lineært mellem de målte grader, fladt uden for dem)."""
    if not table:
        return None
    if lift <= table[0][0]:
        return float(table[0][1])
    for (l0, k0, _n0), (l1, k1, _n1) in zip(table, table[1:]):
        if lift <= l1:
            return float(k0 + (k1 - k0) * (lift - l0) / (l1 - l0))
    return float(table[-1][1])


def lift_levels(table: list[list[float]], p_min: float | None, p_max: float | None) -> dict[str, Any] | None:
    """Prioritetens løft under opvarmning – så hårdt må varmepumpen presses på vej mod planens fremløb:

    - økonomi: det rolige område – løftet, der giver ``ECO_OVER_MIN`` × den laveste effekt, varmepumpen holder stabilt
      (``p_min``). Lavere end det taber den (og stopper/starter); højere koster mere pr. kWh varme og giver flere afrimninger.
    - balance: midt imellem den effekt og varmepumpens største (``p_max``).
    - komfort: intet loft – målet direkte (står ikke i svaret).
    Løft i hele grader (varmepumpens trin). None, når tabellen ikke er lært, eller roligt og fuldt ikke kan skelnes."""
    if not table or not p_min or not p_max:
        return None
    top = table[-1][1]
    full = next(r[0] for r in table if r[1] >= LIFT_FULL * top)
    eco_kw = p_min * ECO_OVER_MIN
    le = lift_for(table, eco_kw)
    if le is None:
        return None
    le = max(float(round(le)), table[0][0])
    if le >= full:
        return None
    out: dict[str, Any] = {"economy": {"løft": le, "kw": round(kw_at(table, le) or 0.0, 2)}, "fuld": {"løft": full, "kw": round(top, 2)}}
    lb = lift_for(table, (eco_kw + max(p_max, eco_kw)) / 2)
    if lb is not None:
        lb = max(float(round(lb)), le + 1)
        if lb < full:
            out["balance"] = {"løft": lb, "kw": round(kw_at(table, lb) or 0.0, 2)}
    return out


# ---------------------------------------------------------------- COP pr. døgn
def daily_cop(hours: list[Hour], day_of, steady: Fit | None, days: list[str]) -> dict[str, Any] | None:
    """Varme ÷ el for de givne døgn (lokal dato via ``day_of(ts)``).

    Giver også COP uden varmt vand og hvad den stabile COP-model forventede for
    den samme varme, ved timernes ude- og fremløbstemperatur.
    """
    hs = [h for h in hours if day_of(h.ts) in days and h.q is not None and h.e is not None]
    if not hs:
        return None
    q = sum(max(h.q, 0.0) for h in hs)
    e = sum(max(h.e, 0.0) for h in hs)
    sp = [h for h in hs if not (h.dhw or 0) > 0 and not (h.dhw is None and (h.t_flow or 0) > MAX_FLOW_SPACE_HEATING)]
    qs = sum(max(h.q, 0.0) for h in sp)
    es = sum(max(h.e, 0.0) for h in sp)
    exp = None
    if steady is not None and steady.value is not None:
        a, b, c, d = (steady.extra.get(k, 0.0) for k in ("a", "b", "c", "d"))
        w = [(h.q, a + b * h.t_out + c * h.t_flow + d * h.q) for h in sp if h.q and h.q > 0.3 and h.t_out is not None and h.t_flow is not None]
        if w:
            exp = sum(x for x, _ in w) / sum(x / max(1.2, m) for x, m in w)
    cop = q / e if e > 0.5 else None
    cop_sp = qs / es if es > 0.5 else None
    return {
        "days": days,
        "heat_kwh": round(q, 1),
        "el_kwh": round(e, 1),
        "cop": None if cop is None else round(cop, 2),
        "cop_without_dhw": None if cop_sp is None else round(cop_sp, 2),
        "dhw_hours": sum(1 for h in hs if (h.dhw or 0) > 0),
        "expected_steady": None if exp is None else round(exp, 2),
        "vs_steady_pct": None if exp is None or cop_sp is None else round((cop_sp / exp - 1) * 100, 1),
        "t_out_mean": round(sum(h.t_out for h in hs if h.t_out is not None) / max(1, sum(1 for h in hs if h.t_out is not None)), 1),
        "hours": len(hs),
    }


# ---------------------------------------------------------------- vejr: vind og sol
SECTORS = ["N", "NØ", "Ø", "SØ", "S", "SV", "V", "NV"]


def wind_sector(u: float, v: float) -> str | None:
    """Retning vinden kommer FRA, ud fra middel-vindvektoren (u = fra øst, v = fra nord)."""
    from math import atan2, degrees, hypot

    if hypot(u, v) < 0.5:
        return None
    b = (degrees(atan2(u, v)) + 360) % 360
    return SECTORS[int(((b + 22.5) % 360) // 45)]


def sun_factor(h: Hour) -> float | None:
    """1 = klar himmel i dagslys, 0 = nat eller helt overskyet."""
    if h.cloud is None or h.uv is None:
        return None
    return (1 - h.cloud / 100) if h.uv > 0 else 0.0


def fit_weather(hours: list[Hour], block_hours: int = 48, min_blocks: int = 15, min_wind_sd: float = 1.0) -> Fit:
    """Varmetab med vind og sol: varme = H·ΔT·(1 + w·vind) − G − S·sol (48-timers blokke).

    Værdien er w i % ekstra varmetab pr. m/s vind. Solens gratis varme S (kW ved
    klar himmel i dagslys) ligger i attributterne.
    """
    known = [h.t_in for h in hours if h.t_in is not None]
    t_in_typ = median(known) if known else None
    blocks: dict[int, list[Hour]] = {}
    for h in hours:
        if h.q is None or h.t_out is None or h.wind is None or sun_factor(h) is None or (h.t_in is None and t_in_typ is None):
            continue
        blocks.setdefault(h.ts // (block_hours * 3600), []).append(h)
    rows, y, winds, touts = [], [], [], []
    for hs in blocks.values():
        if len(hs) < block_hours * 0.9:
            continue
        dt = [(h.t_in if h.t_in is not None else t_in_typ) - h.t_out for h in hs]
        q = sum(max(h.q, 0.0) for h in hs if not (h.dhw or 0) >= 30) / len(hs)
        w = sum(h.wind for h in hs) / len(hs)
        sun = sum(sun_factor(h) for h in hs) / len(hs)
        d = sum(dt) / len(dt)
        rows.append([d, d * w, -1.0, -sun])
        y.append(q)
        winds.append(w)
        touts += [h.t_out for h in hs]
    lo, hi = _range(touts)
    if len(rows) < min_blocks:
        return Fit(None, len(rows), t_out_min=lo, t_out_max=hi, note=f"samler vejrdata – kræver {min_blocks} blokke à {block_hours} t")
    mw = sum(winds) / len(winds)
    sd = sqrt(sum((w - mw) ** 2 for w in winds) / len(winds))
    if sd < min_wind_sd:
        return Fit(None, len(rows), t_out_min=lo, t_out_max=hi, note="for lidt variation i vindstyrke endnu")
    res = ols(rows, y)
    if not res:
        return Fit(None, len(rows), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
    (h_, hw, g, sg), (se_h, se_hw, _, se_s) = res
    if h_ <= 0:
        return Fit(None, len(rows), t_out_min=lo, t_out_max=hi, note="ingen sammenhæng")
    pred = [sum(b * x for b, x in zip((h_, hw, g, sg), r)) for r in rows]
    r2, rmse = _r2_rmse(y, pred)
    return Fit(
        round(hw / h_ * 100, 2), len(rows), r2, rmse and round(rmse, 3), lo, hi, "fit",
        {"heat_loss_calm_w_k": round(h_ * 1000, 1), "wind_pct_se": round(se_hw / h_ * 100, 2),
         "sun_kw_clear": round(sg, 2), "sun_kw_se": round(se_s, 2), "gains_w": round(g * 1000, 0),
         "wind_mean_ms": round(mw, 1), "value_is": "% ekstra varmetab pr. m/s vind"},
    )


def fit_wind_sectors(hours: list[Hour], min_hours: int = 6) -> dict[str, Any]:
    """Afkøling i pauser uden varme pr. vindretning (grader i timen ved 20 graders forskel)."""
    by: dict[str, list[float]] = {}
    sh = sorted(hours, key=lambda h: h.ts)
    for a, b in zip(sh, sh[1:]):
        if b.ts - a.ts != 3600 or None in (a.q, b.q, a.t_in, b.t_in, b.t_out, b.wind_u, b.wind_v):
            continue
        if a.q > 0.05 or b.q > 0.05:
            continue
        dt = (a.t_in + b.t_in) / 2 - b.t_out
        sec = wind_sector(b.wind_u, b.wind_v)
        if dt < 5 or sec is None:
            continue
        by.setdefault(sec, []).append((a.t_in - b.t_in) / dt * 20)
    return {k: {"hours": len(v), "k_per_hour_at_20k": round(median(v), 3) if len(v) >= min_hours else None}
            for k, v in sorted(by.items(), key=lambda kv: SECTORS.index(kv[0]))}


# ---------------------------------------------------------------- rummodel
@dataclass
class RoomHour:
    ts: int
    t_room: float | None
    t_out: float | None
    q: float | None = None  # varme til huset i timen (kWh), når der kun er én varmekilde
    sun: float | None = None  # 0–1, klar himmel i dagslys
    aired: bool = False  # udluftning markeret i denne time
    inputs: dict[str, float | None] | None = None  # kWh pr. varmekilde (varme eller el)
    neighbours: dict[str, float | None] | None = None  # temperatur i nabozoner (fx kælder, trappeopgang)


def _inp(h: RoomHour, k: str) -> float | None:
    return h.inputs.get(k) if h.inputs is not None else h.q


def fit_room(hours: list[RoomHour], block: int = 6, step: int = 3, min_blocks: int = 40, min_coverage: float = 0.6) -> Fit:
    """Hvordan et rum opfører sig, lært af historikken (blokke à 6 timer).

    Temperaturændring over blokken = Σ_kilder (a·kWh nu + a2·kWh de foregående 6
    timer) + b·(ude − rum)·timer + c (gratis varme) [+ s·sol] [+ Σ_naboer n·(nabo − rum)·timer].
    Hver varmekilde får sin egen virkning, så modellen selv finder ud af, hvem der
    varmer rummet. En kilde med data i hele perioden (mindst 60 % af blokkene) er
    altid med. En nyere kilde tages med, så snart der er blokke nok med data fra
    alle kilder, og den har en tydelig virkning på netop dette rum (mindst 2
    standardfejl) – ellers bruges hele perioden uden den.
    Blokke med udluftning udelades, og afvigere (over 3× den typiske afvigelse)
    sorteres fra, før der tilpasses igen.

    Værdien er rummets afkøling i °C pr. time ved 20 graders forskel mellem inde
    og ude – det tal falder, når rummet bliver bedre isoleret.
    """
    hs = sorted(hours, key=lambda h: h.ts)
    idx = {h.ts: h for h in hs}
    keys = sorted({k for h in hs for k in (h.inputs or {})}) if any(h.inputs is not None for h in hs) else ["q"]
    cands = []
    aired_blocks = 0
    for i in range(block, len(hs) - block + 1, step):
        cur = [idx.get(hs[i].ts + k * 3600) for k in range(block)]
        prev = [idx.get(hs[i].ts - (block - k) * 3600) for k in range(block)]
        if None in cur or None in prev:
            continue
        if any(h.aired for h in cur):
            aired_blocks += 1
            continue
        if any(h.t_out is None or h.t_room is None for h in cur) or prev[-1].t_room is None:
            continue
        cands.append((cur, prev))
    cover = {k: sum(1 for cur, prev in cands if all(_inp(h, k) is not None for h in cur + prev)) / max(1, len(cands)) for k in keys}
    dense = [k for k in keys if cover[k] >= min_coverage]
    sparse = sorted((k for k in keys if k not in dense and cover[k] > 0), key=lambda k: -cover[k])
    use_sun = sum(1 for h in hs if h.sun is not None) > len(hs) * 0.5
    nkeys = sorted({k for h in hs for k in (h.neighbours or {})})
    ncover = {k: sum(1 for cur, _ in cands if all((h.neighbours or {}).get(k) is not None for h in cur)) / max(1, len(cands)) for k in nkeys}
    ndense = [k for k in nkeys if ncover[k] >= min_coverage]
    nsparse = sorted((k for k in nkeys if k not in ndense and ncover[k] > 0), key=lambda k: -ncover[k])

    def fit(use: list[str], nuse: list[str]) -> dict[str, Any] | None:
        rows, y, touts = [], [], []
        for cur, prev in cands:
            if any(_inp(h, k) is None for k in use for h in cur + prev):
                continue
            if any((h.neighbours or {}).get(k) is None for k in nuse for h in cur):
                continue
            r: list[float] = []
            for k in use:
                r += [sum(max(_inp(h, k), 0.0) for h in cur), sum(max(_inp(h, k), 0.0) for h in prev)]
            r += [sum(h.t_out - cur[0].t_room for h in cur), 1.0]
            if use_sun:
                r.append(sum(h.sun or 0.0 for h in cur))
            for k in nuse:
                r.append(sum(h.neighbours[k] - cur[0].t_room for h in cur))
            rows.append(r)
            # Ændringen over de samme 6 timer, som tilførslen er summeret over (fra timen før blokken til dens sidste time).
            # Før 0.10.0 dækkede ændringen kun 5 timer, så alle rum blev forudsagt ca. 17 % for langsomt.
            y.append(cur[-1].t_room - prev[-1].t_room)
            touts += [h.t_out for h in cur]
        if len(rows) < min_blocks:
            return None
        res = ols(rows, y)
        if not res:
            return None
        beta, _ = res
        resid = [v - sum(b * x for b, x in zip(beta, r)) for r, v in zip(rows, y)]
        mad = median(abs(e) for e in resid) or 1e-6
        keep = [k for k, e in enumerate(resid) if abs(e) <= 3 * 1.4826 * mad]
        rows2, y2 = [rows[k] for k in keep], [y[k] for k in keep]
        res = ols(rows2, y2)
        if not res:
            return None
        beta, se = res
        pred = [sum(bb * x for bb, x in zip(beta, r)) for r in rows2]
        r2, rmse = _r2_rmse(y2, pred)
        return {"use": use, "nuse": nuse, "beta": beta, "se": se, "n": len(rows2), "r2": r2, "rmse": rmse, "outliers": len(rows) - len(keep),
                "touts": touts}

    best = fit(dense, ndense) if dense else None
    added: list[str] = []
    for k in sparse:
        base_use = (best or {}).get("use", dense)
        cand = fit(base_use + [k], (best or {}).get("nuse", ndense))
        if cand is None:
            continue
        i = 2 * cand["use"].index(k)
        tot = cand["beta"][i] + cand["beta"][i + 1]
        se_tot = sqrt(cand["se"][i] ** 2 + cand["se"][i + 1] ** 2) or 1e-9
        if best is None or (tot > 0 and tot / se_tot >= 2):
            best = cand
            added.append(k)
    # Nabozoner med kortere historik tages med, når de har en tydelig virkning (mindst 2 standardfejl)
    for k in nsparse:
        if best is None:
            break
        cand = fit(best["use"], best["nuse"] + [k])
        if cand is None:
            continue
        j = 2 * len(cand["use"]) + 2 + (1 if use_sun else 0) + cand["nuse"].index(k)
        if cand["se"][j] > 0 and abs(cand["beta"][j] / cand["se"][j]) >= 2:
            best = cand
            added.append(k)
    use = (best or {}).get("use", [])
    nuse = (best or {}).get("nuse", [])
    skipped = [k for k in keys if k not in use]
    base_extra = {"aired_blocks_skipped": aired_blocks, "sources_too_little_data": skipped}
    if best is None:
        return Fit(None, 0, note="for få blokke à 6 timer endnu" if keys else "ingen varmekilde", extra=base_extra)
    beta, se, rmse = best["beta"], best["se"], best["rmse"]
    lo, hi = _range(best["touts"])
    n_src = 2 * len(use)
    b, c = beta[n_src], beta[n_src + 1]
    s = beta[n_src + 2] if use_sun else None
    nb0 = n_src + 2 + (1 if use_sun else 0)
    neighbours = {k: {"per_h_k": round(beta[nb0 + i], 4), "se": round(se[nb0 + i], 4),
                      "k_per_hour_at_5k": round(beta[nb0 + i] * 5, 3)} for i, k in enumerate(nuse)}
    if b <= 0:
        return Fit(None, best["n"], best["r2"], rmse, lo, hi, note="rummet falder ikke med kulden – for lidt data", extra=base_extra)
    per_source = {k: {"now": round(beta[2 * i], 4), "delayed": round(beta[2 * i + 1], 4),
                      "total": round(beta[2 * i] + beta[2 * i + 1], 4), "se_now": round(se[2 * i], 4)} for i, k in enumerate(use)}
    first = per_source[use[0]]
    return Fit(
        round(b * 20, 3), best["n"], best["r2"], rmse and round(rmse, 2), lo, hi, "fit",
        {"time_constant_h": round(1 / b, 0),
         "heat_now_k_per_kwh": first["now"], "heat_delayed_k_per_kwh": first["delayed"],
         "sources": per_source,
         "sources_added_recent": added or None,
         "neighbours": neighbours or None,
         "free_heat_k_per_day": round(c * 24 / block, 2),
         "sun_k_per_clear_hour": None if s is None else round(s, 3),
         "accuracy_6h_k": rmse and round(rmse, 2),
         "aired_blocks_skipped": aired_blocks, "outliers_skipped": best["outliers"], "sources_too_little_data": skipped or None,
         "value_is": "°C pr. time rummet falder ved 20 graders forskel mellem inde og ude"},
    )


def detect_airing(five_min: list[tuple[int, float]], drop: float = 0.3, window: int = 900, after: int = 3 * 3600) -> set[int]:
    """Timer (start) der skal udelades pga. udluftning: rummet faldt mindst ``drop``
    grader inden for ``window`` sekunder. Timerne indtil ``after`` sekunder efter markeres."""
    pts = sorted(five_min)
    hours: set[int] = set()
    for i, (t, v) in enumerate(pts):
        for t2, v2 in pts[i + 1:]:
            if t2 - t > window:
                break
            if v - v2 >= drop:
                h = t // 3600 * 3600
                for k in range(0, after + 3600, 3600):
                    hours.add(h + k)
                break
    return hours


def daily_el(hours: list[Hour], day_of, days: list[str]) -> dict[str, Any] | None:
    """El pr. døgn for en varmekilde uden varmemåler (fx luft-luft)."""
    hs = [h for h in hours if day_of(h.ts) in days and h.e is not None]
    if not hs:
        return None
    touts = [h.t_out for h in hs if h.t_out is not None]
    return {"days": days, "el_kwh": round(sum(max(h.e, 0.0) for h in hs), 2),
            "t_out_mean": round(sum(touts) / len(touts), 1) if touts else None, "hours": len(hs)}


def validate_room(hours: list[RoomHour], fit: Fit, window: int = 6, step: int = 3, min_windows: int = 20) -> dict[str, Any] | None:
    """Rummodellen afprøvet, som planen bruger den: 6 timer frem fra den målte starttemperatur med den målte tilførsel.

    Giver en ærlig præcision (afvigere tæller med, og der er ingen sol i planen) og en eventuel skævhed.
    Tidsvinduer med udluftning eller manglende data springes over."""
    ex = fit.extra or {}
    if fit.value is None or not ex.get("sources"):
        return None
    cool = fit.value / 20
    free = float(ex.get("free_heat_k_per_day") or 0.0) / 24
    srcs = {k: (float(v.get("now") or 0.0), float(v.get("delayed") or 0.0)) for k, v in ex["sources"].items()}
    nbs = {k: float(v.get("per_h_k") or 0.0) for k, v in (ex.get("neighbours") or {}).items()}
    by = {h.ts: h for h in hours}
    errs: list[float] = []
    for t0 in sorted(by)[::step]:
        win = [by.get(t0 + k * 3600) for k in range(window + 1)]
        prev = [by.get(t0 + (k - window) * 3600) for k in range(window)]
        if any(h is None or h.t_room is None or h.aired for h in win) or any(h is None for h in prev):
            continue
        t, ok = win[0].t_room, True
        for h, hp in zip(win[:-1], prev):
            qs = [(_inp(h, k), _inp(hp, k)) for k in srcs]
            tn = [(h.neighbours or {}).get(k) for k in nbs]
            if h.t_out is None or any(a is None or b is None for a, b in qs) or any(x is None for x in tn):
                ok = False
                break
            t = (t + sum(an * max(a, 0.0) + ad * max(b, 0.0) for (an, ad), (a, b) in zip(srcs.values(), qs)) + cool * (h.t_out - t) + free
                 + sum(kk * (x - t) for kk, x in zip(nbs.values(), tn)))
        if ok:
            errs.append(win[-1].t_room - t)
    if len(errs) < min_windows:
        return None
    n = len(errs)
    return {"rmse": round(sqrt(sum(e * e for e in errs) / n), 2), "bias": round(sum(errs) / n, 2), "n": n}
