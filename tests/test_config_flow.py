"""Opsætningsguiden."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.varmemodel.const import DOMAIN

ENERGY = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
TEMP = {"device_class": "temperature", "state_class": "measurement", "unit_of_measurement": "°C"}


def _states(ha: HomeAssistant) -> None:
    ha.states.async_set("sensor.vp_el", "100", ENERGY)
    ha.states.async_set("sensor.vp_varme", "300", ENERGY)
    ha.states.async_set("sensor.ude", "5", TEMP)
    ha.states.async_set("sensor.rum_1", "21", TEMP)
    ha.states.async_set("sensor.uden_statistik", "21", {"device_class": "temperature"})


async def test_create_entry(ha: HomeAssistant) -> None:
    _states(ha)
    r = await ha.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r = await ha.config_entries.flow.async_configure(r["flow_id"], {
        "name": "Varmepumpe", "electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme",
        "outdoor_temp": "sensor.ude", "indoor_temps": ["sensor.rum_1"],
    })
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"]["indoor_temps"] == ["sensor.rum_1"]
    assert "flow_temp" not in r["data"]


async def test_rejects_sensor_without_statistics(ha: HomeAssistant) -> None:
    _states(ha)
    r = await ha.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await ha.config_entries.flow.async_configure(r["flow_id"], {
        "name": "Varmepumpe", "electricity_energy": "sensor.vp_el", "outdoor_temp": "sensor.uden_statistik",
        "indoor_temps": ["sensor.rum_1"],
    })
    assert r["type"] is FlowResultType.FORM
    assert r["errors"] == {"outdoor_temp": "no_statistics"}
