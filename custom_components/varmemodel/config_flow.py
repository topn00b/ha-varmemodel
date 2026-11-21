"""Opsætning: vælg dine egne sensorer – intet er kodet fast."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_COMPRESSOR,
    CONF_DEFROST,
    CONF_DHW,
    CONF_ELECTRICITY,
    CONF_FLOW,
    CONF_HEAT,
    CONF_INDOOR,
    CONF_NAME,
    CONF_OFFSET,
    CONF_OFFSET_ATTR,
    CONF_OFFSET_ZERO,
    CONF_OUTDOOR,
    CONF_RETURN,
    CONF_SETPOINT,
    CONF_SETPOINT_ATTR,
    CONF_WINDOW_DAYS,
    DEFAULT_NAME,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
)

ENERGY = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="energy"))
TEMP = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature"))
TEMPS = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True))
BINARY = selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))
ANY = selector.EntitySelector(selector.EntitySelectorConfig(domain=["climate", "sensor", "number", "input_number", "water_heater"]))
TEXT = selector.TextSelector()
ZERO = selector.NumberSelector(selector.NumberSelectorConfig(min=-50, max=100, step=0.5, mode=selector.NumberSelectorMode.BOX))
DAYS = selector.NumberSelector(selector.NumberSelectorConfig(min=14, max=365, step=1, mode=selector.NumberSelectorMode.BOX))

ENTITY_KEYS = (CONF_HEAT, CONF_ELECTRICITY, CONF_OUTDOOR, CONF_FLOW, CONF_RETURN, CONF_COMPRESSOR, CONF_DEFROST, CONF_DHW, CONF_SETPOINT, CONF_OFFSET)


def _schema(d: dict[str, Any], with_name: bool) -> vol.Schema:
    def opt(key: str, sel: Any) -> tuple[Any, Any]:
        return (vol.Optional(key, description={"suggested_value": d.get(key)}), sel)

    fields: list[tuple[Any, Any]] = []
    if with_name:
        fields.append((vol.Required(CONF_NAME, default=d.get(CONF_NAME, DEFAULT_NAME)), TEXT))
    fields += [
        (vol.Required(CONF_ELECTRICITY, description={"suggested_value": d.get(CONF_ELECTRICITY)}), ENERGY),
        opt(CONF_HEAT, ENERGY),
        (vol.Required(CONF_OUTDOOR, description={"suggested_value": d.get(CONF_OUTDOOR)}), TEMP),
        (vol.Required(CONF_INDOOR, description={"suggested_value": d.get(CONF_INDOOR)}), TEMPS),
        opt(CONF_FLOW, TEMP),
        opt(CONF_RETURN, TEMP),
        opt(CONF_COMPRESSOR, BINARY),
        opt(CONF_DEFROST, BINARY),
        opt(CONF_DHW, BINARY),
        opt(CONF_SETPOINT, ANY),
        opt(CONF_SETPOINT_ATTR, TEXT),
        opt(CONF_OFFSET, ANY),
        opt(CONF_OFFSET_ATTR, TEXT),
        (vol.Optional(CONF_OFFSET_ZERO, default=d.get(CONF_OFFSET_ZERO, 0)), ZERO),
        (vol.Optional(CONF_WINDOW_DAYS, default=d.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS)), DAYS),
    ]
    return vol.Schema(dict(fields))


def _validate(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Energi og temperaturer skal have langtidsstatistik (state_class)."""
    errors: dict[str, str] = {}
    for key in (CONF_ELECTRICITY, CONF_HEAT, CONF_OUTDOOR, CONF_FLOW, CONF_RETURN):
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
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                data = _clean(user_input)
                return self.async_create_entry(title=data.get(CONF_NAME, DEFAULT_NAME), data=data)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}, True), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VarmemodelOptionsFlow()


class VarmemodelOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                # Felter der er tømt, skal også fjernes fra de oprindelige data
                data = {k: v for k, v in self.config_entry.data.items() if k not in ENTITY_KEYS and k not in (CONF_SETPOINT_ATTR, CONF_OFFSET_ATTR)}
                self.hass.config_entries.async_update_entry(self.config_entry, data=data)
                return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(step_id="init", data_schema=_schema(user_input or current, False), errors=errors)
