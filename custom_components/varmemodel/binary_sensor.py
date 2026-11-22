"""»Mangler data«: tændt, når en rum- eller udeføler har været væk i over 30 min.

Imens kører planen videre på forudsigelsen (udeføleren: vejrudsigten). Hvert manglende datapunkt får også en
reparation i Home Assistant, så det kan udbedres hurtigst muligt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .coordinator import VarmemodelCoordinator
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([MissingDataSensor(entry.runtime_data)])


class MissingDataSensor(VarmemodelEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: VarmemodelCoordinator) -> None:
        super().__init__(coordinator, "missing_data")

    @property
    def is_on(self) -> bool:
        md = self.coordinator.missing_data
        return bool(md.get("rum") or md.get("udeføler"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        md = self.coordinator.missing_data
        return {"rum": md.get("rum") or {}, "udeføler": md.get("udeføler")}
