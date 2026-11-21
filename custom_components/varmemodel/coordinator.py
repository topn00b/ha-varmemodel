"""Huset: opsamling og natlig kalibrering for alle varmekilder og rum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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
from .calibration import (
    Fit,
    Hour,
    RoomHour,
    Slot,
    calibrate,
    daily_cop,
    daily_el,
    detect_airing,
    fit_cop_loss,
    fit_cop_steady,
    fit_room,
    guard,
    steady_slots,
    sun_factor,
)
from .const import (
    BACKFILL_DAYS,
    CALIB_VERSION,
    CALIBRATION_HOUR,
    CALIBRATION_MINUTE,
    COLLECT_KEEP_DAYS,
    CONF_COMPRESSOR,
    CONF_DEFROST,
    CONF_DHW,
    CONF_ELECTRICITY,
    CONF_ELECTRICITY_BACKUP,
    CONF_FLOW,
    CONF_HEAT,
    CONF_INDOOR,
    CONF_LEGACY_IDS,
    CONF_MIGRATED_SOURCE,
    CONF_OFFSET,
    CONF_OFFSET_ATTR,
    CONF_OFFSET_ZERO,
    CONF_OUTDOOR,
    CONF_RETURN,
    CONF_ROOM_ALWAYS_OPEN,
    CONF_ROOM_IN_PLAN,
    CONF_ROOM_MIN,
    CONF_ROOM_MIN_ENTITY,
    CONF_ROOM_NAME,
    CONF_ROOM_NEIGHBOURS,
    CONF_ROOM_SOURCE,
    CONF_ROOM_TEMP,
    CONF_ROOM_VALVE,
    CONF_ROOM_WEIGHT,
    CONF_SETPOINT,
    CONF_SETPOINT_ATTR,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_UNIT_TEMP,
    CONF_UNIT_TEMP_ATTR,
    CONF_WEATHER,
    CONF_WINDOW_DAYS,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
    FIT_KEYS,
    MODE_CALIBRATED,
    MODE_FROZEN,
    SOURCE_AIR_AIR,
    SOURCE_HYDRONIC,
    SUBENTRY_ROOM,
    SUBENTRY_SOURCE,
)

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
# Opsamlede felter der hører til en bestemt varmekilde (resten hører til huset)
SOURCE_FIELDS = ("comp", "comp_n", "dhw", "dhw_n", "df", "df_n", "sp", "off", "ut")


def _ts(v: Any) -> float:
    return v.timestamp() if isinstance(v, datetime) else float(v)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


@dataclass
class SourceState:
    """Alt der hører til én varmekilde."""

    sid: str
    name: str
    kind: str
    conf: dict[str, Any]
    legacy_ids: bool = False
    collected: dict[str, dict[str, float]] = field(default_factory=dict)
    accepted: dict[str, dict[str, Any]] = field(default_factory=dict)
    fits: dict[str, dict[str, Any]] = field(default_factory=dict)
    slots: dict[str, list[float]] = field(default_factory=dict)
    daily: dict[str, Any] = field(default_factory=dict)
    last_hours: list[Hour] = field(default_factory=list)
    el_backup: dict[str, Any] = field(default_factory=dict)  # faktor og timer for den supplerende elmåler

    def eid(self, key: str) -> str | None:
        return self.conf.get(key) or None

    def as_store(self) -> dict[str, Any]:
        return {"collected": self.collected, "accepted": self.accepted, "fits": self.fits, "slots": self.slots, "daily": self.daily}

    def load(self, d: dict[str, Any]) -> None:
        self.collected = d.get("collected", {})
        self.accepted = d.get("accepted", {})
        self.fits = d.get("fits", {})
        self.slots = d.get("slots", {})
        self.daily = d.get("daily", {})


class VarmemodelCoordinator:
    """Huset: varmekilder, rum, opsamlede timer og de godkendte tal."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.collected: dict[str, dict[str, Any]] = {}  # husets timer: vejr, udluftning, telestater
        self.sources: dict[str, SourceState] = {}
        self.runs: list[dict[str, Any]] = []
        self.rooms: dict[str, dict[str, Any]] = {}
        self.room_heat: dict[str, dict[str, Any]] = {}
        self.mode: str = MODE_CALIBRATED
        self.status: str = "waiting"
        self.status_note: str = ""
        self._room_t: dict[int, dict[str, float | None]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._build_sources()

    # ------------------------------------------------------------ konfiguration
    @property
    def conf(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    def _id(self, key: str) -> str | None:
        return self.conf.get(key) or None

    def _build_sources(self) -> None:
        for sid, s in self.entry.subentries.items():
            if s.subentry_type != SUBENTRY_SOURCE:
                continue
            self.sources[sid] = SourceState(
                sid=sid, name=s.data.get(CONF_SOURCE_NAME, s.title), kind=s.data.get(CONF_SOURCE_TYPE, SOURCE_HYDRONIC),
                conf=dict(s.data), legacy_ids=bool(s.data.get(CONF_LEGACY_IDS)),
            )

    @property
    def hydronic(self) -> SourceState | None:
        """Den vandbårne varmekilde, som telestaterne hører til (den første)."""
        return next((s for s in self.sources.values() if s.kind == SOURCE_HYDRONIC), None)

    @property
    def rooms_cfg(self) -> list[dict[str, Any]]:
        """Rummene: underpunkter af typen rum, ellers rumtemperaturerne fra opsætningen."""
        subs = [(sid, s) for sid, s in self.entry.subentries.items() if s.subentry_type == SUBENTRY_ROOM]
        if subs:
            return [
                {"key": s.data[CONF_ROOM_TEMP], "name": s.data.get(CONF_ROOM_NAME, s.title), "valve": s.data.get(CONF_ROOM_VALVE),
                 "always_open": bool(s.data.get(CONF_ROOM_ALWAYS_OPEN)), "weight": float(s.data.get(CONF_ROOM_WEIGHT) or 1.0),
                 "min_entity": s.data.get(CONF_ROOM_MIN_ENTITY), "min": s.data.get(CONF_ROOM_MIN),
                 "in_plan": s.data.get(CONF_ROOM_IN_PLAN, True), "neighbours": list(s.data.get(CONF_ROOM_NEIGHBOURS) or []),
                 "source": s.data.get(CONF_ROOM_SOURCE), "subentry_id": sid}
                for sid, s in subs
            ]
        return [{"key": e, "name": None, "valve": None, "always_open": False, "weight": 1.0, "min_entity": None, "min": None,
                 "in_plan": True, "neighbours": [], "source": None, "subentry_id": None} for e in (self.conf.get(CONF_INDOOR) or [])]

    def room_source(self, room: dict[str, Any]) -> SourceState | None:
        """Varmekilden som rummets termostat/telestat styrer: den valgte, ellers den første vandbårne
        for rum med telestat eller altid åben kreds."""
        if room.get("source") in self.sources:
            return self.sources[room["source"]]
        if room.get("valve") or room.get("always_open"):
            return self.hydronic
        return None

    def _room_temps(self) -> list[str]:
        return list(dict.fromkeys(list(self.conf.get(CONF_INDOOR) or []) + [r["key"] for r in self.rooms_cfg]))

    def _neighbour_temps(self) -> list[str]:
        return list(dict.fromkeys(n for r in self.rooms_cfg for n in r.get("neighbours") or []))

    def _indoor_for_mean(self) -> list[str]:
        """Husets indetemperatur: de valgte rumtemperaturer, ellers rummene der tæller med i planen."""
        return list(self.conf.get(CONF_INDOOR) or []) or [r["key"] for r in self.rooms_cfg if r["in_plan"]]

    # ------------------------------------------------------------ livscyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        if "sources" not in data and data.get("collected") is not None:
            data = self._migrate_store_v1(data)
        self.collected = data.get("collected", {})
        self.runs = data.get("runs", [])
        self.mode = data.get("mode", MODE_CALIBRATED)
        self.rooms = data.get("rooms", {})
        self.room_heat = data.get("room_heat", {})
        for sid, s in self.sources.items():
            s.load((data.get("sources") or {}).get(sid, {}))
        if data and data.get("calib_version") != CALIB_VERSION:
            # Ny regnemetode: tallene regnes forfra (opsamlede timer beholdes)
            for s in self.sources.values():
                s.accepted, s.fits = {}, {}
            self.runs = []
        self._unsubs.append(async_track_time_change(self.hass, self._on_hour, minute=0, second=30))
        self._unsubs.append(
            async_track_time_change(self.hass, self._on_night, hour=CALIBRATION_HOUR, minute=CALIBRATION_MINUTE, second=0)
        )
        last = self.runs[-1]["at"] if self.runs else 0
        if time.time() - last > 86400:
            self._unsubs.append(async_call_later(self.hass, 120, self._on_start_delay))
        else:
            self.status = "ok" if any(s.accepted for s in self.sources.values()) else "waiting"

    def _migrate_store_v1(self, data: dict[str, Any]) -> dict[str, Any]:
        """Version 1 havde én varmepumpe: dens opsamlede felter og tal flyttes til den overførte varmekilde."""
        sid = self.conf.get(CONF_MIGRATED_SOURCE)
        house: dict[str, dict[str, Any]] = {}
        src: dict[str, dict[str, Any]] = {}
        for h, v in (data.get("collected") or {}).items():
            house[h] = {k: x for k, x in v.items() if k not in SOURCE_FIELDS}
            src[h] = {k: x for k, x in v.items() if k in SOURCE_FIELDS}
        new = {k: data.get(k) for k in ("runs", "mode", "calib_version", "rooms", "room_heat") if k in data}
        new["collected"] = house
        new["sources"] = {sid: {"collected": src, "accepted": data.get("accepted", {}), "fits": data.get("fits", {}),
                                "slots": data.get("slots", {}), "daily": data.get("daily", {})}} if sid else {}
        return new

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self._save()

    def _data(self) -> dict[str, Any]:
        return {"collected": self.collected, "sources": {sid: s.as_store() for sid, s in self.sources.items()},
                "runs": self.runs[-30:], "mode": self.mode, "calib_version": CALIB_VERSION,
                "rooms": self.rooms, "room_heat": self.room_heat}

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
        """Opsummér time for time: varmekildernes kompressor, varmt vand, afrimning, kurve og
        enhedstemperatur samt husets vejr, udluftning og telestater. Hentes fra HA's historik,
        så huller efter en genstart fyldes op igen (historikken gemmes typisk 10–14 dage)."""
        now = time.time()
        end = agg.hour_start(now)
        earliest = end - BACKFILL_DAYS * 86400
        starts = []
        for s in self.sources.values():
            done = [int(k) for k in s.collected]
            starts.append(max(done) + agg.HOUR if done else earliest)
        done = [int(k) for k in self.collected]
        starts.append(max(done) + agg.HOUR if done else earliest)
        need = (["ws"] if self._id(CONF_WEATHER) else []) + (["airchk"] if self._room_temps() else []) + \
            (["vchk"] if any(r["valve"] for r in self.rooms_cfg) else [])
        missing = [int(k) for k, v in self.collected.items() if any(n not in v for n in need) and int(k) >= earliest]
        start = max(min(starts + ([min(missing)] if missing else [])), earliest)
        if start >= end:
            return
        house, per_source = await get_instance(self.hass).async_add_executor_job(self._collect_sync, start, end)
        for k, v in house.items():
            self.collected.setdefault(str(k), {}).update(v)
        for sid, rows in per_source.items():
            s = self.sources[sid]
            for k, v in rows.items():
                s.collected.setdefault(str(k), {}).update(v)
        cutoff = now - COLLECT_KEEP_DAYS * 86400
        self.collected = {k: v for k, v in self.collected.items() if int(k) >= cutoff}
        for s in self.sources.values():
            s.collected = {k: v for k, v in s.collected.items() if int(k) >= cutoff}

    def _collect_sync(self, start: float, end: float) -> tuple[dict[int, dict[str, Any]], dict[str, dict[int, dict[str, Any]]]]:
        a, b = dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end)
        hours = range(int(start), int(end), agg.HOUR)
        house: dict[int, dict[str, Any]] = {h: {} for h in hours}
        per_source: dict[str, dict[int, dict[str, Any]]] = {sid: {h: {} for h in hours} for sid in self.sources}

        def changes(eid: str) -> list[State]:
            # Alle opdateringer (også når kun en attribut ændrer sig, fx setpunktet)
            res = history.get_significant_states(self.hass, a, b, [eid], include_start_time_state=True, significant_changes_only=False)
            return [s for s in res.get(eid, []) if isinstance(s, State)]

        def onoff(eid: str) -> list[tuple[float, bool]]:
            return [(s.last_changed.timestamp(), s.state == "on") for s in changes(eid) if s.state not in ("unknown", "unavailable")]

        def attr_series(eid: str, attr: str | None, zero: float, off_is_none: bool) -> list[tuple[float, float | None]]:
            pts = []
            for s in changes(eid):
                v = None if off_is_none and s.state == "off" else (_num(s.attributes.get(attr)) if attr else _num(s.state))
                pts.append((s.last_updated.timestamp(), None if v is None else v - zero))
            return pts

        # ---- varmekilderne
        comp_iv_hyd: list[tuple[float, float]] | None = None
        hyd = self.hydronic
        for sid, src in self.sources.items():
            out = per_source[sid]
            for key, short in ((CONF_COMPRESSOR, "comp"), (CONF_DHW, "dhw"), (CONF_DEFROST, "df")):
                if eid := src.eid(key):
                    ch = onoff(eid)
                    for h, (mins, n) in agg.on_minutes(ch, start, end).items():
                        out[h][short] = mins
                        out[h][short + "_n"] = n
                    if key == CONF_COMPRESSOR and hyd is not None and sid == hyd.sid:
                        comp_iv_hyd = agg.intervals(ch, end)
            if eid := src.eid(CONF_SETPOINT):
                for h, v in agg.step_mean(attr_series(eid, src.conf.get(CONF_SETPOINT_ATTR) or None, 0.0, False), start, end).items():
                    out[h]["sp"] = v
            if eid := src.eid(CONF_OFFSET):
                zero = float(src.conf.get(CONF_OFFSET_ZERO) or 0)
                for h, v in agg.step_mean(attr_series(eid, src.conf.get(CONF_OFFSET_ATTR) or None, zero, True), start, end).items():
                    out[h]["off"] = v
            if eid := src.eid(CONF_UNIT_TEMP):
                for h, v in agg.step_mean(attr_series(eid, src.conf.get(CONF_UNIT_TEMP_ATTR) or None, 0.0, False), start, end).items():
                    out[h]["ut"] = v

        # ---- vejr
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
                    if h in house:
                        house[h][key] = v

        # ---- udluftning: et rum faldt mindst 0,3 °C på et kvarter (5-minutters statistik, ca. 10 dage tilbage)
        indoor = self._room_temps()
        if indoor:
            five = statistics_during_period(self.hass, a - timedelta(hours=1), b, set(indoor), "5minute", {"temperature": "°C"}, {"mean"})
            for h in house:
                house[h]["airchk"] = 1
            for eid in indoor:
                pts = [(int(_ts(x["start"])), _num(x.get("mean"))) for x in five.get(eid, [])]
                for h in detect_airing([(t, v) for t, v in pts if v is not None]):
                    if h in house:
                        house[h].setdefault("air", [])
                        if eid not in house[h]["air"]:
                            house[h]["air"].append(eid)

        # ---- telestater: minutter åben mens den vandbårne varmepumpes kompressor kørte
        valves = [r for r in self.rooms_cfg if r["valve"]]
        if valves:
            for h in house:
                house[h]["vchk"] = 1
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
                if comp_iv_hyd is not None:
                    v_iv = agg.intersect(v_iv, comp_iv_hyd)
                for h, mins in agg.minutes_per_hour(v_iv, start, end).items():
                    if h in house:
                        house[h].setdefault("v", {})[r["key"]] = mins
        return house, per_source

    # ------------------------------------------------------------ kalibrering
    async def async_calibrate(self) -> None:
        try:
            await self.async_collect()
            hours_by_source = await get_instance(self.hass).async_add_executor_job(self._hours_sync)
            results: dict[str, dict[str, Fit]] = {}
            for sid, s in self.sources.items():
                hours = hours_by_source.get(sid, [])
                s.last_hours = hours
                if s.kind == SOURCE_HYDRONIC:
                    fits = await self.hass.async_add_executor_job(calibrate, hours)
                    steady, loss = await get_instance(self.hass).async_add_executor_job(self._steady_sync, s)
                    fits["cop_steady"], fits["cop_loss"] = steady, loss
                else:
                    fits = {}
                results[sid] = fits
            room_fits = await self.hass.async_add_executor_job(self._rooms_fit, hours_by_source)
        except Exception as err:  # noqa: BLE001 – vis fejlen i status i stedet for at stoppe
            _LOGGER.exception("Kalibrering fejlede")
            self.status, self.status_note = "error", str(err)
            self._notify()
            return
        now = time.time()
        day_of = lambda ts: dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()  # noqa: E731
        today = dt_util.now().date()
        yday = [(today - timedelta(days=1)).isoformat()]
        week = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
        changed: list[str] = []
        n_ok = n_all = 0
        for sid, s in self.sources.items():
            fits = results[sid]
            hours = s.last_hours
            s.fits = {k: f.as_dict() for k, f in fits.items()}
            st = fits.get("cop_steady")
            if s.kind == SOURCE_HYDRONIC:
                s.daily = {
                    "yesterday": daily_cop(hours, day_of, st, yday),
                    "week": daily_cop(hours, day_of, st, week),
                    "days": [daily_cop(hours, day_of, st, [(today - timedelta(days=i)).isoformat()]) for i in range(1, 15)],
                }
            else:
                s.daily = {"yesterday": daily_el(hours, day_of, yday), "week": daily_el(hours, day_of, week)}
            for key in fits:
                self._accept(s, key, fits[key], now)
                if s.accepted.get(key, {}).get("guard") not in (None, "ok", "ny", "beholdt (ingen ny værdi)"):
                    changed.append(f"{s.name}: {key}")
            n_ok += sum(1 for f in fits.values() if f.source == "fit")
            n_all += len(fits)
        for eid, f in room_fits.items():
            prev = self.rooms.get(eid)
            if self.mode == MODE_FROZEN and prev:
                continue
            value, note = guard(f.value, prev.get("value") if prev else None, (f.t_out_min, f.t_out_max),
                                (prev.get("t_out_min"), prev.get("t_out_max")) if prev else (None, None))
            self.rooms[eid] = {"value": value, "guard": note, "n": f.n, "r2": None if f.r2 is None else round(f.r2, 2),
                               "t_out_min": f.t_out_min, "t_out_max": f.t_out_max, "note": f.note,
                               "fitted_at": now if f.value is not None else (prev or {}).get("fitted_at"),
                               "extra": (prev or {}).get("extra", {}) if note.startswith("begrænset") or f.value is None else f.extra}
        self.room_heat = self._room_heat(hours_by_source)
        n_hours = max((len(s.last_hours) for s in self.sources.values()), default=0)
        if not self.sources:
            self.status, self.status_note = "waiting", "tilføj en varmekilde"
        else:
            self.status = "frozen" if self.mode == MODE_FROZEN else ("ok" if n_ok == n_all else "partial" if n_ok else "insufficient")
            self.status_note = f"{n_ok} af {n_all} tal kunne regnes ud af {n_hours} timer"
        self.runs.append({"at": now, "hours": n_hours, "fitted": n_ok, "limited": changed})
        await self._save()
        self._notify()

    def _accept(self, s: SourceState, key: str, fit: Fit, now: float) -> None:
        prev = s.accepted.get(key)
        if self.mode == MODE_FROZEN and prev:
            return
        prev_v = prev.get("value") if prev else None
        prev_rng = (prev.get("t_out_min"), prev.get("t_out_max")) if prev else (None, None)
        value, note = guard(fit.value, prev_v, (fit.t_out_min, fit.t_out_max), prev_rng)
        if value is None:
            return
        lo = min((x for x in (fit.t_out_min, prev_rng[0]) if x is not None), default=None)
        hi = max((x for x in (fit.t_out_max, prev_rng[1]) if x is not None), default=None)
        s.accepted[key] = {
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

    def _hours_sync(self) -> dict[str, list[Hour]]:
        """Timestatistik for hver varmekilde, med husets ude-, inde- og vejrdata."""
        days = int(self.conf.get(CONF_WINDOW_DAYS) or DEFAULT_WINDOW_DAYS)
        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        outdoor = self._id(CONF_OUTDOOR)
        want = set(self._room_temps()) | set(self._neighbour_temps()) | ({outdoor} if outdoor else set())
        for s in self.sources.values():
            want |= {s.eid(k) for k in (CONF_HEAT, CONF_ELECTRICITY, CONF_ELECTRICITY_BACKUP, CONF_FLOW, CONF_RETURN)} - {None}
        stats = statistics_during_period(self.hass, start, end, want, "hour", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"})
        rows: dict[int, dict[str, Any]] = {}
        for stat_id, lst in stats.items():
            for x in lst:
                rows.setdefault(int(_ts(x["start"])), {})[stat_id] = x
        # Supplerende elmåler: faktor = hovedmåler ÷ supplerende i timer hvor begge har data
        for s in self.sources.values():
            main, bak = s.eid(CONF_ELECTRICITY), s.eid(CONF_ELECTRICITY_BACKUP)
            if not bak:
                s.el_backup = {}
                continue
            pairs = [(_num((r.get(main) or {}).get("change")), _num((r.get(bak) or {}).get("change"))) for r in rows.values()]
            both = [(m, b) for m, b in pairs if m is not None and b is not None and m >= 0 and b >= 0]
            sb = sum(b for _, b in both)
            ratio = (sum(m for m, _ in both) / sb) if len(both) >= 48 and sb > 1 else 1.0
            s.el_backup = {"factor": round(ratio, 3), "overlap_hours": len(both), "used_hours": 0}
        indoor = self._indoor_for_mean()
        self._room_t = {}
        out: dict[str, list[Hour]] = {sid: [] for sid in self.sources}
        for ts in sorted(rows):
            r = rows[ts]

            def val(eid: str | None, fld: str) -> float | None:
                return _num((r.get(eid) or {}).get(fld)) if eid else None

            tin = [v for v in (val(i, "mean") for i in indoor) if v is not None]
            self._room_t[ts] = {i: val(i, "mean") for i in set(self._room_temps()) | set(self._neighbour_temps())}
            c = self.collected.get(str(ts), {})
            for sid, s in self.sources.items():
                sc = s.collected.get(str(ts), {})
                e = val(s.eid(CONF_ELECTRICITY), "change")
                if e is None and s.el_backup:
                    b = val(s.eid(CONF_ELECTRICITY_BACKUP), "change")
                    if b is not None:
                        e = b * s.el_backup["factor"]
                        s.el_backup["used_hours"] += 1
                out[sid].append(Hour(
                    ts=ts, q=val(s.eid(CONF_HEAT), "change"), e=e, t_out=val(outdoor, "mean"),
                    t_in=sum(tin) / len(tin) if tin and len(tin) >= max(1, len(indoor) // 2) else None,
                    t_flow=val(s.eid(CONF_FLOW), "mean"), t_ret=val(s.eid(CONF_RETURN), "mean"),
                    comp=sc.get("comp"), dhw=sc.get("dhw"), defrost=sc.get("df_n"), setpoint=sc.get("sp"), offset=sc.get("off"),
                    wind=c.get("ws"), wind_u=c.get("wu"), wind_v=c.get("wv"), cloud=c.get("cc"), uv=c.get("uv"), humidity=c.get("rh"),
                ))
        # Målefejl (spring i måleren) sorteres fra
        return {sid: [h for h in hs if not ((h.q or 0) > 15 or (h.e or 0) > 8 or (h.q or 0) < -0.5)] for sid, hs in out.items()}

    # ------------------------------------------------------------ COP i stabil drift
    def _steady_sync(self, s: SourceState) -> tuple[Fit, Fit]:
        """5-minutters portioner fra de seneste 14 dage lægges til de gemte; COP regnes på alle."""
        heat, el, flow, comp_id = s.eid(CONF_HEAT), s.eid(CONF_ELECTRICITY), s.eid(CONF_FLOW), s.eid(CONF_COMPRESSOR)
        out = self._id(CONF_OUTDOOR)
        if not (heat and el and out and flow and comp_id):
            note = "kræver varme, el, ude, fremløb og kompressor"
            return Fit(None, 0, note=note), Fit(None, 0, note=note)
        end = dt_util.utcnow()
        start = end - timedelta(days=BACKFILL_DAYS)
        stats = statistics_during_period(self.hass, start, end, {heat, el, out, flow}, "5minute", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"})
        five: dict[int, dict[str, float | None]] = {}
        for stat_id, key, fld in ((heat, "q", "change"), (el, "e", "change"), (out, "t_out", "mean"), (flow, "t_flow", "mean")):
            for x in stats.get(stat_id, []):
                five.setdefault(int(_ts(x["start"])), {})[key] = _num(x.get(fld))

        def ivs(key: str) -> list[tuple[float, float]]:
            eid = s.eid(key)
            if not eid:
                return []
            res = history.get_significant_states(self.hass, start, end, [eid], include_start_time_state=True, significant_changes_only=False)
            ch = [(x.last_changed.timestamp(), x.state == "on") for x in res.get(eid, []) if isinstance(x, State) and x.state not in ("unknown", "unavailable")]
            return agg.intervals(ch, end.timestamp())

        comp, defrost, dhw = ivs(CONF_COMPRESSOR), ivs(CONF_DEFROST), ivs(CONF_DHW)
        for sl in steady_slots(five, comp, defrost, dhw):
            s.slots[str(sl.ts)] = [round(sl.q, 4), round(sl.e, 4), round(sl.t_out, 2), round(sl.t_flow, 2), sl.run_min]
        cutoff = end.timestamp() - COLLECT_KEEP_DAYS * 86400
        s.slots = {k: v for k, v in s.slots.items() if int(k) >= cutoff}
        steady = fit_cop_steady([Slot(int(k), *v) for k, v in s.slots.items()])
        return steady, fit_cop_loss(steady, five, comp, dhw)

    # ------------------------------------------------------------ rum
    def _source_input(self, s: SourceState, h: Hour) -> float | None:
        """Hvad en varmekilde gav huset i timen: varme hvis den måles (uden varmt vand), ellers el."""
        if s.eid(CONF_HEAT):
            if h.q is None:
                return None
            return max(h.q, 0.0) * (1 - min(h.dhw or 0.0, 60.0) / 60)
        return None if h.e is None else max(h.e, 0.0)

    def _label(self, eid: str) -> str:
        """Læsbart navn på en føler: rummets navn, hvis føleren er et rum, ellers entitetens navn."""
        for r in self.rooms_cfg:
            if r["key"] == eid and r["name"]:
                return r["name"]
        st = self.hass.states.get(eid)
        return st.name if st else eid

    def _rooms_fit(self, hours_by_source: dict[str, list[Hour]]) -> dict[str, Fit]:
        """Én model pr. rum med alle varmekilder som input. Timer med udluftning i rummet udelades."""
        if not self.sources:
            return {}
        idx = {sid: {h.ts: h for h in hs} for sid, hs in hours_by_source.items()}
        first = next(iter(self.sources))
        base = hours_by_source.get(first, [])
        out: dict[str, Fit] = {}
        for room in self.rooms_cfg:
            eid = room["key"]
            rh = []
            for h in base:
                c = self.collected.get(str(h.ts), {})
                inputs = {s.name: self._source_input(s, idx[sid].get(h.ts) or Hour(ts=h.ts)) for sid, s in self.sources.items()}
                rt = self._room_t.get(h.ts) or {}
                nb = {self._label(n): rt.get(n) for n in room.get("neighbours") or []}
                rh.append(RoomHour(ts=h.ts, t_room=rt.get(eid), t_out=h.t_out, sun=sun_factor(h),
                                   aired=eid in (c.get("air") or []), inputs=inputs, neighbours=nb or None))
            out[eid] = fit_room(rh)
        return out

    def _room_heat(self, hours_by_source: dict[str, list[Hour]]) -> dict[str, dict[str, Any]]:
        """Hver vandbåren varmekildes varme fordelt på dens rum time for time efter (kredsstørrelse ×
        minutter åben mens kompressoren kørte). Altid åbne kredse tæller med alle kompressorminutter."""
        out: dict[str, dict[str, Any]] = {}
        for sid, src in self.sources.items():
            if src.kind != SOURCE_HYDRONIC:
                continue
            rooms = [r for r in self.rooms_cfg if (r["valve"] or r["always_open"]) and self.room_source(r) is src]
            if rooms:
                out.update(self._split_heat(src, rooms, hours_by_source.get(sid, [])))
        return out

    def _split_heat(self, hyd: SourceState, rooms: list[dict[str, Any]], hours: list[Hour]) -> dict[str, dict[str, Any]]:
        day_of = lambda ts: dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()  # noqa: E731
        today = dt_util.now().date()
        days7 = {(today - timedelta(days=i)).isoformat() for i in range(1, 8)}
        yday = (today - timedelta(days=1)).isoformat()
        acc = {r["key"]: {"week": 0.0, "yday": 0.0, "open_h": 0.0, "drops": 0, "days": set()} for r in rooms}
        weight = {r["key"]: r["weight"] for r in rooms}
        valve_seen = False
        for h in hours:
            c = self.collected.get(str(h.ts), {})
            if "vchk" not in c and any(r["valve"] for r in rooms):
                continue
            valve_seen = True
            d = day_of(h.ts)
            if d in days7:
                for r in rooms:
                    acc[r["key"]]["days"].add(d)
            if d not in days7 or not h.q or h.q <= 0 or (h.dhw or 0) > 0:
                continue
            comp = (hyd.collected.get(str(h.ts)) or {}).get("comp", 60.0)
            w = {r["key"]: r["weight"] * (comp if r["always_open"] else (c.get("v") or {}).get(r["key"], 0.0)) for r in rooms}
            tot = sum(w.values())
            for k, v in w.items():
                share = h.q * v / tot if tot > 0 else 0.0
                acc[k]["week"] += share
                acc[k]["open_h"] += v / weight[k] / 60
                if d == yday:
                    acc[k]["yday"] += share
        # brat afkøling (døre, udluftning) pr. døgn ud fra opsamlerens markeringer
        for k, v in self.collected.items():
            if day_of(int(k)) not in days7:
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
                "valve_data": valve_seen, "source": hyd.name}
            for k, a in acc.items()
        }
