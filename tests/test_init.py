"""Opsætning, flytning fra version 1, entiteter og opsamling."""

from __future__ import annotations

from datetime import timedelta
import time

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.varmemodel.calibration import Fit, Hour
from custom_components.varmemodel.const import DOMAIN

HOUSE = {"name": "Hus", "outdoor_temp": "sensor.ude", "indoor_temps": ["sensor.rum_1", "sensor.rum_2"]}
HYD = {"name": "Luft-vand", "source_type": "hydronic", "electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme",
       "compressor": "binary_sensor.kompressor"}
AIR = {"name": "Luft-luft", "source_type": "air_air", "electricity_energy": "sensor.ll_el"}


def _sub(data: dict, kind: str) -> ConfigSubentryData:
    return ConfigSubentryData(data=data, subentry_type=kind, title=data["name"], unique_id=None)


@pytest.fixture
async def entry(ha: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, data=HOUSE, title="Hus", version=2, subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    return e


async def test_setup_creates_entities(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    ents = er.async_entries_for_config_entry(er.async_get(ha), entry.entry_id)
    uids = {e.unique_id.removeprefix(entry.entry_id + "_") for e in ents}
    hyd = next(s for s, x in entry.subentries.items() if x.data["source_type"] == "hydronic")
    air = next(s for s, x in entry.subentries.items() if x.data["source_type"] == "air_air")
    assert {"status", "recalibrate", "mode"} <= uids
    assert {f"{hyd}_heat_loss", f"{hyd}_cop_steady", f"{hyd}_cop_yesterday", f"{air}_el_yesterday"} <= uids
    assert {"room_sensor.rum_1", "room_sensor.rum_2"} <= uids
    # varmekildens sensorer hører til dens underpunkt
    assert all(e.config_subentry_id == hyd for e in ents if e.unique_id.startswith(f"{entry.entry_id}_{hyd}_"))


async def test_calibrate_without_data(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    c = entry.runtime_data
    await c.async_calibrate()
    await ha.async_block_till_done()
    assert c.status in ("insufficient", "partial")


async def test_frozen_mode_keeps_values(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    c = entry.runtime_data
    s = c.hydronic
    c._accept(s, "cop", Fit(3.0, 50, 0.5, 0.1, 0.0, 5.0, "fit"), 1.0)
    await c.async_set_mode("frozen")
    c._accept(s, "cop", Fit(2.0, 50, 0.5, 0.1, 0.0, 5.0, "fit"), 2.0)
    assert s.accepted["cop"]["value"] == 3.0


async def test_unload(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await ha.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_migrate_from_version_1(ha: HomeAssistant) -> None:
    """Version 1 (én varmepumpe) flyttes til huset med varmepumpen som varmekilde. Entitets-id'er og data bevares."""
    v1 = {"name": "Luft-vand", "electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme", "outdoor_temp": "sensor.ude",
          "indoor_temps": ["sensor.rum_1"], "compressor": "binary_sensor.kompressor", "curve_offset_zero": 17}
    e = MockConfigEntry(domain=DOMAIN, data=v1, title="Luft-vand", version=1,
                        subentries_data=[_sub({"name": "Stue", "temperature": "sensor.rum_1"}, "room")])
    e.add_to_hass(ha)
    reg = er.async_get(ha)
    old = reg.async_get_or_create("sensor", DOMAIN, f"{e.entry_id}_heat_loss", config_entry=e, suggested_object_id="luft_vand_heat_loss")
    h = str(int(time.time()) // 3600 * 3600 - 7200)
    store_v1 = {"collected": {h: {"comp": 60.0, "ws": 3.0, "air": ["sensor.rum_1"]}}, "accepted": {"cop": {"value": 2.5}},
                "fits": {}, "runs": [{"at": time.time()}], "slots": {"1": [0.3, 0.1, 5.0, 33.0, 20.0]}, "daily": {}, "calib_version": 4}
    from homeassistant.helpers.storage import Store

    await Store(ha, 1, f"{DOMAIN}.{e.entry_id}").async_save(store_v1)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    assert e.version == 2 and e.title == "Hus"
    src = [s for s in e.subentries.values() if s.subentry_type == "source"]
    assert len(src) == 1 and src[0].data["source_type"] == "hydronic" and src[0].data["legacy_ids"] is True
    assert src[0].data["curve_offset_zero"] == 17 and "electricity_energy" not in e.data
    assert e.data["outdoor_temp"] == "sensor.ude"
    # Samme entitet (samme entitets-id), nu på varmekildens underpunkt
    new = reg.async_get(old.entity_id)
    assert new is not None and new.config_subentry_id == src[0].subentry_id
    c = e.runtime_data
    s = c.sources[src[0].subentry_id]
    assert s.collected[h] == {"comp": 60.0}
    assert c.collected[h] == {"ws": 3.0, "air": ["sensor.rum_1"]}
    assert s.accepted["cop"]["value"] == 2.5 and "1" in s.slots


async def test_collect_with_weather_and_setpoint(ha: HomeAssistant) -> None:
    """Vejr og setpunkt hentes fra historikken i samme opsamling (regression: variabel blev overskrevet)."""
    ha.states.async_set("weather.hjem", "sunny", {"wind_speed": 18.0, "wind_speed_unit": "km/h", "wind_bearing": 270, "cloud_coverage": 20, "uv_index": 1, "humidity": 80})
    ha.states.async_set("climate.vand", "heat", {"temperature": 35})
    ha.states.async_set("climate.vp", "auto", {"temperature": 19})
    await async_wait_recording_done(ha)
    hyd = {**HYD, "flow_setpoint": "climate.vand", "flow_setpoint_attribute": "temperature", "curve_offset": "climate.vp",
           "curve_offset_attribute": "temperature", "curve_offset_zero": 17}
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "weather": "weather.hjem"}, subentries_data=[_sub(hyd, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    h0 = time.time() // 3600 * 3600
    house, per = c._collect_sync(h0 - 3600, h0 + 3 * 3600)  # timen efter "nu" er helt dækket
    nxt = int(h0 + 3600)
    assert house[nxt]["ws"] == 5.0
    sid = next(iter(per))
    assert per[sid][nxt]["sp"] == 35.0 and per[sid][nxt]["off"] == 2.0
    await c.async_calibrate()
    assert c.status != "error", c.status_note


async def test_collect_valve_minutes_from_history(ha: HomeAssistant) -> None:
    """Telestaternes minutter hentes fra historikken (regression: LazyState har ingen .domain)."""
    ha.states.async_set("binary_sensor.kompressor", "on")
    ha.states.async_set("binary_sensor.stue_ventil", "on")
    ha.states.async_set("climate.kontor", "heat", {"hvac_action": "heating"})
    await async_wait_recording_done(ha)
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[
        _sub(HYD, "source"),
        _sub({"name": "Stue", "temperature": "sensor.rum_1", "valve": "binary_sensor.stue_ventil"}, "room"),
        _sub({"name": "Kontor", "temperature": "sensor.rum_2", "valve": "climate.kontor"}, "room"),
    ])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    h0 = time.time() // 3600 * 3600
    house, _ = c._collect_sync(h0 - 3600, h0 + 3 * 3600)
    nxt = house[int(h0 + 3600)]
    assert nxt["vchk"] == 1 and nxt["v"]["sensor.rum_1"] == 60.0 and nxt["v"]["sensor.rum_2"] == 60.0


async def test_room_heat_split(ha: HomeAssistant) -> None:
    """Varmen fordeles efter telestat og altid åben kreds; rummene har egne enheder."""
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[
        _sub(HYD, "source"),
        _sub({"name": "Stue", "temperature": "sensor.rum_1", "valve": "binary_sensor.stue_ventil", "loop_weight": 1.0}, "room"),
        _sub({"name": "Bad", "temperature": "sensor.rum_2", "always_open": True, "loop_weight": 1.0}, "room"),
    ])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    c = e.runtime_data
    ents = er.async_entries_for_config_entry(er.async_get(ha), e.entry_id)
    assert any(x.unique_id.endswith("room_heat_sensor.rum_1") and x.config_subentry_id for x in ents)
    y = dt_util.start_of_local_day() - timedelta(hours=12)
    ts = int(y.timestamp()) // 3600 * 3600
    c.collected[str(ts)] = {"vchk": 1, "v": {"sensor.rum_1": 30.0}}
    c.hydronic.collected[str(ts)] = {"comp": 60.0}
    res = c._room_heat({c.hydronic.sid: [Hour(ts=ts, q=3.0, dhw=0)]})
    assert res["sensor.rum_1"]["yesterday_kwh"] == 1.0
    assert res["sensor.rum_2"]["yesterday_kwh"] == 2.0


async def test_room_source_selection(ha: HomeAssistant) -> None:
    """Rummets varmekilde kan vælges; varmen fordeles kun på rum, der hører til den vandbårne kilde."""
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    air_id = next(s for s, x in e.subentries.items() if x.data["source_type"] == "air_air")
    hyd_id = next(s for s, x in e.subentries.items() if x.data["source_type"] == "hydronic")
    # vælg varmekilde i formularen
    r = await ha.config_entries.subentries.async_init((e.entry_id, "room"), context={"source": "user"})
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"name": "Gang 1", "temperature": "sensor.rum_1", "heat_source": air_id})
    assert r["type"].value == "create_entry"
    r = await ha.config_entries.subentries.async_init((e.entry_id, "room"), context={"source": "user"})
    r = await ha.config_entries.subentries.async_configure(r["flow_id"], {"name": "Stue", "temperature": "sensor.rum_2", "valve": "binary_sensor.v", "heat_source": hyd_id})
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    c = e.runtime_data
    rooms = {r["name"]: r for r in c.rooms_cfg}
    assert c.room_source(rooms["Gang 1"]).kind == "air_air"
    assert c.room_source(rooms["Stue"]).kind == "hydronic"
    ents = er.async_entries_for_config_entry(er.async_get(ha), e.entry_id)
    assert any(x.unique_id.endswith("room_heat_sensor.rum_2") for x in ents)
    assert not any(x.unique_id.endswith("room_heat_sensor.rum_1") for x in ents)


async def test_room_new_shape_skips_guard(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    """Når et rums model får en ny varmekilde, tages den nye model i brug uden ±10 %-grænsen."""
    c = entry.runtime_data
    old = Fit(0.10, 50, 0.5, 0.2, 0.0, 5.0, "fit", {"sources": {"A": {"total": 0.05}}})
    c.rooms["sensor.rum_1"] = {"value": 0.10, "extra": old.extra, "t_out_min": 0.0, "t_out_max": 5.0}
    new = Fit(0.30, 50, 0.7, 0.2, 0.0, 5.0, "fit", {"sources": {"A": {"total": 0.05}, "B": {"total": 0.4}}})
    # samme logik som i kalibreringen
    from custom_components.varmemodel import coordinator as co

    async def fake_hours():
        return {}
    room_fits = {"sensor.rum_1": new}
    prev = c.rooms["sensor.rum_1"]
    pe = prev["extra"]
    assert set(pe["sources"]) != set(new.extra["sources"])
    # kør acceptlogikken via en kalibrering med de falske tal
    orig = c._rooms_fit
    c._rooms_fit = lambda _h: room_fits
    try:
        await c.async_calibrate()
    finally:
        c._rooms_fit = orig
    assert c.rooms["sensor.rum_1"]["value"] == 0.30
    assert "ny model" in c.rooms["sensor.rum_1"]["guard"]
    assert set(c.rooms["sensor.rum_1"]["extra"]["sources"]) == {"A", "B"}
    del co


async def test_device_tree(ha: HomeAssistant) -> None:
    """Rummets enhed er forbundet via sin varmekildes enhed, som er forbundet via huset."""
    from homeassistant.helpers import device_registry as dr

    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    hyd_id = next(iter(e.subentries))
    ha.config_entries.async_add_subentry(e, __import__("homeassistant.config_entries", fromlist=["ConfigSubentry"]).ConfigSubentry(
        data={"name": "Stue", "temperature": "sensor.rum_1", "heat_source": hyd_id}, subentry_type="room", title="Stue", unique_id=None))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    reg = dr.async_get(ha)
    devs = {next(iter(d.identifiers))[1]: d for d in dr.async_entries_for_config_entry(reg, e.entry_id)}
    room_sid = next(s for s, x in e.subentries.items() if x.subentry_type == "room")
    house, src, room = devs[e.entry_id], devs[f"{e.entry_id}_{hyd_id}"], devs[f"{e.entry_id}_{room_sid}"]
    assert house.name == "Hus" and src.via_device_id == house.id and room.via_device_id == src.id


async def test_replan_with_prices_blocks_and_rooms(ha: HomeAssistant) -> None:
    """Planen bygges af prisliste, rød-tidsplan og rummene; blokerede timer varmes aldrig."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0 + (k % 6) * 0.3} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.5", {"timer": items})
    ha.states.async_set("sensor.zoner", f"{h0 // 3600}|" + "gggg" + "rrrrrr" + "g" * 20)
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_1", "20.5", {"state_class": "measurement"})
    ha.states.async_set("input_number.min_1", "20")
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2,
                        data={**HOUSE, "price_entity": "sensor.pris", "block_entity": "sensor.zoner", "block_codes": "r"},
                        subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    hyd = next(iter(e.subentries))
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Stue", "temperature": "sensor.rum_1", "minimum_entity": "input_number.min_1",
                                                                 "heat_source": hyd}, subentry_type="room", title="Stue", unique_id=None))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.rooms["sensor.rum_1"] = {"value": 0.1, "extra": {"sources": {"Luft-vand": {"now": 0.03, "delayed": 0.02}}, "accuracy_6h_k": 0.2}}
    await c.async_replan()
    p = c.plans[hyd]
    assert c.plan_meta["inputs_ok"] is True or c.plan_meta["missing"] == ["vejrudsigt"]
    assert len(p["hours"]) == 24
    assert all(h["heat_kw"] == 0 for h in p["hours"][4:10])
    assert p["heat_kwh"] > 0 and "Stue" in p["rooms"]
    await ha.async_block_till_done()
    st = next(s for s in ha.states.async_all("sensor") if s.entity_id.endswith("plan_now"))
    assert st.state in ("heat", "off")
    assert str(int(time.time() // 3600 * 3600)) in c.plan_log[hyd]
    num = next(s for s in ha.states.async_all("number") if "max_pause" in s.entity_id)
    await ha.services.async_call("number", "set_value", {"entity_id": num.entity_id, "value": 4}, blocking=True)
    assert c.setting("max_pause") == 4
    assert c.commands[hyd]["day"] and c.plan_meta["fresh_pct_today"] > 0
    cmd = next(s for s in ha.states.async_all("sensor") if "recommended_control" in s.entity_id)
    assert cmd.state in ("heat", "off") and cmd.attributes["within_limits"] is True
