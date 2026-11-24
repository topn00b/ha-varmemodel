"""Sensorer: huset (status), hver varmekilde (kalibrerede tal) og hvert rum (afkøling, varme, varmekilder)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VarmemodelConfigEntry
from .const import FIT_KEYS, SOURCE_HYDRONIC
from .coordinator import SourceState, VarmemodelCoordinator, after_red
from .entity import VarmemodelEntity

PRECISION = {"heat_loss": 0, "cop_steady": 2, "cop_loss": 1, "emission": 3, "max_power": 2, "min_power": 2,
             "flow_learned": 1, "defrost": 2, "cooling": 0, "wind_effect": 1}


def _ts_local(ts: float | None) -> str | None:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat() if ts else None


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([StatusSensor(c), MeasuresSensor(c)])
    for sid, s in c.sources.items():
        ents: list[SensorEntity] = []
        if s.kind == SOURCE_HYDRONIC:
            ents += [FitSensor(c, s, k) for k in FIT_KEYS]
            ents += [DailyCopSensor(c, s, "yesterday"), DailyCopSensor(c, s, "week"), CopTodaySensor(c, s)]
            ents += [PlanNowSensor(c, s), CommandSensor(c, s), PlanValueSensor(c, s, "plan_flow", "flow", "°C", 1),
                     PlanCostSensor(c, s)]
        else:
            # Luft-luft: el pr. døgn og en plan med anbefalet styring (el pr. time, gulv og loft pr. rum)
            ents += [DailyElSensor(c, s, "yesterday"), DailyElSensor(c, s, "week"),
                     PlanNowSensor(c, s), CommandSensor(c, s), PlanCostSensor(c, s)]
            if c.air_climate(s):
                ents.append(AirStepSensor(c, s))
        async_add_entities(ents, config_subentry_id=sid)
    for r in c.rooms_cfg:
        rents: list[SensorEntity] = [RoomSensor(c, r), RoomForecastSensor(c, r)]
        src = c.room_source(r)
        if (r["valve"] or r["always_open"]) and src is not None and src.kind == SOURCE_HYDRONIC:
            rents.append(RoomHeatSensor(c, r))
        if r["subentry_id"]:
            async_add_entities(rents, config_subentry_id=r["subentry_id"])
        else:
            async_add_entities(rents)


class SourceEntity(VarmemodelEntity):
    """En entitet på en varmekildes enhed. Varmekilden fra version 1 beholder sine entitets-id'er."""

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState, key: str) -> None:
        entry_id = coordinator.entry.entry_id
        uid = f"{entry_id}_{key}" if s.legacy_ids else f"{entry_id}_{s.sid}_{key}"
        super().__init__(coordinator, key, sub_id=s.sid, sub_name=s.name,
                         model="Luft-vand" if s.kind == SOURCE_HYDRONIC else "Luft-luft", unique_id=uid)
        self.src = s


class FitSensor(SourceEntity, SensorEntity):
    """Et kalibreret tal. Attributterne viser hvor det kommer fra."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState, key: str) -> None:
        super().__init__(coordinator, s, key)
        self.key = key
        self._attr_native_unit_of_measurement = FIT_KEYS[key]
        self._attr_suggested_display_precision = PRECISION.get(key)

    @property
    def native_value(self) -> float | None:
        a = self.src.accepted.get(self.key)
        return a.get("value") if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self.src.accepted.get(self.key) or {}
        f = self.src.fits.get(self.key) or {}
        return {
            "kilde": a.get("source", "ingen data"),
            "typisk_fejl": a.get("rmse"),
            "begrænsning": a.get("guard"),
            "beregnet_værdi": f.get("value"),
            "målinger": f.get("n"),
            "forklaringsgrad": None if f.get("r2") is None else round(f["r2"], 3),
            "ude_fra": a.get("t_out_min"),
            "ude_til": a.get("t_out_max"),
            "beregnet": _ts_local(a.get("fitted_at")),
            "note": f.get("note") or None,
            **(a.get("extra") or {}),
        }


class DailyCopSensor(SourceEntity, SensorEntity):
    """Varme ÷ el for i går eller de seneste 7 døgn – det rå regnestykke."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState, period: str) -> None:
        super().__init__(coordinator, s, f"cop_{period}")
        self.period = period

    @property
    def _d(self) -> dict[str, Any]:
        return (self.src.daily or {}).get(self.period) or {}

    @property
    def native_value(self) -> float | None:
        return self._d.get("cop")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._d
        attrs = {
            "varme_kwh": d.get("heat_kwh"), "el_kwh": d.get("el_kwh"), "cop_uden_varmt_vand": d.get("cop_without_dhw"),
            "timer_med_varmt_vand": d.get("dhw_hours"), "forventet_stabil_cop": d.get("expected_steady"),
            "forskel_fra_stabil_pct": d.get("vs_steady_pct"), "ude_snit": d.get("t_out_mean"), "døgn": d.get("days"),
        }
        if self.period == "yesterday":
            attrs["seneste_14_døgn"] = [
                {"dato": x["days"][0], "varme": x["heat_kwh"], "el": x["el_kwh"], "cop": x["cop"],
                 "uden_vv": x["cop_without_dhw"], "stabil": x["expected_steady"], "ude": x["t_out_mean"]}
                for x in (self.src.daily or {}).get("days") or [] if x
            ]
        return attrs


