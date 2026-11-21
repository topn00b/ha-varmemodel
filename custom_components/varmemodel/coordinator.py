"""Opsamling og natlig kalibrering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import aggregate as agg
from .calibration import Fit, Hour, Slot, calibrate, daily_cop, fit_cop_loss, fit_cop_steady, guard, steady_slots
from .const import (
    BACKFILL_DAYS,
    CALIBRATION_HOUR,
    CALIBRATION_MINUTE,
    CALIB_VERSION,
    COLLECT_KEEP_DAYS,
    CONF_COMPRESSOR,
    CONF_DEFROST,
    CONF_DHW,
    CONF_ELECTRICITY,
    CONF_FLOW,
    CONF_HEAT,
    CONF_INDOOR,
    CONF_OFFSET,
    CONF_OFFSET_ATTR,
    CONF_OFFSET_ZERO,
    CONF_OUTDOOR,
    CONF_RETURN,
    CONF_SETPOINT,
    CONF_SETPOINT_ATTR,
    CONF_WINDOW_DAYS,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
    FIT_KEYS,
    MODE_CALIBRATED,
    MODE_FROZEN,
)

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


def _ts(v: Any) -> float:
    return v.timestamp() if isinstance(v, datetime) else float(v)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


class VarmemodelCoordinator:
    """Holder opsamlede timer, kalibreringen og de godkendte tal."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.collected: dict[str, dict[str, float]] = {}
        self.accepted: dict[str, dict[str, Any]] = {}
        self.fits: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.slots: dict[str, list[float]] = {}
        self.daily: dict[str, Any] = {}
        self.mode: str = MODE_CALIBRATED
        self.last_hours: list[Hour] = []
        self.status: str = "waiting"
        self.status_note: str = ""
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------ konfiguration
    @property
    def conf(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    def _id(self, key: str) -> str | None:
        v = self.conf.get(key)
        return v or None

    # ------------------------------------------------------------ livscyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.collected = data.get("collected", {})
        self.accepted = data.get("accepted", {})
        self.fits = data.get("fits", {})
        self.runs = data.get("runs", [])
        self.mode = data.get("mode", MODE_CALIBRATED)
        self.slots = data.get("slots", {})
        self.daily = data.get("daily", {})
        if data and data.get("calib_version") != CALIB_VERSION:
            # Ny regnemetode: tallene regnes forfra (opsamlede timer beholdes)
            self.accepted, self.fits, self.runs = {}, {}, []
        self._unsubs.append(async_track_time_change(self.hass, self._on_hour, minute=0, second=30))
        self._unsubs.append(
            async_track_time_change(self.hass, self._on_night, hour=CALIBRATION_HOUR, minute=CALIBRATION_MINUTE, second=0)
        )
        # Første kalibrering lidt efter opstart, hvis den seneste er over et døgn gammel
        last = self.runs[-1]["at"] if self.runs else 0
        if time.time() - last > 86400:
            self._unsubs.append(async_call_later(self.hass, 120, self._on_start_delay))
        else:
            self.status = "ok" if self.accepted else "waiting"

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self._save()

    async def _save(self) -> None:
        await self.store.async_save(
            {"collected": self.collected, "accepted": self.accepted, "fits": self.fits, "runs": self.runs[-30:], "mode": self.mode,
             "calib_version": CALIB_VERSION, "slots": self.slots, "daily": self.daily}
        )

    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def _on_start_delay(self, _now: datetime) -> None:
        await self.async_calibrate()

    async def _on_night(self, _now: datetime) -> None:
        await self.async_calibrate()

    async def _on_hour(self, _now: datetime) -> None:
        await self.async_collect()
        await self._save()

    async def async_set_mode(self, mode: str) -> None:
        self.mode = mode
        await self._save()
        self._notify()

    # ------------------------------------------------------------ opsamling
    async def async_collect(self) -> None:
        """Opsummér kompressor, varmt vand, afrimning og kurve time for time.

        Henter fra HA's historik, så huller efter en genstart fyldes op igen
        (historikken gemmes typisk 10–14 dage).
        """
        now = time.time()
        end = agg.hour_start(now)
        done = [int(k) for k in self.collected]
        start = max(done) + agg.HOUR if done else end - BACKFILL_DAYS * 86400
        start = max(start, end - BACKFILL_DAYS * 86400)
        if start >= end:
            return
        rows = await get_instance(self.hass).async_add_executor_job(self._collect_sync, start, end)
        self.collected.update({str(k): v for k, v in rows.items()})
        cutoff = now - COLLECT_KEEP_DAYS * 86400
        self.collected = {k: v for k, v in self.collected.items() if int(k) >= cutoff}

    def _collect_sync(self, start: float, end: float) -> dict[int, dict[str, float]]:
        a, b = dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end)
        out: dict[int, dict[str, float]] = {h: {} for h in range(int(start), int(end), agg.HOUR)}

        def changes(eid: str) -> list[State]:
            # Alle opdateringer (også når kun en attribut ændrer sig, fx setpunktet)
            res = history.get_significant_states(
                self.hass, a, b, [eid], include_start_time_state=True, significant_changes_only=False
            )
            return [s for s in res.get(eid, []) if isinstance(s, State)]

        for key, short in ((CONF_COMPRESSOR, "comp"), (CONF_DHW, "dhw"), (CONF_DEFROST, "df")):
            eid = self._id(key)
            if not eid:
                continue
            ch = [(s.last_changed.timestamp(), s.state == "on") for s in changes(eid) if s.state not in ("unknown", "unavailable")]
            for h, (mins, starts) in agg.on_minutes(ch, start, end).items():
                out[h][short] = mins
                out[h][short + "_n"] = starts

        def attr_series(eid: str, attr: str | None, zero: float, off_is_none: bool) -> list[tuple[float, float | None]]:
            pts = []
            for s in changes(eid):
                if off_is_none and s.state == "off":
                    v = None
                else:
                    v = _num(s.attributes.get(attr)) if attr else _num(s.state)
                pts.append((s.last_updated.timestamp(), None if v is None else v - zero))
            return pts

        if eid := self._id(CONF_SETPOINT):
            for h, v in agg.step_mean(attr_series(eid, self.conf.get(CONF_SETPOINT_ATTR) or None, 0.0, False), start, end).items():
                if h in out:
                    out[h]["sp"] = v
        if eid := self._id(CONF_OFFSET):
            zero = float(self.conf.get(CONF_OFFSET_ZERO) or 0)
            for h, v in agg.step_mean(attr_series(eid, self.conf.get(CONF_OFFSET_ATTR) or None, zero, True), start, end).items():
                if h in out:
                    out[h]["off"] = v
        return out

    # ------------------------------------------------------------ kalibrering
    async def async_calibrate(self) -> None:
        try:
            await self.async_collect()
            hours = await get_instance(self.hass).async_add_executor_job(self._hours_sync)
            self.last_hours = hours
            fits = await self.hass.async_add_executor_job(calibrate, hours)
            steady, loss = await get_instance(self.hass).async_add_executor_job(self._steady_sync)
            fits["cop_steady"], fits["cop_loss"] = steady, loss
        except Exception as err:  # noqa: BLE001 – vis fejlen i status i stedet for at stoppe
            _LOGGER.exception("Kalibrering fejlede")
            self.status, self.status_note = "error", str(err)
            self._notify()
            return
        now = time.time()
        self.fits = {k: f.as_dict() for k, f in fits.items()}
        # COP pr. døgn: i går og de seneste 7 hele døgn (lokal tid)
        day_of = lambda ts: dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()  # noqa: E731
        today = dt_util.now().date()
        yday = [(today - timedelta(days=1)).isoformat()]
        week = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
        self.daily = {
            "yesterday": daily_cop(hours, day_of, fits.get("cop_steady"), yday),
            "week": daily_cop(hours, day_of, fits.get("cop_steady"), week),
            "days": [daily_cop(hours, day_of, fits.get("cop_steady"), [(today - timedelta(days=i)).isoformat()]) for i in range(1, 15)],
        }
        changed = []
        for key in FIT_KEYS:
            self._accept(key, fits[key], now)
            if self.accepted.get(key, {}).get("guard") not in (None, "ok", "ny", "beholdt (ingen ny værdi)"):
                changed.append(key)
        n_ok = sum(1 for f in fits.values() if f.source == "fit")
        self.status = "frozen" if self.mode == MODE_FROZEN else ("ok" if n_ok == len(fits) else "partial" if n_ok else "insufficient")
        self.status_note = f"{n_ok} af {len(fits)} tal kunne regnes ud af {len(hours)} timer"
        self.runs.append({"at": now, "hours": len(hours), "fitted": n_ok, "limited": changed})
        await self._save()
        self._notify()

    def _accept(self, key: str, fit: Fit, now: float) -> None:
        prev = self.accepted.get(key)
        if self.mode == MODE_FROZEN and prev:
            return
        prev_v = prev.get("value") if prev else None
        prev_rng = (prev.get("t_out_min"), prev.get("t_out_max")) if prev else (None, None)
        value, note = guard(fit.value, prev_v, (fit.t_out_min, fit.t_out_max), prev_rng)
        if value is None:
            return
        lo = min(x for x in (fit.t_out_min, prev_rng[0]) if x is not None) if (fit.t_out_min is not None or prev_rng[0] is not None) else None
        hi = max(x for x in (fit.t_out_max, prev_rng[1]) if x is not None) if (fit.t_out_max is not None or prev_rng[1] is not None) else None
        self.accepted[key] = {
            "value": round(value, 4),
            "fit_value": fit.value,
            "guard": note,
            "source": fit.source if fit.value is not None else "beholdt",
            "n": fit.n,
            "r2": None if fit.r2 is None else round(fit.r2, 3),
            "t_out_min": lo,
            "t_out_max": hi,
            "fitted_at": now if fit.value is not None else (prev or {}).get("fitted_at"),
            "extra": fit.extra or (prev or {}).get("extra", {}),
            "note": fit.note,
        }

    def _hours_sync(self) -> list[Hour]:
        days = int(self.conf.get(CONF_WINDOW_DAYS) or DEFAULT_WINDOW_DAYS)
        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        ids = {k: self._id(k) for k in (CONF_HEAT, CONF_ELECTRICITY, CONF_OUTDOOR, CONF_FLOW, CONF_RETURN)}
        indoor = list(self.conf.get(CONF_INDOOR) or [])
        want = {v for v in ids.values() if v} | set(indoor)
        stats = statistics_during_period(
            self.hass, start, end, want, "hour", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"}
        )
        rows: dict[int, dict[str, Any]] = {}
        for sid, lst in stats.items():
            for x in lst:
                rows.setdefault(int(_ts(x["start"])), {})[sid] = x
        hours: list[Hour] = []
        for ts in sorted(rows):
            r = rows[ts]

            def g(key: str, field: str) -> float | None:
                eid = ids.get(key)
                return _num((r.get(eid) or {}).get(field)) if eid else None

            tin = [v for v in (_num((r.get(i) or {}).get("mean")) for i in indoor) if v is not None]
            c = self.collected.get(str(ts), {})
            hours.append(
                Hour(
                    ts=ts,
                    q=g(CONF_HEAT, "change"),
                    e=g(CONF_ELECTRICITY, "change"),
                    t_out=g(CONF_OUTDOOR, "mean"),
                    t_in=sum(tin) / len(tin) if tin and len(tin) >= max(1, len(indoor) // 2) else None,
                    t_flow=g(CONF_FLOW, "mean"),
                    t_ret=g(CONF_RETURN, "mean"),
                    comp=c.get("comp"),
                    dhw=c.get("dhw"),
                    defrost=c.get("df_n"),
                    setpoint=c.get("sp"),
                    offset=c.get("off"),
                )
            )
        # Målefejl (spring i måleren) sorteres fra
        return [h for h in hours if not ((h.q or 0) > 15 or (h.e or 0) > 8 or (h.q or 0) < -0.5)]

    # ------------------------------------------------------------ COP i stabil drift
    def _steady_sync(self) -> tuple[Fit, Fit]:
        """5-minutters portioner fra de seneste 14 dage lægges til de gemte; COP regnes på alle."""
        heat, el, out, flow = (self._id(k) for k in (CONF_HEAT, CONF_ELECTRICITY, CONF_OUTDOOR, CONF_FLOW))
        comp_id = self._id(CONF_COMPRESSOR)
        if not (heat and el and out and flow and comp_id):
            note = "kræver varme, el, ude, fremløb og kompressor"
            return Fit(None, 0, note=note), Fit(None, 0, note=note)
        end = dt_util.utcnow()
        start = end - timedelta(days=BACKFILL_DAYS)
        stats = statistics_during_period(
            self.hass, start, end, {heat, el, out, flow}, "5minute", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"}
        )
        five: dict[int, dict[str, float | None]] = {}
        for sid, key, field in ((heat, "q", "change"), (el, "e", "change"), (out, "t_out", "mean"), (flow, "t_flow", "mean")):
            for x in stats.get(sid, []):
                five.setdefault(int(_ts(x["start"])), {})[key] = _num(x.get(field))

        def ivs(key: str) -> list[tuple[float, float]]:
            eid = self._id(key)
            if not eid:
                return []
            res = history.get_significant_states(self.hass, start, end, [eid], include_start_time_state=True, significant_changes_only=False)
            ch = [(s.last_changed.timestamp(), s.state == "on") for s in res.get(eid, []) if isinstance(s, State) and s.state not in ("unknown", "unavailable")]
            return agg.intervals(ch, end.timestamp())

        comp, defrost, dhw = ivs(CONF_COMPRESSOR), ivs(CONF_DEFROST), ivs(CONF_DHW)
        for sl in steady_slots(five, comp, defrost, dhw):
            self.slots[str(sl.ts)] = [round(sl.q, 4), round(sl.e, 4), round(sl.t_out, 2), round(sl.t_flow, 2), sl.run_min]
        cutoff = end.timestamp() - COLLECT_KEEP_DAYS * 86400
        self.slots = {k: v for k, v in self.slots.items() if int(k) >= cutoff}
        all_slots = [Slot(int(k), *v) for k, v in self.slots.items()]
        steady = fit_cop_steady(all_slots)
        return steady, fit_cop_loss(steady, five, comp, dhw)
