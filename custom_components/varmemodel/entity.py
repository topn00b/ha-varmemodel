"""Fælles entitetsbase: huset, varmekilder og rum har hver deres enhed."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_NAME, DEFAULT_NAME, DOMAIN
from .coordinator import VarmemodelCoordinator


class VarmemodelEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: VarmemodelCoordinator, key: str, *, sub_id: str | None = None,
                 sub_name: str | None = None, model: str = "Rum", unique_id: str | None = None,
                 parent_sub_id: str | None = None) -> None:
        self.coordinator = coordinator
        entry = coordinator.entry
        self._attr_unique_id = unique_id or f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        if sub_id:
            # Hver varmekilde og hvert rum (underpunkt) har sin egen enhed under huset
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry.entry_id}_{sub_id}")}, name=sub_name, manufacturer="Varmemodel",
                model=model, entry_type=DeviceEntryType.SERVICE,
                # Træet: huset → varmekilde → rummene den varmer
                via_device=(DOMAIN, f"{entry.entry_id}_{parent_sub_id}") if parent_sub_id else (DOMAIN, entry.entry_id),
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry.entry_id)}, name=entry.title or entry.data.get(CONF_NAME) or DEFAULT_NAME,
                manufacturer="Varmemodel", model="Hus", entry_type=DeviceEntryType.SERVICE,
            )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
