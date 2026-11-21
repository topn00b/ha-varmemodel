"""Knap: kalibrér nu."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
from .entity import VarmemodelEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([CalibrateButton(entry.runtime_data, "recalibrate")])


class CalibrateButton(VarmemodelEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.coordinator.async_calibrate()
