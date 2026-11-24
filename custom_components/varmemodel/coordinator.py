"""Huset: opsamling og natlig kalibrering for alle varmekilder og rum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
import logging
from math import ceil, cos, radians, sin
import asyncio
import re
import time
from statistics import median
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import aggregate as agg
from .calibration import (
    Fit,
    Hour,
    RoomHour,
    Slot,
    calibrate,
    cop_today,
    daily_cop,
    daily_el,
    detect_airing,
    fit_cop_loss,
    fit_cop_steady,
    fit_heat_loss,
    fit_room,
    guard,
    steady_slots,
    sun_factor,
    validate_room,
)
from .air_control import (
    LEARN_HOLD,
    W_SETTLE,
    band,
    build_ladder,
    EFF_HOLD,
    choose_step,
    eff_per_kwh,
    eff_update,
    efficient_step,
    fan_options,
    room_gain,
    target_step,
    is_night,
    learn_w,
    preload_per_room,
)
from .command import Command, step
from .inputs import blocked_hours, build_hours, hourly_forecast, hourly_prices
from .planner import FLOW_LOW_OPEN_S, FLOW_MAX as FLOW_MAX_C, PlanHour, PlanRoom, SourceModel, estimate_room, flow_boost_step, make_air_plan, make_plan, simulate_rooms
from .const import (
    AIR_BOOST,
    AIR_SOFT_UP_EVERY,
    AIR_SOFT_MIN_RISE,
    AIR_SOFT_SHARE,
    AIR_START_OPTIONS,
    AIR_SWITCH,
    AIR_TIMES,
    CONF_ASSIST,
    CONF_EL_POWER,
    CONF_ASSIST_SPEED,
    CONF_CLIMATE,
    CONF_FLOW_TARGET,
    CONF_ONOFF_TARGET,
    HYDRO_MAX_FLIPS,
    HYDRO_SWITCH,
    SOFT_EVAL_S,
    SOFT_FLOOR,
    SOFT_OVER_RETURN_K,
    SOFT_START,
    SOFT_STEP_K,
    FLOW_MAX_WRITES,
    FLOW_SWITCH,
    UNCHANGED,
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
    CONF_OUTDOOR,
    CONF_RETURN,
    CONF_ROOM_ALWAYS_OPEN,
    CONF_ROOM_IN_PLAN,
    CONF_ROOM_MIN,
    CONF_ROOM_MAX,
    CONF_ROOM_MIN_ENTITY,
    CONF_ROOM_NAME,
    CONF_ROOM_NEIGHBOURS,
    CONF_ROOM_SOURCE,
    CONF_ROOM_EXTRA_SOURCES,
    CONF_ROOM_TEMP,
    CONF_ROOM_VALVE,
    CONF_ROOM_WEIGHT,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_UNIT_TEMP,
    CONF_UNIT_TEMP_ATTR,
    CONF_WEATHER,
    CONF_WINDOW_DAYS,
    CONF_PRICE_ENTITY,
    CONF_PRICE_ATTR,
    CONF_BLOCK_ENTITY,
    CONF_BLOCK_CODES,
    CONF_BLOCK_NOW_ENTITY,
    CONF_BLOCK_QUARTERS_ENTITY,
    BLOCK_QUARTERS_MIN,
    LEARN_IGNORE_WORDS,
    AIR_EFF_BACKFILL_DAYS,
    AIR_EFF_BF_VERSION,
    AIR_EFF_AIRING_DROP,
    CONF_BLOCK_NOW_STATES,
    DEFAULT_BLOCK_NOW_STATES,
    LOW_DOWN_HOLD,
    LOW_DOWN_MARGIN,
    LOW_LEARN_HOLD,
    LOW_RESERVE,
    LOW_UP_HOLD,
    LOW_URGENT_HOLD,
    ANTI_STOP_GAP,
    DF_VALVE_S,
    DHW_AFTER_S,
    START_K_DECAY,
    START_K_MAX,
    START_PHASE_S,
    CONF_MAX_PAUSE,
    CONF_COMFORT_OFFSET,
    CONF_BOOST_ENTITY,
    CONF_BOOST_DELTA,
    PLAN_HORIZON_H,
    CONF_BOOST_HOURS,
    CONF_ECONOMY,
    CONF_RED_ALLOWANCE,
    CONF_COLD_CAUTION,
    CONF_FLOW_MARGIN,
    CONF_MEASURES_CALENDAR,
    CONF_PLANT_ROOM,
    CONF_PLANT_LOSS,
    CONF_GROUND_TEMP,
    DEFAULT_PLANT_LOSS,
    DEFAULT_GROUND_TEMP,
    SETTINGS,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
    MODE_CALIBRATED,
    MODE_FROZEN,
    SOURCE_HYDRONIC,
    SUBENTRY_ROOM,
    SUBENTRY_SOURCE,
)

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
# Opsamlede felter der hører til en bestemt varmekilde (resten hører til huset)
# Rum- og udeføler: den sidst kendte værdi bruges i op til 30 min. (fx når en skytjeneste er kortvarigt nede).
# Derefter forudsiges rummet fra sidste måling med den målte tilførsel (udeføleren: vejrudsigten), og »Mangler data« slår til.
HOLD_S = 30 * 60
EST_MARGIN_MAX = 1.0  # forudsagte rum: usikkerheden vokser med rummets præcision pr. 6 timer uden måling, højst 1 °C
STARTUP_GRACE_S = 10 * 60  # ingen »Mangler data« de første 10 min. efter start (forudsigelsen bruges dog)
SOURCE_FIELDS = ("comp", "comp_n", "dhw", "dhw_n", "df", "df_n", "ut")


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
    today: dict[str, Any] = field(default_factory=dict)  # dagens COP, regnes hver time
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
        # 0.7.0: timemodellen for COP er fjernet; 0.8.0: LG's varmekurve er erstattet af det lærte fremløb
        for k in ("cop", "curve"):
            self.accepted.pop(k, None)
            self.fits.pop(k, None)


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
        self.plans: dict[str, dict[str, Any]] = {}
        self.plan_meta: dict[str, Any] = {"updated": None, "inputs_ok": False, "missing": ["ikke planlagt endnu"]}
        self.settings: dict[str, float] = {}
        self.plan_log: dict[str, dict[str, dict[str, Any]]] = {}  # kilde -> timens start -> det planlagte ved timens start
        self.fc_log: dict[str, dict[str, list[float]]] = {}  # timens start -> rum -> forudsigelse lavet da
        self.rooms_check: dict[str, dict[str, Any]] = {}
        # Røde perioder med kvarterer [(fra, til)] (timeplanens blokerede timer + blokerede kvarterer) til visning og starttider
        self.red_spans: list[tuple[int, int]] = []
        self._red_q: set[int] = set()
        self.yesterday: dict[str, dict[str, Any]] = {}
        self.measures: list[dict[str, Any]] = []
        self.commands: dict[str, dict[str, Any]] = {}
        self.air: dict[str, dict[str, Any]] = {}
        self.plant_week: float | None = None  # luft-luft-styring pr. kilde: trin, lært forbrug og starttrin  # anbefalet styring pr. vandbåren kilde
        self.plan_stats: dict[str, Any] = {}  # andel kvarterer med frisk plan i dag
        self.last_known: dict[str, list[float]] = {}  # føler -> [værdi, tidspunkt]: bro over korte udfald
        self.valve_since: dict[str, float] = {}  # termostaten kalder på varme siden (rummets nøgle)
        self.flow_boost: dict[str, dict[str, Any]] = {}  # fremløb for lavt: °C over grænsen pr. vandbåren kilde
        self.estimates: dict[str, dict[str, Any]] = {}  # rumføler væk i over 30 min. -> forudsagt temperatur nu
        self.missing_data: dict[str, Any] = {}  # datapunkter der har manglet i over 30 min. (»Mangler data«)
        self._started = time.time()
        self.device_ids: dict[str | None, str] = {}  # underpunkt (None = huset) -> enheds-id i HA
        self._listeners: list[Callable[[], None]] = []
        self._stopped = False  # sat ved afmelding/genindlæsning: denne instans styrer og gemmer ikke mere
        self._unsubs: list[CALLBACK_TYPE] = []
        self._replan_lock = asyncio.Lock()
        self._refresh_pending: set[str] = set()  # vandbårne kilder, hvor fremløbet skrives om lidt (efter et skift)
        self._air_rise: dict[str, dict[str, float]] = {}  # luft-luft: rummenes målte stigning (°C/t) seneste 30 min.  # én planlægning ad gangen (planlagt og »blokering lige nu«)
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
                 "min_entity": s.data.get(CONF_ROOM_MIN_ENTITY), "min": s.data.get(CONF_ROOM_MIN), "max": s.data.get(CONF_ROOM_MAX),
                 "in_plan": s.data.get(CONF_ROOM_IN_PLAN, True), "neighbours": list(s.data.get(CONF_ROOM_NEIGHBOURS) or []),
                 "source": s.data.get(CONF_ROOM_SOURCE), "extra_sources": list(s.data.get(CONF_ROOM_EXTRA_SOURCES) or []),
                 "subentry_id": sid}
                for sid, s in subs
            ]
        return [{"key": e, "name": None, "valve": None, "always_open": False, "weight": 1.0, "min_entity": None, "min": None, "max": None,
                 "in_plan": True, "neighbours": [], "source": None, "extra_sources": [], "subentry_id": None} for e in (self.conf.get(CONF_INDOOR) or [])]

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
        self.settings = data.get("settings", {})
        if "flow_over_curve" in self.settings:  # 0.8.0: omdøbt
            self.settings.setdefault("flow_margin", self.settings.pop("flow_over_curve"))
        self.plan_log = data.get("plan_log", {})
        self.fc_log = data.get("fc_log", {})
        self.yesterday = data.get("yesterday", {})
        self.measures = data.get("measures", [])
        self.commands = data.get("commands", {})
        self.air = data.get("air", {})
        self.plant_week = data.get("plant_week")  # varmecentralens snit (7 døgn) – gemmes, så planen har tabet straks efter en genstart
        self.plan_stats = data.get("plan_stats", {})
        self.last_known = data.get("last_known", {})
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
        self._unsubs.append(async_track_time_change(self.hass, self._on_replan, minute=[2, 17, 32, 47], second=0))
        if beid := self._id(CONF_BLOCK_NOW_ENTITY):
            # Blokering lige nu (fx prisloft pr. kvarter): planlæg straks, når den skifter, så varmepumperne følger med
            self._unsubs.append(async_track_state_change_event(self.hass, [beid], self._on_block_now))
        hyd_ids = sorted({e for s in self.sources.values() if s.conf.get(CONF_FLOW_TARGET)
                          for e in (s.eid(CONF_COMPRESSOR), s.eid(CONF_DEFROST), s.eid(CONF_DHW), s.eid(CONF_FLOW)) if e})
        if hyd_ids:
            # Opstart, afrimning og stop: skriv fremløbsmålet straks (før varmepumpen genstarter) – ikke først ved næste kvarter
            self._unsubs.append(async_track_state_change_event(self.hass, hyd_ids, self._on_hydro_state))
        self._unsubs.append(async_call_later(self.hass, 45, self._on_replan))
        self._unsubs.append(async_call_later(self.hass, 90, self._on_today))
        self._unsubs.append(async_track_time_change(self.hass, self._on_today, minute=[5, 20, 35, 50], second=0))
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
        # Først flaget: en planlægning, der stadig kører i denne instans, må ikke tænde/slukke eller skrive bagefter
        # (24/11: en gammel instans tændte luft-vand efter en genindlæsning, mens den nye var slået fra)
        self._stopped = True
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        self._sync_issues({})
        await self._save(force=True)

    def _data(self) -> dict[str, Any]:
        return {"collected": self.collected, "sources": {sid: s.as_store() for sid, s in self.sources.items()},
                "runs": self.runs[-30:], "mode": self.mode, "calib_version": CALIB_VERSION,
                "rooms": self.rooms, "room_heat": self.room_heat, "settings": self.settings, "plan_log": self.plan_log,
                "fc_log": self.fc_log, "yesterday": self.yesterday, "measures": self.measures,
                "commands": self.commands, "plan_stats": self.plan_stats, "last_known": self.last_known, "air": self.air,
                "plant_week": self.plant_week}

    async def _save(self, force: bool = False) -> None:
        if self._stopped and not force:
            return
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

    async def _on_today(self, _now: datetime | None) -> None:
        """Dagens COP for hver vandbåren kilde (5-minutters portioner siden midnat), hvert kvarter."""
        for s in self.sources.values():
            if s.kind != SOURCE_HYDRONIC:
                continue
            try:
                s.today = await get_instance(self.hass).async_add_executor_job(self._today_sync, s) or {}
            except Exception:  # noqa: BLE001 – en diagnose må ikke vælte resten
                _LOGGER.exception("COP i dag fejlede")
        self._notify()

    def _today_sync(self, s: SourceState) -> dict[str, Any] | None:
        heat, el, flow, comp_id = s.eid(CONF_HEAT), s.eid(CONF_ELECTRICITY), s.eid(CONF_FLOW), s.eid(CONF_COMPRESSOR)
        out = self._id(CONF_OUTDOOR)
        if not (heat and el and out and flow and comp_id):
            return None
        end = dt_util.utcnow()
        start = dt_util.as_utc(dt_util.start_of_local_day())
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

        st = s.accepted.get("cop_steady") or {}
        steady = Fit(st.get("value"), extra=st.get("extra") or {}) if st.get("value") is not None else None
        d = cop_today(five, ivs(CONF_COMPRESSOR), ivs(CONF_DEFROST), ivs(CONF_DHW), steady)
        if d is not None:
            d["date"] = dt_util.now().date().isoformat()
            d["updated"] = dt_util.now().isoformat(timespec="minutes")
        return d

    def _plant_loss_w(self) -> float:
        """Varmecentralens tab nu (W) ud fra snittet de seneste 7 døgn."""
        h, g = self.plant()
        t = self.plant_week
        return h * (t - g) if h > 0 and t is not None else 0.0

    def plant(self) -> tuple[float, float]:
        """Varmecentralens faste tab (W/K) og jordtemperatur; 0 W/K uden føler."""
        if not self._id(CONF_PLANT_ROOM):
            return 0.0, DEFAULT_GROUND_TEMP
        return float(self.conf.get(CONF_PLANT_LOSS, DEFAULT_PLANT_LOSS)), float(self.conf.get(CONF_GROUND_TEMP, DEFAULT_GROUND_TEMP))

    def setting(self, key: str) -> float:
        """Planens indstilling: tal-entiteten, ellers opsætningen, ellers standarden."""
        default = next(d for k, *_, d in SETTINGS if k == key)
        v = self.settings.get(key, self.conf.get(key))
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    async def async_set_setting(self, key: str, value: float) -> None:
        self.settings[key] = value
        await self._save()
        await self.async_replan()

    async def async_set_mode(self, mode: str) -> None:
        self.mode = mode
        await self._save()
        self._notify()

    # ------------------------------------------------------------ opsamling
    async def async_collect(self) -> None:
        """Opsummér time for time: varmekildernes kompressor, varmt vand, afrimning og
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
                    fits = await self.hass.async_add_executor_job(calibrate, hours, *self.plant())
                    steady, loss = await get_instance(self.hass).async_add_executor_job(self._steady_sync, s)
                    fits["cop_steady"], fits["cop_loss"] = steady, loss
                else:
                    fits = {}
                results[sid] = fits
            room_hours: dict[str, list[RoomHour]] = {}
            room_fits = await self.hass.async_add_executor_job(self._rooms_fit, hours_by_source, room_hours)
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
            # Ny modelopbygning (andre varmekilder eller nabozoner end sidst) er en ny model – ingen ±10 %-grænse
            pe = (prev or {}).get("extra") or {}
            # Ny regnemetode (CALIB_VERSION) eller ny opbygning er en ny model – den gamle ±10 %-grænse gælder ikke
            new_shape = f.value is not None and prev is not None and (prev.get("v") != CALIB_VERSION or
                set((pe.get("sources") or {})) != set((f.extra.get("sources") or {}))
                or set((pe.get("neighbours") or {})) != set((f.extra.get("neighbours") or {})))
            if new_shape:
                value, note = f.value, "ny model (ny regnemetode, varmekilder eller naboer ændret)"
            else:
                value, note = guard(f.value, prev.get("value") if prev else None, (f.t_out_min, f.t_out_max),
                                    (prev.get("t_out_min"), prev.get("t_out_max")) if prev else (None, None))
            keep_prev = note.startswith("begrænset") or f.value is None
            extra = dict(pe) if keep_prev else f.extra
            if keep_prev and prev and prev.get("value") is not None:
                # Vagten holder hele den forrige model (værdi og koefficienter sammen), så den hænger sammen med sig selv
                value = prev["value"]
            if keep_prev and value is not None and eid in room_hours:
                # Vagten beholder den forrige model: afprøv den, der faktisk bruges, så præcisionen passer til den
                for k in ("validated_6h_k", "validated_bias_6h_k", "validated_windows"):
                    extra.pop(k, None)
                val = await self.hass.async_add_executor_job(validate_room, room_hours[eid], Fit(value, extra=extra))
                if val is not None:
                    extra.update(validated_6h_k=val["rmse"], validated_bias_6h_k=val["bias"], validated_windows=val["n"])
            self.rooms[eid] = {"v": CALIB_VERSION if not keep_prev else (prev or {}).get("v"),
                               "value": value, "guard": note, "n": f.n, "r2": None if f.r2 is None else round(f.r2, 2),
                               "t_out_min": f.t_out_min, "t_out_max": f.t_out_max, "note": f.note,
                               "fitted_at": now if f.value is not None else (prev or {}).get("fitted_at"), "extra": extra}
        self.room_heat = self._room_heat(hours_by_source)
        self.yesterday = self._yesterday(day_of, yday[0])
        try:
            self.measures = await self._measures(room_hours)
        except Exception:  # noqa: BLE001 – tiltag må ikke vælte kalibreringen
            _LOGGER.exception("Før/efter for tiltag fejlede")
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
        if key == "cop_loss":
            # Ekstra el er en måling (målt mod målt), ikke en model: ingen ±10 %-grænse
            prev = None
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
            "rmse": None if fit.rmse is None else round(fit.rmse, 4),
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
        plant = self._id(CONF_PLANT_ROOM)
        want = set(self._room_temps()) | set(self._neighbour_temps()) | ({outdoor} if outdoor else set()) | ({plant} if plant else set())
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
                    comp=sc.get("comp"), dhw=sc.get("dhw"), defrost=sc.get("df_n"), 
                    wind=c.get("ws"), wind_u=c.get("wu"), wind_v=c.get("wv"), cloud=c.get("cc"), uv=c.get("uv"), humidity=c.get("rh"),
                    t_plant=val(plant, "mean"),
                ))
        # Varmecentralens snit de seneste 7 døgn (planen regner med det, fordi rummet ændrer sig langsomt)
        week = [_num((rows[ts].get(plant) or {}).get("mean")) for ts in sorted(rows)[-168:]] if plant else []
        week = [v for v in week if v is not None]
        self.plant_week = round(sum(week) / len(week), 2) if week else None
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
        return steady, fit_cop_loss(steady, five, comp, defrost, dhw)

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

    def _rooms_fit(self, hours_by_source: dict[str, list[Hour]], keep: dict[str, list[RoomHour]] | None = None) -> dict[str, Fit]:
        """Én model pr. rum med alle varmekilder som input. Timer med udluftning i rummet udelades.

        ``keep`` får rummenes timer med, så en model, vagten beholder, kan afprøves bagefter."""
        if not self.sources:
            return {}
        idx = {sid: {h.ts: h for h in hs} for sid, hs in hours_by_source.items()}
        first = next(iter(self.sources))
        base = hours_by_source.get(first, [])
        out: dict[str, Fit] = {}
        for room in self.rooms_cfg:
            eid = room["key"]
            # Rummet lærer af sin egen varmekilde og de valgte ekstra kilder; uden valg af alle kilder.
            # (Kilder der kører på samme tider kan ikke skilles ad af data alene.)
            if room.get("source") in self.sources:
                allowed = {room["source"], *[x for x in room.get("extra_sources") or [] if x in self.sources]}
            else:
                allowed = set(self.sources)
            rh = []
            for h in base:
                c = self.collected.get(str(h.ts), {})
                inputs = {s.name: self._source_input(s, idx[sid].get(h.ts) or Hour(ts=h.ts)) for sid, s in self.sources.items() if sid in allowed}
                rt = self._room_t.get(h.ts) or {}
                nb = {self._label(n): rt.get(n) for n in room.get("neighbours") or []}
                rh.append(RoomHour(ts=h.ts, t_room=rt.get(eid), t_out=h.t_out, sun=sun_factor(h),
                                   aired=eid in (c.get("air") or []), inputs=inputs, neighbours=nb or None))
            out[eid] = fit_room(rh)
            if keep is not None:
                keep[eid] = rh
            # Ærlig præcision: modellen afprøvet 6 timer frem med målt tilførsel, som planen og tjekket bruger den
            if (val := validate_room(rh, out[eid])) is not None:
                out[eid].extra["validated_6h_k"] = val["rmse"]
                out[eid].extra["validated_bias_6h_k"] = val["bias"]
                out[eid].extra["validated_windows"] = val["n"]
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

    def _plan_room(self, r: dict[str, Any], s: SourceState, t_min: float, start: float | None = None) -> PlanRoom | None:
        """Rummets model til planen og forudsigelsen (None hvis rummet mangler måling eller model for kilden).

        Har føleren været væk i over 30 min., bruges den forudsagte temperatur (``estimates``). ``start`` giver
        modellen med en bestemt starttemperatur (til at regne rummet frem fra sidste måling)."""
        t_room, held = self._held_num(r["key"])
        est = None
        if start is not None:
            t_room, held = start, True
        elif t_room is None and (est := self.estimates.get(r["key"])) is not None:
            t_room, held = float(est["t"]), True
        fitd = self.rooms.get(r["key"]) or {}
        ex = fitd.get("extra") or {}
        src = (ex.get("sources") or {}).get(s.name)
        if t_room is None or not src or fitd.get("value") is None:
            return None
        nbs = []
        for nb_name, nb in (ex.get("neighbours") or {}).items():
            nb_eid = next((n for n in r.get("neighbours") or [] if self._label(n) == nb_name), None)
            if (tn := self._nb_value(nb_eid)) is not None:
                nbs.append((float(nb.get("per_h_k") or 0.0), tn))
        acc = max(float(ex.get("accuracy_6h_k") or 0.3), float(ex.get("validated_6h_k") or 0.0))
        # Usikkerhed = rummodellens afprøvede præcision over 6 timer (data som de er; mindst 0,1 °C for følerens opløsning)
        margin = max(0.1, acc)
        if est is not None:
            # Uden måling vokser usikkerheden: + rummets præcision for hver 6 timer ud over de første 6, højst 1 °C
            blind_h = (time.time() - float(est["since"])) / 3600
            margin = max(margin, min(EST_MARGIN_MAX, margin + acc * max(0.0, blind_h / 6 - 1)))
        return PlanRoom(
            name=r["name"] or r["key"], t_now=t_room, t_min=t_min,
            heat_now=float(src.get("now") or 0.0), heat_delayed=float(src.get("delayed") or 0.0),
            cool_per_hk=float(fitd["value"]) / 20, free_per_h=float(ex.get("free_heat_k_per_day") or 0.0) / 24,
            margin=margin, accuracy=acc, neighbours=nbs, held=held, estimated=est is not None,
            t_max=_num(r.get("max")) if r.get("max") is not None else None,
            # Termostat: målt andel af kildens varme (ventil-data) og lukketemperatur = gulv + bånd (kun vandbårne rum)
            # Kun rum med en rigtig telestat/termostat kan lukke kredsen; »altid åben« får sin andel hele tiden
            share=self._room_share(r, s), t_close=(t_min + self.setting(CONF_COMFORT_OFFSET))
            if s.kind == SOURCE_HYDRONIC and t_min > -50 and self._room_share(r, s) and r.get("valve") and not r.get("always_open") else None,
        )

    def _room_share(self, r: dict[str, Any], s: SourceState) -> float | None:
        """Rummets målte andel af den vandbårne kildes varme (fra ventilernes åbne tid), 0–1; None uden ventil-data."""
        if s.kind != SOURCE_HYDRONIC:
            return None
        rh = (self.room_heat or {}).get(r["key"]) or {}
        pct = _num(rh.get("share_pct"))
        return pct / 100 if rh.get("valve_data") and pct and pct > 0 else None

    def _valve_open_since(self, r: dict[str, Any], now: float) -> float | None:
        """Siden hvornår rummets termostat/telestat har kaldt på varme uafbrudt (None = lukket eller ukendt)."""
        eid = r.get("valve")
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in ("unknown", "unavailable"):
            self.valve_since.pop(r["key"], None)
            return None
        is_open = st.attributes.get("hvac_action") == "heating" if eid.split(".", 1)[0] == "climate" else st.state in ("on", "open", "opening")
        if not is_open:
            self.valve_since.pop(r["key"], None)
            return None
        return self.valve_since.setdefault(r["key"], now)

    def _nb_value(self, eid: str | None) -> float | None:
        """Naboens temperatur: målt eller holdt, ellers forudsagt (et rum uden føler), ellers sidst kendte."""
        tn, _ = self._held_num(eid)
        if tn is None and eid:
            if (e := self.estimates.get(eid)) is not None:
                tn = float(e["t"])
            elif lk := self.last_known.get(eid):
                tn = float(lk[0])
        return tn

    def _recent5_sync(self, s: SourceState, start: int, end: int) -> tuple[dict[int, float], dict[int, float]]:
        """Kildens tilførsel (som ``_recent_sync``) og udetemperaturen pr. 5 minutter i [start, end)."""
        heat, outdoor = s.eid(CONF_HEAT) or s.eid(CONF_ELECTRICITY), self._id(CONF_OUTDOOR)
        if not heat:
            return {}, {}
        stats = statistics_during_period(self.hass, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end),
                                         {x for x in (heat, outdoor) if x}, "5minute", {"energy": "kWh", "temperature": "°C"},
                                         {"change", "mean"})
        q: dict[int, float] = {}
        for x in stats.get(heat, []):
            if (v := _num(x.get("change"))) is not None:
                b = int(_ts(x["start"]))
                dhw = ((s.collected.get(str(b // 3600 * 3600)) or {}).get("dhw") or 0.0) if s.eid(CONF_HEAT) else 0.0
                q[b] = max(v, 0.0) * (1 - min(dhw, 60.0) / 60)
        out = {int(_ts(x["start"])): v for x in (stats.get(outdoor, []) if outdoor else []) if (v := _num(x.get("mean"))) is not None}
        return q, out

    async def _estimate_missing(self, now: float, t_out: float | None) -> None:
        """Rum hvis føler har været væk i over 30 min.: temperaturen nu regnes frem fra sidste måling med den
        tilførsel, kilden faktisk gav. Planen og den anbefalede styring kører så videre på forudsigelsen."""
        todo: dict[str, list[dict[str, Any]]] = {}
        for r in self.rooms_cfg:
            lk = self.last_known.get(r["key"])
            if self._state_num(r["key"]) is not None or not lk or now - float(lk[1]) <= HOLD_S:
                continue
            if (s := self.room_source(r)) is not None:
                todo.setdefault(s.sid, []).append(r)
        est: dict[str, dict[str, Any]] = {}
        for sid, rs in todo.items():
            s = self.sources[sid]
            first = min(float(self.last_known[r["key"]][1]) for r in rs)
            try:
                q5, out5 = await get_instance(self.hass).async_add_executor_job(self._recent5_sync, s, int(first) - 6 * 3600 - 300, int(now))
            except Exception:  # noqa: BLE001 – uden statistik regnes uden tilførsel (rummet køler: planen varmer hellere for meget)
                q5, out5 = {}, {}
            # De seneste 5-minutters tal kommer få minutter forsinket: regn kun frem til det sidste, medmindre de mangler i over 15 min.
            end = now if not q5 or now - (max(q5) + 300) > 900 else min(now, max(q5) + 300)
            for r in rs:
                v0, ts0 = (float(x) for x in self.last_known[r["key"]])
                if (pr := self._plan_room(r, s, -99.0, start=v0)) is None:
                    continue
                t = estimate_room(pr, v0, ts0, end, q5, out5, t_out if t_out is not None else 5.0)
                est[r["key"]] = {"t": round(t, 2), "since": ts0, "name": r["name"] or r["key"]}
        self.estimates = est

    def _flag_missing(self, now: float, outdoor: dict[str, Any] | None) -> None:
        """»Mangler data«: rum- og udeføler der har været væk i over 30 min. Hver får en reparation i HA, så den kan udbedres."""
        rooms: dict[str, dict[str, Any]] = {}
        if now - self._started < STARTUP_GRACE_S:
            return  # lige efter en genstart: andre integrationer (fx en skytjeneste) er måske ikke klar endnu
        for r in self.rooms_cfg:
            eid = r["key"]
            if self._state_num(eid) is not None:
                continue
            lk = self.last_known.get(eid)
            if (lk and now - float(lk[1]) <= HOLD_S) or (not lk and now - self._started <= HOLD_S):
                continue  # kortvarigt væk, eller lige startet: ingen alarm endnu
            e = self.estimates.get(eid)
            rooms[r["name"] or eid] = {
                "føler": eid, "sidste_måling": _ts_local_iso(float(lk[1])) if lk else None,
                "minutter": round((now - float(lk[1])) / 60) if lk else None, "forudsagt": e["t"] if e else None,
                "bruger": "forudsigelsen" if e else "intet – rummet er ikke med i planen",
            }
        self.missing_data = {"rum": rooms, **({"udeføler": outdoor} if outdoor else {})}
        want: dict[str, tuple[str, dict[str, str]]] = {}
        for name, x in rooms.items():
            want[x["føler"]] = ("missing_room" if x["forudsagt"] is not None else "missing_room_unplanned",
                                {"name": name, "entity": x["føler"], "since": _hhmm(x["sidste_måling"])})
        if outdoor:
            want[outdoor["føler"]] = ("missing_outdoor", {"entity": outdoor["føler"], "since": _hhmm(outdoor["sidste_måling"])})
        self._sync_issues(want)

    def _sync_issues(self, want: dict[str, tuple[str, dict[str, str]]]) -> None:
        """Én reparation pr. manglende datapunkt; den forsvinder, når føleren måler igen."""
        prefix = f"missing_{self.entry.entry_id}_"
        for eid, (key, ph) in want.items():
            ir.async_create_issue(self.hass, DOMAIN, prefix + eid, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                  translation_key=key, translation_placeholders=ph)
        for dom, iid in list(ir.async_get(self.hass).issues):
            if dom == DOMAIN and iid.startswith(prefix) and iid[len(prefix):] not in want:
                ir.async_delete_issue(self.hass, DOMAIN, iid)

    def _yesterday(self, day_of: Callable[[int], str], day: str) -> dict[str, dict[str, Any]]:
        """Gårsdagen time for time: det planlagte (ved timens start) mod det, der faktisk skete."""
        out: dict[str, dict[str, Any]] = {}
        for sid, s in self.sources.items():
            log = self.plan_log.get(sid) or {}
            hrs = [h for h in s.last_hours if day_of(h.ts) == day]
            if s.kind != SOURCE_HYDRONIC or not hrs:
                continue
            planned = [log.get(str(h.ts)) for h in hrs]
            known = [(h, x) for h, x in zip(hrs, planned) if x is not None]
            q = lambda h: max(h.q or 0.0, 0.0)  # noqa: E731
            e = lambda h: max(h.e or 0.0, 0.0)  # noqa: E731
            out[sid] = {
                "dato": day, "timer_med_plan": len(known), "timer": len(hrs),
                "plan_varme_kwh": round(sum(x["p"] for _, x in known), 1),
                "plan_el_kwh": round(sum(x.get("e", 0.0) for _, x in known), 1),
                "plan_kr": round(sum(x.get("e", 0.0) * x["pr"] for _, x in known), 1),
                "faktisk_varme_kwh": round(sum(q(h) for h, _ in known), 1),
                "faktisk_varme_uden_vv_kwh": round(sum(q(h) * (1 - min(h.dhw or 0.0, 60.0) / 60) for h, _ in known), 1),
                "faktisk_el_kwh": round(sum(e(h) for h, _ in known), 1),
                "faktisk_kr": round(sum(e(h) * x["pr"] for h, x in known), 1),
                "faktisk_varme_i_rød_kwh": round(sum(q(h) for h, x in known if x["b"]), 1),
                "timer": [{"t": _ts_local_iso(h.ts), "plan": x["p"], "faktisk": round(q(h), 2), "blokeret": x["b"]} for h, x in known],
            }
        return out

    def measure_source(self, text: str) -> SourceState | None:
        """Hvilken varmekilde (etage) et tiltag gælder: kildens navn i titel/beskrivelse, ellers etage-ord, ellers den vandbårne."""
        t = (text or "").lower()
        for s in self.sources.values():
            if s.name and s.name.lower() in t:
                return s
        # Et rums navn i teksten: rummets varmekilde (fx »termostat i <rum>« → rummets etage)
        for r in sorted(self.rooms_cfg, key=lambda x: -len(x.get("name") or "")):
            if r.get("name") and r["name"].lower() in t and (rs := self.room_source(r)) is not None:
                return rs
        air = next((s for s in self.sources.values() if s.kind != SOURCE_HYDRONIC), None)
        if air and any(w in t for w in ("1. sal", "1.sal", "første sal", "overetage", "1st floor", "upstairs")):
            return air
        return self.hydronic

    def _zone_hours(self, s: SourceState, room_hours: dict[str, list[Any]] | None) -> list[Hour]:
        """Kildens timer med kildens egen tilførsel (varme, eller el for luft-luft) og etagens temperatur (snit af dens rum)."""
        zone: dict[int, list[float]] = {}
        for r in self.rooms_cfg:
            if self.room_source(r) is s:
                for rh in (room_hours or {}).get(r["key"]) or []:
                    if rh.t_room is not None:
                        zone.setdefault(rh.ts, []).append(rh.t_room)
        out = []
        for h in s.last_hours:
            q = self._source_input(s, h)
            zt = zone.get(h.ts)
            out.append(replace(h, q=q, t_in=(sum(zt) / len(zt)) if zt else (h.t_in if s.kind == SOURCE_HYDRONIC else None)))
        return out

    async def _measures(self, room_hours: dict[str, list[Any]] | None = None) -> list[dict[str, Any]]:
        """Før/efter for hvert energitiltag i kalenderen, for den etage (varmekilde) tiltaget gælder.

        Vandbåren kilde: etagens varmetab (W/K) fra den målte varme. Luft-luft: etagens el pr. grad (W el/K) – varmen
        måles ikke, men ændringen i % viser, om tiltaget virker (så længe COP er nogenlunde den samme)."""
        cal = self._id(CONF_MEASURES_CALENDAR)
        if not cal or self.hydronic is None:
            return []
        days = int(self.conf.get(CONF_WINDOW_DAYS) or DEFAULT_WINDOW_DAYS)
        end = dt_util.now()
        resp = await self.hass.services.async_call(
            "calendar", "get_events", {"start_date_time": end - timedelta(days=days), "end_date_time": end},
            target={"entity_id": cal}, blocking=True, return_response=True)
        events = ((resp or {}).get(cal) or {}).get("events") or []
        zh: dict[str, list[Hour]] = {}
        out = []
        for ev in events:
            if any(w in f"{ev.get('summary') or ''} {ev.get('description') or ''}".lower() for w in LEARN_IGNORE_WORDS):
                continue  # en periode, der skal ignoreres i læringen – ikke et energitiltag
            src = self.measure_source(f"{ev.get('summary') or ''} {ev.get('description') or ''}") or self.hydronic
            if src.sid not in zh:
                zh[src.sid] = self._zone_hours(src, room_hours)
            hours = zh[src.sid]
            d = dt_util.parse_datetime(str(ev.get("start"))) or None
            if d is None:
                dd = dt_util.parse_date(str(ev.get("start")))
                d = datetime.combine(dd, datetime.min.time(), dt_util.get_default_time_zone()) if dd else None
            if d is None:
                continue
            ts = d.timestamp()
            before = [h for h in hours if h.ts < ts]
            after = [h for h in hours if h.ts >= ts + 86400]
            pw, pg = self.plant()
            fb = await self.hass.async_add_executor_job(partial(fit_heat_loss, before, plant_w_k=pw, ground=pg))
            fa = await self.hass.async_add_executor_job(partial(fit_heat_loss, after, plant_w_k=pw, ground=pg))
            chg = round((fa.value - fb.value) / fb.value * 100, 1) if fa.value and fb.value else None
            out.append({"navn": ev.get("summary"), "dato": d.date().isoformat(), "kilde": src.name,
                        "enhed": "W/K" if src.kind == SOURCE_HYDRONIC else "W el/K", "før_w_k": fb.value and round(fb.value, 1),
                        "efter_w_k": fa.value and round(fa.value, 1), "ændring_pct": chg,
                        "dage_før": round(len(before) / 24), "dage_efter": round(len(after) / 24),
                        "forklaringsgrad_før": fb.r2 and round(fb.r2, 2), "forklaringsgrad_efter": fa.r2 and round(fa.r2, 2),
                        "note": None if chg is not None else "for lidt data – kræver ca. 16 døgn på hver side"})
        return out

    # ------------------------------------------------------------ planlægning
    def live_blocked(self) -> bool:
        """Blokeret lige nu af »Blokering lige nu«-entiteten (fx et prisloft pr. kvarter), ud over timeplanens blokeringer."""
        eid = self._id(CONF_BLOCK_NOW_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        if st is None:
            return False
        states = {x.strip().lower() for x in str(self.conf.get(CONF_BLOCK_NOW_STATES) or DEFAULT_BLOCK_NOW_STATES).split(",") if x.strip()}
        return str(st.state).strip().lower() in states

    async def _on_block_now(self, event: Any) -> None:
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None or (old is not None and old.state == new.state):
            return
        await self._on_replan(dt_util.now())

    @callback
    def _on_hydro_state(self, event: Any) -> None:
        """Kompressor, afrimning, varmt vand eller fremløb skiftede på en vandbåren kilde: notér rigtige skift (et Modbus-
        udfald via »utilgængelig« er intet skift) og skriv fremløbsmålet om 5 s, når opstartsløft eller stopværn skal ændre
        det – varmepumpen genstarter typisk 3 min. efter et stop, og planlægningen kører kun hvert kvarter."""
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None or old is None or new.state == old.state:
            return
        if new.state in ("unavailable", "unknown") or old.state in ("unavailable", "unknown"):
            return
        eid = event.data.get("entity_id")
        now = time.time()
        for s in self.sources.values():
            if not s.conf.get(CONF_FLOW_TARGET):
                continue
            hs = self.air.setdefault(f"hydro:{s.sid}", {})
            kick = False
            if eid == s.eid(CONF_COMPRESSOR):
                hs["comp_on_t" if new.state == "on" else "comp_off_t"] = now
                kick = True
            elif eid == s.eid(CONF_DEFROST):
                if new.state == "off":
                    hs["df_off_t"] = now
                kick = True
            elif eid == s.eid(CONF_DHW):
                if new.state == "off":
                    hs["dhw_off_t"] = now
                kick = True
            elif eid == s.eid(CONF_FLOW):
                g, _why = self._guard_value(s, hs)
                tgt = self._target_now(s)
                kick = g is not None and tgt is not None and g >= tgt + 1
            if kick and self.settings.get(f"{s.sid}.{SOFT_START}", "test") == "lavest" and s.sid not in self._refresh_pending:
                self._refresh_pending.add(s.sid)
                async_call_later(self.hass, 5, partial(self._flow_refresh, s.sid))

    async def _flow_refresh(self, sid: str, _now: Any = None) -> None:
        """Skriv fremløbsmålet nu efter et skift (opstartsløft/stopværn) – samme regler som planlægningen."""
        self._refresh_pending.discard(sid)
        s = self.sources.get(sid)
        if s is None or self._stopped:
            return
        full = bool(s.conf.get(CONF_ONOFF_TARGET) and self.settings.get(f"{sid}.{HYDRO_SWITCH}"))
        if not (full or self.settings.get(f"{sid}.{FLOW_SWITCH}")):
            return
        try:
            async with self._replan_lock:
                hours = (self.plans.get(sid) or {}).get("hours") or []
                h0 = int(time.time() // 3600 * 3600)
                cur = next((x for x in hours if x.get("ts") == h0), None)
                await self._flow_apply(s, hours, bool(cur and cur.get("blocked")) or self.live_blocked())
            self._notify()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("%s: fremløbet kunne ikke opdateres efter et skift", s.name, exc_info=True)

    def _guard_value(self, s: SourceState, hs: dict[str, Any]) -> tuple[float | None, str | None]:
        """Varmemodel bestemmer start og stop – varmepumpens egen termostat skal ikke nå at stoppe kompressoren:
        • opstart (kompressoren står i varme, de første START_PHASE_S efter start, under en afrimning og START_PHASE_S efter):
          mindst retur + startløftet – varmepumpen kører lavt flow i opstarten, så fremløbet skyder over målet
        • drift (kompressoren kører, ikke lige efter afrimning): mindst fremløbet − ANTI_STOP_GAP, så fremløbet aldrig når
          målet + varmepumpens stopmargen (ca. 4 °C) – heller ikke på laveste effekt
        Intet under eller lige efter varmt vand. Returnerer (mindste mål, »opstart«/»stopværn«) eller (None, None)."""
        now = time.time()
        comp = self.hass.states.get(s.eid(CONF_COMPRESSOR) or "")
        if comp is None or comp.state not in ("on", "off"):
            return None, None
        dhw = self.hass.states.get(s.eid(CONF_DHW) or "")
        if (dhw is not None and dhw.state == "on") or now - (_num(hs.get("dhw_off_t")) or 0) < DHW_AFTER_S:
            return None, None
        dfs = self.hass.states.get(s.eid(CONF_DEFROST) or "")
        df_on = dfs is not None and dfs.state == "on"
        df_age = now - (_num(hs.get("df_off_t")) or 0)
        on = comp.state == "on"
        on_t = max(_num(hs.get("comp_on_t")) or 0, _num(hs.get("comp_since")) or 0) if on else 0
        start = (not on) or now - on_t < START_PHASE_S or df_on or df_age < START_PHASE_S
        vals: list[tuple[float, str]] = []
        if start and (v := start_flow(self._state_num(s.eid(CONF_RETURN)), self._start_k(hs), SOFT_OVER_RETURN_K, SOFT_FLOOR)) is not None:
            vals.append((v, "opstart"))
        if on and not df_on and df_age >= 300 and (out := self._state_num(s.eid(CONF_FLOW))) is not None:
            vals.append((float(ceil(out - ANTI_STOP_GAP - 1e-9)), "stopværn"))
        if not vals:
            return None, None
        return max(vals)

    async def _on_replan(self, _now: datetime) -> None:
        try:
            async with self._replan_lock:
                await self.async_replan()
        except Exception as err:  # noqa: BLE001 – planen må aldrig vælte integrationen
            _LOGGER.exception("Planlægning fejlede")
            self.plan_meta = {"updated": time.time(), "inputs_ok": False, "missing": [f"fejl: {err}"]}
            self._notify()

    def _parse_local(self, s: str) -> datetime | None:
        d = dt_util.parse_datetime(s)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt_util.get_default_time_zone())
        return d

    # ------------------------------------------------------------ luft-luft-styring
    def air_climate(self, s: SourceState) -> str | None:
        """Luft-luft-kildens klimaentitet: den valgte, ellers den på samme enhed som enhedens føler eller elmåler."""
        if s.kind == SOURCE_HYDRONIC:
            return None
        if eid := s.conf.get(CONF_CLIMATE):
            return eid
        reg = er.async_get(self.hass)
        for key in (CONF_UNIT_TEMP, CONF_ELECTRICITY):
            eid = s.conf.get(key)
            if not eid:
                continue
            if eid.startswith("climate."):
                return eid
            e = reg.async_get(eid)
            if e and e.device_id:
                for x in er.async_entries_for_device(reg, e.device_id):
                    if x.domain == "climate":
                        return x.entity_id
        return None

    def air_assist(self, s: SourceState) -> bool | None:
        """Kører ventilationen, der fordeler varmen? None uden en valgt ventilation."""
        eid = s.conf.get(CONF_ASSIST)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in ("unknown", "unavailable"):
            return None
        on = st.state.lower() not in ("off", "fra", "0", "false", "slukket")
        if on and (sp := s.conf.get(CONF_ASSIST_SPEED)):
            v = self._state_num(sp)
            on = v is not None and v > 0
        return on

    def air_options(self, s: SourceState, key: str) -> list[str]:
        """Valgmuligheder for en luft-luft-indstilling, læst fra klimaentiteten."""
        st = self.hass.states.get(self.air_climate(s) or "")
        a = st.attributes if st else {}
        if key in ("air_fan_day", "air_fan_night"):
            return fan_options(a.get("fan_modes")) or ["–"]
        if key == "air_swing_v":
            return [UNCHANGED, *(a.get("swing_modes") or [])]
        if key == "air_swing_h":
            return [UNCHANGED, *(a.get("swing_horizontal_modes") or [])]
        if key == "air_start":
            return list(AIR_START_OPTIONS)
        return []

    def air_setting(self, s: SourceState, key: str) -> Any:
        """En luft-luft-indstilling for kilden (gemt i Varmemodel), ellers standarden."""
        v = self.settings.get(f"{s.sid}.{key}")
        if v is not None:
            return v
        if key == AIR_SWITCH:
            return False
        for k, dflt in AIR_TIMES:
            if k == key:
                return dflt
        if key == "air_start":
            return AIR_START_OPTIONS[0]
        fans = self.air_options(s, "air_fan_day")
        if key == "air_fan_day":
            return fans[-1]
        if key == "air_fan_night":
            return fans[-2] if len(fans) > 1 else fans[-1]
        return UNCHANGED

    def _rise_sync(self, rooms: dict[str, str], seconds: int = 1800) -> dict[str, float]:
        """Rummenes målte stigning (°C/t) de seneste ``seconds`` (mindste kvadrater, inkl. værdien nu)."""
        end = dt_util.utcnow()
        start = end - timedelta(seconds=seconds)
        res = history.get_significant_states(self.hass, start, end, list(rooms.values()), include_start_time_state=True,
                                             significant_changes_only=False, no_attributes=True)
        out: dict[str, float] = {}
        for name, eid in rooms.items():
            pts = [(max(z.last_updated, start).timestamp(), v) for z in res.get(eid, []) if (v := _num(z.state)) is not None]
            now_v = self._state_num(eid)
            if now_v is not None:
                pts.append((end.timestamp(), now_v))
            if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= seconds * 0.6:
                n = len(pts)
                mt, mv = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
                den = sum((p[0] - mt) ** 2 for p in pts)
                out[name] = round(sum((p[0] - mt) * (p[1] - mv) for p in pts) / den * 3600, 3) if den > 0 else 0.0
        return out

    def air_boost_active(self, s: SourceState) -> bool:
        """»Jeg fryser« for kilden: slået til for under »jeg fryser gælder højst« timer siden."""
        since = float(self.settings.get(f"{s.sid}.{AIR_BOOST}") or 0)
        return since > 0 and time.time() - since < self.setting(CONF_BOOST_HOURS) * 3600

    async def async_set_air(self, s: SourceState, key: str, value: Any) -> None:
        self.settings[f"{s.sid}.{key}"] = value
        await self._save()
        await self.async_replan()

    def _air_step(self, s: SourceState, clim: str, p: Any, show: list[PlanRoom], on: bool, blocked: bool, now: float,
                  p_w: float | None = None) -> dict[str, Any]:
        """Stigen for en luft-luft-kilde: hvilket trin (blæser + setpunkt) planens »varme« skal køre på.

        Lærer samtidig forbrug pr. trin og starttrin pr. udetemperatur ud fra det, der faktisk kører –
        også når Varmemodel ikke selv styrer (så tallene er klar, når den gør)."""
        st = self.hass.states.get(clim)
        attrs = st.attributes if st else {}
        fans = fan_options(attrs.get("fan_modes"))
        quiet = "quiet" in (attrs.get("preset_modes") or [])
        lt = dt_util.now()
        night = is_night(lt.hour * 60 + lt.minute, str(self.air_setting(s, "air_night_start")), str(self.air_setting(s, "air_night_end")))
        cap = self.air_setting(s, "air_fan_night" if night else "air_fan_day")
        # Flydende setpunkter: fra det højeste gulv blandt rummene (mindst klimaentitetens laveste) og 3 °C op
        floors = [r.t_min for r in show if r.t_min > -50]
        stp = float(attrs.get("target_temp_step") or 0.5)
        sp_min = max(float(attrs.get("min_temp") or 16), round((max(floors) if floors else 20.0) / stp) * stp)
        sp_max = min(float(attrs.get("max_temp") or 30), sp_min + 3.0)
        ladder = build_ladder(fans, cap, sp_min, sp_max, stp, quiet)
        # Trin navngives relativt til laveste setpunkt (det flyder med gulvene), så læring overlever, at gulvene ændres
        rel = lambda fan, sp: f"{fan} +{float(sp) - sp_min:.1f}"  # noqa: E731
        label = lambda x: rel(x.preset or x.fan, x.setpoint)  # noqa: E731
        a = self.air.setdefault(s.sid, {})
        pre, blk = preload_per_room([{"blocked": h["blocked"]} for h in p.hours], p.rooms, 3.0)
        # Må rummene gå under gulvet i røde timer (brugerens valg), forvarmes der tilsvarende mindre
        allow = self.setting(CONF_RED_ALLOWANCE)
        pre = {k: round(v - allow, 2) for k, v in pre.items() if v - allow > 0}
        meas = [r for r in show if r.t_min > -50 and not r.held]
        margins = {r.name: round(r.t_now - r.t_min - pre.get(r.name, 0.0), 2) for r in meas}
        # Ned kun når alle rum er over gulv + forvarmning + rummodellens egen usikkerhed (ingen fast margen)
        down_ok = bool(meas) and all(r.t_now - r.t_min - pre.get(r.name, 0.0) >= r.margin for r in meas)
        t_out = p.hours[0]["t_out"] if p.hours else None
        b = band(t_out)
        starts = a.setdefault("start", {})
        idx_of = {label(x): i for i, x in enumerate(ladder)}
        w_l = a.get("w", {})
        el_now = p.hours[0].get("el_kw") if p.hours else None
        eff_l = a.get("eff", {})
        vent = self.air_assist(s)
        vkey = "" if vent is None else ("|vent til" if vent else "|vent fra")
        # Pr. udetemperatur-bånd (5 °C): ved kulde trækker samme trin mere og giver mindre varme pr. kWh el.
        # Uden nok data i båndet bruges trinets samlede tal (alle udetemperaturer).
        bkey = f"@{b}" if b is not None else ""
        wb = lambda k: w_l.get(k + bkey, w_l.get(k))  # noqa: E731
        lw = [wb(label(x)) for x in ladder]
        # Virkningen læres pr. ventilations-tilstand: ventilationen ændrer, hvor meget af varmen rummene får
        eb = lambda k: eff_per_kwh(eff_l, k + bkey) if eff_per_kwh(eff_l, k + bkey) is not None else eff_per_kwh(eff_l, k)  # noqa: E731
        le = [eb(label(x) + vkey) for x in ladder]
        # Mest varme pr. kWh el, når trinenes virkning er lært; ellers laveste trin, hvis forbrug dækker planens el
        tgt = efficient_step(lw, le, el_now * 1000 if el_now else None)
        if tgt is None:
            tgt = target_step(lw, el_now * 1000 if el_now else None)
        boost = self.air_boost_active(s)
        if boost:
            i, changed, down_since, reason = len(ladder) - 1, now, None, "jeg fryser – øverste trin"
        elif on and not blocked:
            start = idx_of.get(starts.get(b)) if b else None
            if start is None:
                start = tgt
            prev = a.get("step") if a.get("on") else None
            n_steps, up_every, rising = len(ladder), None, None
            if self.air_setting(s, "air_start") == "rolig":
                # Rolig opstart: højst ét trin pr. halve time og aldrig over de effektive trin – også når et rum er under
                # gulvet (så tager det 30–60 min. længere). Kun »Jeg fryser« bruger øverste trin.
                # Rolig opstart: start højst midt på stigen, hold hvert trin 30 min., og gå kun op, hvis det kolde rum
                # ikke stiger mindst AIR_SOFT_MIN_RISE °C/t (målt) – så prøves et trin af, før der skrues op
                # Lært virkning pr. trin (kW el, °C pr. kWh el) til planen; med mindst tre lærte trin vælger planen selv
                # lavere trin og flere timer, og den faste midte bruges ikke
                a["eff_curve"] = sorted({(round(lw[k] / 1000, 3), round(le[k], 4)) for k in range(min(len(lw), len(le)))
                                         if lw[k] and le[k]})
                learned = len(a["eff_curve"]) >= 3
                mid = len(ladder) - 1 if learned else max(1, round((len(ladder) - 1) * AIR_SOFT_SHARE))
                if tgt is not None:
                    tgt = min(tgt, mid)
                # Bund: under gulvet køres aldrig under planens trin (dets forbrug dækker planens varme) – lavere trin kan ikke
                # følge med varmetabet (fx et rum med stort varmetab), så de prøves ikke
                under = bool(margins) and min(margins.values()) < 0
                floor_step = tgt if (tgt is not None and under) else None
                start = mid if start is None else min(start, mid)
                if floor_step is not None:
                    start = max(start, floor_step)
                a["soft_cap_w"] = lw[mid] if mid < len(lw) else None  # planen regner med midterste trin pr. time
                up_every = AIR_SOFT_UP_EVERY
                rising = self._air_rise.get(s.sid) or {}
                # Højere end det afprøvede: gå ned til midten (eller det trin, der er vist nødvendigt) og prøv det i 30 min.
                tested = a.get("soft_tested")
                ceiling = max(mid, tested) if tested is not None else mid
                if prev is not None and prev > ceiling:
                    prev = ceiling
                    a["changed"] = now
                    a["soft_note"] = f"rolig opstart: prøver trin {ceiling} i 30 min., før der skrues op"
                elif prev is not None and floor_step is not None and prev < floor_step:
                    prev = floor_step
                    a["changed"] = now
                    a["soft_note"] = f"rummene er under gulvet – prøver planens trin {floor_step} i 30 min."
            i, changed, down_since, reason = choose_step(prev, n_steps, margins, down_ok,
                                                         now, float(a.get("changed", 0.0)), a.get("down_since"), start, tgt,
                                                         **({"up_every": up_every, "rising": rising, "min_rise": AIR_SOFT_MIN_RISE}
                                                            if up_every else {}))
            if up_every:
                if prev is not None and i is not None and i > prev:
                    a["soft_tested"] = i  # trinnet under var ikke nok: dette trin er vist nødvendigt
                elif margins and min(margins.values()) >= 0:
                    a["soft_tested"] = None  # rummene er nået op: næste gang prøves fra midten igen
                if note := a.pop("soft_note", None):
                    reason = f"{note} – {reason}"
        else:
            i, changed, down_since = a.get("step"), float(a.get("changed", 0.0)), None
            reason = "blokeret – slukket" if blocked else "planen varmer ikke nu"
        a.update(on=bool((on and not blocked) or boost), step=i, changed=changed, down_since=down_since)
        if not a["on"]:
            a["soft_tested"] = None  # ny start prøver fra midten igen
        if a["on"] and not boost and i is not None and margins and min(margins.values()) >= 0 and not pre and b and now - changed >= LEARN_HOLD:
            starts[b] = label(ladder[min(i, len(ladder) - 1)])
        # Det trin, der faktisk kører nu (matches på blæser og setpunkt), og forbruget pr. trin ud fra elmåleren
        act = None
        if st is not None and st.state == "heat":
            if quiet and attrs.get("preset_mode") == "quiet":
                act = rel("quiet", attrs.get("temperature") or sp_min)
            else:
                act = rel(attrs.get("fan_mode"), attrs.get("temperature") or sp_min)
        if act != (a.get("act") or [None])[0]:
            a["act"] = [act, now]
        e_eid = s.eid(CONF_ELECTRICITY)
        e_now = self._state_num(e_eid)
        unit = str((self.hass.states.get(e_eid).attributes if e_eid and self.hass.states.get(e_eid) else {}).get("unit_of_measurement") or "kWh")
        prev_e = a.get("e")
        df = s.eid(CONF_DEFROST)
        defrosting = bool(df and (x := self.hass.states.get(df)) and x.state == "on")
        if (act and prev_e and 600 <= now - prev_e[1] <= 1300 and prev_e[2] == act
                and now - a["act"][1] >= W_SETTLE + (now - prev_e[1]) and not defrosting
                and (p_w is not None or (e_now is not None and prev_e[0] is not None and e_now >= prev_e[0]))):
            # Effektmåleren (W, snit af kvarteret) er præcis; energimåleren er grov (fx 0,1 kWh) og bruges kun uden den
            if p_w is not None:
                kwh, w_meas = p_w * (now - prev_e[1]) / 3.6e6, p_w
            else:
                kwh = (e_now - prev_e[0]) / (1000.0 if unit.lower() == "wh" else 1.0)
                w_meas = kwh * 3600 / (now - prev_e[1]) * 1000
            if w_meas > 50:
                a["w"] = learn_w(learn_w(a.get("w", {}), act, w_meas), act + bkey, w_meas) if bkey else learn_w(a.get("w", {}), act, w_meas)  # type: ignore[arg-type]
            # Virkning: rummenes temperaturændring ud over afkøling og opdrift, pr. kWh el (kun når trinnet har kørt længe)
            tp = a.get("tp") or {}
            if (kwh > 0.02 and now - a["act"][1] >= EFF_HOLD + (now - prev_e[1]) and t_out is not None
                    and (len(prev_e) < 4 or prev_e[3] == vkey)):
                gains = [room_gain(tp[r.name], r.t_now, (now - prev_e[1]) / 3600, t_out, r.cool_per_hk, r.free_per_h, r.neighbours)
                         for r in meas if r.name in tp]
                if gains:
                    g_, k_ = sum(gains) / len(gains), act + (prev_e[3] if len(prev_e) > 3 else "")
                    a["eff"] = eff_update(a.get("eff", {}), k_, g_, kwh)
                    if bkey:
                        a["eff"] = eff_update(a["eff"], k_ + bkey, g_, kwh)
        a["e"] = [e_now, now, act, vkey] if (e_now is not None or p_w is not None) else None
        a["tp"] = {r.name: r.t_now for r in meas}
        w = a.get("w", {})
        cur = ladder[min(i, len(ladder) - 1)] if i is not None else None
        a["view"] = {"klima": clim, "trin": i, "mål_trin": tgt, "el_mål_w": round(el_now * 1000) if el_now else None, "nat": night, "maks_blæser": cap, "styrer": bool(self.air_setting(s, AIR_SWITCH)), "grund": _dk_txt(reason),
                     "kører_nu": act, "blokering": blk, "forvarmning": pre or None, "margen": margins or None,
                     "ude_bånd": b,
                     "trin_liste": [{"trin": k, "blæser": x.preset or x.fan, "setpunkt": x.setpoint, "w": wb(label(x)),
                                     "grader_pr_kwh_el": None if (ev := eb(label(x) + vkey)) is None else round(ev, 3),
                                     "målinger": int((a.get("eff", {}).get(label(x) + vkey) or [0, 0, 0])[2])} for k, x in enumerate(ladder)],
                     "ventilation": None if vent is None else ("til" if vent else "fra"),
                     "ventilation_entitet": s.conf.get(CONF_ASSIST), "ventilation_hastighed_entitet": s.conf.get(CONF_ASSIST_SPEED),
                     "ventilation_virkning_pct": self._vent_effect(a.get("eff", {})),
                     "starttrin": dict(starts), "skiftet": _ts_local_iso(changed) if changed else None}
        out: dict[str, Any] = {"step": i}
        if a["on"] and cur is not None:
            out.update(setpoint=cur.setpoint, fan_mode=cur.fan, preset=cur.preset)
        if boost:
            presets = attrs.get("preset_modes") or []
            out.update(on=True, preset="powerful" if "powerful" in presets else out.get("preset"))
        a["view"]["jeg_fryser"] = boost
        return out

    @staticmethod
    def _vent_effect(eff: dict[str, list[float]]) -> float | None:
        """Hvor meget mere varme rummene får pr. kWh el med ventilationen kørende (%), over trin målt begge veje."""
        ratios = []
        for k in eff:
            if k.endswith("|vent til"):
                base = k[: -len("|vent til")]
                on, off = eff_per_kwh(eff, k), eff_per_kwh(eff, base + "|vent fra")
                if on is not None and off is not None and off > 0:
                    ratios.append(on / off)
        return round((sum(ratios) / len(ratios) - 1) * 100) if ratios else None

    async def _flow_apply(self, s: SourceState, hours: list[dict[str, Any]], blocked: bool, pre_start: bool = False) -> None:
        """Skriv planens fremløbsmål til varmepumpen (P1: Varmemodel styrer kun fremløbet; tænd/sluk og termostater
        styres stadig andetsteds). Kun når varmepumpen står i varme-tilstand, aldrig i blokerede timer, kun ved en
        ændring på mindst 1 °C og højst FLOW_MAX_WRITES gange pr. døgn. Uden planlagt varme nu bruges næste planlagte
        fremløb, så en varmepumpe, der startes af anden styring, kører med planens lave fremløb."""
        if self._stopped:
            return
        eid = s.conf.get(CONF_FLOW_TARGET)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in ("unavailable", "unknown") or blocked:
            return
        dom = eid.split(".", 1)[0]
        if dom == "climate" and st.state != "heat" and not pre_start:
            return  # fremløbsmålet virker kun i varme-tilstand (auto følger varmepumpens egen kurve); før en blød start skrives det dog
        want = next((x["flow"] for x in hours if x.get("flow") is not None and not x.get("blocked")), None)
        cmd = self.commands.get(s.sid) or {}
        if cmd.get("flow") is not None:
            want = cmd["flow"]
        if want is None:
            return
        # Blød start: lige efter tænding løftes målet gradvist (retur + 4 °C, mindst 20 °C, +SOFT_STEP_K pr. kvarter),
        # så kompressoren ikke går til fuld effekt mod et koldt gulv (bedre COP, færre afrimninger i kulde)
        hs = self.air.setdefault(f"hydro:{s.sid}", {})
        if self.settings.get(f"{s.sid}.{SOFT_START}", "test") == "lavest" and (pre_start or st.state == "heat" or dom != "climate"):
            plan_now = want
            want = await self._low_flow(s, hs, hs.get("low_loft") or want, pre_start)
            if hs.get("low_raise") and plan_now is not None:
                want = max(want, min(plan_now, hs.get("low_loft") or plan_now))  # planens forvarmning går forud
            # Opstartsløft og stopværn: varmepumpens egen termostat må ikke nå at stoppe kompressoren (start/stop slider).
            # Søgningens mål er stadig det laveste, der holder rummene; værnet løfter kun det, der skrives – aldrig over loftet
            if pre_start:
                g, why = start_flow(self._state_num(s.eid(CONF_RETURN)), self._start_k(hs), SOFT_OVER_RETURN_K, SOFT_FLOOR), "opstart"
            else:
                g, why = self._guard_value(s, hs)
            hs["guard"] = None
            cap = max(hs.get("low_loft") or plan_now or want, want)
            if g is not None and min(g, cap) > want:
                want = min(g, cap)
                hs["guard"] = {"mål": want, "grund": why}
            hs["soft"] = True
        elif hs.get("soft") and hs.get("start_t"):
            if hs.get("ramp0") is None:
                ret = self._state_num(s.eid(CONF_RETURN))
                hs["ramp0"] = max((ret + SOFT_OVER_RETURN_K) if ret is not None else want - 3 * SOFT_STEP_K, SOFT_FLOOR)
            ramp = hs["ramp0"] + SOFT_STEP_K * int((time.time() - hs["start_t"]) // 900)
            if ramp < want:
                want = ramp
            else:
                hs["soft"] = False  # blød start færdig: planens fremløb
        if not hs.get("soft"):
            hs["soft_now"] = None
        a = st.attributes
        lo, hi = _num(a.get("min_temp", a.get("min"))), _num(a.get("max_temp", a.get("max")))
        # Entitetens trin (fx hele grader – nogle varmepumper runder ellers et halvt fremløbsmål ned); uden trin: hele grader
        stp = _num(a.get("target_temp_step", a.get("step"))) or 1.0
        want = float(min(max(round(want / stp) * stp, lo if lo is not None else 20.0), hi if hi is not None else 45.0))
        if hs.get("soft"):
            hs["soft_now"] = want  # det fremløb, blød start kører med nu (vises på kortet)
        cur = _num(a.get("temperature")) if dom == "climate" else _num(st.state)
        f = self.air.setdefault(f"flow:{s.sid}", {})
        day = dt_util.now().date().isoformat()
        if f.get("day") != day:
            f["day"], f["writes"] = day, 0
        if cur is not None and abs(cur - want) < 1.0:
            return
        if f["writes"] >= FLOW_MAX_WRITES:
            _LOGGER.warning("%s: over %s skrivninger af fremløbet i dag – Varmemodel holder pause", s.name, FLOW_MAX_WRITES)
            return
        try:
            if dom == "climate":
                await self.hass.services.async_call("climate", "set_temperature", {"entity_id": eid, "temperature": want}, blocking=True)
            else:
                await self.hass.services.async_call(dom, "set_value", {"entity_id": eid, "value": want}, blocking=True)
            f["writes"] += 1
            f["last"] = {"t": time.time(), "flow": want}
            f["hist"] = (f.get("hist") or [])[-39:] + [[round(time.time()), want]]  # målet på et givent tidspunkt (korte kørsler)
            self._notify()  # »seneste« på kontakten med det samme – ellers ser vagten et nyt mål mod en gammel skrivning
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: kunne ikke skrive fremløbet", s.name)

    async def _hydro_apply(self, s: SourceState, want_on: bool, blocked: bool, hours: list[dict[str, Any]]) -> None:
        """Tænd (varme) og sluk den vandbårne varmepumpe efter den anbefalede styring (mindst 60 min. tændt, 30 min. slukket,
        højst én ændring pr. 15 min. – reglerne i command.py). Blokeret: sluk straks. Højst HYDRO_MAX_FLIPS skift pr. døgn."""
        if self._stopped:
            return
        eid = s.conf.get(CONF_ONOFF_TARGET)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in ("unavailable", "unknown"):
            return
        h = self.air.setdefault(f"hydro:{s.sid}", {})
        day = dt_util.now().date().isoformat()
        if h.get("day") != day:
            h["day"], h["flips"] = day, 0
        is_on = st.state != "off"
        if blocked:
            want_on = False
        if want_on == is_on:
            return
        if h["flips"] >= HYDRO_MAX_FLIPS and not want_on and not blocked:
            # Loftet skåner kompressoren mod mange korte kørsler: efter loftet slukker Varmemodel kun ved blokering – men
            # tænder stadig, så huset aldrig står uden varme resten af døgnet (kodegennemgang 23/11)
            _LOGGER.warning("%s: over %s tænd/sluk i dag – Varmemodel slukker kun ved blokering resten af døgnet", s.name,
                            HYDRO_MAX_FLIPS)
            return
        soft, mode = False, None
        if want_on:
            # Ny start: blød eller almindelig (test: hver anden) – vurderes efter SOFT_EVAL_S. Ved blød start skrives det lave
            # fremløbsmål FØR tænding, så varmepumpen ikke de første minutter går mod et gammelt mål
            mode = self.settings.get(f"{s.sid}.{SOFT_START}", "test")
            soft = mode in ("til", "lavest") or (mode == "test" and not h.get("last_soft", False))
            h.update(start_t=time.time(), soft=soft, ramp0=None, soft_now=None, low=None, low_t=None, start_short=0, start_ok=False)
            if soft and s.conf.get(CONF_FLOW_TARGET):
                try:
                    await self._flow_apply(s, hours, False, pre_start=True)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Blød start: kunne ikke skrive fremløbet før tænding", exc_info=True)
        try:
            await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "heat" if want_on else "off"},
                                                blocking=True)
            h["flips"] += 1
            h["last"] = {"t": time.time(), "on": want_on}
            self._notify()
            if want_on:
                if mode == "test":
                    h["last_soft"] = soft
                h.setdefault("starts", []).append({"t": round(time.time()), "soft": soft, "lavest": mode == "lavest",
                                                   "t_out": self._state_num(self._id(CONF_OUTDOOR))})
                h["starts"] = h["starts"][-80:]
            else:
                if h.get("low") is not None:
                    h["low_last"] = h["low"]  # næste plan og start regner med det sidste fungerende mål
                h["start_t"], h["soft"], h["soft_now"], h["low"] = None, False, None, None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: kunne ikke %s varmepumpen", s.name, "tænde" if want_on else "slukke")
            if want_on:
                h["start_t"], h["soft"], h["soft_now"] = None, False, None
            return
        if want_on and s.conf.get(CONF_FLOW_TARGET):
            # Varmepumpen går i varme-tilstand om lidt: skriv planens fremløb, så den ikke kører på et gammelt mål
            async def _later(_now: Any) -> None:
                await self._flow_apply(s, hours, False)
            async_call_later(self.hass, 120, _later)

    def _low_rooms_sync(self, s: SourceState) -> list[dict[str, Any]]:
        """Kildens rum i planen: margen over gulvet nu og målt tendens (°C/t) de seneste 2 timer."""
        rooms = []
        for r in self.rooms_cfg:
            if self.room_source(r) is not s or not r["in_plan"]:
                continue
            t_min = self._state_num(r["min_entity"]) if r["min_entity"] else (_num(r["min"]) if r["min"] is not None else None)
            t = self._state_num(r["key"])
            if t_min is None or t is None:
                continue
            rooms.append({"navn": r["name"] or r["key"], "e": r["key"], "t": t, "gulv": t_min, "margen": round(t - t_min, 2)})
        if not rooms:
            return rooms
        end = dt_util.utcnow()
        start = end - timedelta(hours=2)
        res = history.get_significant_states(self.hass, start, end, [x["e"] for x in rooms], include_start_time_state=True,
                                             significant_changes_only=False, no_attributes=True)
        for x in rooms:
            pts = [(max(z.last_updated, start).timestamp(), v) for z in res.get(x["e"], []) if (v := _num(z.state)) is not None]
            if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 3600:
                # Mindste kvadraters hældning (følerens 0,1-trin udjævnes), inkl. værdien nu
                pts.append((end.timestamp(), x["t"]))
                n = len(pts)
                mt = sum(p[0] for p in pts) / n
                mv = sum(p[1] for p in pts) / n
                den = sum((p[0] - mt) ** 2 for p in pts)
                x["tendens"] = round(sum((p[0] - mt) * (p[1] - mv) for p in pts) / den * 3600, 3) if den > 0 else 0.0
            else:
                x["tendens"] = 0.0
            x["om_3t"] = round(x["margen"] + 3 * x["tendens"], 2)
        return rooms

    def _low_cap(self, s: SourceState, hs: dict[str, Any], t_out: float | None) -> float | None:
        """Fremløbet, planen skal regne med under »søg laveste«: målet nu; ellers lært laveste for udetemperaturen, det sidste
        mål eller retur + SOFT_OVER_RETURN_K (det, næste start begynder ved)."""
        if hs.get("low") is not None:
            return float(hs["low"])
        if (lr := (hs.get("low_learned") or {}).get(band(t_out) or "?")) is not None:
            return float(lr)
        if hs.get("low_last") is not None:
            return float(hs["low_last"])
        return start_flow(self._state_num(s.eid(CONF_RETURN)), SOFT_OVER_RETURN_K, SOFT_OVER_RETURN_K, SOFT_FLOOR)

    def _target_at(self, s: SourceState, t: float) -> float | None:
        """Fremløbsmålet, der stod i varmepumpen på tidspunktet t (Varmemodels egne skrivninger; ellers målet nu)."""
        before = [v for ts, v in (self.air.get(f"flow:{s.sid}") or {}).get("hist") or [] if ts <= t]
        return float(before[-1]) if before else self._target_now(s)

    @staticmethod
    def _start_k(hs: dict[str, Any]) -> float:
        """Startløftet over returen: det lærte (korte kørsler i opstarten), aldrig under standarden."""
        return max(_num(hs.get("start_k")) or SOFT_OVER_RETURN_K, SOFT_OVER_RETURN_K)

    def _target_now(self, s: SourceState) -> float | None:
        """Fremløbsmålet, der står i varmepumpen nu (climate: attributten temperature; number: tilstanden)."""
        st = self.hass.states.get(s.conf.get(CONF_FLOW_TARGET) or "")
        if st is None:
            return None
        return _num(st.attributes.get("temperature")) if st.domain == "climate" else _num(st.state)

    def _comp_on_since_sync(self, s: SourceState) -> float | None:
        """Hvornår kompressoren sidst gik fra slukket til tændt (de seneste 8 timer); None, når den er slukket.
        Utilgængelig/ukendt (fx under en genstart) springes over, så en genstart ikke tæller som en ny start."""
        comp = s.eid(CONF_COMPRESSOR)
        if not comp:
            return None
        end = dt_util.utcnow()
        start = end - timedelta(hours=8)
        res = history.get_significant_states(self.hass, start, end, [comp], include_start_time_state=True,
                                             significant_changes_only=False, no_attributes=True)
        since, last = None, None
        for z in res.get(comp, []):
            if z.state in ("unavailable", "unknown"):
                continue
            if z.state == "on" and last != "on":
                since = max(z.last_changed, start).timestamp()
            last = z.state
        return since if last == "on" else None

    def _comp_stops_sync(self, s: SourceState, since: float) -> list[dict[str, Any]]:
        """Kompressorstop siden ``since`` uden varmt vand eller afrimning (se ``comp_stops``) – historikken hentes fra 30 min.
        før, så en afrimning eller kompressorstart lige før vinduet ses, og returen ved kompressorstart kan læses."""
        comp, dhw, df, ret = s.eid(CONF_COMPRESSOR), s.eid(CONF_DHW), s.eid(CONF_DEFROST), s.eid(CONF_RETURN)
        if not comp:
            return []
        ids = [e for e in (comp, dhw, df, ret) if e]
        res = history.get_significant_states(self.hass, dt_util.utc_from_timestamp(since - 1800), dt_util.utcnow(), ids,
                                             include_start_time_state=True, significant_changes_only=False, no_attributes=True)
        rows = {e: [(z.last_updated.timestamp(), z.state) for z in res.get(e, [])] for e in ids}
        return comp_stops(rows, comp, dhw, df, since, ret)

    async def _low_flow(self, s: SourceState, hs: dict[str, Any], plan: float, pre_start: bool = False) -> float:
        """Søg laveste fremløb: start ved retur + 4 °C (mindst 20 °C) og ændr målet stille og roligt efter rummene –
        +2 K/30 min., når et rum er under gulvet og ikke på vej op; +1 K/t, når et rum er på vej under gulvet inden for
        3 timer; −1 K hver 2. time, når alle rum er mindst 0,5 °C over gulvet og stabile. Aldrig over planens fremløb."""
        now = time.time()
        t_out = self._state_num(self._id(CONF_OUTDOOR))
        bk = band(t_out) or "?"
        learned = (hs.get("low_learned") or {}).get(bk)
        k = self._start_k(hs)
        if hs.get("low") is None or pre_start:
            # Søgningen starter lavt (retur + 3 °C); opstartsløftet (det lærte) lægges kun på det, der skrives i opstarten
            ret = self._state_num(s.eid(CONF_RETURN))
            v = start_flow(ret, SOFT_OVER_RETURN_K, SOFT_OVER_RETURN_K, SOFT_FLOOR)
            if v is None:
                v = float(round(max(plan - 3 * SOFT_STEP_K, SOFT_FLOOR)))
            hs.update(low=v, low_t=now, low_hold_since=now, start_t=hs.get("start_t") or now, low_step=0.0,
                      low_status="start", low_why=f"start: retur + {SOFT_OVER_RETURN_K:.0f} °C"
                      + (f" (opstartsløft retur + {_k_txt(k)} °C, lært af varmepumpens opstart)" if k > SOFT_OVER_RETURN_K else "")
                      + (f" (lært laveste ved {_band_txt(bk)} ude: {learned:.0f} °C – løftes 2 °C pr. kvarter derop)" if learned else ""))
            self._low_log(hs, now, hs["low"], plan, hs["low_why"])
            return min(plan, hs["low"])
        try:
            rooms = await get_instance(self.hass).async_add_executor_job(self._low_rooms_sync, s)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Søg laveste fremløb: rummene kunne ikke læses", exc_info=True)
            rooms = []
        hs["low_rooms"] = rooms
        since = now - (hs.get("low_t") or 0)
        low, step, why, status = hs["low"], 0.0, None, "holder"
        try:
            stops = await get_instance(self.hass).async_add_executor_job(
                self._comp_stops_sync, s, max(now - 1800, (hs.get("start_t") or now) + 300))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Søg laveste fremløb: kompressorstop kunne ikke læses", exc_info=True)
            stops = []
        short = [x for x in stops if x["run"] is not None and x["run"] <= START_PHASE_S]
        # Stop efter længere drift (ikke opstart, ikke lige efter en afrimning) siden målet sidst blev ændret: målet er under
        # varmepumpens laveste ydelse – søgningen hæver (stopværnet skulle normalt forhindre dem)
        long_ = [x for x in stops if not (x["run"] is not None and x["run"] <= START_PHASE_S) and x.get("df") != "efter"
                 and x["t"] >= (hs.get("low_t") or 0)]
        hs["low_stops"] = len(long_)
        new = [x for x in short if x["t"] > (_num(hs.get("short_seen")) or 0)]
        if new:
            # Kort kørsel (stop i opstarten): opstarten skød fremløbet over målet + varmepumpens margen, før flowet var oppe –
            # lær løftet over returen ved den start (målet dengang − returen + 1 K). Opstartsløftet bruger det straks;
            # søgningens eget mål hæves ikke (det er opstarten, ikke rummene, der kræver mere)
            for x in new:
                x["mål"] = self._target_at(s, x["t"])
            k = learn_start_k(k, new, SOFT_OVER_RETURN_K)
            last = max(new, key=lambda x: x["t"])
            hs.update(start_k=k, start_short=(hs.get("start_short") or 0) + len(new), short_seen=last["t"],
                      short_last={"t": round(last["t"]), "min": round(last["run"] / 60, 1), "mål": last.get("mål"),
                                  "retur": last.get("ret0"), "efter_afrimning": last.get("df") == "efter"})
        elif (k > SOFT_OVER_RETURN_K and not hs.get("start_ok") and not hs.get("start_short") and hs.get("start_t")
              and (cs := hs.get("comp_since")) and cs >= hs["start_t"] - 60 and now - cs >= START_PHASE_S):
            # Starten kom igennem opstarten ved det lærte løft: sænk det lidt, så det prøver lavere igen (fx efter en ændring
            # af varmepumpens flow) – en ny kort kørsel løfter det straks tilbage
            k = hs["start_k"] = round(max(SOFT_OVER_RETURN_K, k - START_K_DECAY), 2)
            hs["start_ok"] = True
        cold = [x for x in rooms if x["margen"] < 0 and x["tendens"] < 0.05]
        going = sorted((x for x in rooms if x["om_3t"] < LOW_RESERVE), key=lambda x: x["om_3t"])
        if long_ and low < plan:
            status = "hæver"
            if since >= LOW_URGENT_HOLD:
                step, why = 1.0, "kompressoren stoppede efter længere drift – målet var under varmepumpens laveste ydelse"
        elif learned and low < learned - 1 and hs.get("low_status") in ("start", "løfter") and not going and not cold:
            if since >= 900:
                step, why, status = 2.0, f"blød start mod lært laveste {learned:.0f} °C", "løfter"
            else:
                status = "løfter"
        elif cold:
            x = min(cold, key=lambda r: r["margen"])
            status = "hæver"
            if since >= LOW_URGENT_HOLD:
                step, why = 2.0, f"{x['navn']} er {abs(x['margen']):.1f} °C under gulvet og ikke på vej op"
        elif going:
            x = going[0]
            status = "hæver"
            if since >= LOW_UP_HOLD:
                step, why = 1.0, f"{x['navn']} er på vej under gulvet (om 3 t ca. {x['om_3t']:+.1f} °C)"
        elif rooms and all(x["margen"] >= LOW_DOWN_MARGIN and x["tendens"] >= -0.02 for x in rooms):
            status = "sænker"
            if since >= LOW_DOWN_HOLD:
                step, why = -1.0, f"alle rum mindst {LOW_DOWN_MARGIN:.1f} °C over gulvet og stabile"
        new = min(max(low + step, SOFT_FLOOR), plan)
        hs["low_step"] = 0.0
        if step and new != low:
            hs.update(low=new, low_t=now, low_why=why, low_hold_since=now, low_step=new - low)  # det nye mål skal holde 3 t, før det læres
            self._low_log(hs, now, new, plan, why)
        hs["low_status"] = status
        # Lært: målet har holdt alle rum over gulvet i 3 timer uden at hæve – laveste fungerende fremløb i dette ude-bånd
        if rooms and all(x["margen"] >= 0 for x in rooms) and now - (hs.get("low_hold_since") or now) >= LOW_LEARN_HOLD:
            lr = hs.setdefault("low_learned", {})
            if lr.get(bk) is None or hs["low"] < lr[bk] or now - hs.get("low_learned_t", 0) > 6 * 3600:
                lr[bk] = hs["low"]
                hs["low_learned_t"] = now
        return min(plan, hs["low"])

    @staticmethod
    def _low_log(hs: dict[str, Any], t: float, v: float, plan: float, why: str | None) -> None:
        hs.setdefault("low_log", []).append([round(t), v, plan, why])
        hs["low_log"] = hs["low_log"][-96:]

    def flow_search(self, s: SourceState) -> dict[str, Any] | None:
        """Visning af »søg laveste fremløb«: mål nu, loft, status i klart sprog, rummet der bestemmer, lært og loggen."""
        hs = self.air.get(f"hydro:{s.sid}") or {}
        if self.settings.get(f"{s.sid}.{SOFT_START}", "test") != "lavest":
            return None
        now = time.time()
        rooms = hs.get("low_rooms") or []
        cold = [x for x in rooms if x["margen"] < 0 and x["tendens"] < 0.05]
        status = hs.get("low_status") or "start"
        holds = {"hæver": LOW_URGENT_HOLD if (cold or hs.get("low_stops")) else LOW_UP_HOLD, "sænker": LOW_DOWN_HOLD, "løfter": 900}
        nxt = (hs.get("low_t") or 0) + holds.get(status, 0)
        nxt_txt = dt_util.as_local(dt_util.utc_from_timestamp(nxt)).strftime("%H:%M") if status in holds and nxt > now else None
        bind = min(rooms, key=lambda x: x["om_3t"]) if rooms else None
        st = self.hass.states.get(s.conf.get(CONF_ONOFF_TARGET) or "")
        off = st is not None and st.state == "off"
        low = hs.get("low")
        cap = low if low is not None else self._low_cap(s, hs, self._state_num(self._id(CONF_OUTDOOR)))
        # Næste planlagte start (første varmetime, der ikke er blokeret)
        start_at = None
        if off:
            for x in (self.plans.get(s.sid) or {}).get("hours") or []:
                if x.get("heat_kw") and not x.get("blocked") and x.get("ts", 0) + 3600 > now:
                    # Rød med kvarterer: er planens starttime rød i de første kvarterer (fx 10:00–10:15), starter den først bagefter
                    start_at = after_red(max(x["ts"], now), self.red_spans)
                    break
        f = lambda v: f"{v:.0f}" if v is not None else "–"  # noqa: E731
        k = float(hs.get("low_raise") or 0.0)
        sk = self._start_k(hs)
        sk_txt = f"; opstarten løftes til retur + {_k_txt(sk)} °C, lært af varmepumpens opstart" if sk > SOFT_OVER_RETURN_K else ""
        eff = min((low if low is not None else cap or 0) + k, hs.get("low_loft") or 99) if k else None
        g = hs.get("guard") if not off else None
        if g and g.get("mål") is not None:
            eff = max(eff or 0, g["mål"])
        if off:
            when = dt_util.as_local(dt_util.utc_from_timestamp(start_at)).strftime("%H:%M") if start_at else None
            text = (f"Luft-vand er slukket. Næste start {'kl. ' + when if when else 'når planen varmer'} ved ca. {f(cap)} °C"
                    f" (retur + {SOFT_OVER_RETURN_K:.0f} °C eller det sidst fundne mål{sk_txt}).")
        elif status == "start":
            text = f"Starter ved {f(low)} °C (retur + {SOFT_OVER_RETURN_K:.0f} °C{sk_txt}) – rummene vurderes ved næste planlægning."
        elif status == "løfter":
            text = f"Løfter blødt mod det lærte laveste fremløb (+2 °C pr. kvarter). Nu {f(low)} °C."
        elif status == "hæver" and hs.get("low_stops") and hs.get("low_step"):
            text = f"Hævet til {f(low)} °C – {hs.get('low_why') or 'kompressoren stoppede'}."
        elif status == "hæver" and hs.get("low_stops"):
            text = (f"Hæver til {f((low or 0) + 1)} °C{' kl. ' + nxt_txt if nxt_txt else ''} – kompressoren stoppede efter længere drift: "
                    f"målet var under varmepumpens laveste ydelse.")
        elif status == "hæver" and cold:
            x = min(cold, key=lambda r: r["margen"])
            text = (f"Hæver til {f((low or 0) + 2)} °C{' kl. ' + nxt_txt if nxt_txt else ''} – {x['navn']} er {abs(x['margen']):.1f} °C "
                    f"under gulvet og ikke på vej op ({x['tendens']:+.2f} °C/t).")
        elif status == "hæver" and bind:
            text = (f"Hæver til {f((low or 0) + 1)} °C{' kl. ' + nxt_txt if nxt_txt else ''}, hvis det fortsætter – {bind['navn']} er på vej "
                    f"under gulvet (nu {bind['margen']:+.1f} °C, {bind['tendens']:+.2f} °C/t, om 3 t ca. {bind['om_3t']:+.1f} °C).")
        elif status == "sænker":
            text = f"Sænker til {f((low or 0) - 1)} °C{' kl. ' + nxt_txt if nxt_txt else ''} – alle rum er mindst {LOW_DOWN_MARGIN:.1f} °C over gulvet og stabile."
        elif bind:
            text = (f"Holder {f(low)} °C – {bind['navn']} er tættest på gulvet ({bind['margen']:+.1f} °C, {bind['tendens']:+.2f} °C/t, "
                    f"om 3 t ca. {bind['om_3t']:+.1f} °C).")
        else:
            text = f"Holder {f(low)} °C."
        if k and not off:
            text = (f"Forvarmer: {f(eff)} °C (søgningens {f(low)} °C + {k:.0f} °C), fordi modellen forudser, at {f(low)} °C i flere timer "
                    f"ikke holder rummene gennem den næste blokering. Søgningen fortsætter bagefter. ") + text
        if g and g.get("grund") == "opstart":
            text = (f"Opstartsløft: {f(g['mål'])} °C (søgningens {f(low)} °C) – varmepumpen kører lavt flow i opstarten, så målet "
                    f"holdes på retur + {_k_txt(sk)} °C, til flowet er oppe, og kompressoren ikke stopper af sig selv. ") + text
        elif g and g.get("grund") == "stopværn":
            text = (f"Stopværn: {f(g['mål'])} °C (søgningens {f(low)} °C) – fremløbet nærmer sig varmepumpens stopgrænse (målet + ca. "
                    f"4 °C); målet følger med, så kompressoren kører videre i stedet for at stoppe og starte. ") + text
        return {
            "aktiv": low is not None and bool(hs.get("start_t")) and not off,
            "slukket": off, "mål": low if low is not None else cap, "loft": hs.get("low_loft"), "forvarmer": k or None, "mål_nu": eff,
            "plan": (self.commands.get(s.sid) or {}).get("flow"), "status": "slukket" if off else status,
            "tekst": _dk_txt(text), "grund": _dk_txt(hs.get("low_why") or ""), "næste_ændring": _ts_local_iso(nxt) if nxt_txt else None,
            "bestemmer": bind["navn"] if bind and not off else None, "kompressorstop": hs.get("low_stops") or 0,
            "startløft": round(sk, 1), "korte_kørsler": hs.get("start_short") or 0, "værn": g or None,
            "sidste_korte": hs.get("short_last"),
            "næste_start": dt_util.as_local(dt_util.utc_from_timestamp(start_at)).strftime("%H:%M") if off and start_at else None,
            "rum": [{k: x.get(k) for k in ("navn", "t", "gulv", "margen", "tendens", "om_3t")} for x in rooms],
            "lært": hs.get("low_learned") or {}, "log": (hs.get("low_log") or [])[-48:],
        }

    def _soft_eval_sync(self, s: SourceState, starts: list[dict[str, Any]]) -> None:
        """Vurdér færdige starte: varme, el og afrimninger de første 90 min. (5-minutters statistik og afrimningens historik)."""
        heat, el, df = s.eid(CONF_HEAT), s.eid(CONF_ELECTRICITY), s.eid(CONF_DEFROST)
        for x in starts:
            if "cop" in x or time.time() - x["t"] < SOFT_EVAL_S + 600 or not heat or not el:
                continue
            a, b = dt_util.utc_from_timestamp(x["t"]), dt_util.utc_from_timestamp(x["t"] + SOFT_EVAL_S)
            st = statistics_during_period(self.hass, a, b, {heat, el}, "5minute", {"energy": "kWh"}, {"change"})
            q = sum(v for r in st.get(heat, []) if (v := _num(r.get("change"))) is not None)
            e = sum(v for r in st.get(el, []) if (v := _num(r.get("change"))) is not None)
            n = 0
            if df:
                res = history.get_significant_states(self.hass, a, b, [df], include_start_time_state=False, significant_changes_only=False)
                prev = None
                for z in res.get(df, []):  # tæl skift til »on« (rækker med samme tilstand er attributændringer)
                    n += z.state == "on" and prev != "on"
                    prev = z.state
            x.update(q=round(q, 2), e=round(e, 3), cop=round(q / e, 2) if e > 0.05 else None, afrim=n)

    def soft_summary(self, s: SourceState) -> dict[str, Any]:
        """Blød start mod almindelig start (ude under 6 °C): COP og afrimninger pr. start de første 90 min."""
        rows = [x for x in (self.air.get(f"hydro:{s.sid}") or {}).get("starts", []) if x.get("cop") is not None
                and (x.get("t_out") is None or x["t_out"] < 6)]
        out: dict[str, Any] = {}
        for name, soft in (("blød", True), ("almindelig", False)):
            r = [x for x in rows if x["soft"] == soft]
            q, e = sum(x["q"] for x in r), sum(x["e"] for x in r)
            out[name] = {"starter": len(r), "cop": round(q / e, 2) if e > 0 else None,
                         "afrimninger_pr_start": round(sum(x.get("afrim", 0) for x in r) / len(r), 2) if r else None}
        if out["blød"]["cop"] and out["almindelig"]["cop"]:
            out["cop_forskel_pct"] = round((out["blød"]["cop"] / out["almindelig"]["cop"] - 1) * 100, 1)
        return out

    def hydro_control(self, s: SourceState) -> bool:
        return bool(self.settings.get(f"{s.sid}.{HYDRO_SWITCH}"))

    async def async_set_hydro_control(self, s: SourceState, on: bool) -> None:
        self.settings[f"{s.sid}.{HYDRO_SWITCH}"] = bool(on)
        await self._save()
        await self.async_replan()

    def flow_control(self, s: SourceState) -> bool:
        return bool(self.settings.get(f"{s.sid}.{FLOW_SWITCH}"))

    async def async_set_flow_control(self, s: SourceState, on: bool) -> None:
        self.settings[f"{s.sid}.{FLOW_SWITCH}"] = bool(on)
        await self._save()
        await self.async_replan()

    def _power_mean_sync(self, eid: str, start: float, end: float) -> float | None:
        """Snit af eleffekten (W) i perioden fra 5-minutters statistik."""
        st = statistics_during_period(self.hass, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end), {eid}, "5minute",
                                      None, {"mean"})
        vals = [v for x in st.get(eid, []) if (v := _num(x.get("mean"))) is not None]
        return sum(vals) / len(vals) if vals else None

    async def _learn_skip_windows(self, start: float, end: float) -> list[tuple[float, float]]:
        """Perioder, læringen skal springe over: begivenheder i tiltags-kalenderen med »ignorer« (fx en dørtest)."""
        cal = self._id(CONF_MEASURES_CALENDAR)
        if not cal:
            return []
        try:
            resp = await self.hass.services.async_call(
                "calendar", "get_events", {"start_date_time": dt_util.utc_from_timestamp(start), "end_date_time": dt_util.utc_from_timestamp(end)},
                target={"entity_id": cal}, blocking=True, return_response=True)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for ev in ((resp or {}).get(cal) or {}).get("events") or []:
            if not any(w in f"{ev.get('summary') or ''} {ev.get('description') or ''}".lower() for w in LEARN_IGNORE_WORDS):
                continue
            a, b = dt_util.parse_datetime(str(ev.get("start"))), dt_util.parse_datetime(str(ev.get("end")))
            if a is None:  # heldags: hele døgnet
                d = dt_util.parse_date(str(ev.get("start")))
                a = datetime.combine(d, datetime.min.time(), dt_util.get_default_time_zone()) if d else None
                b = a + timedelta(days=1) if a else None
            if a is not None and b is not None:
                out.append((a.timestamp(), b.timestamp()))
        return out

    def _air_rooms_meta(self, s: SourceState) -> list[dict[str, Any]]:
        """Kildens rum med gulv og model til læring fra historikken (afkøling, gratis varme, naborum som entiteter)."""
        out = []
        for r in self.rooms_cfg:
            if self.room_source(r) is not s or not r["in_plan"]:
                continue
            t_min = self._state_num(r["min_entity"]) if r["min_entity"] else (_num(r["min"]) if r["min"] is not None else None)
            fitd = self.rooms.get(r["key"]) or {}
            ex = fitd.get("extra") or {}
            if t_min is None or fitd.get("value") is None:
                continue
            nbs = []
            for nb_name, nb in (ex.get("neighbours") or {}).items():
                if nb_eid := next((n for n in r.get("neighbours") or [] if self._label(n) == nb_name), None):
                    nbs.append((float(nb.get("per_h_k") or 0.0), nb_eid))
            out.append({"navn": r["name"] or r["key"], "e": r["key"], "gulv": t_min, "cool": float(fitd["value"]) / 20,
                        "free": float(ex.get("free_heat_k_per_day") or 0.0) / 24, "nb": nbs})
        return out

    def _air_eff_backfill_sync(self, clim: str, peid: str, rooms: list[dict[str, Any]], sp_min: float, out_eid: str | None,
                               vent: str | None, vent_speed: str | None, df: str | None, start: float, end: float,
                               skip: list[tuple[float, float]], ctl: str | None = None,
                               fans: list[str] | None = None) -> tuple[dict[str, list[float]], dict[str, Any]]:
        """Virkning pr. trin (°C pr. kWh el) fra historikken – samme beregning som den løbende læring: kvarterer, hvor
        trinnet har kørt mindst EFF_HOLD, rummenes stigning ud over afkøling, gratis varme og naborum pr. kWh el.
        Stramme filtre, så der ikke læres forkert: varmepumpen varmer (> 100 W), ingen afrimning (fra 10 min. før til
        15 min. efter), ingen udluftning (et rum falder > AIR_EFF_AIRING_DROP på et kvarter), ventilationen skifter ikke,
        perioder med »ignorer« i kalenderen springes over, og målinger, der stikker voldsomt ud for samme rum og trin,
        kasseres (median ± 3 × MAD). Kun kvarterer, hvor stigen selv styrede (``ctl``: »Varmemodel styrer« til) og
        blæseren stod på et af stigens trin (``fans``; fx ikke »Auto«) – data fra en anden styring er ikke stigens trin.
        Giver (virkning, oversigt)."""
        import bisect
        from statistics import median as _med

        utc = dt_util.utc_from_timestamp
        ents = list(dict.fromkeys([clim] + [m["e"] for m in rooms] + [n for m in rooms for _, n in m["nb"]]
                                  + [x for x in (out_eid, vent, vent_speed, df, ctl) if x]))
        res = history.get_significant_states(self.hass, utc(start), utc(end), ents, include_start_time_state=True,
                                             significant_changes_only=False)
        ser = {e: [(z.last_updated.timestamp(), z) for z in res.get(e, [])] for e in ents}
        tix = {e: [t for t, _ in v] for e, v in ser.items()}

        def at(e: str | None, t: float) -> Any:
            if not e:
                return None
            i = bisect.bisect_right(tix.get(e, []), t) - 1
            return ser[e][i][1] if i >= 0 else None

        def num(e: str | None, t: float) -> float | None:
            z = at(e, t)
            return _num(z.state) if z is not None else None

        # Trinnet (samme navne som stigen: blæser/forudindstilling + setpunkt over laveste setpunkt) og hvornår det skiftede
        changes: list[tuple[float, str | None]] = []
        for t, z in ser.get(clim, []):
            atr = z.attributes or {}
            sp = _num(atr.get("temperature"))
            lab = None
            name = "quiet" if atr.get("preset_mode") == "quiet" else atr.get("fan_mode")
            if (z.state == "heat" and sp is not None and sp >= sp_min - 1e-6
                    and (fans is None or name == "quiet" or name in fans)):
                lab = f"{name} +{sp - sp_min:.1f}"
            if not changes or changes[-1][1] != lab:
                changes.append((t, lab))
        cht = [t for t, _ in changes]

        def step(t: float) -> tuple[str | None, float]:
            i = bisect.bisect_right(cht, t) - 1
            return (changes[i][1], changes[i][0]) if i >= 0 else (None, t)

        df_on = [t for t, z in ser.get(df, []) if z.state == "on"] if df else []
        df_off = [t for t, z in ser.get(df, []) if z.state == "off"] if df else []

        def vent_on(t: float) -> bool | None:
            z = at(vent, t)
            if z is None or z.state in ("unknown", "unavailable"):
                return None
            on = z.state.lower() not in ("off", "fra", "0", "false", "slukket")
            if on and vent_speed:
                v = num(vent_speed, t)
                on = v is not None and v > 0
            return on

        st = statistics_during_period(self.hass, utc(start), utc(end), {peid}, "5minute", None, {"mean"})
        pw = {int(_ts(x["start"])): _num(x.get("mean")) for x in st.get(peid, [])}
        segs: list[dict[str, Any]] = []
        stat = {"kvarterer": 0, "afrimning": 0, "udluftning": 0, "ventilation": 0, "ignoreret": 0, "for_kort": 0, "anden_styring": 0}
        t = float(int(start // 900 * 900) + 3600)
        while t + 900 <= end:
            lab, since = step(t)
            lab2, _ = step(t + 899)
            if lab is None or lab2 != lab:
                t += 900
                continue
            stat["kvarterer"] += 1
            c0, c1 = (at(ctl, t), at(ctl, t + 899)) if ctl else (None, None)
            if ctl and not (c0 is not None and c1 is not None and c0.state == "on" and c1.state == "on"):
                stat["anden_styring"] += 1
            elif t - since < EFF_HOLD:
                stat["for_kort"] += 1
            elif any(a <= t + 900 and b >= t for a, b in skip):
                stat["ignoreret"] += 1
            elif any(t - 600 <= x <= t + 900 for x in df_on) or any(t - 900 <= x <= t for x in df_off):
                stat["afrimning"] += 1
            else:
                w = [pw.get(int(t + k * 300)) for k in range(3)]
                v0, v1 = vent_on(t), vent_on(t + 899)
                if v0 != v1:
                    stat["ventilation"] += 1
                elif None not in w and min(w) > 100 and (t_out := num(out_eid, t)) is not None:
                    gains, drops = {}, False
                    for m in rooms:
                        a_, b_ = num(m["e"], t), num(m["e"], t + 900)
                        if a_ is None or b_ is None:
                            continue
                        if b_ - a_ < -AIR_EFF_AIRING_DROP:
                            drops = True
                            break
                        nb = [(k, x) for k, e in m["nb"] if (x := num(e, t)) is not None]
                        gains[m["navn"]] = room_gain(a_, b_, 0.25, t_out, m["cool"], m["free"], nb)
                    if drops:
                        stat["udluftning"] += 1
                    elif gains:
                        vk = "" if v0 is None else ("|vent til" if v0 else "|vent fra")
                        segs.append({"t": t, "lab": lab, "vk": vk, "b": band(t_out), "g": gains, "kwh": sum(w) / 3 * 0.25 / 1000})
            t += 900
        # Udskud pr. rum og trin: median ± 3 × MAD (mindst 0,02 °C pr. kWh) – fx et rum med lukket dør
        by: dict[tuple[str, str], list[float]] = {}
        for x in segs:
            for r, g in x["g"].items():
                by.setdefault((r, x["lab"]), []).append(g / x["kwh"])
        lim: dict[tuple[str, str], tuple[float, float]] = {}
        for k, v in by.items():
            if len(v) >= 6:
                md = _med(v)
                lim[k] = (md, 3 * max(_med([abs(y - md) for y in v]), 0.02))
        eff: dict[str, list[float]] = {}
        kept = outl = 0
        for x in segs:
            if any(abs(g / x["kwh"] - lim[(r, x["lab"])][0]) > lim[(r, x["lab"])][1]
                   for r, g in x["g"].items() if (r, x["lab"]) in lim):
                outl += 1
                continue
            g = sum(x["g"].values()) / len(x["g"])
            k = x["lab"] + x["vk"]
            eff = eff_update(eff, k, g, x["kwh"])
            if x["b"] is not None:
                eff = eff_update(eff, k + f"@{x['b']}", g, x["kwh"])
            kept += 1
        stat.update(brugt=kept, udskud=outl, trin=sorted({x["lab"] for x in segs}))
        return eff, stat

    async def async_air_relearn(self, s: SourceState | None = None) -> None:
        """Lær stigens virkning pr. trin forfra fra historikken (de seneste AIR_EFF_BACKFILL_DAYS døgn)."""
        for src in ([s] if s else [x for x in self.sources.values() if self.air_climate(x)]):
            clim, peid = self.air_climate(src), src.conf.get(CONF_EL_POWER)
            if not clim or not peid:
                continue
            a = self.air.setdefault(src.sid, {})
            rooms = self._air_rooms_meta(src)
            cst = self.hass.states.get(clim)
            atr = cst.attributes if cst else {}
            stp = float(atr.get("target_temp_step") or 0.5)
            floors = [m["gulv"] for m in rooms]
            sp_min = max(float(atr.get("min_temp") or 16), round((max(floors) if floors else 20.0) / stp) * stp)
            now = time.time()
            start = now - AIR_EFF_BACKFILL_DAYS * 86400
            skip = await self._learn_skip_windows(start, now)
            # Kun stigens egne kvarterer: »Varmemodel styrer« for kilden til, og blæseren på et af stigens trin
            from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

            uid = f"{self.entry.entry_id}_{AIR_SWITCH}" if src.legacy_ids else f"{self.entry.entry_id}_{src.sid}_{AIR_SWITCH}"
            ctl = er.async_get(self.hass).async_get_entity_id("switch", DOMAIN, uid)
            fans = fan_options(atr.get("fan_modes")) or None
            try:
                eff, stat = await get_instance(self.hass).async_add_executor_job(
                    self._air_eff_backfill_sync, clim, peid, rooms, sp_min, self._id(CONF_OUTDOOR), src.conf.get(CONF_ASSIST),
                    src.conf.get(CONF_ASSIST_SPEED), src.eid(CONF_DEFROST), start, now, skip, ctl, fans)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: virkning pr. trin kunne ikke læres fra historikken", src.name)
                a["bf_eff"] = AIR_EFF_BF_VERSION
                continue
            a["eff"] = eff
            a["bf_eff"] = AIR_EFF_BF_VERSION
            a["bf_eff_info"] = {**stat, "tid": _ts_local_iso(now), "ignorerede_perioder": len(skip)}
            _LOGGER.info("%s: virkning pr. trin lært fra historikken: %s", src.name, a["bf_eff_info"])
        await self._save()
        self._notify()

    def _air_backfill_sync(self, clim: str, peid: str, start: float, end: float) -> dict[str, list[float]]:
        """Forbrug pr. trin fra historikken: klimaentitetens blæser/setpunkt/forudindstilling og 5-minutters effekt.

        Trin navngives relativt til det laveste setpunkt, der blev brugt til varme i perioden (samme navne som stigen)."""
        res = history.get_significant_states(self.hass, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end), [clim],
                                             include_start_time_state=True, significant_changes_only=False)
        segs = []
        for x in res.get(clim, []):
            at_ = getattr(x, "attributes", {}) or {}
            segs.append((x.last_updated.timestamp(), x.state, at_.get("fan_mode"), _num(at_.get("temperature")), at_.get("preset_mode")))
        heat = [t for _, sta, _, t, _ in segs if sta == "heat" and t is not None]
        if not heat:
            return {}
        base = min(heat)
        st = statistics_during_period(self.hass, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end), {peid}, "5minute",
                                      None, {"mean"})
        out: dict[str, list[float]] = {}
        k = 0
        for x in st.get(peid, []):
            ts, w = _ts(x["start"]), _num(x.get("mean"))
            while k + 1 < len(segs) and segs[k + 1][0] <= ts:
                k += 1
            if w is None or w <= 50 or not segs or segs[k][0] > ts:
                continue
            t0, sta, fan, sp, pre = segs[k]
            if sta != "heat" or sp is None or ts < t0 + W_SETTLE:
                continue
            name = "quiet" if pre == "quiet" else fan
            out.setdefault(f"{name} +{sp - base:.1f}", []).append(w)
        return out

    async def _air_apply(self, s: SourceState, clim: str, d: dict[str, Any]) -> None:
        """Skriv anbefalingen til klimaentiteten – kun det, der er forskelligt, og højst 60 skrivninger pr. døgn."""
        if self._stopped:
            return
        st = self.hass.states.get(clim)
        if st is None or st.state in ("unavailable", "unknown"):
            return
        a, at_ = self.air.setdefault(s.sid, {}), st.attributes
        day = dt_util.now().date().isoformat()
        if a.get("wday") != day:
            a["wday"], a["writes"] = day, 0
        calls: list[tuple[str, dict[str, Any]]] = []
        if not d.get("on") or d.get("setpoint") is None:
            if st.state != "off":
                calls.append(("set_hvac_mode", {"hvac_mode": "off"}))
        else:
            if st.state != "heat":
                calls.append(("set_hvac_mode", {"hvac_mode": "heat"}))
            presets = at_.get("preset_modes") or []
            want_preset = d.get("preset") or ("none" if "none" in presets else None)
            if want_preset and want_preset in presets and at_.get("preset_mode") != want_preset:
                calls.append(("set_preset_mode", {"preset_mode": want_preset}))
            if d.get("fan_mode") and want_preset != "quiet" and at_.get("fan_mode") != d["fan_mode"]:
                calls.append(("set_fan_mode", {"fan_mode": d["fan_mode"]}))
            if _num(at_.get("temperature")) != d["setpoint"]:
                calls.append(("set_temperature", {"temperature": d["setpoint"]}))
            for key, attr, svc, field_ in (("air_swing_v", "swing_mode", "set_swing_mode", "swing_mode"),
                                           ("air_swing_h", "swing_horizontal_mode", "set_swing_horizontal_mode", "swing_horizontal_mode")):
                want = self.air_setting(s, key)
                if want and want != UNCHANGED and at_.get(attr) is not None and at_.get(attr) != want:
                    calls.append((svc, {field_: want}))
        for svc, data in calls:
            if a["writes"] >= 60:
                _LOGGER.warning("%s: over 60 skrivninger i dag – Varmemodel holder pause", s.name)
                break
            try:
                await self.hass.services.async_call("climate", svc, {"entity_id": clim, **data}, blocking=True)
                a["writes"] += 1
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: kunne ikke sende %s", s.name, svc)
                break

    def _state_num(self, eid: str | None) -> float | None:
        if not eid:
            return None
        st = self.hass.states.get(eid)
        return _num(st.state) if st else None

    def _recent_sync(self, s: SourceState, start: int, end: int) -> tuple[dict[int, float], dict[int, float]]:
        """Det kilden faktisk gav huset time for time i [start, end) fra 5-minutters statistik – varme uden varmt vand,
        eller el, hvis varmen ikke måles (samme som rummodellerne lærer af) – og udetemperaturen."""
        heat, outdoor = s.eid(CONF_HEAT) or s.eid(CONF_ELECTRICITY), self._id(CONF_OUTDOOR)
        want = {x for x in (heat, outdoor) if x}
        if not heat:
            return {}, {}
        stats = statistics_during_period(self.hass, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end), want,
                                         "5minute", {"energy": "kWh", "temperature": "°C"}, {"change", "mean"})
        q: dict[int, float] = {}
        tsum: dict[int, list[float]] = {}
        for x in stats.get(heat, []):
            if (v := _num(x.get("change"))) is not None:
                h = int(_ts(x["start"]) // 3600 * 3600)
                q[h] = q.get(h, 0.0) + v
        for x in stats.get(outdoor, []) if outdoor else []:
            if (v := _num(x.get("mean"))) is not None:
                tsum.setdefault(int(_ts(x["start"]) // 3600 * 3600), []).append(v)
        for h in list(q):
            dhw = ((s.collected.get(str(h)) or {}).get("dhw") or 0.0) if s.eid(CONF_HEAT) else 0.0
            q[h] = max(q[h], 0.0) * (1 - min(dhw, 60.0) / 60)
        return q, {h: sum(v) / len(v) for h, v in tsum.items()}

    def _held_num(self, eid: str | None) -> tuple[float | None, bool]:
        """Følerens værdi. Er føleren væk, bruges den sidst kendte værdi i højst HOLD_S (og »holdt« er True)."""
        if not eid:
            return None, False
        v = self._state_num(eid)
        now = time.time()
        if v is not None:
            self.last_known[eid] = [v, now]
            return v, False
        lk = self.last_known.get(eid)
        if lk and now - float(lk[1]) <= HOLD_S:
            return float(lk[0]), True
        return None, False

    async def _air_plan(self, s: SourceState, hours: list[PlanHour], now: float, h0: int) -> dict[str, Any]:
        """Plan og anbefalet styring for en luft-luft-kilde: el pr. time, gulv og loft pr. rum, forvarmning før blokeringer."""
        show: list[PlanRoom] = []
        skipped: list[str] = []
        for r in self.rooms_cfg:
            if self.room_source(r) is not s:
                continue
            t_min = self._state_num(r["min_entity"]) if r["min_entity"] else (_num(r["min"]) if r["min"] is not None else None)
            pr = self._plan_room(r, s, t_min if (r["in_plan"] and t_min is not None) else -99.0)
            if pr is None:
                skipped.append(r["name"] or r["key"])
                continue
            show.append(pr)
        # Laveste og højeste el pr. time, når kilden kører (kvartil og 95 %-fraktil af de sidste timer)
        vals = sorted(v for h in s.last_hours if (v := self._source_input(s, h)) is not None and v > 0.08)
        if len(vals) >= 10:
            e_min = max(0.1, vals[len(vals) // 4])
            e_max = max(e_min + 0.2, vals[min(len(vals) - 1, int(len(vals) * 0.95))])
        else:
            e_min, e_max = 0.3, 1.0
        # Rolig opstart: planen må højst regne med loft-trinnets lærte forbrug pr. time (så den lægger flere timer med
        # lavere effekt i stedet for at regne med de øverste trin – og forudsigelserne passer med det, stigen kører)
        ast = self.air.get(s.sid) or {}
        curve = [tuple(x) for x in (ast.get("eff_curve") or [])]
        cap_w = ast.get("soft_cap_w")
        if self.air_setting(s, "air_start") == "rolig" and cap_w and len(curve) < 3:
            # Nødløsning, indtil stigens virkning pr. trin er lært: planen regner med højst midterste trin
            e_max = max(e_min + 0.05, min(e_max, cap_w / 1000))
        try:
            rq, _ = await get_instance(self.hass).async_add_executor_job(self._recent_sync, s, h0 - 12 * 3600, h0)
        except Exception:  # noqa: BLE001
            rq = {}
        past = [rq.get(h0 - k * 3600, 0.0) for k in range(6, 0, -1)]
        p = await self.hass.async_add_executor_job(
            partial(make_air_plan, hours, show, e_min, e_max, past_q=past, label=_clock,
                    comfort=self.setting(CONF_COMFORT_OFFSET), red_allowance=self.setting(CONF_RED_ALLOWANCE),
                    economy=self.setting(CONF_ECONOMY), eff_curve=curve if len(curve) >= 3 else None, red_spans=self.red_spans))
        if (eco := self.setting(CONF_ECONOMY)) > 0:
            p.reason += f" – økonomi: rummene må gå {eco:.2f} °C under gulvet"
        plan = {"kind": "air_air", "hours": p.hours, "rooms": p.rooms, "rooms_planned": [r.name for r in show if r.t_min > -50],
                "reason": p.reason, "warnings": p.warnings, "el_kwh": p.el_kwh, "heat_kwh": p.heat_kwh, "cost_kr": p.cost_kr,
                "demand_kwh": None, "target": None, "rooms_skipped": skipped,
                "rooms_held": sorted({r.name for r in show if r.held and not r.estimated}),
                "rooms_estimated": {r.name: {"forudsagt": round(r.t_now, 2), "sidste_måling": _ts_local_iso(float(e["since"])),
                                             "minutter": round((now - float(e["since"])) / 60), "usikkerhed": round(r.margin, 2)}
                                    for r in show if r.estimated
                                    for e in [next((x for x in self.estimates.values() if x["name"] == r.name), None)] if e},
                "rooms_band": {r.name: round(r.margin, 2) for r in show}, "caps": {r.name: r.t_max for r in show if r.t_max is not None},
                "e_min": round(e_min, 2), "e_max": round(e_max, 2), "preload_window": p.preload,
                "comfort_offset": self.setting(CONF_COMFORT_OFFSET), "red_allowance": self.setting(CONF_RED_ALLOWANCE)}
        # Anbefalet styring: luft-luft reagerer hurtigt – mindst 30 min. tændt, 15 min. slukket, højst én ændring pr. 15 min.
        cur = p.hours[0] if p.hours and p.hours[0]["ts"] == h0 else None
        cmd = step(Command.load(self.commands.get(s.sid)), now, dt_util.now().date().isoformat(),
                   bool(cur and cur["heat_kw"] > 0), None, (bool(cur and cur["blocked"]) or self.live_blocked()), min_on=1800, min_off=900, min_gap=900)
        d = cmd.dump()
        # Setpunkt når styringen står på »tændt« – også når den holder sine 30 min., mens planens time er 0
        caps = [r.t_max for r in show if r.t_max is not None]
        sp = cur.get("setpoint") if cur and cur.get("setpoint") is not None else (min(caps) - 0.5 if caps else None)
        d["setpoint"] = sp if cmd.on else None
        clim = self.air_climate(s)
        if clim:
            try:
                peid = s.conf.get(CONF_EL_POWER)
                p_w = await self.hass.async_add_executor_job(self._power_mean_sync, peid, now - 900, now) if peid else None
                a = self.air.setdefault(s.sid, {})
                if peid and not a.get("bf"):
                    # Første gang: forbrug pr. trin fra de seneste døgns historik (det, varmepumpen faktisk har kørt)
                    bf = await self.hass.async_add_executor_job(self._air_backfill_sync, clim, peid, now - 3 * 86400, now)
                    a["w"] = {**{k: round(median(v)) for k, v in bf.items() if len(v) >= 3}, **(a.get("w") or {})}
                    a["bf"] = True
                if peid and a.get("bf_eff") != AIR_EFF_BF_VERSION:
                    # Første gang (og når metoden ændres): virkning pr. trin fra historikken, så stigen ikke skal vente dage
                    await self.async_air_relearn(s)
                if self.air_setting(s, "air_start") == "rolig":
                    # Rolig opstart: rummenes målte stigning de seneste 30 min. (prøv trinnet, før der skrues op)
                    rr = {r["name"] or r["key"]: r["key"] for r in self.rooms_cfg if self.room_source(r) is s}
                    try:
                        self._air_rise[s.sid] = await get_instance(self.hass).async_add_executor_job(self._rise_sync, rr, 1800)
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("Rolig opstart: stigning kunne ikke læses", exc_info=True)
                d.update(self._air_step(s, clim, p, show, cmd.on, (bool(cur and cur["blocked"]) or self.live_blocked()), now, p_w))
            except Exception:  # noqa: BLE001 – stigen må aldrig vælte planen
                _LOGGER.exception("Luft-luft-stigen fejlede")
        self.commands[s.sid] = d
        if clim and self.air_setting(s, AIR_SWITCH):
            await self._air_apply(s, clim, d)
        return {"plan": plan, "rooms": show, "rq": rq}

    async def async_replan(self) -> None:
        """Ny plan for hver vandbåren varmekilde ud fra priser, røde timer, vejrudsigt og rummene."""
        if self._stopped:
            return
        missing: list[str] = []
        now = time.time()
        h0 = int(now // 3600 * 3600)
        price_eid = self._id(CONF_PRICE_ENTITY)
        prices: dict[int, float] = {}
        if price_eid and (st := self.hass.states.get(price_eid)):
            prices = hourly_prices(dict(st.attributes), self.conf.get(CONF_PRICE_ATTR) or None, self._parse_local)
            if h0 not in prices:
                # Listen starter ofte med næste time: brug den nærmeste kendte pris for den aktuelle time
                later = sorted(t for t in prices if t > h0)
                if later:
                    prices[h0] = prices[later[0]]
                elif (cur := _num(st.state)) is not None:
                    prices[h0] = cur
        if not prices:
            missing.append("priser")
        blocks: dict[int, bool] = {}
        red_q: set[int] = set()
        if (beid := self._id(CONF_BLOCK_ENTITY)) and (bst := self.hass.states.get(beid)):
            codes = [c.strip() for c in str(self.conf.get(CONF_BLOCK_CODES) or "r").split(",") if c.strip()]
            blocks = blocked_hours(bst.state, dict(bst.attributes), codes, self._parse_local)
            red_q = {int(k) + j * 900 for k, b in blocks.items() if b for j in range(4)}
        if (qeid := self._id(CONF_BLOCK_QUARTERS_ENTITY)) and (qst := self.hass.states.get(qeid)):
            # Blokerede kvarterer (fx over et prisloft): en time med mindst BLOCK_QUARTERS_MIN af dem planlægges som blokeret,
            # så varmen lægges før – i stedet for en kørsel, der klippes over midt i timen (24/11-2026)
            items = qst.attributes.get("kvarterer") or []
            for k, n in quarter_block_counts(items, self._parse_local).items():
                if n >= BLOCK_QUARTERS_MIN:
                    blocks[k] = True
            red_q |= quarter_starts(items, self._parse_local)
        # Røde perioder med kvarterer: fremtiden altid fra de friske data, fortiden (op til 13 t) huskes hen over midnat
        q_now = int(now // 900 * 900)
        self._red_q = {q for q in self._red_q if now - 13 * 3600 <= q < q_now} | {q for q in red_q if q >= now - 13 * 3600}
        self.red_spans = spans_from_quarters(self._red_q)
        fc: dict[int, float] = {}
        if w := self._id(CONF_WEATHER):
            try:
                resp = await self.hass.services.async_call("weather", "get_forecasts", {"type": "hourly"}, target={"entity_id": w},
                                                          blocking=True, return_response=True)
                fc = hourly_forecast(((resp or {}).get(w) or {}).get("forecast") or [], self._parse_local)
            except Exception:  # noqa: BLE001
                missing.append("vejrudsigt")
        out_eid = self._id(CONF_OUTDOOR)
        t_now, _ = self._held_num(out_eid)
        out_flag: dict[str, Any] | None = None
        if t_now is None and out_eid and ((lk := self.last_known.get(out_eid)) or now - self._started > HOLD_S):
            # Udeføleren har været væk i over 30 min.: vejrudsigten for timen tager over
            t_now = fc.get(h0)
            out_flag = {"føler": out_eid, "sidste_måling": _ts_local_iso(float(lk[1])) if lk else None,
                        "minutter": round((now - float(lk[1])) / 60) if lk else None, "forudsagt": t_now,
                        "bruger": "vejrudsigten" if t_now is not None else "5 °C (ingen vejrudsigt)"}
        hours, safety = build_hours(h0, prices, fc, blocks, t_now, PLAN_HORIZON_H)
        await self._estimate_missing(now, t_now)
        # »Jeg fryser«: gælder fra den blev slået til, i højst boost_hours timer og kun til næste blokerede periode starter
        boost_on, boost_h, boost_note = False, 0, None
        if (be := self._id(CONF_BOOST_ENTITY)) and (bs := self.hass.states.get(be)) and bs.state == "on":
            since = bs.last_changed.timestamp()
            until = since + self.setting(CONF_BOOST_HOURS) * 3600
            started_in_block = bool(blocks.get(int(since // 3600 * 3600)))
            for k, h in enumerate(hours):
                if h.ts + 3600 <= since:
                    continue
                if h.blocked and not started_in_block and h.ts >= since - 3600:
                    until = min(until, h.ts)
                    break
                if not h.blocked:
                    started_in_block = False
            boost_h = max(0, min(len(hours), int(-(-(until - h0) // 3600))))
            boost_on = boost_h > 0
            boost_note = _ts_local_iso(until) if boost_on else "udløbet"
        boost = self.setting(CONF_BOOST_DELTA) if boost_on else 0.0
        offset = self.setting(CONF_COMFORT_OFFSET)
        plans: dict[str, dict[str, Any]] = {}
        air_plans: dict[str, dict[str, Any]] = {}
        for s2 in self.sources.values():
            if s2.kind != SOURCE_HYDRONIC:
                air_plans[s2.sid] = await self._air_plan(s2, hours, now, h0)
                plans[s2.sid] = air_plans[s2.sid]["plan"]
                # Log som for vandbårne kilder: timens plan, pris og blokering (kortets tidslinje og kWh-søjler)
                ah = plans[s2.sid]["hours"]
                alog = self.plan_log.setdefault(s2.sid, {})
                if ah and ah[0]["ts"] == h0 and str(h0) not in alog:
                    alog[str(h0)] = {"p": ah[0]["heat_kw"], "e": ah[0]["heat_kw"], "pr": ah[0]["price"], "b": ah[0]["blocked"]}
                for k in [k for k in alog if int(k) < h0 - 3 * 86400]:
                    del alog[k]
        for sid, s in self.sources.items():
            if s.kind != SOURCE_HYDRONIC:
                continue
            a = s.accepted

            def v(key: str, default: float) -> float:
                x = (a.get(key) or {}).get("value")
                return float(x) if x is not None else default

            st_extra = (a.get("cop_steady") or {}).get("extra") or {}
            # Planen regner med stabil COP; det målte tab ved opstart og afrimning (%, kun et tab – aldrig en gevinst) trækkes fra
            lf = 1 - min(max(v("cop_loss", 0.0), 0.0), 30.0) / 100
            model = SourceModel(
                heat_loss_w_k=v("heat_loss", 90.0),
                # Varmecentralens tab til jorden lægges til husets behov som negativ gratis varme
                gains_w=float(((a.get("heat_loss") or {}).get("extra") or {}).get("gains_w") or 0.0) - self._plant_loss_w(),
                p_max=v("max_power", 6.0), p_min=min(v("min_power", 2.0), v("max_power", 6.0) * 0.8),
                # Til planlægning må høj effekt eller højt fremløb aldrig gøre COP bedre (fysik). Et positivt
                # effektled i kalibreringen skyldes koldt gulv efter pauser (lav retur), ikke effekten selv.
                # Er effektleddet positivt, holdes det fast ved 4 kW (kalibreringens referenceeffekt), så niveauet passer.
                cop=tuple(lf * x for x in (float(st_extra.get("a", 3.0)) + max(float(st_extra.get("d", 0.0)), 0.0) * 4.0, float(st_extra.get("b", 0.15)),
                                           min(float(st_extra.get("c", -0.04)), 0.0), min(float(st_extra.get("d", 0.0)), 0.0))),
                # Gulvets typiske afgivelse (median) – 25 %-fraktilen gav et urealistisk højt fremløb
                emission_kw_k=float(((a.get("emission") or {}).get("extra") or {}).get("median") or v("emission", 0.25)),
                flow_c0=v("flow_learned", 34.0),
                flow_slope=float(((a.get("flow_learned") or {}).get("extra") or {}).get("slope", -0.6)),
            )
            rooms: list[PlanRoom] = []  # rum med komfortgulv: styrer planen
            show: list[PlanRoom] = []  # alle varmekildens rum: forudsiges og vises
            mins: list[float] = []
            skipped: list[str] = []
            low: list[str] = []
            for r in self.rooms_cfg:
                if self.room_source(r) is not s:
                    continue
                t_min = self._state_num(r["min_entity"]) if r["min_entity"] else (_num(r["min"]) if r["min"] is not None else None)
                pr = self._plan_room(r, s, t_min if t_min is not None else -99.0)
                if pr is None:
                    skipped.append(r["name"] or r["key"])
                    continue
                show.append(pr)
                if r["in_plan"] and t_min is not None:
                    rooms.append(pr)
                    mins.append(t_min)
                    # Reserve: termostaten har kaldt på varme i 6 timer, og rummet er under gulvet og under det, modellen ventede
                    since = self._valve_open_since(r, now)
                    chk = self.rooms_check.get(pr.name) or {}
                    if (since is not None and now - since >= FLOW_LOW_OPEN_S and not pr.held and pr.t_now < t_min
                            and chk.get("status") == "under"):
                        low.append(pr.name)
            fb = self.flow_boost[sid] = flow_boost_step(self.flow_boost.get(sid), now, low)
            # Koldere end det, modellen er målt ved: forsigtig plan (+ indstillingens % varmetab, +0,2 °C margen i rummene)
            caution = self.setting(CONF_COLD_CAUTION)
            cold_lim = (a.get("heat_loss") or {}).get("t_out_min")
            coldest = min((x.t_out for x in hours), default=None)
            cold = caution > 0 and cold_lim is not None and coldest is not None and coldest < cold_lim - 1
            if cold:
                model.heat_loss_w_k *= 1 + caution / 100
            target = (sum(mins) / len(mins) + offset - self.setting(CONF_ECONOMY)) if mins else 20.5
            # Gulvet afgiver varmen fra de seneste 6 timer med forsinkelse: den målte varme går med i forudsigelsen
            try:
                recent_q, recent_out = await get_instance(self.hass).async_add_executor_job(self._recent_sync, s, h0 - 12 * 3600, h0)
            except Exception:  # noqa: BLE001 – uden statistik regnes som før uden de seneste timers varme
                recent_q, recent_out = {}, {}
            past_q = [recent_q.get(h0 - k * 3600, 0.0) for k in range(6, 0, -1)]
            fm, raise_max, low_cap = self.setting(CONF_FLOW_MARGIN) + fb["k"], None, None
            hs = self.air.setdefault(f"hydro:{sid}", {})
            if self.settings.get(f"{sid}.{SOFT_START}", "test") == "lavest" and hours:
                # Søg laveste fremløb: planen regner med søgningens mål som fremløb (én sandhed for plan, forudsigelser og
                # visning) – loftet er det lærte fremløb + margen, som planen ellers ville bruge
                t0 = hours[0].t_out
                base_f = model.flow_c0 + model.flow_slope * t0
                hs["low_loft"] = round(min(FLOW_MAX_C, max(SOFT_FLOOR, base_f + fm)), 1)
                low_cap = self._low_cap(s, hs, t0)
                if low_cap is not None:
                    low_cap = min(low_cap, hs["low_loft"])
                    # Planen må stadig hæve (højst FLOW_BOOST_MAX), når flere timer ved søgningens mål ikke kan holde
                    # rummene – fx gennem en lang blokering (forvarmning går forud for søgningen)
                    fm = low_cap - base_f
            else:
                hs.pop("low_loft", None)
            p = await self.hass.async_add_executor_job(
                partial(make_plan, hours, model, rooms, target, int(self.setting(CONF_MAX_PAUSE)), past_q=past_q, label=_clock,
                        boost=boost, boost_hours=boost_h, red_allowance=self.setting(CONF_RED_ALLOWANCE), economy=self.setting(CONF_ECONOMY),
                        comfort=offset, flow_margin=fm, red_spans=self.red_spans,
                        **({"flow_raise_max": raise_max} if raise_max is not None else {})))
            if low_cap is not None:
                k = float(p.flow_raise or 0.0)
                hs["low_raise"] = k
                for x in p.hours:
                    if x.get("heat_kw") and x.get("flow") is not None:
                        # varmepumpen kører søgningens mål (+ planens forvarmning) i varmetimerne
                        x["flow"] = float(round(min(low_cap + k, hs["low_loft"])))
                p.reason += (f" – søger laveste fremløb: planen regner med {low_cap:.0f} °C (loft {hs['low_loft']:.0f} °C)"
                             + (f"; forvarmer +{k:.0f} °C, fordi {low_cap:.0f} °C i flere timer ikke holder rummene" if k else ""))
            elif sid:
                (self.air.get(f"hydro:{sid}") or {}).pop("low_raise", None)
            if (eco := self.setting(CONF_ECONOMY)) > 0:
                p.reason += f" – økonomi: rummene må gå {eco:.2f} °C under gulvet"
            # Log: det planlagte for timen (første plan i timen) og rummenes forudsigelse – til »i går« og »!«
            log = self.plan_log.setdefault(sid, {})
            if p.hours and p.hours[0]["ts"] == h0 and str(h0) not in log:
                x = p.hours[0]
                log[str(h0)] = {"p": x["heat_kw"], "e": round(x["heat_kw"] / x["cop"], 2) if x["cop"] else 0.0,
                                "pr": x["price"], "b": x["blocked"], "f": x["flow"]}
            for k in [k for k in log if int(k) < h0 - 3 * 86400]:
                del log[k]
            # Forudsigelse for alle rum: varmekildens rum med planens varme, luft-luft-kilders rum med
            # samme tilførsel som de seneste 24 timer (de har ingen plan endnu)
            fc_all = simulate_rooms(show, hours, [x["heat_kw"] for x in p.hours], past_q) if show else {}
            measured: dict[str, tuple[dict[int, float], str]] = {r.name: (recent_q, "målt varme") for r in show}
            for s2 in self.sources.values():
                if s2.kind == SOURCE_HYDRONIC:
                    continue
                # Luft-luft-kildens rum: forudsigelsen med dens egen plan (lavet ovenfor)
                ap = air_plans.get(s2.sid) or {}
                air = list(ap.get("rooms") or [])
                if air:
                    fc_all.update({x.name: ap["plan"]["rooms"][x.name] for x in air if x.name in ap["plan"]["rooms"]})
                    rq2 = ap.get("rq") or {}
                    for x in air:
                        measured[x.name] = (rq2, "målt varme" if s2.eid(CONF_HEAT) else "målt el")
                    show += air
            # Rum med holdt temperatur forudsiges og planlægges, men logges og tjekkes ikke (startpunktet er ikke målt nu)
            held = sorted({r.name for r in show if r.held})
            if fc_all and str(h0) not in self.fc_log:
                self.fc_log[str(h0)] = {name: v[:8] for name, v in fc_all.items() if name not in held}
                self.fc_log[str(h0)]["_t0"] = {r.name: round(r.t_now, 2) for r in show if not r.held}
            for k in [k for k in self.fc_log if int(k) < h0 - 8 * 3600]:
                del self.fc_log[k]
            checks: dict[str, dict[str, Any]] = {}
            old_fc = self.fc_log.get(str(h0 - 6 * 3600)) or {}
            t0s = old_fc.get("_t0") or {}
            h6 = [PlanHour(ts=h0 - (6 - k) * 3600, price=0.0, t_out=recent_out.get(h0 - (6 - k) * 3600, t_now if t_now is not None else 0.0))
                  for k in range(6)]
            for r in show:
                f = old_fc.get(r.name) or []
                if len(f) > 5 and not r.held:
                    exp, basis = f[5], "planens varme"
                    rq, label = measured.get(r.name) or ({}, "")
                    if r.name in t0s and rq and all(h.ts in rq and h.ts - 6 * 3600 in rq for h in h6):
                        # Samme rummodel, men med det kilden faktisk gav: tjekker modellen, også når planen ikke blev fulgt
                        exp = simulate_rooms([replace(r, t_now=float(t0s[r.name]))], h6, [rq[h.ts] for h in h6],
                                             [rq[h.ts - 6 * 3600] for h in h6])[r.name][5]
                        basis = label
                    dev = round(r.t_now - exp, 2)
                    # Afvigelse først ud over 2 × rummets ærlige præcision: margenens loft på 0,6 gælder planen, ikke tjekket
                    hit, lim = max(0.15, r.margin), round(max(0.3, 2 * r.margin, 2 * r.accuracy), 2)
                    checks[r.name] = {"ventet": exp, "ventet_med_plan": f[5], "grundlag": basis, "målt": round(r.t_now, 2), "afvigelse": dev,
                                      "grænse": lim, "status": "ramt" if abs(dev) <= hit else ("over" if dev > 0 else "under"), "flag": abs(dev) > lim}
            self.rooms_check = checks
            if cold:
                note = (f"koldere end det kalibrerede område ({coldest:.0f} °C mod {cold_lim:.0f} °C) – "
                        f"planen er forsigtig (+{caution:.0f} % varmetab)")
                p.warnings.append(note)
                p.reason += " – " + note
            if safety:
                p.warnings.append("nødplan uden priser: flad pris, rummene holdes over komfortgulvet")
                p.reason = "Nødplan uden priser: " + p.reason
            plans[sid] = {"hours": p.hours, "rooms": fc_all, "rooms_planned": [r.name for r in rooms], "cautious": cold, "safety": safety,
                          "rooms_band": {r.name: round(r.margin, 2) for r in show}, "demand_kwh": p.demand_kwh, "heat_kwh": p.heat_kwh,
                          "el_kwh": p.el_kwh, "cost_kr": p.cost_kr, "reason": p.reason, "warnings": p.warnings,
                          "target": round(target, 1), "rooms_skipped": skipped,
                          "rooms_held": sorted({r.name for r in show if r.held and not r.estimated}),
                          "rooms_estimated": {r.name: {"forudsagt": round(r.t_now, 2), "sidste_måling": _ts_local_iso(float(e["since"])),
                                                       "minutter": round((now - float(e["since"])) / 60), "usikkerhed": round(r.margin, 2)}
                                              for r in show if r.estimated
                                              for e in [next((x for x in self.estimates.values() if x["name"] == r.name), None)] if e},
                          "boost": boost_on, "boost_until": boost_note,
                          "boost_delta": boost, "red_allowance": self.setting(CONF_RED_ALLOWANCE),
                          "preload_window": p.preload, "flow_boost": fb if fb["k"] or fb["rooms"] else None, "flow_raise": p.flow_raise or None, "max_pause": int(self.setting(CONF_MAX_PAUSE)), "comfort_offset": offset}
            # Anbefalet styring: planens ønske nu omsat efter reglerne, der skåner varmepumpen
            cur = p.hours[0] if p.hours and p.hours[0]["ts"] == h0 else None
            cmd = step(Command.load(self.commands.get(sid)), now, dt_util.now().date().isoformat(),
                       bool(cur and cur["heat_kw"] > 0), (round(cur["flow"]) if cur and cur.get("flow") is not None else None),
                       (bool(cur and cur["blocked"]) or self.live_blocked()))
            self.commands[sid] = cmd.dump()
            full = bool(s.conf.get(CONF_ONOFF_TARGET) and self.settings.get(f"{sid}.{HYDRO_SWITCH}"))
            hst = self.air.setdefault(f"hydro:{sid}", {})
            try:
                # Kompressoren kører siden … (fra historikken – en genstart nulstiller last_changed, så COP ellers skjules)
                hst["comp_since"] = await get_instance(self.hass).async_add_executor_job(self._comp_on_since_sync, s)
                hst["comp_checked"] = time.time()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Kompressorens starttid kunne ikke læses", exc_info=True)
            if any("cop" not in x and now - x["t"] > SOFT_EVAL_S + 600 for x in hst.get("starts", [])):
                try:
                    await get_instance(self.hass).async_add_executor_job(self._soft_eval_sync, s, hst["starts"])
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Vurdering af blød start fejlede", exc_info=True)
            if full:
                await self._hydro_apply(s, cmd.on, (bool(cur and cur["blocked"]) or self.live_blocked()), p.hours)
            if s.conf.get(CONF_FLOW_TARGET) and (full or self.settings.get(f"{sid}.{FLOW_SWITCH}")):
                await self._flow_apply(s, p.hours, (bool(cur and cur["blocked"]) or self.live_blocked()))
        self.plans = plans
        self._flag_missing(now, out_flag)
        if len(hours) < 6:
            missing.append("for få timer med pris")
        # P2: andel af dagens kvarterer med en frisk plan med alle input
        day = dt_util.now().date().isoformat()
        ps = self.plan_stats if self.plan_stats.get("day") == day else {"day": day, "fresh": 0, "start": now}
        if not missing:
            ps["fresh"] = ps.get("fresh", 0) + 1
        start = max(float(ps.get("start", now)), dt_util.start_of_local_day().timestamp())
        ps["pct"] = round(min(100.0, 100.0 * ps["fresh"] / (int((now - start) // 900) + 1)), 1)
        self.plan_stats = ps
        self.plan_meta = {"updated": now, "inputs_ok": not missing, "missing": missing, "horizon_h": len(hours),
                          "blocked_known": bool(blocks), "fresh_pct_today": ps["pct"], "safety": safety}
        self.store.async_delay_save(self._data, 600)
        self._notify()


def _clock(ts: int) -> str:
    """Lokal klokkeslæt til planens advarsler, fx 'kl. 23'."""
    d = dt_util.as_local(dt_util.utc_from_timestamp(ts))
    days = (d.date() - dt_util.now().date()).days
    return {0: "", 1: "i morgen ", 2: "i overmorgen "}.get(days, d.strftime("%d/%m ")) + d.strftime("kl. %H") + (d.strftime(":%M") if d.minute else "")


def quarter_starts(items: list[Any], parse: Callable[[str], datetime | None]) -> set[int]:
    """Kvarterernes start (unix-sekunder, rundet ned til kvarteret) fra en liste af starttider."""
    out: set[int] = set()
    for x in items:
        d = parse(str(x))
        if d is not None:
            out.add(int(d.timestamp() // 900 * 900))
    return out


def spans_from_quarters(quarters: set[int]) -> list[tuple[int, int]]:
    """Sammenhængende perioder [(fra, til)] af kvarterer, fx {06:30, 06:45, 07:00} → [(06:30, 07:15)]."""
    out: list[tuple[int, int]] = []
    for q in sorted(quarters):
        if out and out[-1][1] == q:
            out[-1] = (out[-1][0], q + 900)
        else:
            out.append((q, q + 900))
    return out


def after_red(ts: float, spans: list[tuple[int, int]]) -> float:
    """Første tidspunkt fra ``ts``, der ikke ligger i en rød periode (perioderne er sorterede og slået sammen)."""
    for a, b in spans:
        if a <= ts < b:
            ts = b
    return ts


def quarter_block_counts(items: list[Any], parse: Callable[[str], datetime | None]) -> dict[int, int]:
    """Antal blokerede kvarterer pr. time (timens start i unix-sekunder) fra en liste af kvarterernes starttider."""
    cnt: dict[int, int] = {}
    for x in items:
        d = parse(str(x))
        if d is not None:
            k = int(d.timestamp() // 3600 * 3600)
            cnt[k] = cnt.get(k, 0) + 1
    return cnt


def comp_stops(rows: dict[str, list[tuple[float, str]]], comp: str, dhw: str | None, df: str | None, since: float,
               ret: str | None = None) -> list[dict[str, Any]]:
    """Kompressorstop (tændt → slukket) fra ``since``. Pr. stop: tidspunkt ``t``; ``run`` = sekunder siden opstarten
    begyndte (kompressorstart eller afrimningens slut, det seneste – efter en afrimning kører varmepumpen opstart igen;
    None når starten ligger før data); ``ret0`` = returen ved den start (None uden data); ``df`` = »efter«, når stoppet kom
    inden for 10 min. efter en afrimning.

    Tæller ikke: 4-vejsventilens skift (1 min. før til 5 min. efter en afrimnings start og DF_VALVE_S efter dens slut), stop
    under afrimning eller varmt vand og 3 min. efter varmt vand.

    ``rows`` er historikken pr. entitet [(tid, tilstand)] – gerne fra 30 min. før ``since``. Første række pr. entitet er
    tilstanden ved forespørgslens start (tidsstemplet = start) og er intet skift: før 0.17.3 blev en sådan »slukket« på
    afrimningen læst som »afrimningen sluttede lige« og skjulte stop i vinduets første 10 min. Utilgængelig/ukendt springes
    over (et Modbus-udfald er hverken stop eller afrimning)."""
    ev: list[tuple[float, str, str | None, str]] = []
    for e in (comp, dhw, df):
        if not e:
            continue
        prev: str | None = None
        for t, v in sorted(rows.get(e) or [], key=lambda r: r[0]):
            if v in ("unavailable", "unknown") or v == prev:
                continue
            ev.append((t, e, prev, v))
            prev = v
    ev.sort(key=lambda r: r[0])
    df_on = [t for t, e, p, v in ev if e == df and p is not None and v == "on"]
    df_off = [t for t, e, p, v in ev if e == df and p == "on" and v == "off"]
    dhw_off = [t for t, e, p, v in ev if e == dhw and p == "on" and v == "off"]
    rv = sorted((t, x) for t, v in (rows.get(ret) or [] if ret else []) if (x := _num(v)) is not None)
    st: dict[str, str] = {}
    on_t: float | None = None
    out: list[dict[str, Any]] = []
    for t, e, p, v in ev:
        if e == comp and v == "on":
            on_t = t if p is not None else None  # tændt allerede ved datastart: længden er ukendt
        elif e == comp and p == "on" and v == "off":
            if t >= since and st.get(dhw or "") != "on" and st.get(df or "") != "on":
                valve = any(-60 <= x - t <= 300 for x in df_on) or any(0 <= t - x <= DF_VALVE_S for x in df_off)
                if not valve and not any(0 <= t - x <= 180 for x in dhw_off):
                    dfe = [x for x in df_off if x <= t]
                    t0 = max((x for x in (on_t, dfe[-1] if dfe else None) if x is not None), default=None)
                    r0 = [x for tt, x in rv if tt <= t0] if t0 is not None else []
                    out.append({"t": t, "run": (t - t0) if t0 is not None else None, "ret0": r0[-1] if r0 else None,
                                "df": "efter" if dfe and t - dfe[-1] <= 600 else None})
            on_t = None
        st[e] = v
    return out


def learn_start_k(k: float, short: list[dict[str, Any]], k_min: float, k_max: float = START_K_MAX) -> float:
    """Startløftet over returen, som opstarten kræver: en kort kørsel ved målet ``mål`` med returen ``ret0`` ved starten
    viser, at løftet skal være over mål − ret0 – lær mål − ret0 + 1 K (målets trin). Aldrig under ``k_min`` eller over
    ``k_max``; et løft, der allerede er højere, beholdes."""
    for x in short:
        if x.get("mål") is not None and x.get("ret0") is not None:
            k = max(k, x["mål"] - x["ret0"] + 1.0)
    return round(min(max(k, k_min), k_max), 2)


def start_flow(ret: float | None, k: float, k_default: float, floor: float) -> float | None:
    """Fremløbsmålet ved start: retur + startløftet, mindst ``floor``. Med et lært løft (over standarden) rundes op –
    opstarten har vist, at en halv grad for lidt får kompressoren til at stoppe; standarden rundes som før."""
    if ret is None:
        return None
    v = ret + k
    return float(max(ceil(v - 1e-9) if k > k_default else round(v), floor))


def _k_txt(k: float) -> str:
    """Startløftet som tekst: hele grader uden decimal, ellers én decimal (dansk komma sættes af _dk_txt)."""
    return f"{k:.0f}" if abs(k - round(k)) < 0.05 else f"{k:.1f}"


def _dk_txt(t: str) -> str:
    """Tal i dansk skrivemåde i en tekst: decimalkomma og minus (−), fx »+0.2 og -0.08« → »+0,2 og −0,08«; −0,0 → ±0,0."""
    t = re.sub(r"(?<=\d)\.(?=\d)", ",", t)
    t = re.sub(r"(?<![\w])-(?=\d)", "−", t)
    return re.sub(r"[−+]0,0(?![\d])", "±0,0", t)


def _band_txt(bk: str) -> str:
    """Ude-båndets nøgle som tekst, fx »−5…0 °C«."""
    try:
        lo = int(bk)
    except (TypeError, ValueError):
        return "ukendt ude"
    return f"{lo}…{lo + 5} °C".replace("-", "−")


def _ts_local_iso(ts: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat()


def _hhmm(iso: str | None) -> str:
    """Kort lokal tid til beskeder: »HH:MM« i dag, ellers »d/m HH:MM«."""
    d = dt_util.parse_datetime(iso) if iso else None
    if d is None:
        return "–"
    d = dt_util.as_local(d)
    return d.strftime("%H:%M") if d.date() == dt_util.now().date() else f"{d.day}/{d.month} {d:%H:%M}"
