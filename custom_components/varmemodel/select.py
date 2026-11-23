"""Valg: lær løbende eller fastfrys tallene."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from homeassistant.const import EntityCategory

from .const import AIR_SELECTS, MODE_CALIBRATED, MODE_FROZEN
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([ModeSelect(c, "mode")])
    from .sensor import SourceEntity  # noqa: PLC0415 – undgår cirkulær import

    class AirSelect(SourceEntity, SelectEntity):
        """Maks. blæser dag/nat og lamellerne – valgene læses fra klimaentiteten."""

        _attr_entity_category = EntityCategory.CONFIG

        def __init__(self, s, key) -> None:  # noqa: ANN001
            super().__init__(c, s, key)
            self.key = key

        @property
        def options(self) -> list[str]:
            return c.air_options(self.src, self.key)

        @property
        def current_option(self) -> str | None:
            v = c.air_setting(self.src, self.key)
            return v if v in self.options else None

        async def async_select_option(self, option: str) -> None:
            await c.async_set_air(self.src, self.key, option)

    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirSelect(s, k) for k in AIR_SELECTS], config_subentry_id=sid)


class ModeSelect(VarmemodelEntity, SelectEntity):
    _attr_options = [MODE_CALIBRATED, MODE_FROZEN]

    @property
    def current_option(self) -> str:
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode(option)
