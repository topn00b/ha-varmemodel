"""Kalibreringstallene som sensorer – ét tal pr. sensor, så de kan vises som grafer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VarmemodelConfigEntry
from .const import FIT_KEYS
from .coordinator import VarmemodelCoordinator
from .entity import VarmemodelEntity

PRECISION = {"heat_loss": 0, "cop": 2, "emission": 3, "max_power": 2, "min_power": 2, "curve": 1, "defrost": 2, "cooling": 0}


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([FitSensor(c, k) for k in FIT_KEYS] + [StatusSensor(c)])


class FitSensor(VarmemodelEntity, SensorEntity):
    """Et kalibreret tal. Attributterne viser hvor det kommer fra."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: VarmemodelCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_native_unit_of_measurement = FIT_KEYS[key]
        self._attr_suggested_display_precision = PRECISION.get(key)

    @property
    def native_value(self) -> float | None:
        a = self.coordinator.accepted.get(self.key)
        return a.get("value") if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self.coordinator.accepted.get(self.key) or {}
        f = self.coordinator.fits.get(self.key) or {}
        ts = a.get("fitted_at")
        return {
            "kilde": a.get("source", "ingen data"),
            "begrænsning": a.get("guard"),
            "beregnet_værdi": f.get("value"),
            "målinger": f.get("n"),
            "forklaringsgrad": None if f.get("r2") is None else round(f["r2"], 3),
            "ude_fra": a.get("t_out_min"),
            "ude_til": a.get("t_out_max"),
            "beregnet": dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat() if ts else None,
            "note": f.get("note") or None,
            **(a.get("extra") or {}),
        }


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
            "seneste_kalibrering": dt_util.as_local(dt_util.utc_from_timestamp(last["at"])).isoformat() if last else None,
            "timer_brugt": last.get("hours"),
            "begrænsede_tal": last.get("limited") or [],
            "opsamlet_fra": dt_util.as_local(dt_util.utc_from_timestamp(hrs[0])).isoformat() if hrs else None,
            "opsamlede_timer": len(hrs),
        }
