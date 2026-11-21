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
from .coordinator import SourceState, VarmemodelCoordinator
from .entity import VarmemodelEntity

PRECISION = {"heat_loss": 0, "cop": 2, "cop_steady": 2, "cop_loss": 1, "emission": 3, "max_power": 2, "min_power": 2,
             "curve": 1, "defrost": 2, "cooling": 0, "wind_effect": 1}


def _ts_local(ts: float | None) -> str | None:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat() if ts else None


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([StatusSensor(c)])
    for sid, s in c.sources.items():
        ents: list[SensorEntity] = []
        if s.kind == SOURCE_HYDRONIC:
            ents += [FitSensor(c, s, k) for k in FIT_KEYS]
            ents += [DailyCopSensor(c, s, "yesterday"), DailyCopSensor(c, s, "week")]
        else:
            ents += [DailyElSensor(c, s, "yesterday"), DailyElSensor(c, s, "week")]
        async_add_entities(ents, config_subentry_id=sid)
    for r in c.rooms_cfg:
        rents: list[SensorEntity] = [RoomSensor(c, r)]
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
        return {"ude_snit": d.get("t_out_mean"), "døgn": d.get("days"),
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
            "kilde": self.source,
            "varmekilde": (lambda s: s.name if s else None)(self.coordinator.room_source(next((x for x in self.coordinator.rooms_cfg if x["key"] == self.source), {}))),
            "tidskonstant_timer": e.get("time_constant_h"),
            "varmekilder_grader_pr_kwh": {k: v.get("total") for k, v in (e.get("sources") or {}).items()} or None,
            "varmekilder_detaljer": e.get("sources"),
            "nabozoner": e.get("neighbours"),
            "varmekilder_for_lidt_data": e.get("sources_too_little_data") or None,
            "gratis_varme_grader_pr_døgn": e.get("free_heat_k_per_day"),
            "sol_grader_pr_klar_time": e.get("sun_k_per_clear_hour"),
            "præcision_6_timer": e.get("accuracy_6h_k"),
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
