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
  setpoint LG's fremløbs-setpunkt, middel (valgfri)
  offset   kurve-forskydning, middel (valgfri)

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
    setpoint: float | None = None
    offset: float | None = None


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
def fit_heat_loss(hours: list[Hour], block_hours: int = 48, min_blocks: int = 8, min_spread: float = 4.0) -> Fit:
    """Husets varmetab H (W/K) og gratis varme G (W) fra blokke af 48 timer.

    Gennemsnitlig varmeeffekt = H·(inde − ude) − G. Blokke på 48 timer,
    fordi gulvet gemmer varme fra dag til dag. Timer med varmt vand trækkes
    fra, når opsamleren ved hvornår der blev lavet varmt vand.
    """
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
    dhw_corrected = 0
    for hs in blocks.values():
        if len(hs) < block_hours * 0.9:
            continue
        q = 0.0
        for h in hs:
            if h.dhw is not None and h.dhw >= 30:
                dhw_corrected += 1
                continue
            q += max(h.q or 0.0, 0.0)
        x.append(sum(h.t_in - h.t_out for h in hs) / len(hs))
        y.append(q / len(hs))
        touts.append(sum(h.t_out for h in hs) / len(hs))
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
         "hours_with_assumed_indoor": assumed, "indoor_assumed": round(t_in_typical, 1) if t_in_typical else None},
    )


# ---------------------------------------------------------------- COP
def fit_cop(hours: list[Hour], min_n: int = 30) -> Fit:
    """COP = a + b·ude + c·fremløb, inkl. afrimning (afrimningstimer tælles med).

    Fremløbsleddet c kan kun bestemmes, hvis fremløbet har varieret
    uafhængigt af udetemperaturen. Er det for usikkert, bruges en standard
    (−0,08 pr. grad) og c markeres som ``prior``.
    """
    s = [
        h for h in hours
        if h.q is not None and h.e is not None and h.t_out is not None
        and h.q >= 1.0 and h.e >= 0.25 and _space_heating(h)
        and COP_LIMITS[0] <= h.q / h.e <= COP_LIMITS[1]
    ]
    lo, hi = _range([h.t_out for h in s])
    if len(s) < min_n:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="for få driftstimer")
    y = [h.q / h.e for h in s]
    with_flow = [h for h in s if h.t_flow is not None]
    c_source = "prior"
    beta = None
    # Fremløbet skal have varieret uafhængigt af udetemperaturen (fx pga. kurve-
    # forskydning), ellers kan c ikke skilles fra b. Mål: spredning på fremløbet
    # efter at udetemperaturens andel er trukket fra.
    indep = None
    if len(with_flow) >= min_n:
        fl = ols([[1.0, h.t_out] for h in with_flow], [h.t_flow for h in with_flow])
        if fl:
            (fa, fb), _ = fl
            r = [h.t_flow - fa - fb * h.t_out for h in with_flow]
            indep = sqrt(sum(v * v for v in r) / len(r))
    if indep is not None and indep >= 1.0:
        res = ols([[1.0, h.t_out, h.t_flow] for h in with_flow], [h.q / h.e for h in with_flow])
        if res:
            (a, b, c), (_, _, se_c) = res
            if c < 0 and se_c < abs(c) / 2:
                beta, c_source = (a, b, c), "fit"
                s, y = with_flow, [h.q / h.e for h in with_flow]
    if beta is None:
        # c fra standard; tilpas a og b på (COP − c·fremløb)
        c = PRIOR_COP_PER_K_FLOW
        ref = [(h, h.q / h.e - c * (h.t_flow if h.t_flow is not None else 35.0)) for h in s]
        res = ols([[1.0, h.t_out] for h, _ in ref], [v for _, v in ref])
        if not res:
            return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
        (a, b), _ = res
        beta = (a, b, c)
    a, b, c = beta
    pred = [a + b * h.t_out + c * (h.t_flow if h.t_flow is not None else 35.0) for h in s]
    r2, rmse = _r2_rmse(y, pred)
    defrost_hours = sum(1 for h in s if (h.defrost or 0) > 0)
    return Fit(
        round(a + c * 35.0, 3), len(s), r2, rmse and round(rmse, 3), lo, hi, "fit",
        {"a": round(a, 4), "b": round(b, 4), "c": round(c, 4), "c_source": c_source,
         "flow_independent_k": None if indep is None else round(indep, 2),
         "formula": "COP = a + b·ude + c·fremløb", "value_is": "COP ved 0 °C ude og 35 °C fremløb",
         "hours_with_defrost": defrost_hours},
    )


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


