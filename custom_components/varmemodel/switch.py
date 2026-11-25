"""Kontakt pr. luft-luft-kilde: Varmemodel styrer varmepumpen (fra = kun anbefaling)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VarmemodelConfigEntry
import time as _time

from .const import AIR_BOOST, AIR_SWITCH, CONF_DHW_SWITCH, CONF_DHW_TEMP, CONF_FLOW_TARGET, CONF_ONOFF_TARGET, DHW_CONTROL, FLOW_SWITCH, HYDRO_SWITCH
from .sensor import SourceEntity


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    for sid, s in c.sources.items():
        if c.air_climate(s):
            async_add_entities([AirControlSwitch(c, s), AirBoostSwitch(c, s)], config_subentry_id=sid)
        else:
            if s.conf.get(CONF_FLOW_TARGET):
                async_add_entities([FlowControlSwitch(c, s)], config_subentry_id=sid)
            if s.conf.get(CONF_ONOFF_TARGET):
                async_add_entities([HydroControlSwitch(c, s)], config_subentry_id=sid)
            if s.eid(CONF_DHW_TEMP) and s.eid(CONF_DHW_SWITCH):
                async_add_entities([DhwControlSwitch(c, s)], config_subentry_id=sid)


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


class AirBoostSwitch(SourceEntity, SwitchEntity):
    """»Jeg fryser«: øverste trin (powerful, hvis varmepumpen har det) – også i blokerede timer – i højst
    »Jeg fryser gælder højst« timer; slår selv fra bagefter."""

    _attr_icon = "mdi:snowflake-thermometer"

    def __init__(self, coordinator: Any, s: Any) -> None:
        super().__init__(coordinator, s, AIR_BOOST)

    @property
    def is_on(self) -> bool:
        return self.coordinator.air_boost_active(self.src)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_air(self.src, AIR_BOOST, _time.time())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_air(self.src, AIR_BOOST, 0)


class FlowControlSwitch(SourceEntity, SwitchEntity):
    """Vandbåren: Varmemodel skriver fremløbsmålet (tænd/sluk og termostater styres stadig andetsteds)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, s: Any) -> None:
        super().__init__(coordinator, s, FLOW_SWITCH)

    @property
    def is_on(self) -> bool:
        return self.coordinator.flow_control(self.src)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        f = self.coordinator.air.get(f"flow:{self.src.sid}") or {}
        return {"skrivninger_i_dag": f.get("writes", 0), "seneste": f.get("last")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_flow_control(self.src, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_flow_control(self.src, False)


class HydroControlSwitch(SourceEntity, SwitchEntity):
    """Vandbåren: Varmemodel tænder og slukker varmepumpen og skriver fremløbet (termostater og varmt vand styres andetsteds)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, s: Any) -> None:
        super().__init__(coordinator, s, HYDRO_SWITCH)

    @property
    def is_on(self) -> bool:
        return self.coordinator.hydro_control(self.src)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        h = self.coordinator.air.get(f"hydro:{self.src.sid}") or {}
        return {"skift_i_dag": h.get("flips", 0), "seneste": h.get("last")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_hydro_control(self.src, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_hydro_control(self.src, False)


class DhwControlSwitch(SourceEntity, SwitchEntity):
    """Vandbåren: Varmemodel styrer varmt vand – skriver planens fra / normalt / højt til varmepumpen (mål først, så til),
    aldrig til i en blokering (undtagen »bad nu«). Fra: planen er kun en anbefaling."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-boiler-auto"

    def __init__(self, coordinator: Any, s: Any) -> None:
        super().__init__(coordinator, s, DHW_CONTROL)

    @property
    def is_on(self) -> bool:
        return self.coordinator.dhw_control(self.src)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dhw_control(self.src, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_dhw_control(self.src, False)
