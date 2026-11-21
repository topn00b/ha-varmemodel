"""Opsætning, entiteter og kalibrering med en tom database."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.varmemodel.const import DOMAIN

DATA = {
    "name": "Varmepumpe",
    "electricity_energy": "sensor.vp_el",
    "heat_energy": "sensor.vp_varme",
    "outdoor_temp": "sensor.ude",
    "indoor_temps": ["sensor.rum_1", "sensor.rum_2"],
    "compressor": "binary_sensor.kompressor",
}


@pytest.fixture
async def entry(ha: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, data=DATA, title="Varmepumpe")
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    return e


async def test_setup_creates_entities(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    reg = er.async_get(ha)
    ents = er.async_entries_for_config_entry(reg, entry.entry_id)
    keys = {e.unique_id.removeprefix(entry.entry_id + "_") for e in ents}
    assert {"heat_loss", "cop", "cop_steady", "cop_loss", "cop_yesterday", "cop_week", "emission", "curve", "cooling", "status", "recalibrate", "mode"} <= keys


async def test_calibrate_without_data(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    c = entry.runtime_data
    await c.async_calibrate()
    await ha.async_block_till_done()
    assert c.status in ("insufficient", "partial")
    st = ha.states.get("sensor.varmepumpe_kalibrering") or next(
        s for s in ha.states.async_all("sensor") if s.entity_id.endswith("calibration")
    )
    assert st.state == c.status


async def test_frozen_mode_keeps_values(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    from custom_components.varmemodel.calibration import Fit

    c = entry.runtime_data
    c._accept("cop", Fit(3.0, 50, 0.5, 0.1, 0.0, 5.0, "fit"), 1.0)
    await c.async_set_mode("frozen")
    c._accept("cop", Fit(2.0, 50, 0.5, 0.1, 0.0, 5.0, "fit"), 2.0)
    assert c.accepted["cop"]["value"] == 3.0


async def test_unload(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await ha.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_collect_with_weather_and_setpoint(ha: HomeAssistant) -> None:
    """Vejr og setpunkt hentes fra historikken i samme opsamling (regression: variabel blev overskrevet)."""
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    ha.states.async_set("weather.hjem", "sunny", {"wind_speed": 18.0, "wind_speed_unit": "km/h", "wind_bearing": 270, "cloud_coverage": 20, "uv_index": 1, "humidity": 80})
    ha.states.async_set("climate.vand", "heat", {"temperature": 35})
    ha.states.async_set("climate.vp", "auto", {"temperature": 19})
    await async_wait_recording_done(ha)
    e = MockConfigEntry(domain=DOMAIN, title="VP", data={**DATA, "weather": "weather.hjem", "flow_setpoint": "climate.vand",
                                                         "flow_setpoint_attribute": "temperature", "curve_offset": "climate.vp",
                                                         "curve_offset_attribute": "temperature", "curve_offset_zero": 17})
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    rows = c._collect_sync(c.collected and 0 or __import__("time").time() // 3600 * 3600 - 3600, __import__("time").time() // 3600 * 3600 + 3600)
    assert any("ws" in v for v in rows.values())
    await c.async_calibrate()
    assert c.status != "error", c.status_note
