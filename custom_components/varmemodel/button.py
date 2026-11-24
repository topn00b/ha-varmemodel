"""Knap: kalibrér nu."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([CalibrateButton(c, "recalibrate")])
    from .sensor import SourceEntity  # noqa: PLC0415 – undgår cirkulær import

    class AirRelearnButton(SourceEntity, ButtonEntity):
        """Lær stigens virkning pr. trin forfra fra historikken (fx efter at en forstyrrelse er markeret »ignorer«)."""

        def __init__(self, s) -> None:  # noqa: ANN001
            super().__init__(c, s, "air_relearn")

        @property
        def extra_state_attributes(self) -> dict:
            return {"seneste": (c.air.get(self.src.sid) or {}).get("bf_eff_info")}

        async def async_press(self) -> None:
            await c.async_air_relearn(self.src)

    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirRelearnButton(s)], config_subentry_id=sid)


class CalibrateButton(VarmemodelEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.coordinator.async_calibrate()
