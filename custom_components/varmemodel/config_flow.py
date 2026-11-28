"""Opsætning: huset (ude, vejr), varmekilder og rum som underpunkter – intet er kodet fast."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ASSIST,
    CONF_EL_POWER,
    CONF_FLOW_TARGET,
    CONF_ONOFF_FALLBACK,
    CONF_ONOFF_TARGET,
    CONF_ASSIST_SPEED,
    CONF_CLIMATE,
    CONF_COMPRESSOR,
    CONF_DEFROST,
    CONF_DHW,
    CONF_DHW_BOOST,
    CONF_DHW_FALLBACK,
    CONF_LOOP_PUMP,
    CONF_LOOP_RETURN,
    CONF_LOOP_SUPPLY,
    CONF_DHW_SWITCH,
    CONF_DHW_TARGET,
    CONF_DHW_TEMP,
    CONF_ELECTRICITY,
    CONF_ELECTRICITY_BACKUP,
    CONF_FLOW,
    CONF_HEAT,
    CONF_INDOOR,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_RETURN,
    CONF_ROOM_ALWAYS_OPEN,
    CONF_ROOM_IN_PLAN,
    CONF_ROOM_MIN,
    CONF_ROOM_MAX,
    CONF_ROOM_MIN_ENTITY,
    CONF_ROOM_NAME,
    CONF_ROOM_NEIGHBOURS,
    CONF_ROOM_SOURCE,
    CONF_ROOM_EXTRA_SOURCES,
    CONF_ROOM_TEMP,
    CONF_ROOM_VALVE,
    CONF_ROOM_WEIGHT,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_UNIT_TEMP,
    CONF_UNIT_TEMP_ATTR,
    CONF_WEATHER,
    CONF_WINDOW_DAYS,
    CONF_PRICE_ENTITY,
    CONF_PRICE_ATTR,
    CONF_BLOCK_ENTITY,
    CONF_BLOCK_CODES,
    CONF_BLOCK_NOW_ENTITY,
    CONF_BLOCK_QUARTERS_ENTITY,
    CONF_PRICE_CAP_ENTITY,
    CONF_BLOCK_NOW_STATES,
    DEFAULT_BLOCK_NOW_STATES,
    CONF_BOOST_ENTITY,
    CONF_MEASURES_CALENDAR,
    CONF_PLANT_ROOM,
    CONF_PLANT_LOSS,
    CONF_GROUND_TEMP,
    DEFAULT_PLANT_LOSS,
    DEFAULT_GROUND_TEMP,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
    SOURCE_AIR_AIR,
    SOURCE_HYDRONIC,
    SUBENTRY_ROOM,
    SUBENTRY_SOURCE,
)

ENERGY = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="energy"))
TEMP = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature"))
TEMPS = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True))
WEATHER = selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))
BINARY = selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))
TEXT = selector.TextSelector()
DAYS = selector.NumberSelector(selector.NumberSelectorConfig(min=14, max=365, step=1, mode=selector.NumberSelectorMode.BOX))
TYPE = selector.SelectSelector(
    selector.SelectSelectorConfig(options=[SOURCE_HYDRONIC, SOURCE_AIR_AIR], translation_key="source_type", mode=selector.SelectSelectorMode.LIST)
)

# Felter, der kræver langtidsstatistik
STAT_KEYS = (CONF_ELECTRICITY, CONF_ELECTRICITY_BACKUP, CONF_HEAT, CONF_OUTDOOR, CONF_FLOW, CONF_RETURN)


def _house_schema(with_name: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME, default="Hus")] = TEXT
    fields.update({
        vol.Required(CONF_OUTDOOR): TEMP,
        vol.Optional(CONF_WEATHER): WEATHER,
        vol.Optional(CONF_INDOOR): TEMPS,
        vol.Optional(CONF_WINDOW_DAYS, default=DEFAULT_WINDOW_DAYS): DAYS,
        vol.Optional(CONF_PRICE_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_PRICE_ATTR): TEXT,
        vol.Optional(CONF_BLOCK_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_text"])),
        vol.Optional(CONF_BLOCK_CODES, default="r"): TEXT,
        vol.Optional(CONF_BLOCK_NOW_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "binary_sensor", "input_boolean", "input_select"])),
        vol.Optional(CONF_BLOCK_NOW_STATES, default=DEFAULT_BLOCK_NOW_STATES): TEXT,
        vol.Optional(CONF_BLOCK_QUARTERS_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_PRICE_CAP_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain=["input_number", "number"])),
        vol.Optional(CONF_BOOST_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])),
        vol.Optional(CONF_MEASURES_CALENDAR): selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar")),
        vol.Optional(CONF_PLANT_ROOM): TEMP,
        vol.Optional(CONF_PLANT_LOSS, default=DEFAULT_PLANT_LOSS): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="W/K", mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_GROUND_TEMP, default=DEFAULT_GROUND_TEMP): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=20, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)),
    })
    return vol.Schema(fields)


HYDRONIC_SCHEMA = vol.Schema({
    vol.Required(CONF_ELECTRICITY): ENERGY,
    vol.Optional(CONF_ELECTRICITY_BACKUP): ENERGY,
    vol.Optional(CONF_HEAT): ENERGY,
    vol.Optional(CONF_FLOW): TEMP,
    vol.Optional(CONF_RETURN): TEMP,
    vol.Optional(CONF_LOOP_SUPPLY): TEMP,
    vol.Optional(CONF_LOOP_RETURN): TEMP,
    vol.Optional(CONF_LOOP_PUMP): BINARY,
    vol.Optional(CONF_COMPRESSOR): BINARY,
    vol.Optional(CONF_DEFROST): BINARY,
    vol.Optional(CONF_DHW): BINARY,
    vol.Optional(CONF_DHW_TEMP): TEMP,
    vol.Optional(CONF_DHW_SWITCH): selector.EntitySelector(selector.EntitySelectorConfig(domain=["switch", "input_boolean", "water_heater"])),
    vol.Optional(CONF_DHW_TARGET): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "climate", "water_heater", "number", "input_number"])),
    vol.Optional(CONF_DHW_BOOST): selector.EntitySelector(selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])),
    vol.Optional(CONF_DHW_FALLBACK): selector.EntitySelector(selector.EntitySelectorConfig(domain=["water_heater", "switch", "input_boolean"])),
    vol.Optional(CONF_FLOW_TARGET): selector.EntitySelector(selector.EntitySelectorConfig(domain=["climate", "number", "input_number"])),
    vol.Optional(CONF_ONOFF_TARGET): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
    vol.Optional(CONF_ONOFF_FALLBACK): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
})

AIR_AIR_SCHEMA = vol.Schema({
    vol.Required(CONF_ELECTRICITY): ENERGY,
    vol.Optional(CONF_ELECTRICITY_BACKUP): ENERGY,
    vol.Optional(CONF_CLIMATE): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
    vol.Optional(CONF_EL_POWER): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
    vol.Optional(CONF_ASSIST): selector.EntitySelector(selector.EntitySelectorConfig(domain=["fan", "switch", "select", "input_boolean", "input_select"])),
    vol.Optional(CONF_ASSIST_SPEED): selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number", "sensor"])),
    vol.Optional(CONF_UNIT_TEMP): selector.EntitySelector(selector.EntitySelectorConfig(domain=["climate", "sensor"])),
    vol.Optional(CONF_UNIT_TEMP_ATTR): TEXT,
    vol.Optional(CONF_COMPRESSOR): BINARY,
    vol.Optional(CONF_DEFROST): BINARY,
})

ROOM_SCHEMA = vol.Schema({
    vol.Required(CONF_ROOM_NAME): TEXT,
    vol.Required(CONF_ROOM_TEMP): TEMP,
    vol.Optional(CONF_ROOM_MIN_ENTITY): selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["input_number", "number", "sensor", "climate"])
    ),
    vol.Optional(CONF_ROOM_MIN): selector.NumberSelector(
        selector.NumberSelectorConfig(min=5, max=30, step=0.5, mode=selector.NumberSelectorMode.BOX)
    ),
    vol.Optional(CONF_ROOM_MAX): selector.NumberSelector(
        selector.NumberSelectorConfig(min=10, max=30, step=0.5, mode=selector.NumberSelectorMode.BOX)
    ),
    vol.Optional(CONF_ROOM_VALVE): selector.EntitySelector(selector.EntitySelectorConfig(domain=["binary_sensor", "climate", "switch", "valve"])),
    vol.Optional(CONF_ROOM_ALWAYS_OPEN, default=False): selector.BooleanSelector(),
    vol.Optional(CONF_ROOM_WEIGHT, default=1.0): selector.NumberSelector(
        selector.NumberSelectorConfig(min=0.1, max=10, step=0.1, mode=selector.NumberSelectorMode.BOX)
    ),
    vol.Optional(CONF_ROOM_IN_PLAN, default=True): selector.BooleanSelector(),
    vol.Optional(CONF_ROOM_NEIGHBOURS): TEMPS,
})


def _validate(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Energi og temperaturer skal have langtidsstatistik (state_class)."""
    errors: dict[str, str] = {}
    for key in STAT_KEYS:
        eid = data.get(key)
        if not eid:
            continue
        st = hass.states.get(eid)
        if st is None:
            errors[key] = "not_found"
        elif not st.attributes.get("state_class"):
            errors[key] = "no_statistics"
    for eid in data.get(CONF_INDOOR) or []:
        st = hass.states.get(eid)
        if st is not None and not st.attributes.get("state_class"):
            errors[CONF_INDOOR] = "no_statistics"
    return errors


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [])}


class VarmemodelConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                data = _clean(user_input)
                return self.async_create_entry(title=data.get(CONF_NAME, "Hus"), data=data)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(_house_schema(True), user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VarmemodelOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_SOURCE: SourceSubentryFlow, SUBENTRY_ROOM: RoomSubentryFlow}


class VarmemodelOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(_house_schema(False), user_input or current), errors=errors
        )


class SourceSubentryFlow(ConfigSubentryFlow):
    """En varmekilde: luft-vand (vandbåren) eller luft-luft."""

    def __init__(self) -> None:
        self._base: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self._base = {CONF_SOURCE_NAME: str(user_input[CONF_SOURCE_NAME]).strip(), CONF_SOURCE_TYPE: user_input[CONF_SOURCE_TYPE]}
            return await self.async_step_details()
        schema = vol.Schema({vol.Required(CONF_SOURCE_NAME): TEXT, vol.Required(CONF_SOURCE_TYPE, default=SOURCE_HYDRONIC): TYPE})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        self._base = {k: sub.data[k] for k in (CONF_SOURCE_NAME, CONF_SOURCE_TYPE) if k in sub.data}
        return await self.async_step_details()

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry() if self.source == "reconfigure" else None
        air = self._base.get(CONF_SOURCE_TYPE) == SOURCE_AIR_AIR
        schema = AIR_AIR_SCHEMA if air else HYDRONIC_SCHEMA
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                data = {**self._base, **_clean(user_input)}
                if sub is None:
                    return self.async_create_entry(title=data[CONF_SOURCE_NAME], data=data)
                # Bevar markeringer, der ikke vises i formularen (fx at entitets-id'erne er fra version 1)
                fields = {str(k) for k in schema.schema}
                keep = {k: v for k, v in sub.data.items() if k not in fields and k not in data}
                return self.async_update_and_abort(self._get_entry(), sub, title=data[CONF_SOURCE_NAME], data={**keep, **data})
        suggested = user_input or (dict(sub.data) if sub else {})
        return self.async_show_form(step_id="air_air" if air else "hydronic",
                                    data_schema=self.add_suggested_values_to_schema(schema, suggested), errors=errors)

    async def async_step_hydronic(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_details(user_input)

    async def async_step_air_air(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_details(user_input)


class RoomSubentryFlow(ConfigSubentryFlow):
    """Et rum: temperaturføler, min.-temperatur, telestat/kreds og om det tæller med i planen."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._form("user", user_input, None)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self._form("reconfigure", user_input, self._get_reconfigure_subentry())

    def _schema(self) -> vol.Schema:
        """Rummets felter; varmekilden vælges blandt husets varmekilder."""
        sources = [
            selector.SelectOptionDict(value=sid, label=s.title)
            for sid, s in self._get_entry().subentries.items() if s.subentry_type == SUBENTRY_SOURCE
        ]
        if not sources:
            return ROOM_SCHEMA
        return ROOM_SCHEMA.extend({
            vol.Optional(CONF_ROOM_SOURCE): selector.SelectSelector(
                selector.SelectSelectorConfig(options=sources, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_ROOM_EXTRA_SOURCES): selector.SelectSelector(
                selector.SelectSelectorConfig(options=sources, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
        })

    @callback
    def _form(self, step_id: str, user_input: dict[str, Any] | None, subentry: ConfigSubentry | None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(dict(user_input))
            data[CONF_ROOM_NAME] = str(data.get(CONF_ROOM_NAME, "")).strip()
            st = self.hass.states.get(data[CONF_ROOM_TEMP])
            if st is not None and not st.attributes.get("state_class"):
                errors[CONF_ROOM_TEMP] = "no_statistics"
            elif any(
                s.data.get(CONF_ROOM_TEMP) == data[CONF_ROOM_TEMP] and (subentry is None or sid != subentry.subentry_id)
                for sid, s in self._get_entry().subentries.items()
            ):
                errors[CONF_ROOM_TEMP] = "duplicate_room"
            else:
                if subentry is None:
                    return self.async_create_entry(title=data[CONF_ROOM_NAME], data=data)
                return self.async_update_and_abort(self._get_entry(), subentry, title=data[CONF_ROOM_NAME], data=data)
        suggested = dict(subentry.data) if subentry else {}
        if user_input is not None:
            suggested = user_input
        return self.async_show_form(step_id=step_id, data_schema=self.add_suggested_values_to_schema(self._schema(), suggested), errors=errors)
