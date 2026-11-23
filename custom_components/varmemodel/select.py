"""Valg: lær løbende eller fastfrys tallene."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from homeassistant.const import EntityCategory

from .const import AIR_SELECTS, CONF_ONOFF_TARGET, MODE_CALIBRATED, MODE_FROZEN, SOFT_OPTIONS, SOFT_START
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

    class SoftStartSelect(SourceEntity, SelectEntity):
        """Blød start for den vandbårne varmepumpe: fra, til eller test (hver anden start) – med resultatet i attributterne."""

        _attr_entity_category = EntityCategory.CONFIG
        _attr_options = list(SOFT_OPTIONS)

        def __init__(self, s) -> None:  # noqa: ANN001
            super().__init__(c, s, SOFT_START)

        @property
        def current_option(self) -> str:
            return c.settings.get(f"{self.src.sid}.{SOFT_START}", "test")

        @property
        def extra_state_attributes(self) -> dict:
            return {"resultat_ude_under_6": c.soft_summary(self.src)}

        async def async_select_option(self, option: str) -> None:
            await c.async_set_air(self.src, SOFT_START, option)

    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirSelect(s, k) for k in AIR_SELECTS], config_subentry_id=sid)
        elif s.conf.get(CONF_ONOFF_TARGET):
            async_add_entities([SoftStartSelect(s)], config_subentry_id=sid)


class ModeSelect(VarmemodelEntity, SelectEntity):
    _attr_options = [MODE_CALIBRATED, MODE_FROZEN]

    @property
    def current_option(self) -> str:
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode(option)
