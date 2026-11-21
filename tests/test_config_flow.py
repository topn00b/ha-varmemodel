"""Opsætningsguiden: huset, varmekilder og rum."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.varmemodel.const import DOMAIN

ENERGY = {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"}
TEMP = {"device_class": "temperature", "state_class": "measurement", "unit_of_measurement": "°C"}


def _states(ha: HomeAssistant) -> None:
    ha.states.async_set("sensor.vp_el", "100", ENERGY)
    ha.states.async_set("sensor.vp_varme", "300", ENERGY)
    ha.states.async_set("sensor.ll_el", "50", ENERGY)
    ha.states.async_set("sensor.ude", "5", TEMP)
    ha.states.async_set("sensor.rum_1", "21", TEMP)
    ha.states.async_set("sensor.kaelder", "18", TEMP)
    ha.states.async_set("sensor.uden_statistik", "21", {"device_class": "temperature"})


async def test_create_house(ha: HomeAssistant) -> None:
    _states(ha)
    r = await ha.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r = await ha.config_entries.flow.async_configure(r["flow_id"], {"name": "Hus", "outdoor_temp": "sensor.ude"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert {"name": "Hus", "outdoor_temp": "sensor.ude", "window_days": 60}.items() <= r["data"].items()


async def test_rejects_sensor_without_statistics(ha: HomeAssistant) -> None:
    _states(ha)
    r = await ha.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await ha.config_entries.flow.async_configure(r["flow_id"], {"name": "Hus", "outdoor_temp": "sensor.uden_statistik"})
    assert r["type"] is FlowResultType.FORM
    assert r["errors"] == {"outdoor_temp": "no_statistics"}


async def test_add_sources_and_room(ha: HomeAssistant) -> None:
    _states(ha)
    e = MockConfigEntry(domain=DOMAIN, title="Hus", data={"name": "Hus", "outdoor_temp": "sensor.ude"}, version=2)
    e.add_to_hass(ha)
    # luft-vand
    r = await ha.config_entries.subentries.async_init((e.entry_id, "source"), context={"source": "user"})
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"name": "Luft-vand", "source_type": "hydronic"})
    assert r["step_id"] == "hydronic"
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    # luft-luft
    r = await ha.config_entries.subentries.async_init((e.entry_id, "source"), context={"source": "user"})
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"name": "Luft-luft", "source_type": "air_air"})
    assert r["step_id"] == "air_air"
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"electricity_energy": "sensor.ll_el"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    # rum med nabozone
    r = await ha.config_entries.subentries.async_init((e.entry_id, "room"), context={"source": "user"})
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"name": "Stue", "temperature": "sensor.rum_1", "neighbours": ["sensor.kaelder"]})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    kinds = sorted((s.subentry_type, s.data.get("source_type", "")) for s in e.subentries.values())
    assert kinds == [("room", ""), ("source", "air_air"), ("source", "hydronic")]
    room = next(s for s in e.subentries.values() if s.subentry_type == "room")
    assert room.data["neighbours"] == ["sensor.kaelder"]
