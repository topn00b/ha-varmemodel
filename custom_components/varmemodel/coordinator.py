"""Opsamling og natlig kalibrering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from math import cos, radians, sin
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
from .calibration import Fit, Hour, RoomHour, Slot, calibrate, daily_cop, detect_airing, fit_room, sun_factor, fit_cop_loss, fit_cop_steady, guard, steady_slots
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
    CONF_WEATHER,
    CONF_ROOM_ALWAYS_OPEN,
    CONF_ROOM_IN_PLAN,
    CONF_ROOM_MIN,
    CONF_ROOM_MIN_ENTITY,
    CONF_ROOM_NAME,
    CONF_ROOM_TEMP,
    CONF_ROOM_VALVE,
    CONF_ROOM_WEIGHT,
    CONF_WINDOW_DAYS,
    SUBENTRY_ROOM,
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
        self.rooms: dict[str, dict[str, Any]] = {}
        self.room_heat: dict[str, dict[str, Any]] = {}
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

    @property
    def rooms_cfg(self) -> list[dict[str, Any]]:
        """Rummene: underpunkter af typen rum, ellers rumtemperaturerne fra opsætningen."""
        subs = [s for s in self.entry.subentries.values() if s.subentry_type == SUBENTRY_ROOM]
        if subs:
            return [
                {"key": s.data[CONF_ROOM_TEMP], "name": s.data.get(CONF_ROOM_NAME, s.title), "valve": s.data.get(CONF_ROOM_VALVE),
                 "always_open": bool(s.data.get(CONF_ROOM_ALWAYS_OPEN)), "weight": float(s.data.get(CONF_ROOM_WEIGHT) or 1.0),
                 "min_entity": s.data.get(CONF_ROOM_MIN_ENTITY), "min": s.data.get(CONF_ROOM_MIN),
                 "in_plan": s.data.get(CONF_ROOM_IN_PLAN, True), "subentry_id": sid}
                for sid, s in self.entry.subentries.items() if s.subentry_type == SUBENTRY_ROOM
            ]
        return [{"key": e, "name": None, "valve": None, "always_open": False, "weight": 1.0, "min_entity": None, "min": None,
                 "in_plan": True, "subentry_id": None} for e in (self.conf.get(CONF_INDOOR) or [])]

    def _room_temps(self) -> list[str]:
        return list(dict.fromkeys(list(self.conf.get(CONF_INDOOR) or []) + [r["key"] for r in self.rooms_cfg]))

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
        self.rooms = data.get("rooms", {})
        self.room_heat = data.get("room_heat", {})
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

    def _data(self) -> dict[str, Any]:
        return (
            {"collected": self.collected, "accepted": self.accepted, "fits": self.fits, "runs": self.runs[-30:], "mode": self.mode,
             "calib_version": CALIB_VERSION, "slots": self.slots, "daily": self.daily, "rooms": self.rooms, "room_heat": self.room_heat}
        )

    async def _save(self) -> None:
        await self.store.async_save(self._data())

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
        self.store.async_delay_save(self._data, 300)

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
        # Nye datakilder (vejr, udluftningstjek) hentes ind for de timer, der mangler dem
        need = (["ws"] if self._id(CONF_WEATHER) else []) + (["airchk"] if self._room_temps() else []) + \
            (["vchk"] if any(r["valve"] for r in self.rooms_cfg) else [])
        missing = [int(k) for k, v in self.collected.items()
                   if any(n not in v for n in need) and int(k) >= end - BACKFILL_DAYS * 86400]
        if missing:
            start = min(start, min(missing))
        start = max(start, end - BACKFILL_DAYS * 86400)
        if start >= end:
            return
        rows = await get_instance(self.hass).async_add_executor_job(self._collect_sync, start, end)
        for k, v in rows.items():
            self.collected.setdefault(str(k), {}).update(v)
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

        if eid := self._id(CONF_WEATHER):
            ws, wu, wv, cc, uv, rh = [], [], [], [], [], []
            for st in changes(eid):
                wa = st.attributes
                t = st.last_updated.timestamp()
                speed = _num(wa.get("wind_speed"))
                if speed is not None:
                    unit = str(wa.get("wind_speed_unit") or "km/h")
                    speed = speed / 3.6 if unit == "km/h" else speed * 0.44704 if unit == "mph" else speed / 1.944 if unit == "kn" else speed
                bearing = _num(wa.get("wind_bearing"))
                ws.append((t, speed))
                # Vinden kommer FRA bearing; vektoren peger mod hvor den kommer fra
                wu.append((t, None if speed is None or bearing is None else speed * sin(radians(bearing))))
                wv.append((t, None if speed is None or bearing is None else speed * cos(radians(bearing))))
                cc.append((t, _num(wa.get("cloud_coverage"))))
                uv.append((t, _num(wa.get("uv_index"))))
                rh.append((t, _num(wa.get("humidity"))))
            for key, series in (("ws", ws), ("wu", wu), ("wv", wv), ("cc", cc), ("uv", uv), ("rh", rh)):
                for h, v in agg.step_mean(series, start, end).items():
                    if h in out:
                        out[h][key] = v

        # Udluftning: et rum faldt mindst 0,3 °C på et kvarter (5-minutters statistik, ca. 10 dage tilbage)
        indoor = self._room_temps()
        if indoor:
            five = statistics_during_period(self.hass, a - timedelta(hours=1), b, set(indoor), "5minute", {"temperature": "°C"}, {"mean"})
            for h in out:
                out[h]["airchk"] = 1
            for eid in indoor:
                pts = [(int(_ts(x["start"])), _num(x.get("mean"))) for x in five.get(eid, [])]
                for h in detect_airing([(t, v) for t, v in pts if v is not None]):
                    if h in out:
                        out[h].setdefault("air", [])
                        if eid not in out[h]["air"]:
                            out[h]["air"].append(eid)

        # Telestater/ventiler: minutter åben mens kompressoren kørte (uden kompressor: minutter åben)
        valves = [r for r in self.rooms_cfg if r["valve"]]
        if valves:
            comp_iv: list[tuple[float, float]] | None = None
            if cid := self._id(CONF_COMPRESSOR):
                comp_iv = agg.intervals([(st.last_changed.timestamp(), st.state == "on") for st in changes(cid)
                                         if st.state not in ("unknown", "unavailable")], end)
            for h in out:
                out[h]["vchk"] = 1
            for r in valves:
                ch = []
                for st in changes(r["valve"]):
                    if st.state in ("unknown", "unavailable"):
                        continue
                    if r["valve"].split(".", 1)[0] == "climate":
                        is_open = st.attributes.get("hvac_action") == "heating"
                    else:
                        is_open = st.state in ("on", "open", "opening")
                    ch.append((st.last_updated.timestamp(), is_open))
                v_iv = agg.intervals(ch, end)
                if comp_iv is not None:
                    v_iv = agg.intersect(v_iv, comp_iv)
                for h, mins in agg.minutes_per_hour(v_iv, start, end).items():
                    if h in out:
                        out[h].setdefault("v", {})[r["key"]] = mins

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
            room_fits = await self.hass.async_add_executor_job(self._rooms_fit, hours)
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
        for eid, f in room_fits.items():
            prev = self.rooms.get(eid)
            value, note = guard(f.value, prev.get("value") if prev else None, (f.t_out_min, f.t_out_max),
                                (prev.get("t_out_min"), prev.get("t_out_max")) if prev else (None, None))
            if self.mode == MODE_FROZEN and prev:
                continue
            self.rooms[eid] = {"value": value, "guard": note, "n": f.n, "r2": None if f.r2 is None else round(f.r2, 2),
                               "t_out_min": f.t_out_min, "t_out_max": f.t_out_max, "note": f.note,
                               "fitted_at": now if f.value is not None else (prev or {}).get("fitted_at"),
                               "extra": (prev or {}).get("extra", {}) if note.startswith("begrænset") or f.value is None else f.extra}
        self.room_heat = self._room_heat(hours)
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
            "extra": (prev or {}).get("extra", {}) if note.startswith("begrænset") else (fit.extra or (prev or {}).get("extra", {})),
            "note": fit.note,
        }

    def _hours_sync(self) -> list[Hour]:
        days = int(self.conf.get(CONF_WINDOW_DAYS) or DEFAULT_WINDOW_DAYS)
        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        ids = {k: self._id(k) for k in (CONF_HEAT, CONF_ELECTRICITY, CONF_OUTDOOR, CONF_FLOW, CONF_RETURN)}
        indoor = list(self.conf.get(CONF_INDOOR) or []) or self._room_temps()
        want = {v for v in ids.values() if v} | set(self._room_temps())
        stats = statistics_during_period(
            self.hass, start, end, want, "hour", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"}
        )
        rows: dict[int, dict[str, Any]] = {}
        for sid, lst in stats.items():
            for x in lst:
                rows.setdefault(int(_ts(x["start"])), {})[sid] = x
        hours: list[Hour] = []
        self._room_t: dict[int, dict[str, float | None]] = {}
        for ts in sorted(rows):
            r = rows[ts]

            def g(key: str, field: str) -> float | None:
                eid = ids.get(key)
                return _num((r.get(eid) or {}).get(field)) if eid else None

            tin = [v for v in (_num((r.get(i) or {}).get("mean")) for i in indoor) if v is not None]
            self._room_t[ts] = {i: _num((r.get(i) or {}).get("mean")) for i in self._room_temps()}
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
                    wind=c.get("ws"),
                    wind_u=c.get("wu"),
                    wind_v=c.get("wv"),
                    cloud=c.get("cc"),
                    uv=c.get("uv"),
                    humidity=c.get("rh"),
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

    # ------------------------------------------------------------ rum
    def _rooms_fit(self, hours: list[Hour]) -> dict[str, Fit]:
        """Én model pr. rum. Timer med udluftning i rummet udelades."""
        out: dict[str, Fit] = {}
        room_t = getattr(self, "_room_t", {})
        for eid in [r["key"] for r in self.rooms_cfg]:
            rh = []
            for h in hours:
                c = self.collected.get(str(h.ts), {})
                rh.append(RoomHour(ts=h.ts, t_room=(room_t.get(h.ts) or {}).get(eid), t_out=h.t_out, q=h.q,
                                   sun=sun_factor(h), aired=eid in (c.get("air") or [])))
            out[eid] = fit_room(rh)
        return out

    def _room_heat(self, hours: list[Hour]) -> dict[str, dict[str, Any]]:
        """Varme fordelt på rummene time for time efter (kredsstørrelse × minutter åben mens kompressoren kørte).
        Altid åbne kredse tæller med alle kompressorminutter. Gælder kun rum med telestat eller altid åben kreds."""
        rooms = [r for r in self.rooms_cfg if r["valve"] or r["always_open"]]
        if not rooms:
            return {}
        day_of = lambda ts: dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()  # noqa: E731
        today = dt_util.now().date()
        days7 = {(today - timedelta(days=i)).isoformat() for i in range(1, 8)}
        yday = (today - timedelta(days=1)).isoformat()
        acc = {r["key"]: {"week": 0.0, "yday": 0.0, "open_h": 0.0, "drops": 0, "days": set()} for r in rooms}
        valve_seen = False
        for h in hours:
            c = self.collected.get(str(h.ts), {})
            if "vchk" not in c and any(r["valve"] for r in rooms):
                continue
            valve_seen = True
            d = day_of(h.ts)
            for r in rooms:
                if d in days7:
                    acc[r["key"]]["days"].add(d)
            if d not in days7 or not h.q or h.q <= 0 or (h.dhw or 0) > 0:
                continue
            comp = c.get("comp", 60.0)
            w = {r["key"]: r["weight"] * (comp if r["always_open"] else (c.get("v") or {}).get(r["key"], 0.0)) for r in rooms}
            tot = sum(w.values())
            for k, v in w.items():
                share = h.q * v / tot if tot > 0 else 0.0
                acc[k]["week"] += share
                acc[k]["open_h"] += (v / next(r["weight"] for r in rooms if r["key"] == k)) / 60
                if d == yday:
                    acc[k]["yday"] += share
        # brat afkøling (døre, udluftning) pr. døgn ud fra opsamlerens markeringer
        for k, v in self.collected.items():
            d = day_of(int(k))
            if d not in days7:
                continue
            for eid in v.get("air") or []:
                prev = self.collected.get(str(int(k) - 3600), {})
                if eid in acc and eid not in (prev.get("air") or []):
                    acc[eid]["drops"] += 1
        tot_week = sum(a["week"] for a in acc.values())
        return {
            k: {"kwh_per_day": round(a["week"] / max(1, len(a["days"])), 2), "yesterday_kwh": round(a["yday"], 2),
                "share_pct": round(a["week"] / tot_week * 100, 1) if tot_week > 0 else None,
                "open_hours_per_day": round(a["open_h"] / max(1, len(a["days"])), 1),
                "sudden_drops_per_day": round(a["drops"] / max(1, len(a["days"])), 1), "days": len(a["days"]),
                "valve_data": valve_seen}
            for k, a in acc.items()
        }
