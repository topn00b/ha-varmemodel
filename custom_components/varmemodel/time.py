"""Tider: pr. luft-luft-kilde hvornår natten (med sin egen maks. blæser) begynder og slutter; på huset brugstiden for
varmt vand (fra–til; ens = den lærte brugstid)."""

from __future__ import annotations

from datetime import time as dtime
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .const import AIR_TIMES, CONF_DHW_TEMP, DHW_TIMES
from .entity import VarmemodelEntity
from .sensor import SourceEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirTime(c, s, k) for k, _ in AIR_TIMES], config_subentry_id=sid)
    if any(s.eid(CONF_DHW_TEMP) for s in c.sources.values() if not c.air_climate(s)):
        async_add_entities([DhwTime(c, k) for k, _ in DHW_TIMES])


class AirTime(SourceEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, s: Any, key: str) -> None:
        super().__init__(coordinator, s, key)
        self.key = key

    @property
    def native_value(self) -> dtime | None:
        try:
            h, m = str(self.coordinator.air_setting(self.src, self.key)).split(":")[:2]
            return dtime(int(h), int(m))
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: dtime) -> None:
        await self.coordinator.async_set_air(self.src, self.key, value.strftime("%H:%M"))


class DhwTime(VarmemodelEntity, TimeEntity):
    """Brugstiden for varmt vand: fra og til. Uden for den må tanken gerne være kold (komfort: ikke under »mindst«).
    Står fra og til ens, bruges den brugstid, der er lært af tankens fald."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key

    @property
    def native_value(self) -> dtime | None:
        try:
            h, m = self.coordinator.dhw_time(self.key).split(":")[:2]
            return dtime(int(h), int(m))
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: dtime) -> None:
        await self.coordinator.async_set_dhw_time(self.key, value.strftime("%H:%M"))
