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
    assert {"heat_loss", "cop", "cop_steady", "cop_loss", "cop_yesterday", "cop_week", "room_sensor.rum_1", "emission", "curve", "cooling", "status", "recalibrate", "mode"} <= keys


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
    h0 = __import__("time").time() // 3600 * 3600
    rows = c._collect_sync(h0 - 3600, h0 + 3 * 3600)  # timen efter "nu" er helt dækket af vejret
    assert any("ws" in v for v in rows.values())
    await c.async_calibrate()
    assert c.status != "error", c.status_note


async def test_room_subentry_and_room_heat(ha: HomeAssistant) -> None:
    """Rum som underpunkt: rumsensorer knyttes til underpunktet, og varmen fordeles efter telestaten."""
    from homeassistant.config_entries import ConfigSubentryData

    from custom_components.varmemodel.calibration import Hour

    e = MockConfigEntry(domain=DOMAIN, title="VP", data=DATA, subentries_data=[
        ConfigSubentryData(data={"name": "Stue", "temperature": "sensor.rum_1", "valve": "binary_sensor.stue_ventil", "loop_weight": 1.0},
                           subentry_type="room", title="Stue", unique_id=None),
        ConfigSubentryData(data={"name": "Bad", "temperature": "sensor.rum_2", "always_open": True, "loop_weight": 1.0},
                           subentry_type="room", title="Bad", unique_id=None),
    ])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    assert [r["name"] for r in c.rooms_cfg] == ["Stue", "Bad"]
    await ha.async_block_till_done(wait_background_tasks=True)
    reg = er.async_get(ha)
    ents = er.async_entries_for_config_entry(reg, e.entry_id)
    assert any(x.unique_id.endswith("room_heat_sensor.rum_1") and x.config_subentry_id for x in ents), [(x.unique_id, x.config_subentry_id) for x in ents]
    # Én time i går: stuen åben 30 min, badet altid åben (60 min) → 1/3 og 2/3 af varmen
    from homeassistant.util import dt as dt_util
    from datetime import timedelta

    y = dt_util.start_of_local_day() - timedelta(hours=12)
    ts = int(y.timestamp()) // 3600 * 3600
    c.collected[str(ts)] = {"comp": 60.0, "vchk": 1, "v": {"sensor.rum_1": 30.0}}
    res = c._room_heat([Hour(ts=ts, q=3.0, dhw=0)])
    assert res["sensor.rum_1"]["yesterday_kwh"] == 1.0
    assert res["sensor.rum_2"]["yesterday_kwh"] == 2.0


async def test_collect_valve_minutes_from_history(ha: HomeAssistant) -> None:
    """Telestaternes minutter hentes fra historikken (regression: LazyState har ingen .domain)."""
    from homeassistant.config_entries import ConfigSubentryData
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    ha.states.async_set("binary_sensor.kompressor", "on")
    ha.states.async_set("binary_sensor.stue_ventil", "on")
    ha.states.async_set("climate.kontor", "heat", {"hvac_action": "heating"})
    await async_wait_recording_done(ha)
    e = MockConfigEntry(domain=DOMAIN, title="VP", data=DATA, subentries_data=[
        ConfigSubentryData(data={"name": "Stue", "temperature": "sensor.rum_1", "valve": "binary_sensor.stue_ventil"},
                           subentry_type="room", title="Stue", unique_id=None),
        ConfigSubentryData(data={"name": "Kontor", "temperature": "sensor.rum_2", "valve": "climate.kontor"},
                           subentry_type="room", title="Kontor", unique_id=None),
    ])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    h0 = __import__("time").time() // 3600 * 3600
    rows = c._collect_sync(h0 - 3600, h0 + 3 * 3600)
    nxt = rows[int(h0 + 3600)]
    assert nxt["vchk"] == 1
    assert nxt["v"]["sensor.rum_1"] == 60.0 and nxt["v"]["sensor.rum_2"] == 60.0
