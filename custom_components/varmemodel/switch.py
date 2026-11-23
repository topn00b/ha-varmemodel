"""Kontakt pr. luft-luft-kilde: Varmemodel styrer varmepumpen (fra = kun anbefaling)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .const import AIR_SWITCH
from .sensor import SourceEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirControlSwitch(c, s)], config_subentry_id=sid)


class AirControlSwitch(SourceEntity, SwitchEntity):
    """Til: Varmemodel skriver tænd/sluk, blæser og setpunkt til klimaentiteten. Fra: kun anbefaling."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, s: Any) -> None:
        super().__init__(coordinator, s, AIR_SWITCH)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.air_setting(self.src, AIR_SWITCH))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_air(self.src, AIR_SWITCH, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_air(self.src, AIR_SWITCH, False)