# ---------------------------------------------------------------- kurve
def fit_curve(hours: list[Hour], min_n: int = 24, min_spread: float = 3.0) -> Fit:
    """LG's kurve uden forskydning: setpunkt − forskydning = C0 + C1·ude."""
    s = [h for h in hours if None not in (h.setpoint, h.offset, h.t_out) and (h.comp is None or h.comp >= 30)]
    x = [h.t_out for h in s]
    y = [h.setpoint - h.offset for h in s]
    lo, hi = _range(x)
    if len(s) < min_n or (max(x) - min(x) if x else 0) < min_spread:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="for få timer eller for lille spredning i udetemperatur")
    ab = theil_sen(x, y)
    if ab is None:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
    a, b = ab
    r2, rmse = _r2_rmse(y, [a + b * v for v in x])
    return Fit(round(a, 2), len(s), r2, rmse and round(rmse, 2), lo, hi, "fit",
               {"slope": round(b, 3), "formula": "fremløb uden forskydning = værdi + hældning·ude"})


# ---------------------------------------------------------------- afrimning
def fit_defrost(hours: list[Hour], min_comp_hours: float = 10.0) -> Fit:
    """Afrimninger pr. kompressortime, i alt og fordelt på ude og effekt (kun rapport)."""
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
    return Fit(
        round(sum(h.defrost for h in s) / comp_h, 3), len(s), None, None, lo, hi, "fit",
        {"comp_hours": round(comp_h, 1),
         "per_bin": {k: {"comp_hours": round(v[0], 1), "per_hour": round(v[1] / v[0], 2) if v[0] else None} for k, v in sorted(bins.items())},
         "value_is": "afrimninger pr. kompressortime"},
    )


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
def calibrate(hours: list[Hour]) -> dict[str, Fit]:
    pmax, pmin = fit_power(hours)
    return {
        "heat_loss": fit_heat_loss(hours),
        "cop": fit_cop(hours),
        "emission": fit_emission(hours),
        "max_power": pmax,
        "min_power": pmin,
        "curve": fit_curve(hours),
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


def fit_cop_steady(slots: list[Slot], min_n: int = 100) -> Fit:
    """COP i stabil drift = a + b·ude + c·fremløb (uden opstart og afrimning)."""
    s = [x for x in slots if COP_LIMITS[0] <= x.q / x.e <= COP_LIMITS[1]]
    lo, hi = _range([x.t_out for x in s])
    if len(s) < min_n:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="for få 5-minutters portioner i stabil drift")
    y = [x.q / x.e for x in s]
    res = ols([[1.0, x.t_out, x.t_flow] for x in s], y)
    if not res:
        return Fit(None, len(s), t_out_min=lo, t_out_max=hi, note="kunne ikke tilpasses")
    (a, b, c), (_, sb, sc) = res
    c_source = "fit"
    if not (c < 0 and sc < abs(c) / 2):
        c, c_source = PRIOR_COP_PER_K_FLOW / 2, "prior"
        r2 = ols([[1.0, x.t_out] for x in s], [v - c * x.t_flow for v, x in zip(y, s)])
        if r2:
            (a, b), _ = r2
    pred = [a + b * x.t_out + c * x.t_flow for x in s]
    r2v, rmse = _r2_rmse(y, pred)
    return Fit(
        round(a + c * 35.0, 3), len(s), r2v, rmse and round(rmse, 3), lo, hi, "fit",
        {"a": round(a, 4), "b": round(b, 4), "c": round(c, 4), "c_source": c_source, "c_se": round(sc, 4),
         "per_k_flow_pct": round(-c / (a + b * 5 + c * 33) * 100, 1),
         "formula": "COP = a + b·ude + c·fremløb", "value_is": "COP i stabil drift ved 0 °C ude og 35 °C fremløb",
         "method": "5-minutters portioner fra 10 min efter start til afrimning, varmt vand eller stop"},
    )


def fit_cop_loss(steady: Fit, five: dict[int, dict[str, float | None]], comp: list[tuple[float, float]],
                 dhw: list[tuple[float, float]]) -> Fit:
    """Tab ved opstart og afrimning: målt COP over al kompressortid mod stabil COP for samme varme."""
    if steady.value is None:
        return Fit(None, 0, note="kræver COP i stabil drift")
    a, b, c = steady.extra["a"], steady.extra["b"], steady.extra["c"]
    qa = ea = qs = es = 0.0
    n = 0
    for x, y in comp:
        t = int(x // 300 * 300)
        while t < y:
            r = five.get(t) or {}
            q, e, u, f = r.get("q"), r.get("e"), r.get("t_out"), r.get("t_flow")
            if None not in (q, e) and not any(p < t + 300 and k > t for p, k in dhw):
                qa += q
                ea += e
                n += 1
                if None not in (u, f) and q > 0:
                    qs += q
                    es += q / max(1.2, a + b * u + c * f)
            t += 300
    if ea <= 1 or es <= 0:
        return Fit(None, n, note="for lidt kompressortid")
    eff, st = qa / ea, qs / es
    return Fit(round((1 - eff / st) * 100, 1), n, None, None, steady.t_out_min, steady.t_out_max, "fit",
               {"cop_effective": round(eff, 2), "cop_steady_same_heat": round(st, 2), "value_is": "% lavere COP end stabil drift"})
