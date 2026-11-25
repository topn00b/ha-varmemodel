"""Planens indstillinger som tal: maks. pause, komfort, »jeg fryser« og hvor meget rum må falde i blokerede timer.

Indstillingerne påvirker kun planen. Varmemodel styrer ikke selv noget."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .const import CONF_DHW_TEMP, DHW_SETTINGS, SETTINGS, SOURCE_HYDRONIC
from .coordinator import VarmemodelCoordinator
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([SettingNumber(c, *s) for s in SETTINGS])
    # Varmt vand: brugerens tre temperaturer – kun når en vandbåren kilde har en tankføler
    if any(s.kind == SOURCE_HYDRONIC and s.eid(CONF_DHW_TEMP) for s in c.sources.values()):
        async_add_entities([SettingNumber(c, *s) for s in DHW_SETTINGS])


class SettingNumber(VarmemodelEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: VarmemodelCoordinator, key: str, lo: float, hi: float, step: float, unit: str, _default: float) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float:
        return self.coordinator.setting(self.key)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_setting(self.key, value)
