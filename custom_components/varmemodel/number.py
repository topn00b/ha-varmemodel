"""Planens indstillinger som tal: maks. pause, komfort, »jeg fryser« og hvor meget rum må falde i blokerede timer.

Indstillingerne påvirker kun planen. Varmemodel styrer ikke selv noget."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .const import AIR_NUMBERS, SETTINGS
from .coordinator import VarmemodelCoordinator
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([SettingNumber(c, *s) for s in SETTINGS])
    from .sensor import SourceEntity  # noqa: PLC0415 – undgår cirkulær import

    class AirNumber(SourceEntity, NumberEntity):
        _attr_mode = NumberMode.BOX
        _attr_entity_category = EntityCategory.CONFIG

        def __init__(self, s, key, lo, hi, step, unit, _default) -> None:  # noqa: ANN001
            super().__init__(c, s, key)
            self.key = key
            self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step = lo, hi, step
            self._attr_native_unit_of_measurement = unit

        @property
        def native_value(self) -> float:
            return float(c.air_setting(self.src, self.key))

        async def async_set_native_value(self, value: float) -> None:
            await c.async_set_air(self.src, self.key, value)

    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirNumber(s, *n) for n in AIR_NUMBERS], config_subentry_id=sid)


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