class CopTodaySensor(SourceEntity, SensorEntity):
    """Dagens COP (kWh varme ÷ kWh el siden midnat) med opdeling: varmt vand, stabil drift og tab ved opstart/afrimning."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState) -> None:
        super().__init__(coordinator, s, "cop_today")

    @property
    def _d(self) -> dict[str, Any]:
        d = self.src.today or {}
        return d if d.get("date") == dt_util.now().date().isoformat() else {}

    @property
    def native_value(self) -> float | None:
        return self._d.get("cop")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._d
        return {
            "kwh_varme": d.get("heat_kwh"), "kwh_el": d.get("el_kwh"),
            "varmt_vand_kwh_varme": d.get("dhw_heat_kwh"), "varmt_vand_kwh_el": d.get("dhw_el_kwh"),
            "cop_uden_varmt_vand": d.get("cop_without_dhw"),
            "cop_stabil_drift": d.get("cop_steady"), "minutter_stabil_drift": d.get("steady_minutes"),
            "ekstra_el_opstart_afrimning_standby_pct": d.get("loss_start_defrost_pct"),
            "varmt_vand_traek_pct": d.get("dhw_effect_pct"),
            # Dagens driftstal: starter, korte kørsler, afrimninger, driftstid og Varmemodels egne skrivninger
            "drift": ({k: v for k, v in h.items() if k != "date"} if (h := self.src.health or {}).get("date") == dt_util.now().date().isoformat() else None),
            "stabil_cop_ifølge_model": d.get("expected_steady"), "stabil_mod_model_pct": d.get("steady_vs_model_pct"),
            "metode": "5-minutters portioner siden midnat; stabil drift = fra 10 min efter start/afrimning til afrimning, varmt vand eller stop",
            "opdateret": d.get("updated"),
        }


class DailyElSensor(SourceEntity, SensorEntity):
    """Luft-luft: el pr. døgn (i går eller snit af de seneste 7 døgn)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState, period: str) -> None:
        super().__init__(coordinator, s, f"el_{period}")
        self.period = period

    @property
    def native_value(self) -> float | None:
        d = (self.src.daily or {}).get(self.period) or {}
        if d.get("el_kwh") is None:
            return None
        return round(d["el_kwh"] / (1 if self.period == "yesterday" else max(1, len(d.get("days") or []))), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = (self.src.daily or {}).get(self.period) or {}
        b = self.src.el_backup or {}
        return {"ude_snit": d.get("t_out_mean"), "døgn": d.get("days"), "følere": _sensors(self.src),
                "supplerende_måler_faktor": b.get("factor"), "supplerende_måler_timer_brugt": b.get("used_hours"),
                "supplerende_måler_timer_overlap": b.get("overlap_hours")}


class StatusSensor(VarmemodelEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["waiting", "ok", "partial", "insufficient", "frozen", "error"]

    def __init__(self, coordinator: VarmemodelCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        last = c.runs[-1] if c.runs else {}
        hrs = sorted(int(k) for k in c.collected)
        return {
            "note": c.status_note or None,
            "tilstand": c.mode,
            "varmekilder": [s.name for s in c.sources.values()],
            "rum": [r["name"] or r["key"] for r in c.rooms_cfg],
            "seneste_kalibrering": _ts_local(last.get("at")),
            "timer_brugt": last.get("hours"),
            "begrænsede_tal": last.get("limited") or [],
            "opsamlet_fra": _ts_local(hrs[0]) if hrs else None,
            "opsamlede_timer": len(hrs),
            "følere": {"ude": c.conf.get("outdoor_temp"), "vejr": c.conf.get("weather")},
        }


def _room_name(coordinator: VarmemodelCoordinator, source: str) -> str:
    st = coordinator.hass.states.get(source)
    return (st.name if st else source.split(".")[-1]).removesuffix(" Temperature").removesuffix(" temperatur")


class RoomEntity(VarmemodelEntity):
    def __init__(self, coordinator: VarmemodelCoordinator, room: dict[str, Any], key: str) -> None:
        sub = room.get("subentry_id")
        src = coordinator.room_source(room)
        super().__init__(coordinator, key, sub_id=sub, sub_name=room.get("name"), model="Rum",
                         unique_id=f"{coordinator.entry.entry_id}_{key}_{room['key']}",
                         parent_sub_id=src.sid if src is not None else None)
        self.source = room["key"]
        if sub:
            self._attr_translation_key = f"{key}_device"
        else:
            self._attr_translation_placeholders = {"room": room.get("name") or _room_name(coordinator, room["key"])}


class RoomSensor(RoomEntity, SensorEntity):
    """Et rums afkøling (°C pr. time ved 20 graders forskel) – falder når rummet isoleres bedre."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/h"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: VarmemodelCoordinator, room: dict[str, Any]) -> None:
        super().__init__(coordinator, room, "room")

    @property
    def native_value(self) -> float | None:
        r = self.coordinator.rooms.get(self.source)
        return r.get("value") if r else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self.coordinator.rooms.get(self.source) or {}
        e = r.get("extra") or {}
        return {
            "temperaturføler": self.source,
            "min_entitet": (cfg := next((x for x in self.coordinator.rooms_cfg if x["key"] == self.source), {})).get("min_entity"),
            "telestat": cfg.get("valve") or ("altid åben" if cfg.get("always_open") else None),
            "i_plan": cfg.get("in_plan"),
            # Rummets gulv (min.) og loft (maks.) lige nu – til kortenes farver og planens grænser
            "gulv": (self.coordinator._state_num(cfg.get("min_entity")) if cfg.get("min_entity")
                     else (float(cfg["min"]) if cfg.get("min") is not None else None)),
            "loft": float(cfg["max"]) if cfg.get("max") is not None else None,
            "varmekilde": (lambda s: s.name if s else None)(self.coordinator.room_source(next((x for x in self.coordinator.rooms_cfg if x["key"] == self.source), {}))),
            "tidskonstant_timer": e.get("time_constant_h"),
            "varmekilder_grader_pr_kwh": {k: v.get("total") for k, v in (e.get("sources") or {}).items()} or None,
            "varmekilder_detaljer": e.get("sources"),
            "nabozoner": e.get("neighbours"),
            "varmekilder_for_lidt_data": e.get("sources_too_little_data") or None,
            "gratis_varme_grader_pr_døgn": e.get("free_heat_k_per_day"),
            "sol_grader_pr_klar_time": e.get("sun_k_per_clear_hour"),
            "præcision_6_timer": e.get("accuracy_6h_k"),
            "præcision_6_timer_afprøvet": e.get("validated_6h_k"),
            "skævhed_6_timer_afprøvet": e.get("validated_bias_6h_k"),
            "afprøvede_vinduer": e.get("validated_windows"),
            "forklaringsgrad": r.get("r2"),
            "blokke": r.get("n"),
            "udeladt_udluftning": e.get("aired_blocks_skipped"),
            "udeladt_afvigere": e.get("outliers_skipped"),
            "begrænsning": r.get("guard"),
            "beregnet": _ts_local(r.get("fitted_at")),
            "note": r.get("note") or None,
        }


class RoomHeatSensor(RoomEntity, SensorEntity):
    """Varme til rummet pr. døgn (snit af de seneste 7 døgn), fordelt efter telestatens åbningstid."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh/d"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: VarmemodelCoordinator, room: dict[str, Any]) -> None:
        super().__init__(coordinator, room, "room_heat")

    @property
    def native_value(self) -> float | None:
        r = self.coordinator.room_heat.get(self.source)
        return r.get("kwh_per_day") if r and r.get("valve_data") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self.coordinator.room_heat.get(self.source) or {}
        return {
            "i_går_kwh": r.get("yesterday_kwh"),
            "andel_af_gulvvarmen_pct": r.get("share_pct"),
            "åben_med_varme_timer_pr_døgn": r.get("open_hours_per_day"),
            "brat_afkøling_pr_døgn": r.get("sudden_drops_per_day"),
            "døgn": r.get("days"),
            "varmekilde": r.get("source"),
            "metode": "den vandbårne varme fordelt efter kredsstørrelse × minutter åben mens kompressoren kørte",
        }


def _sensors(src: SourceState) -> dict[str, Any]:
    """Varmekildens målinger fra opsætningen (entiteter og deres attribut-/nulpunkt-felter)."""
    return {k: v for k, v in src.conf.items() if k not in ("name", "source_type", "legacy_ids") and v not in (None, "", [])}


def _now_hour(plan: dict[str, Any]) -> dict[str, Any] | None:
    import time as _t

    h0 = int(_t.time() // 3600 * 3600)
    return next((h for h in plan.get("hours") or [] if h["ts"] == h0), None)


class PlanNowSensor(SourceEntity, SensorEntity):
    """Hvad planen siger lige nu: varme, slukket eller blokeret. Hele planen ligger i attributterne."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["heat", "off", "blocked", "unknown"]
    _unrecorded_attributes = frozenset({"plan", "rooms_forecast", "history", "rooms_check", "rooms_band", "røde_perioder", "problemer"})

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState) -> None:
        super().__init__(coordinator, s, "plan_now")

    @property
    def _plan(self) -> dict[str, Any]:
        return self.coordinator.plans.get(self.src.sid) or {}

    @property
    def native_value(self) -> str:
        h = _now_hour(self._plan)
        if h is None:
            return "unknown"
        return "blocked" if h["blocked"] else ("heat" if h["heat_kw"] > 0 else "off")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p, m = self._plan, self.coordinator.plan_meta
        h = _now_hour(p) or {}
        hrs = p.get("hours") or []
        nxt_on = next((x["ts"] for x in hrs if x["heat_kw"] > 0 and x["ts"] > (h.get("ts") or 0)), None)
        nxt_off = next((x["ts"] for x in hrs if x["heat_kw"] == 0 and x["ts"] > (h.get("ts") or 0)), None)
        spans = self.coordinator.red_spans
        if nxt_on is not None:
            nxt_on = after_red(nxt_on, spans)  # rød med kvarterer: fx 10:00–10:15 rødt → start 10:15
        upd = m.get("updated")
        return {
            "heat_kw": h.get("heat_kw"), "flow": h.get("flow"), "price": h.get("price"),
            "t_out": h.get("t_out"), "cop": h.get("cop"),
            "reason": p.get("reason"), "warnings": p.get("warnings") or None, "target": p.get("target"),
            # Rum, der forudsiges under grænsen (besked forud): laveste temperatur og tid, og hvornår det kommer under
            "problemer": [{"rum": x["rum"], "kl": _ts_local(x["ts"]), "fra": _ts_local(x["fra_ts"]), "temp": x["temp"],
                           "grænse": x["grænse"], "under": x["under"], "blokeret": x["blokeret"]} for x in p.get("problems") or []] or None,
            "boost": p.get("boost"), "rooms_skipped": p.get("rooms_skipped") or None, "rooms_held": p.get("rooms_held") or None,
            "rooms_estimated": p.get("rooms_estimated") or None,
            "demand_kwh": p.get("demand_kwh"), "heat_kwh": p.get("heat_kwh"), "el_kwh": p.get("el_kwh"), "cost_kr": p.get("cost_kr"),
            "next_on": _ts_local(nxt_on), "next_off": _ts_local(nxt_off),
            "updated": _ts_local(upd), "valid_until": _ts_local(upd + 2700) if upd else None,
            "inputs_ok": m.get("inputs_ok"), "missing": m.get("missing") or None, "horizon_h": m.get("horizon_h"),
            "plan": [{"t": _ts_local(x["ts"]), **{k: x.get(k) for k in ("heat_kw", "flow", "price", "t_out", "blocked", "cost")},
                      **({"setpoint": x["setpoint"]} if x.get("setpoint") is not None else {}),
                      **({"preload": x["preload"]} if x.get("preload") else {})} for x in hrs],
            "kind": p.get("kind") or "hydronic", "caps": p.get("caps") or None, "e_min": p.get("e_min"), "e_max": p.get("e_max"),
            "rooms_forecast": p.get("rooms"),
            "rooms_check": self.coordinator.rooms_check or None,
            "rooms_planned": p.get("rooms_planned"),
            "rooms_band": p.get("rooms_band"), "cautious": p.get("cautious"), "safety": p.get("safety"),
            "fresh_pct_today": m.get("fresh_pct_today"),
            "følere": _sensors(self.src),
            "boost_until": p.get("boost_until"), "boost_delta": p.get("boost_delta"), "red_allowance": p.get("red_allowance"), "preload": p.get("preload"),
            "preload_window": (lambda w: {"fra": _ts_local(w["from"]), "til": _ts_local(w["to"]), "blokering_fra": _ts_local(w["block_from"]),
                                          "blokering_til": _ts_local(w["block_to"]), "kwh": w["kwh"]} if w else None)(p.get("preload_window")),
            "flow_boost": (lambda b: {"grader": b["k"], "rum": b["rooms"], "siden": _ts_local(b["t"])} if b else None)(p.get("flow_boost")),
            "flow_raise": p.get("flow_raise"),
            "røde_perioder": [{"fra": _ts_local(a), "til": _ts_local(b)} for a, b in spans] or None,
            "max_pause": p.get("max_pause"), "comfort_offset": p.get("comfort_offset"),
            "history": [{"t": _ts_local(int(k)), "plan": v["p"], "price": v["pr"], "blocked": v["b"]}
                        for k, v in sorted((self.coordinator.plan_log.get(self.src.sid) or {}).items(), key=lambda kv: int(kv[0]))[-48:]],
        }


class PlanValueSensor(SourceEntity, SensorEntity):
    """Planens fremløb i denne time (tom når planen ikke varmer)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState, key: str, field: str, unit: str | None, prec: int) -> None:
        super().__init__(coordinator, s, key)
        self.field = field
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = prec

    @property
    def _next(self) -> dict[str, Any] | None:
        p = self.coordinator.plans.get(self.src.sid) or {}
        h = _now_hour(p) or {}
        return next((x for x in p.get("hours") or [] if x.get(self.field) is not None and x["ts"] >= (h.get("ts") or 0)), None)

    @property
    def native_value(self) -> float | None:
        # Varmer planen ikke nu, vises næste planlagte værdi (attributterne siger fra hvornår)
        x = self._next
        return x.get(self.field) if x else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        h = _now_hour(self.coordinator.plans.get(self.src.sid) or {}) or {}
        x = self._next
        return {"varmer_nu": bool(h.get("heat_kw")), "gælder_fra": _ts_local(x["ts"]) if x else None}


class PlanCostSensor(SourceEntity, SensorEntity):
    """Planens forventede elpris for hele planperioden."""

    _attr_native_unit_of_measurement = "kr"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState) -> None:
        super().__init__(coordinator, s, "plan_cost")

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.plans.get(self.src.sid) or {}).get("cost_kr")

    _unrecorded_attributes = frozenset({"i_går_timer"})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.coordinator.plans.get(self.src.sid) or {}
        y = dict(self.coordinator.yesterday.get(self.src.sid) or {})
        hrs = y.pop("timer", None)
        return {"heat_kwh": p.get("heat_kwh"), "el_kwh": p.get("el_kwh"), "demand_kwh": p.get("demand_kwh"),
                "horizon_h": self.coordinator.plan_meta.get("horizon_h"), "i_går": y or None, "i_går_timer": hrs}


class MeasuresSensor(VarmemodelEntity, SensorEntity):
    """Energitiltag fra kalenderen med varmetabet før og efter (antal tiltag med resultat)."""

    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: VarmemodelCoordinator) -> None:
        super().__init__(coordinator, "measures")

    @property
    def native_value(self) -> int:
        return sum(1 for m in self.coordinator.measures if m.get("ændring_pct") is not None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import CONF_MEASURES_CALENDAR

        return {"kalender": self.coordinator.conf.get(CONF_MEASURES_CALENDAR), "tiltag": self.coordinator.measures or None}


class RoomForecastSensor(RoomEntity, SensorEntity):
    """Det, rummodellen for 6 timer siden forudsagde for nu. Historikken viser, om forudsigelserne holder."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: VarmemodelCoordinator, room: dict[str, Any]) -> None:
        super().__init__(coordinator, room, "room_forecast")
        self.room_name = room.get("name") or room["key"]

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.rooms_check.get(self.room_name) or {}).get("ventet")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        k = self.coordinator.rooms_check.get(self.room_name) or {}
        return {"målt": k.get("målt"), "afvigelse": k.get("afvigelse"), "status": k.get("status"), "grænse": k.get("grænse"), "afviger": k.get("flag"),
                "ventet_med_plan": k.get("ventet_med_plan"), "grundlag": k.get("grundlag")}


class CommandSensor(SourceEntity, SensorEntity):
    """Anbefalet styring: hvad en automation bør sende nu, efter reglerne der skåner varmepumpen.

    Tællerne pr. døgn viser, hvor mange skrivninger og tænd/sluk-skift planen giver."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["heat", "off"]

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState) -> None:
        super().__init__(coordinator, s, "command")

    @property
    def _c(self) -> dict[str, Any]:
        return self.coordinator.commands.get(self.src.sid) or {}

    @property
    def native_value(self) -> str | None:
        c = self._c
        return None if not c else ("heat" if c.get("on") else "off")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._c
        since = c.get("since_onoff")
        return {
            # Vandbåren: fremløbsmålet. Luft-luft: setpunkt.
            "flow_setpoint": c.get("flow") if self.src.kind == SOURCE_HYDRONIC else None,
            # Blød start: det fremløb, der faktisk er skrevet nu (planens mål nås med +2 °C pr. kvarter)
            "soft_start_flow": (self.coordinator.air.get(f"hydro:{self.src.sid}") or {}).get("soft_now")
            if self.src.kind == SOURCE_HYDRONIC else None,
            # Søg laveste fremløb: mål, plan, status, grund, rummenes margen/tendens, lært pr. ude-bånd og log
            "flow_search": self.coordinator.flow_search(self.src) if self.src.kind == SOURCE_HYDRONIC else None,
            # Kompressoren kører siden (fra historikken, så en genstart ikke nulstiller »stabil drift«)
            "compressor_on_since": _ts_local(cs) if (cs := (self.coordinator.air.get(f"hydro:{self.src.sid}") or {}).get("comp_since")) else None,
            "compressor_checked": _ts_local(ck) if (ck := (self.coordinator.air.get(f"hydro:{self.src.sid}") or {}).get("comp_checked")) else None,
            "setpoint": c.get("setpoint"), "fan_mode": c.get("fan_mode"), "preset": c.get("preset"), "step": c.get("step"),
            "reason": c.get("reason"),
            "since": _ts_local(since) if since and since > 0 else None,
            "writes_today": c.get("writes", 0), "switches_today": c.get("flips", 0), "heat_in_blocked_today": c.get("blocked_on", 0),
            "within_limits": c.get("flips", 0) <= 8 and c.get("blocked_on", 0) == 0,
        }


class AirStepSensor(SourceEntity, SensorEntity):
    """Luft-luft-stigen: trinnet nu, trinene (blæser og setpunkt) med lært forbrug, margen og forvarmning pr. rum."""

    _attr_icon = "mdi:stairs"

    def __init__(self, coordinator: VarmemodelCoordinator, s: SourceState) -> None:
        super().__init__(coordinator, s, "air_step")

    @property
    def _v(self) -> dict[str, Any]:
        return (self.coordinator.air.get(self.src.sid) or {}).get("view") or {}

    @property
    def native_value(self) -> int | None:
        return self._v.get("trin")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._v)
