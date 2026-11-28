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
          "indoor_temps": ["sensor.rum_1"], "compressor": "binary_sensor.kompressor", "defrost": "binary_sensor.afrim"}
    e = MockConfigEntry(domain=DOMAIN, data=v1, title="Luft-vand", version=1,
                        subentries_data=[_sub({"name": "Stue", "temperature": "sensor.rum_1"}, "room")])
    e.add_to_hass(ha)
    reg = er.async_get(ha)
    old = reg.async_get_or_create("sensor", DOMAIN, f"{e.entry_id}_heat_loss", config_entry=e, suggested_object_id="luft_vand_heat_loss")
    h = str(int(time.time()) // 3600 * 3600 - 7200)
    store_v1 = {"collected": {h: {"comp": 60.0, "ws": 3.0, "air": ["sensor.rum_1"]}}, "accepted": {"heat_loss": {"value": 2.5}},
                "fits": {}, "runs": [{"at": time.time()}], "slots": {"1": [0.3, 0.1, 5.0, 33.0, 20.0]}, "daily": {}, "calib_version": 5}
    from homeassistant.helpers.storage import Store

    await Store(ha, 1, f"{DOMAIN}.{e.entry_id}").async_save(store_v1)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done(wait_background_tasks=True)
    assert e.version == 2 and e.title == "Hus"
    src = [s for s in e.subentries.values() if s.subentry_type == "source"]
    assert len(src) == 1 and src[0].data["source_type"] == "hydronic" and src[0].data["legacy_ids"] is True
    assert src[0].data["defrost"] == "binary_sensor.afrim" and "electricity_energy" not in e.data
    assert e.data["outdoor_temp"] == "sensor.ude"
    # Samme entitet (samme entitets-id), nu på varmekildens underpunkt
    new = reg.async_get(old.entity_id)
    assert new is not None and new.config_subentry_id == src[0].subentry_id
    c = e.runtime_data
    s = c.sources[src[0].subentry_id]
    assert s.collected[h] == {"comp": 60.0}
    assert c.collected[h] == {"ws": 3.0, "air": ["sensor.rum_1"]}
    assert s.accepted["heat_loss"]["value"] == 2.5 and "1" in s.slots


async def test_collect_with_weather_and_setpoint(ha: HomeAssistant) -> None:
    """Vejr og setpunkt hentes fra historikken i samme opsamling (regression: variabel blev overskrevet)."""
    ha.states.async_set("weather.hjem", "sunny", {"wind_speed": 18.0, "wind_speed_unit": "km/h", "wind_bearing": 270, "cloud_coverage": 20, "uv_index": 1, "humidity": 80})
    ha.states.async_set("climate.vand", "heat", {"temperature": 35})
    ha.states.async_set("climate.vp", "auto", {"temperature": 19})
    await async_wait_recording_done(ha)
    hyd = dict(HYD)
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
    assert "sp" not in per[sid].get(nxt, {}) and "off" not in per[sid].get(nxt, {})
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
    c._rooms_fit = lambda _h, _k=None: room_fits
    try:
        await c.async_calibrate()
    finally:
        c._rooms_fit = orig
    assert c.rooms["sensor.rum_1"]["value"] == 0.30
    assert "ny model" in c.rooms["sensor.rum_1"]["guard"]
    assert set(c.rooms["sensor.rum_1"]["extra"]["sources"]) == {"A", "B"}
    del co


async def test_guarded_room_validates_stored_model(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    """Bremser vagten et rums tal, beholdes den forrige model – og det er den, der afprøves (ikke den nye)."""
    from custom_components.varmemodel.calibration import RoomHour

    c = entry.runtime_data
    prev_extra = {"sources": {"A": {"now": 0.03, "delayed": 0.0}}, "validated_6h_k": 9.9}
    from custom_components.varmemodel.const import CALIB_VERSION

    c.rooms["sensor.rum_1"] = {"v": CALIB_VERSION, "value": 0.10, "extra": prev_extra, "t_out_min": 0.0, "t_out_max": 5.0}
    new = Fit(0.30, 50, 0.7, 0.2, 0.0, 5.0, "fit", {"sources": {"A": {"now": 0.05, "delayed": 0.0}}, "validated_6h_k": 0.1})
    # Rummet følger præcis den model, der gemmes: hele den forrige model (0.10.0: værdi og koefficienter holdes sammen)
    t, rh = 20.0, []
    for i in range(200):
        q = 3.0 if i % 12 < 6 else 0.0
        rh.append(RoomHour(ts=1_700_000_000 + i * 3600, t_room=round(t, 4), t_out=2.0, inputs={"A": q}))
        t = t + 0.03 * q + 0.10 / 20 * (2.0 - t)

    def fake(_h, keep=None):
        if keep is not None:
            keep["sensor.rum_1"] = rh
        return {"sensor.rum_1": new}

    orig = c._rooms_fit
    c._rooms_fit = fake
    try:
        await c.async_calibrate()
    finally:
        c._rooms_fit = orig
    r = c.rooms["sensor.rum_1"]
    assert r["guard"].startswith("begrænset") and r["value"] == 0.10
    ex = r["extra"]
    assert ex["sources"]["A"]["now"] == 0.03  # den forrige model er beholdt
    # Afprøvet på den gemte model: hverken den forrige 9,9 eller den nye models 0,1
    assert ex["validated_windows"] >= 20 and ex["validated_6h_k"] < 0.05
    assert prev_extra["validated_6h_k"] == 9.9  # den forrige ordbog er ikke ændret


async def test_device_tree(ha: HomeAssistant) -> None:
    """Rummets enhed er forbundet via sin varmekildes enhed, som er forbundet via huset."""
    from homeassistant.helpers import device_registry as dr

    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    hyd_id = next(iter(e.subentries))
    ha.config_entries.async_add_subentry(e, __import__("homeassistant.config_entries", fromlist=["ConfigSubentry"]).ConfigSubentry(
        data={"name": "Stue", "temperature": "sensor.rum_1", "heat_source": hyd_id}, subentry_type="room", title="Stue", unique_id=None))
    from unittest.mock import patch

    with patch("homeassistant.helpers.device_registry.report_usage") as ru:
        assert await ha.config_entries.async_setup(e.entry_id)
        await ha.async_block_till_done(wait_background_tasks=True)
    # Træet bygges med enheds-id'er: ingen advarsel om det udfasede via_device
    assert not [c for c in ru.call_args_list if "via_device" in str(c)]
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


async def test_replan_holds_last_known_room_temperature(ha: HomeAssistant) -> None:
    """Falder en rumføler ud, bruges den sidst kendte værdi i op til 30 min., derefter forudsigelsen – og »Mangler data«
    slår til med en reparation. Imens logges og tjekkes rummet ikke. Måler føleren igen, forsvinder alarmen."""
    from homeassistant.helpers import issue_registry as ir

    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0 + (k % 6) * 0.3} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.5", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_1", "20.5", {"state_class": "measurement"})
    ha.states.async_set("input_number.min_1", "20")
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
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
    assert c.plans[hyd]["rooms_planned"] == ["Stue"] and not c.plans[hyd]["rooms_held"]
    assert "Stue" in c.fc_log[str(h0)]
    # Føleren falder ud: rummet planlægges stadig med 20,5 °C, men logges ikke som ny forudsigelse
    ha.states.async_set("sensor.rum_1", "unavailable")
    c.fc_log.clear()
    await c.async_replan()
    p = c.plans[hyd]
    assert p["rooms_planned"] == ["Stue"] and p["rooms_held"] == ["Stue"] and not p["rooms_skipped"]
    assert "Stue" not in (c.fc_log.get(str(h0)) or {})
    await ha.async_block_till_done()
    st = next(s for s in ha.states.async_all("sensor") if s.entity_id.endswith("plan_now"))
    assert st.attributes["rooms_held"] == ["Stue"]
    flag = lambda: next(s for s in ha.states.async_all("binary_sensor") if s.entity_id.endswith("missing_data"))  # noqa: E731
    issues = lambda: [i for d, i in ir.async_get(ha).issues if d == DOMAIN]  # noqa: E731
    assert flag().state == "off" and not issues()  # kortvarigt væk: ingen alarm
    # Væk i 45 min.: rummet regnes frem fra sidste måling med den målte varme (6 kWh i timen), og planen kører videre på det
    c._started -= 3600  # ude af de første 10 min. efter start
    c.last_known["sensor.rum_1"][1] -= 45 * 60
    b0 = int(now // 300 * 300)
    q5 = {b0 - k * 300: 0.5 for k in range(0, 100)}
    c._recent5_sync = lambda s, a, b: (q5, {k: 2.0 for k in q5})
    await c.async_replan()
    await ha.async_block_till_done()
    p = c.plans[hyd]
    assert p["rooms_planned"] == ["Stue"] and not p["rooms_skipped"] and not p["rooms_held"]
    est = p["rooms_estimated"]["Stue"]
    # 45 min.: 4,5 kWh × 0,03 + forsinket 4,5 kWh × 0,02 − afkøling 0,005 × 18,5 × 0,75 ≈ +0,16 °C
    assert 20.6 < est["forudsagt"] < 20.75 and est["minutter"] >= 45
    assert "Stue" not in (c.fc_log.get(str(h0)) or {})
    assert c.missing_data["rum"]["Stue"]["bruger"] == "forudsigelsen"
    assert flag().state == "on" and flag().attributes["rum"]["Stue"]["føler"] == "sensor.rum_1"
    assert issues() == [f"missing_{e.entry_id}_sensor.rum_1"]
    assert ir.async_get(ha).async_get_issue(DOMAIN, issues()[0]).translation_key == "missing_room"
    # Føleren måler igen: forudsigelsen og alarmen forsvinder
    ha.states.async_set("sensor.rum_1", "20.7")
    await c.async_replan()
    await ha.async_block_till_done()
    assert not c.plans[hyd]["rooms_estimated"] and flag().state == "off" and not issues()


async def test_outdoor_missing_uses_forecast_and_flags(ha: HomeAssistant) -> None:
    """Udeføleren væk i over 30 min.: planen bruger vejrudsigten (her ingen: 5 °C), og »Mangler data« slår til."""
    from homeassistant.helpers import issue_registry as ir

    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "unavailable")
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.last_known["sensor.ude"] = [3.0, now - 40 * 60]
    await c.async_replan()
    assert not c.missing_data  # de første 10 min. efter start: ingen alarm
    c._started -= 3600
    await c.async_replan()
    await ha.async_block_till_done()
    out = c.missing_data["udeføler"]
    assert out["føler"] == "sensor.ude" and out["bruger"] == "5 °C (ingen vejrudsigt)" and out["minutter"] >= 40
    iid = f"missing_{e.entry_id}_sensor.ude"
    assert ir.async_get(ha).async_get_issue(DOMAIN, iid).translation_key == "missing_outdoor"
    ha.states.async_set("sensor.ude", "2.5")
    await c.async_replan()
    assert "udeføler" not in c.missing_data and ir.async_get(ha).async_get_issue(DOMAIN, iid) is None


async def test_replan_uses_measured_heat(ha: HomeAssistant) -> None:
    """De seneste 6 timers målte varme går med i forudsigelsen (gulvets forsinkelse), og tjekket regnes med den målte varme."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0 + (k % 6) * 0.3} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.5", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_1", "20.5", {"state_class": "measurement"})
    ha.states.async_set("input_number.min_1", "20")
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
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
    c._recent_sync = lambda s, a, b: ({}, {})
    await c.async_replan()
    without = c.plans[hyd]["rooms"]["Stue"][0]
    q = {h0 - k * 3600: 3.0 for k in range(1, 13)}
    c._recent_sync = lambda s, a, b: (q, {h: 2.0 for h in q})
    c.fc_log.clear()
    await c.async_replan()
    # 3 kWh for 6 timer siden × 0,02 °C/kWh forsinket: rummet ventes varmere (planen kan så give lidt mindre varme nu)
    assert c.plans[hyd]["rooms"]["Stue"][0] > without
    assert c.fc_log[str(h0)]["_t0"] == {"Stue": 20.5}
    # Forudsigelsen fra for 6 timer siden tjekkes med den varme, der faktisk kom (ikke planens)
    c.fc_log = {str(h0 - 6 * 3600): {"Stue": [20.0] * 8, "_t0": {"Stue": 20.0}}}
    await c.async_replan()
    k = c.rooms_check["Stue"]
    assert k["grundlag"] == "målt varme" and k["ventet_med_plan"] == 20.0 and k["ventet"] > 20.2
    assert k["status"] in ("ramt", "over", "under") and k["målt"] == 20.5


async def test_check_air_room_with_measured_electricity(ha: HomeAssistant) -> None:
    """Rum på en luft-luft-kilde tjekkes med den målte el (ikke døgnets gennemsnit)."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "21.0", {"state_class": "measurement"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air},
                                                           subentry_type="room", title="Loft", unique_id=None))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.rooms["sensor.rum_2"] = {"value": 0.4, "extra": {"sources": {"Luft-luft": {"now": 0.5, "delayed": 0.0}}, "accuracy_6h_k": 0.3}}
    el = {h0 - k * 3600: 0.8 for k in range(1, 13)}
    c._recent_sync = lambda s, a, b: (el, {h: 2.0 for h in el}) if s.sid == air else ({}, {})
    c.fc_log = {str(h0 - 6 * 3600): {"Loft": [17.0] * 8, "_t0": {"Loft": 21.0}}}
    await c.async_replan()
    k = c.rooms_check["Loft"]
    assert k["grundlag"] == "målt el" and k["ventet_med_plan"] == 17.0
    # 0,8 kWh el i timen × 0,5 °C/kWh holder rummet varmt: ventet langt over planens 17 °C
    assert k["ventet"] > 19.5


async def test_check_limit_follows_honest_precision(ha: HomeAssistant) -> None:
    """Et svagt rum (afprøvet ±1,2 °C) får grænse 2 × 1,2 – margenens loft på 0,6 gælder kun planen."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "21.0", {"state_class": "measurement"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air},
                                                           subentry_type="room", title="Loft", unique_id=None))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.rooms["sensor.rum_2"] = {"value": 0.4, "r2": 0.5, "extra": {"sources": {"Luft-luft": {"now": 0.5, "delayed": 0.0}},
                                                                   "accuracy_6h_k": 0.7, "validated_6h_k": 1.2}}
    c._recent_sync = lambda s, a, b: ({}, {})
    c.fc_log = {str(h0 - 6 * 3600): {"Loft": [19.5] * 8, "_t0": {"Loft": 19.5}}}
    await c.async_replan()
    k = c.rooms_check["Loft"]
    assert k["grundlag"] == "planens varme" and k["afvigelse"] == 1.5
    assert k["grænse"] == 2.4 and k["status"] == "over" and k["flag"] is False


async def test_cold_caution_setting(ha: HomeAssistant) -> None:
    """Forsigtighed ved kulde: standard +10 % varmetab, når det er koldere end målt; 0 slår den fra."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    hyd = next(iter(e.subentries))
    # Varmetabet er målt ned til 5 °C; i dag er det 2 °C (mere end 1 °C koldere)
    c.sources[hyd].accepted["heat_loss"] = {"value": 100.0, "t_out_min": 5.0, "extra": {"gains_w": 0.0}}
    await c.async_replan()
    p = c.plans[hyd]
    assert p["cautious"] is True and "+10 % varmetab" in p["reason"]
    d10 = p["demand_kwh"]
    await c.async_set_setting("cold_caution", 0)
    await c.async_replan()
    p = c.plans[hyd]
    assert p["cautious"] is False and abs(d10 / p["demand_kwh"] - 1.1) < 0.01


async def test_air_source_gets_plan_and_command(ha: HomeAssistant) -> None:
    """En luft-luft-kilde får sin egen plan (el pr. time) og anbefalet styring; loftet fra rummets opsætning bruges."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "19.0", {"state_class": "measurement"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air,
                                                                 "minimum": 19.5, "maximum": 22.5}, subentry_type="room", title="Loft", unique_id=None))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.rooms["sensor.rum_2"] = {"value": 0.4, "r2": 0.6, "extra": {"sources": {"Luft-luft": {"now": 0.7, "delayed": 0.0}}, "accuracy_6h_k": 0.3}}
    c._recent_sync = lambda s, a, b: ({}, {})
    await c.async_replan()
    await ha.async_block_till_done()
    p = c.plans[air]
    assert p["kind"] == "air_air" and p["rooms_planned"] == ["Loft"] and p["caps"] == {"Loft": 22.5}
    assert p["el_kwh"] > 0 and p["hours"][0]["heat_kw"] > 0  # 19,0 °C mod gulv 19,5 + komfort + usikkerhed: der varmes straks
    ents = {s.entity_id: s for s in ha.states.async_all("sensor")}
    cmd = next(s for k, s in ents.items() if k.endswith("recommended_control") and "luft_luft" in k)
    assert cmd.state == "heat" and cmd.attributes["setpoint"] == 22.0
    plan_now = next(s for k, s in ents.items() if k.endswith("plan_now") and "luft_luft" in k)
    assert plan_now.state == "heat" and plan_now.attributes["kind"] == "air_air"


async def test_air_ladder_shadow_and_control(ha: HomeAssistant) -> None:
    """Luft-luft med klimaentitet: stigen regnes i skygge (ingen skrivning); med »Varmemodel styrer« skrives trinnet."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "19.0", {"state_class": "measurement"})
    ha.states.async_set("climate.ll", "off", {"fan_modes": ["Auto", "Low", "Mid", "High"], "preset_modes": ["none", "quiet"],
                                               "swing_modes": ["Auto", "Up"], "temperature": 20, "fan_mode": "Low"})
    air_cfg = {**AIR, "climate": "climate.ll"}
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub(air_cfg, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air,
                                                                 "minimum": 19.5, "maximum": 22.5}, subentry_type="room", title="Loft", unique_id=None))
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(("hvac", call.data["hvac_mode"])))
    ha.services.async_register("climate", "set_fan_mode", lambda call: calls.append(("fan", call.data["fan_mode"])))
    ha.services.async_register("climate", "set_temperature", lambda call: calls.append(("sp", call.data["temperature"])))
    ha.services.async_register("climate", "set_preset_mode", lambda call: calls.append(("preset", call.data["preset_mode"])))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c.rooms["sensor.rum_2"] = {"value": 0.4, "r2": 0.6, "extra": {"sources": {"Luft-luft": {"now": 0.7, "delayed": 0.0}}, "accuracy_6h_k": 0.3}}
    c._recent_sync = lambda s, a, b: ({}, {})
    for sid in c.sources:
        c.settings[f"{sid}.air_start"] = "hurtig"  # testen gælder stigens mekanik; rolig opstart testes for sig
        # Samme maks. blæser dag og nat, så stigen ikke afhænger af, hvornår testen køres
        c.settings[f"{sid}.air_fan_day"] = c.settings[f"{sid}.air_fan_night"] = "High"
    await c.async_replan()
    await ha.async_block_till_done()
    step = next(s for s in ha.states.async_all("sensor") if s.entity_id.endswith("ladder_step") or s.entity_id.endswith("air_step"))
    # en start begynder på højeste blæser (trin 3 = High på den laveste temperatur); quiet + 3 blæsere på hver af 7 temperaturer
    assert step.state == "3" and len(step.attributes["trin_liste"]) == (1 + 3) * 7
    assert not calls  # kun anbefaling, så længe kontakten er slået fra
    # Afrimninger uden afrimningsføler læses af el-effekten hvert kvarter og står på trin-føleren
    assert "afrimninger_24t" not in step.attributes
    c.sources[air].conf = {**c.sources[air].conf, "el_power": "sensor.ll_effekt"}
    c._air_defrost_sync = lambda _s: {"p_off": 5.0, "defrost": {"n": 2, "last": h0 - 600, "t": h0,  # noqa: SLF001
                                                                 "list": [[h0 - 3000, h0 - 2880, 130], [h0 - 780, h0 - 600, 104]]}}
    await c._on_today(None)  # noqa: SLF001
    await c.async_replan()
    await ha.async_block_till_done()
    step = ha.states.get(step.entity_id)
    assert step.attributes["afrimninger_24t"] == 2 and step.attributes["seneste_afrimning"].startswith("20")
    assert step.attributes["afrimninger"][1]["min"] == 3 and step.attributes["afrimninger"][1]["laveste_w"] == 104 and c.air[air]["p_off"] == 5.0
    assert not calls
    sel = {s.entity_id: s for s in ha.states.async_all("select")}
    day = next(s for k, s in sel.items() if k.endswith("max_fan_day"))
    assert day.state == "High" and day.attributes["options"] == ["Low", "Mid", "High"]
    sw = next(s for s in ha.states.async_all("switch") if s.entity_id.endswith("controls"))
    await ha.services.async_call("switch", "turn_on", {"entity_id": sw.entity_id}, blocking=True)
    await ha.async_block_till_done()
    assert ("hvac", "heat") in calls and ("fan", "High") in calls
    # »Jeg fryser«: øverste trin med powerful – også selvom planen ikke varmer
    ha.states.async_set("climate.ll", "heat", {"fan_modes": ["Auto", "Low", "Mid", "High"], "preset_modes": ["none", "quiet", "powerful"],
                                                "swing_modes": ["Auto", "Up"], "temperature": 20, "fan_mode": "Low", "preset_mode": "quiet"})
    calls.clear()
    boost = next(s for s in ha.states.async_all("switch") if "cold" in s.entity_id)
    await ha.services.async_call("switch", "turn_on", {"entity_id": boost.entity_id}, blocking=True)
    await ha.async_block_till_done()
    assert ("preset", "powerful") in calls and any(k == "sp" for k, _ in calls)


async def test_plant_week_survives_restart(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    """Varmecentralens 7-døgns snit gemmes, så planen har tabet straks efter en genstart (før 0.10.2: først efter natkalibreringen)."""
    c = entry.runtime_data
    c.plant_week = 14.5
    await c._save()
    data = await c.store.async_load()
    assert data["plant_week"] == 14.5



async def test_room_model_from_older_method_is_replaced(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    """En rummodel regnet med en ældre metode (uden versionsmærke) erstattes af den nye uden ±10 %-grænsen."""
    c = entry.runtime_data
    c.rooms["sensor.rum_1"] = {"value": 0.10, "extra": {"sources": {"A": {"now": 0.03, "delayed": 0.0}}}, "t_out_min": 0.0, "t_out_max": 5.0}
    new = Fit(0.30, 50, 0.7, 0.2, 0.0, 5.0, "fit", {"sources": {"A": {"now": 0.05, "delayed": 0.0}}})
    orig = c._rooms_fit
    c._rooms_fit = lambda _h, keep=None: {"sensor.rum_1": new}
    try:
        await c.async_calibrate()
    finally:
        c._rooms_fit = orig
    r = c.rooms["sensor.rum_1"]
    assert r["value"] == 0.30 and r["guard"].startswith("ny model") and r["extra"]["sources"]["A"]["now"] == 0.05


async def test_measure_is_tied_to_floor(ha: HomeAssistant) -> None:
    """Et energitiltag måles på den etage, det gælder: kildens navn eller »1. sal« i teksten, ellers den vandbårne."""
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE, subentries_data=[_sub(HYD, "source"), _sub(AIR, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    assert c.measure_source("Ny tætningsliste – 1. sal").kind == "air_air"
    assert c.measure_source("Justering af luft-luft lameller").kind == "air_air"
    assert c.measure_source("Isolering af rør ved varmepumpen").kind == "hydronic"
    assert c.measure_source("Flyttet termostat i stue").kind == "hydronic"  # rummets egen kilde (ingen rum i testen → vandbåren)


async def test_flow_control_writes_plan_flow_only_in_heat_mode(ha: HomeAssistant) -> None:
    """P1: med »Varmemodel styrer fremløb« skrives planens fremløb til varmepumpen – kun i varme-tilstand og kun ved ændring."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_flow", "auto", {"temperature": 17, "min_temp": 20, "max_temp": 50})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "flow_target": "climate.vp_flow"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: calls.append(call.data["temperature"]))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    sw = next(s for s in ha.states.async_all("switch") if s.entity_id.endswith("flow"))
    await ha.services.async_call("switch", "turn_on", {"entity_id": sw.entity_id}, blocking=True)
    await ha.async_block_till_done()
    assert not calls  # auto-tilstand: varmepumpen følger sin egen kurve – ingen skrivning
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 17, "min_temp": 20, "max_temp": 50})
    await c.async_replan()
    await ha.async_block_till_done()
    assert len(calls) == 1 and 20 <= calls[0] <= 50
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": calls[0], "min_temp": 20, "max_temp": 50})
    await c.async_replan()
    await ha.async_block_till_done()
    assert len(calls) == 1  # uændret mål: ingen ny skrivning


async def test_hydro_control_switches_heat_pump(ha: HomeAssistant) -> None:
    """P2: »Varmemodel styrer varmepumpen« tænder varmepumpen (varme), når den anbefalede styring siger varme."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_onoff", "off", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp_onoff"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(call.data["hvac_mode"]))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    sw = next(s for s in ha.states.async_all("switch") if s.entity_id.endswith("heat_pump"))
    await ha.services.async_call("switch", "turn_on", {"entity_id": sw.entity_id}, blocking=True)
    await ha.async_block_till_done()
    sid = next(iter(c.sources))
    if c.commands[sid].get("on"):
        assert calls == ["heat"]
    else:
        assert calls == []


async def test_soft_start_ramps_flow_target(ha: HomeAssistant) -> None:
    """Blød start: lige efter tænding skrives retur + 4 °C (mindst 20 °C), derefter +2 °C pr. kvarter."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.retur", "22.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 33, "min_temp": 20, "max_temp": 45})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "flow_target": "climate.vp_flow", "return_temp": "sensor.retur"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: calls.append(call.data["temperature"]))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    s = next(iter(c.sources.values()))
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    hs.update(soft=True, start_t=time.time(), ramp0=None)
    hours = [{"flow": 34.0, "blocked": False}]
    c.commands[s.sid] = {"flow": 34}
    await c._flow_apply(s, hours, False)
    assert calls == [25.0]  # retur 22 + 3
    hs["start_t"] = time.time() - 1800  # to kvarterer inde
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 25, "min_temp": 20, "max_temp": 45})
    await c._flow_apply(s, hours, False)
    assert calls[-1] == 29.0


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_soft_start_writes_low_flow_before_switching_on(ha: HomeAssistant) -> None:
    """Blød start: det lave fremløbsmål skrives FØR varmepumpen tændes (ellers kører den minutter mod et gammelt mål)."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.retur", "21.5", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_flow", "off", {"temperature": 33, "min_temp": 20, "max_temp": 45})
    ha.states.async_set("climate.vp", "off", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "flow_target": "climate.vp_flow", "onoff_target": "climate.vp",
                                               "return_temp": "sensor.retur"}, "source")])
    e.add_to_hass(ha)
    order: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: order.append(("flow", call.data["temperature"])))
    ha.services.async_register("climate", "set_hvac_mode", lambda call: order.append(("mode", call.data["hvac_mode"])))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    s = next(iter(c.sources.values()))
    c.settings[f"{s.sid}.soft_start"] = "til"
    c.commands[s.sid] = {"flow": 35}
    order.clear()
    await c._hydro_apply(s, True, False, [{"flow": 35.0, "blocked": False}])
    assert order[:2] == [("flow", 24.0), ("mode", "heat")]  # retur 21,5 + 3 → 24 (afrundet til lige), før tænding
    assert c.air[f"hydro:{s.sid}"]["soft_now"] == 24.0


async def _lv_entry(ha: HomeAssistant, extra_house: dict | None = None, extra_source: dict | None = None):
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.retur", "21.5", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 33, "min_temp": 20, "max_temp": 45})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris", **(extra_house or {})},
                        subentries_data=[_sub({**HYD, "flow_target": "climate.vp_flow", "return_temp": "sensor.retur", **(extra_source or {})}, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    return c, next(iter(c.sources.values()))


async def test_live_block_entity(ha: HomeAssistant) -> None:
    """»Blokering lige nu«: fx prisstatus rød, når kvarterets pris er over loftet."""
    ha.states.async_set("sensor.prisstatus", "gul")
    c, _s = await _lv_entry(ha, {"block_now_entity": "sensor.prisstatus", "block_now_states": "rød, on"})
    assert c.live_blocked() is False
    ha.states.async_set("sensor.prisstatus", "rød")
    assert c.live_blocked() is True


async def test_low_flow_search_rules(ha: HomeAssistant) -> None:
    """Søg laveste fremløb: start ved retur + 4, hold, +1 K/t på vej under gulvet, +2 K/30 min. under gulvet,
    −1 K hver 2. time med god margen – aldrig over planens fremløb."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    rooms: list = []
    c._low_rooms_sync = lambda _s: [dict(x) for x in rooms]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 24.0  # retur 21,5 + 3 → 24 (afrundet)
    rooms[:] = [{"navn": "Gang", "margen": 0.2, "tendens": 0.0, "om_3t": 0.2}]
    assert await c._low_flow(s, hs, 35.0) == 24.0 and hs["low_status"] == "holder"
    rooms[:] = [{"navn": "Gang", "margen": 0.2, "tendens": -0.05, "om_3t": 0.05}]
    assert await c._low_flow(s, hs, 35.0) == 24.0 and hs["low_status"] == "hæver"  # venter en time
    hs["low"] = 26.0
    hs["low_t"] -= 3600
    assert await c._low_flow(s, hs, 35.0) == 27.0 and "Gang" in hs["low_why"]
    rooms[:] = [{"navn": "Gang", "margen": -0.2, "tendens": 0.0, "om_3t": -0.2}]
    hs["low_t"] -= 1800
    assert await c._low_flow(s, hs, 35.0) == 29.0  # under gulvet og ikke på vej op: +2
    rooms[:] = [{"navn": "Gang", "margen": -0.1, "tendens": 0.08, "om_3t": 0.14}]
    hs["low_t"] -= 1800
    assert await c._low_flow(s, hs, 35.0) == 29.0  # på vej op: hold
    rooms[:] = [{"navn": "Gang", "margen": 0.6, "tendens": 0.0, "om_3t": 0.6}, {"navn": "Stue", "margen": 0.9, "tendens": 0.01, "om_3t": 0.93}]
    hs["low_t"] -= 7200
    assert await c._low_flow(s, hs, 35.0) == 28.0 and hs["low_status"] == "sænker"
    assert await c._low_flow(s, hs, 27.0) == 27.0  # aldrig over planens fremløb
    v = c.flow_search(s)
    assert v["mål"] == 28.0 and v["status"] == "sænker" and v["log"]


async def test_neutral_return_sets_the_floor_of_the_search(ha: HomeAssistant) -> None:
    """Gulvenes neutrale retur: fordelerens retur efter 10 min. cirkulation uden varme (ind ≈ retur). Søgningen starter fra
    den – ikke fra varmepumpens kolde føler efter stilstand – og går aldrig under den + 3 °C."""
    ha.states.async_set("binary_sensor.komp", "off")
    ha.states.async_set("binary_sensor.vv", "off")
    ha.states.async_set("binary_sensor.p2", "on")
    ha.states.async_set("sensor.fordeler_ind", "22.9")
    ha.states.async_set("sensor.fordeler_retur", "22.8")
    c, s = await _lv_entry(ha, extra_source={"compressor": "binary_sensor.komp", "dhw": "binary_sensor.vv",
                                             "loop_supply_temp": "sensor.fordeler_ind", "loop_return_temp": "sensor.fordeler_retur",
                                             "loop_pump": "binary_sensor.p2"})
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    now = time.time()
    c._neutral_sample(s, hs, now)  # noqa: SLF001
    assert hs.get("neutral") is None  # pumpe og stilstand er lige begyndt: ikke 10 min. endnu
    c._neutral_sample(s, hs, now + 700)  # noqa: SLF001
    assert hs["neutral"]["v"] == 22.8 and hs["neutral"]["via"] == "slukket"
    assert c._neutral_floor(hs) == 26.0  # noqa: SLF001 – 22,8 + 3 → 26
    # Varmepumpens egen retur er kold efter stilstand (stillestående vand i udedelen): starten regnes fra gulvene
    ha.states.async_set("sensor.retur", "17.1")
    assert c._ret_start(s, hs) == 22.8  # noqa: SLF001
    rooms: list = []
    c._low_rooms_sync = lambda _s: [dict(x) for x in rooms]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 26.0
    # Alle rum har god margen: søgningen sænker – men aldrig under bunden
    rooms[:] = [{"navn": "Gang", "margen": 0.9, "tendens": 0.0, "om_3t": 0.9}]
    hs["low_t"] -= 7200
    assert await c._low_flow(s, hs, 35.0) == 26.0
    # Et gammelt mål under bunden løftes straks
    hs["low"], hs["low_t"] = 21.0, time.time()
    assert await c._low_flow(s, hs, 35.0) == 26.0 and "neutrale retur" in hs["low_why"]
    # Ind og retur langt fra hinanden (bufferen varmer gulvene): ingen ny måling
    ha.states.async_set("sensor.fordeler_ind", "33.5")
    c._neutral_sample(s, hs, now + 2000)  # noqa: SLF001
    assert hs["neutral"]["t"] == round(now + 700)
    v = c.flow_search(s)
    assert v["bund"] == 26.0 and v["neutral_retur"]["v"] == 22.8 and v["fordeler"]["forskel"] == 10.7


async def test_no_heat_to_floor_loop_raises_the_target(ha: HomeAssistant) -> None:
    """Under drift: fordelerens ind − retur under 0,3 °C i 20 min. = gulvkredsen får næsten ingen varme → målet +1 °C."""
    ha.states.async_set("binary_sensor.komp", "on")
    ha.states.async_set("sensor.fordeler_ind", "24.4")
    ha.states.async_set("sensor.fordeler_retur", "24.3")
    c, s = await _lv_entry(ha, extra_source={"compressor": "binary_sensor.komp", "loop_supply_temp": "sensor.fordeler_ind",
                                             "loop_return_temp": "sensor.fordeler_retur"})
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    c._low_rooms_sync = lambda _s: [{"navn": "Gang", "margen": 0.3, "tendens": 0.0, "om_3t": 0.3}]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 24.0
    t0 = time.time() + 1300  # kompressoren har kørt længe nok (opstarten er forbi)
    c._neutral_sample(s, hs, t0)  # noqa: SLF001
    assert hs["nodelta_since"] == t0
    hs["nodelta_since"] = time.time() - 1300
    c._neutral_sample = lambda *_a: None  # noqa: SLF001 – testens kompressor har ikke kørt i virkeligheden
    hs["low_t"] -= 1800
    assert await c._low_flow(s, hs, 35.0) == 25.0 and "næsten ingen varme" in hs["low_why"]


def test_calm_ladder_drops_untested_high_step() -> None:
    """Rolig opstart: et trin over det afprøvede (fx 11 fra før) sættes ned til midten og prøves 30 min."""
    import inspect

    from custom_components.varmemodel import coordinator as co

    src = inspect.getsource(co.VarmemodelCoordinator._air_step)
    assert "prøver trin" in src and "soft_tested" in src


async def test_low_cap_order(ha: HomeAssistant) -> None:
    """Planens fremløb under »søg laveste«: målet nu → lært for udetemperaturen → sidste mål → retur + 3."""
    c, s = await _lv_entry(ha)
    hs: dict = {}
    assert c._low_cap(s, hs, 1.0) == 24.0  # retur 21,5 + 3 → 24 (afrundet)
    hs["low_last"] = 27.0
    assert c._low_cap(s, hs, 1.0) == 27.0
    hs["low_learned"] = {"0": 26.0}
    assert c._low_cap(s, hs, 1.0) == 26.0
    hs["low"] = 29.0
    assert c._low_cap(s, hs, 1.0) == 29.0
    from custom_components.varmemodel.coordinator import _dk_txt

    assert _dk_txt("Gang +0.2 °C, -0.08 °C/t, om 3 t -0.0") == "Gang +0,2 °C, −0,08 °C/t, om 3 t ±0,0"


async def test_low_flow_preheat_shown(ha: HomeAssistant) -> None:
    """Søg laveste: når planen hæver for at forvarme før en blokering, vises det (mål nu = søgning + hævning, højst loftet)."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    ha.states.async_set("climate.vp", "heat", {})
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    hs.update(low=28.0, start_t=time.time(), low_t=time.time(), low_status="holder", low_loft=35.0, low_raise=3.0)
    v = c.flow_search(s)
    assert v["forvarmer"] == 3.0 and v["mål_nu"] == 31.0 and v["tekst"].startswith("Forvarmer: 31 °C")
    # søgningen har selv hævet sit mål, siden planen regnede: forvarmningen vises, som den står – klippet af loftet
    hs.update(low=29.0, low_loft=34.8, low_raise=8.0)
    v = c.flow_search(s)
    assert v["forvarmer"] == 6.0 and v["mål_nu"] == 34.8 and v["tekst"].startswith("Forvarmer: 35 °C (søgningens 29 °C + 6 °C)")
    hs.update(low=35.0, low_loft=35.0, low_raise=4.0)  # søgningen står på loftet: ingen forvarmning at vise
    v = c.flow_search(s)
    assert v["forvarmer"] is None and v["mål_nu"] is None and not v["tekst"].startswith("Forvarmer")


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_flip_cap_still_switches_on(ha: HomeAssistant) -> None:
    """Efter loftet på tænd/sluk slukker Varmemodel kun ved blokering – men tænder stadig (huset står aldrig uden varme)."""
    c, s = await _lv_entry(ha)
    s.conf["onoff_target"] = "climate.vp"
    ha.states.async_set("climate.vp", "off", {})
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(call.data["hvac_mode"]))
    h = c.air.setdefault(f"hydro:{s.sid}", {})
    h.update(day=dt_util.now().date().isoformat(), flips=10)
    await c._hydro_apply(s, True, False, [{"flow": 30.0, "blocked": False}])
    assert calls == ["heat"]
    ha.states.async_set("climate.vp", "heat", {})
    calls.clear()
    await c._hydro_apply(s, False, False, [])
    assert calls == []  # frivillig sluk afvises efter loftet
    await c._hydro_apply(s, False, True, [])
    assert calls == ["off"]  # blokering slukker altid


def test_comp_stops_start_row_is_no_defrost_end() -> None:
    """Historikkens første række (tilstanden ved forespørgslens start) er intet skift: en »slukket« afrimning dér skjuler
    ikke et stop (før 0.17.3 blev stoppet 10:21 overset, fordi vinduet startede 10:20). Kørslens længde og returen ved
    kompressorstart følger med."""
    from custom_components.varmemodel.coordinator import comp_stops

    rows = {"c": [(0, "off"), (1780, "on"), (2060, "off")], "d": [(0, "off")], "w": [(0, "off")],
            "r": [(0, "23.5"), (1700, "23.5"), (1900, "24.2")]}
    assert comp_stops(rows, "c", "w", "d", 1800, "r") == [{"t": 2060, "run": 280, "ret0": 23.5, "df": None}]
    assert comp_stops(rows, "c", "w", "d", 2100, "r") == []  # før vinduet


def test_comp_stops_defrost_dhw_and_dropouts() -> None:
    """Ventilskiftet ved en afrimning og stop lige efter varmt vand tæller ikke; et stop efter afrimningens genstart er en
    kort kørsel (opstarten regnes fra afrimningens slut); et Modbus-udfald er hverken stop eller start."""
    from custom_components.varmemodel.coordinator import comp_stops

    # Afrimning slutter 1740: ventilskiftet (1745) tæller ikke; stoppet 4 min. efter genstarten (1980) er en kort kørsel
    rows = {"c": [(0, "on"), (1745, "off"), (1755, "on"), (1980, "off")], "d": [(0, "off"), (1500, "on"), (1740, "off")]}
    assert comp_stops(rows, "c", None, "d", 1800) == [{"t": 1980, "run": 225, "ret0": None, "df": "efter"}]
    # 24/11 01:54: kompressoren kørte videre gennem afrimningen (intet ventilstop), men opstarten efter afrimningen stoppede
    rows = {"c": [(0, "on"), (2300, "off")], "d": [(0, "off"), (1500, "on"), (1740, "off")], "r": [(0, "27.8")]}
    assert comp_stops(rows, "c", None, "d", 1800, "r") == [{"t": 2300, "run": 560, "ret0": 27.8, "df": "efter"}]
    # Varmt vand slutter 2000: stop 100 s efter tæller ikke, stop 300 s efter tæller (lang kørsel)
    rows = {"c": [(0, "off"), (900, "on"), (2100, "off")], "w": [(0, "off"), (1000, "on"), (2000, "off")]}
    assert comp_stops(rows, "c", "w", None, 1800) == []
    rows["c"][2] = (2300, "off")
    assert comp_stops(rows, "c", "w", None, 1800) == [{"t": 2300, "run": 1400, "ret0": None, "df": None}]
    # Udfald: »utilgængelig« → »tændt« er ingen ny start; længden er ukendt (tændt før data)
    rows = {"c": [(0, "on"), (1900, "unavailable"), (1920, "on"), (2500, "off")], "d": [(0, "off"), (1900, "unavailable"), (1930, "off")]}
    assert comp_stops(rows, "c", None, "d", 1800) == [{"t": 2500, "run": None, "ret0": None, "df": None}]


def test_start_lift_learning() -> None:
    """Startløftet læres af korte kørsler (målet dengang − returen ved start + 1 K) og rundes op; standarden rundes som før."""
    from custom_components.varmemodel.coordinator import learn_start_k, start_flow

    k = learn_start_k(3.0, [{"ret0": 24.2, "mål": 27.0}], 3.0)  # 24/11 12:22: mål 27, retur 24,2 → stop efter 6 min.
    assert k == 3.8
    k = learn_start_k(k, [{"ret0": 24.2, "mål": 28.0}], 3.0)  # 12:33: mål 28 → stop igen
    assert k == 4.8
    assert learn_start_k(k, [{"ret0": None, "mål": 28.0}], 3.0) == 4.8 and learn_start_k(2.0, [], 3.0) == 3.0
    assert learn_start_k(3.0, [{"ret0": 20.0, "mål": 40.0}], 3.0) == 10.0  # loft
    assert start_flow(24.2, 4.8, 3.0, 20.0) == 29.0  # lært løft: 29,0 → 29 (rundes op)
    assert start_flow(21.5, 3.0, 3.0, 20.0) == 24.0 and start_flow(23.5, 4.0, 3.0, 20.0) == 28.0  # standard: 24,5 → 24; lært: 27,5 → 28
    assert start_flow(None, 4.8, 3.0, 20.0) is None and start_flow(10.0, 3.0, 3.0, 20.0) == 20.0


async def test_low_flow_short_run_learns_lift_not_search(ha: HomeAssistant) -> None:
    """Kort kørsel: startløftet læres af målet, der stod i varmepumpen ved stoppet (Varmemodels egne skrivninger) − returen
    + 1 K – én gang pr. stop. Søgningens mål hæves ikke (det er opstarten, ikke rummene, der kræver mere). Stop efter længere
    drift hæver som før efter en halv time; en start, der kommer igennem opstarten, sænker løftet 0,25."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    c._low_rooms_sync = lambda _s: [{"navn": "Gang", "margen": 0.2, "tendens": 0.0, "om_3t": 0.2}]  # noqa: SLF001
    stops: list = []
    c._comp_stops_sync = lambda _s, _since: [dict(x) for x in stops]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 24.0  # retur 21,5 + 3 = 24,5 → 24
    now = time.time()
    c.air[f"flow:{s.sid}"] = {"hist": [[now - 600, 26.0], [now - 30, 29.0]]}  # 26 ved stoppet, 29 skrevet bagefter
    stops[:] = [{"t": now - 60, "run": 280.0, "ret0": 21.5, "df": None}]
    assert await c._low_flow(s, hs, 35.0) == 24.0  # søgningens mål uændret
    assert hs["start_k"] == 5.5 and hs["start_short"] == 1 and hs["low_stops"] == 0  # 26 − 21,5 + 1
    assert await c._low_flow(s, hs, 35.0) == 24.0 and hs["start_k"] == 5.5 and hs["start_short"] == 1  # samme stop én gang
    v = c.flow_search(s)
    assert v["startløft"] == 5.5 and v["korte_kørsler"] == 1 and v["sidste_korte"]["mål"] == 26.0
    # Stop efter længere drift (efter sidste ændring af målet): et helt nyt stop dømmes ikke endnu (varmt vand kan nå at
    # melde tændt – så var det skiftet til varmt vand); derefter venter søgningen en halv time, så +1
    short_stop = dict(stops[0])
    stops[:] = [short_stop, {"t": time.time() - 5, "run": 3600.0, "ret0": 22.0, "df": None}]
    hs["low_t"] = time.time() - 10
    assert await c._low_flow(s, hs, 35.0) == 24.0 and hs["low_status"] == "holder" and hs["low_stops"] == 0
    stops[1]["t"] = time.time() - 130
    hs["low_t"] = time.time() - 140
    assert await c._low_flow(s, hs, 35.0) == 24.0 and hs["low_status"] == "hæver" and hs["low_stops"] == 1
    hs["low_t"] -= 1800
    assert await c._low_flow(s, hs, 35.0) == 25.0 and "længere drift" in hs["low_why"]
    # Ny start: søgningen starter ved retur + 3; løftet nævnes (det lægges på det, der skrives)
    stops[:] = []
    hs.update(start_t=time.time() - 1500, start_short=0, start_ok=False, short_seen=hs["short_seen"] - 3600)  # den korte kørsel lå før denne start
    assert await c._low_flow(s, hs, 35.0, pre_start=True) == 24.0 and "opstartsløft retur + 5.5 °C" in hs["low_why"]
    hs["comp_since"] = time.time() - 1300  # kørt 21 min. siden starten uden kort kørsel
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 5.25 and hs["start_ok"] is True
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 5.25  # kun én gang pr. start


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_guard_start_lift_and_anti_stop(ha: HomeAssistant) -> None:
    """Varmemodel bestemmer start og stop: opstartsløft (kompressoren står, lige startet, under/efter afrimning), stopværn
    under drift (målet højst 2 °C under fremløbet), intet ved varmt vand; skrives straks efter et skift, aldrig over loftet."""
    from homeassistant.core import State

    c, s = await _lv_entry(ha)
    s.conf.update(compressor="binary_sensor.komp", defrost="binary_sensor.afrim", dhw="binary_sensor.vv", flow_temp="sensor.frem")
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    hs["start_k"] = 5.1
    for e, v in (("binary_sensor.afrim", "off"), ("binary_sensor.vv", "off"), ("sensor.frem", "30.0"), ("binary_sensor.komp", "off")):
        ha.states.async_set(e, v)
    assert c._guard_value(s, hs) == (27.0, "opstart")  # retur 21,5 + 5,1 = 26,6 → 27
    ha.states.async_set("binary_sensor.komp", "on")
    hs["comp_on_t"] = time.time() - 300
    assert c._guard_value(s, hs) == (28.0, "stopværn")  # opstart 27, men fremløbet 30 − 2 = 28 er højere
    hs["comp_on_t"] = time.time() - 1500
    ha.states.async_set("sensor.frem", "27.0")
    assert c._guard_value(s, hs) == (25.0, "stopværn")  # drift: kun stopværn
    hs["df_off_t"] = time.time() - 120
    assert c._guard_value(s, hs) == (27.0, "opstart")  # lige efter afrimning: opstart igen, intet stopværn endnu
    hs["df_off_t"] = 0
    ha.states.async_set("binary_sensor.vv", "on")
    assert c._guard_value(s, hs) == (None, None)
    ha.states.async_set("binary_sensor.vv", "off")
    hs["dhw_off_t"] = time.time() - 100
    assert c._guard_value(s, hs) == (None, None)  # lige efter varmt vand
    hs["dhw_off_t"] = 0
    # _flow_apply skriver værnet oven på søgningens mål – aldrig over loftet
    calls: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: calls.append(call.data["temperature"]))
    c._low_rooms_sync = lambda _s: []  # noqa: SLF001
    c._comp_stops_sync = lambda _s, _since: []  # noqa: SLF001
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 24, "min_temp": 20, "max_temp": 45})
    hs.update(low=24.0, low_t=time.time(), start_t=time.time() - 3000, low_loft=34.0)
    ha.states.async_set("binary_sensor.komp", "off")
    await c._flow_apply(s, [{"flow": 30.0, "blocked": False}], False)
    assert calls[-1] == 27.0 and hs["guard"] == {"mål": 27.0, "grund": "opstart"}
    v = c.flow_search(s)
    assert v["mål_nu"] == 27.0 and v["mål"] == 24.0 and v["tekst"].startswith("Opstartsløft: 27 °C (søgningens 24 °C)")
    assert v["mål_vp"] == 24.0 and v["mål_kl"] is not None  # varmepumpen står endnu på 24 (testens service ændrer ikke tilstanden)
    hs["low_loft"] = 26.0
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 27, "min_temp": 20, "max_temp": 45})
    await c._flow_apply(s, [{"flow": 30.0, "blocked": False}], False)
    assert calls[-1] == 26.0  # loftet
    # Et rigtigt skift på kompressoren noteres og skriver straks; et Modbus-udfald er intet skift
    hs["low_loft"] = 34.0
    ev = lambda o, n: type("E", (), {"data": {"entity_id": "binary_sensor.komp", "old_state": State("binary_sensor.komp", o),  # noqa: E731
                                                "new_state": State("binary_sensor.komp", n)}})()
    c._on_hydro_state(ev("unavailable", "on"))
    assert "comp_on_t" not in hs or hs["comp_on_t"] < time.time() - 1000
    c._on_hydro_state(ev("on", "off"))
    assert time.time() - hs["comp_off_t"] < 5 and s.sid in c._refresh_pending  # noqa: SLF001
    c.settings[f"{s.sid}.flow_control"] = True
    h0 = int(time.time() // 3600 * 3600)
    c.plans[s.sid] = {"hours": [{"ts": h0, "flow": 30.0, "blocked": False}]}
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 24, "min_temp": 20, "max_temp": 45})
    await c._flow_refresh(s.sid)
    assert calls[-1] == 27.0 and s.sid not in c._refresh_pending  # noqa: SLF001


def test_next_replan() -> None:
    """Næste planlægning (minut 2, 17, 32, 47) på eller efter et tidspunkt – til »→ 35 °C kl. 13:47«."""
    from datetime import datetime

    from custom_components.varmemodel.coordinator import _next_replan

    loc = dt_util.get_default_time_zone()
    t = lambda h, m, sec=0: datetime(2026, 11, 24, h, m, sec, tzinfo=loc).timestamp()  # noqa: E731
    assert _next_replan(t(13, 33, 10)) == t(13, 47) and _next_replan(t(13, 47)) == t(13, 47)
    assert _next_replan(t(13, 50)) == t(14, 2) and _next_replan(t(23, 48)) == datetime(2026, 11, 25, 0, 2, tzinfo=loc).timestamp()


def test_quarter_block_counts() -> None:
    """Blokerede kvarterer tælles pr. time; mindst 2 i en time = blokeret i planen."""
    from datetime import datetime, timezone

    from custom_components.varmemodel.coordinator import quarter_block_counts

    items = ["2026-11-24T06:30:00+01:00", "2026-11-24T06:45:00+01:00", "2026-11-24T10:00:00+01:00"]
    c = quarter_block_counts(items, lambda s: datetime.fromisoformat(s))
    h6 = int(datetime(2026, 11, 24, 5, 0, tzinfo=timezone.utc).timestamp())
    h10 = int(datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc).timestamp())
    assert c[h6] == 2 and c[h10] == 1


def test_red_spans_with_quarters() -> None:
    """Røde perioder samles fra kvarterer, og en start i et rødt kvarter flyttes til periodens slut (rød 10:00–10:15 → 10:15)."""
    from datetime import datetime

    from custom_components.varmemodel.coordinator import after_red, quarter_starts, spans_from_quarters

    items = ["2026-11-24T06:30:00+01:00", "2026-11-24T06:45:00+01:00", "2026-11-24T10:00:00+01:00", "x"]
    qs = quarter_starts(items, lambda s: datetime.fromisoformat(s) if s[0] == "2" else None)
    t = lambda h, m: int(datetime.fromisoformat(f"2026-11-24T{h:02d}:{m:02d}:00+01:00").timestamp())  # noqa: E731
    hours = {t(7, 0) + j * 900 for j in range(12)}  # timeplanens røde timer 07–10
    spans = spans_from_quarters(qs | hours)
    assert spans == [(t(6, 30), t(10, 15))]
    assert after_red(t(10, 0), spans) == t(10, 15) and after_red(t(10, 15), spans) == t(10, 15) and after_red(t(6, 0), spans) == t(6, 0)
    assert spans_from_quarters(set()) == []


async def test_varsel_urgent_only(ha: HomeAssistant) -> None:
    """Rum-beskeden er en nødbesked: kun et nyt, usædvanligt problem, der begynder inden for 3 timer – højst én pr. 6 timer,
    ingen gentagelser. Problemer længere ude dækkes af døgnets besked om lange røde dage."""
    c, s = await _lv_entry(ha)
    events: list = []
    ha.bus.async_listen("varmemodel_varsel", lambda e: events.append(e.data))
    now = time.time()
    h = int(now // 3600 * 3600)
    prob = {"rum": "Gang", "ts": h + 9 * 3600, "temp": 19.7, "grænse": 20.0, "under": 0.3, "fra_ts": h + 5 * 3600, "blokeret": True}
    plan = {"problems": [dict(prob)], "warnings": ["behovet kan ikke dækkes i de frie timer"], "flow_raise": 5.0}
    c.red_spans = [(h + 3600, h + 10 * 3600)]

    async def run() -> None:
        c._varsel_all({s.sid: plan}, now)  # noqa: SLF001
        await ha.async_block_till_done()
    await run()
    assert events == []  # begynder om 4–5 timer: ingen nødbesked
    plan["problems"] = [dict(prob, fra_ts=h + 2 * 3600)]
    await run()
    assert len(events) == 1 and events[0]["nye"] == ["Gang"] and events[0]["titel"] == "Varme: problem inden for 3 timer"
    t = events[0]["tekst"]
    assert t.startswith("Luft-vand:\n- Gang 19,7 °C") and "Blokeret" in t and "kan ikke dække behovet" in t and "hæver allerede fremløbet 5 °C" in t
    assert events[0]["kort"].startswith("Gang under grænsen fra")
    await run()
    assert len(events) == 1  # samme problem: ingen ny besked
    plan["problems"].append(dict(prob, rum="Stue", fra_ts=h + 2 * 3600))
    await run()
    assert len(events) == 1  # nyt rum, men under 6 timer siden sidste besked
    c.air["varsel"]["t"] = now - 7 * 3600
    await run()
    assert len(events) == 2 and events[1]["nye"] == ["Stue"]


async def test_varsel_only_unusual(ha: HomeAssistant) -> None:
    """Kun det usædvanlige: et rum, der plejer at dykke (fx 1,2 °C under på en almindelig dårlig dag), giver ingen besked for
    et sædvanligt dyk – kun når planen forudser mindst 0,3 °C koldere end det. Et rum, der plejer at holde sig over, varsles."""
    c, s = await _lv_entry(ha)
    events: list = []
    ha.bus.async_listen("varmemodel_varsel", lambda e: events.append(e.data))
    now = time.time()
    h = int(now // 3600 * 3600)
    c.air[f"varsel:{s.sid}"] = {"base": {"Værelse": -1.2, "Gang": 0.4}}

    def p(rum: str, temp: float) -> dict:
        g = 19.5 if rum == "Værelse" else 20.0
        return {"rum": rum, "ts": h + 7200, "temp": temp, "grænse": g, "under": round(g - temp, 2), "fra_ts": h + 3600, "blokeret": True}
    c._varsel_all({s.sid: {"problems": [p("Værelse", 18.5)]}}, now)  # noqa: SLF001 – 1,0 under: sædvanligt
    await ha.async_block_till_done()
    assert events == []
    c._varsel_all({s.sid: {"problems": [p("Værelse", 17.9)]}}, now)  # noqa: SLF001 – 1,6 under: usædvanligt
    await ha.async_block_till_done()
    assert len(events) == 1 and "normalt ned til 18,3" in events[0]["tekst"]
    c.air["varsel"]["t"] = now - 7 * 3600
    c._varsel_all({s.sid: {"problems": [p("Værelse", 17.9), p("Gang", 19.7)]}}, now)  # noqa: SLF001 – Gang plejer at holde sig over
    await ha.async_block_till_done()
    assert len(events) == 2 and events[1]["nye"] == ["Gang"]


async def test_varsel_one_message_for_the_house(ha: HomeAssistant, entry: MockConfigEntry) -> None:
    """Én samlet besked for alle varmekilder (ikke én pr. kilde)."""
    c = entry.runtime_data
    events: list = []
    ha.bus.async_listen("varmemodel_varsel", lambda e: events.append(e.data))
    now = time.time()
    h = int(now // 3600 * 3600)
    srcs = list(c.sources.values())
    assert len(srcs) == 2
    plans = {x.sid: {"problems": [{"rum": f"Rum {i}", "ts": h + 7200, "temp": 19.0, "grænse": 20.0, "under": 1.0, "fra_ts": h + 3600,
                                   "blokeret": True}]} for i, x in enumerate(srcs)}
    c._varsel_all(plans, now)  # noqa: SLF001
    await ha.async_block_till_done()
    assert len(events) == 1 and all(f"{x.name}:" in events[0]["tekst"] for x in srcs) and len(events[0]["problemer"]) == 2


async def test_red_day_message(ha: HomeAssistant) -> None:
    """Lang rød dag (brugerens regel): over 8 røde timer i et døgn (talt pr. kvarter) → én besked for døgnet med de røde perioder
    og planens laveste temperaturer; ikke ved første kørsel (kendte dage), ikke to gange, ikke under 8 timer; døgnets rum får
    ikke en nødbesked bagefter."""
    from datetime import datetime, timedelta

    from custom_components.varmemodel.coordinator import spans_from_quarters

    c, s = await _lv_entry(ha)
    events: list = []
    ha.bus.async_listen("varmemodel_varsel", lambda e: events.append(e.data))
    loc = dt_util.get_default_time_zone()
    d1 = (dt_util.now() + timedelta(days=1)).date()
    q = lambda hh, mm: int(datetime(d1.year, d1.month, d1.day, hh, mm, tzinfo=loc).timestamp())  # noqa: E731
    c._red_q = set(range(q(6, 30), q(10, 15), 900)) | set(range(q(14, 0), q(21, 15), 900))  # noqa: SLF001 – 11 røde timer
    c.red_spans = spans_from_quarters(c._red_q)  # noqa: SLF001
    plan = {"problems": [{"rum": "Gang", "ts": q(21, 0), "temp": 18.9, "grænse": 20.0, "under": 1.1, "fra_ts": q(17, 0), "blokeret": True}]}
    now = time.time()

    async def run() -> None:
        c._red_day_msg({s.sid: plan}, now)  # noqa: SLF001
        c._varsel_all({s.sid: plan}, now)  # noqa: SLF001
        await ha.async_block_till_done()
    red = c._red_q  # noqa: SLF001
    c._red_q = set()  # noqa: SLF001 – lige efter en genstart: ingen røde data endnu → første kørsel venter
    await run()
    assert events == [] and "dage" not in c.air.get("varsel", {})
    c._red_q = red  # noqa: SLF001
    await run()
    assert events == [] and d1.isoformat() in c.air["varsel"]["dage"]  # første kørsel: kendt dag, ingen besked
    c.air["varsel"] = {"dage": []}
    await run()
    assert len(events) == 1 and events[0]["titel"] == "Varme: lang rød dag i morgen" and events[0]["røde_timer"] == 11
    assert events[0]["tekst"].startswith("I morgen: 11 røde timer (06:30–10:15, 14:00–21:15).\n\nPlanen forventer:\n- Gang 18,9 °C")
    assert events[0]["kort"] == "11 røde timer (06:30–10:15, 14:00–21:15). 1 rum under grænsen – værst Gang 18,9 °C."
    await run()
    assert len(events) == 1  # én gang pr. døgn
    c.air["varsel"] = {"dage": []}
    c.air[f"varsel:{s.sid}"] = {}
    c._red_q = set(range(q(14, 0), q(21, 45), 900))  # noqa: SLF001 – 7,75 timer
    await run()
    assert len(events) == 1  # under 8 timer: ingen dagsbesked (og Gang begynder ikke inden for 3 timer)


def test_usual_margin() -> None:
    """Det sædvanlige: 20 %-fraktilen af døgnenes laveste margen over gulvet; for få døgn eller intet gulv: ukendt."""
    from custom_components.varmemodel.coordinator import usual_margin

    assert usual_margin([19.0, 18.8, 19.2, 19.4, 18.5, 19.1], 19.5) == -0.7
    assert usual_margin([19.0, 18.8, 19.2, 19.4], 19.5) is None and usual_margin([19.0] * 6, None) is None


def _price_items(h0: int, n: int = 24) -> list[dict]:
    from datetime import datetime as _dt

    loc = dt_util.get_default_time_zone()
    return [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(n)]


async def test_replan_waits_for_inputs_after_start(ha: HomeAssistant) -> None:
    """Lige efter en genstart er pris- og blokeringsentiteterne måske ikke gendannet: ingen nødplan, før de er der."""
    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_onoff", "heat", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2,
                        data={**HOUSE, "price_entity": "sensor.pris", "block_now_entity": "sensor.zone_nu", "block_now_states": "rød"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp_onoff"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(call.data["hvac_mode"]))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    sid = next(iter(c.sources))
    c.settings[f"{sid}.hydro_control"] = True
    await c.async_replan()
    assert not c.plans and calls == []
    assert c.plan_meta["inputs_ok"] is False and c.plan_meta["missing"][0].startswith("venter efter start på sensor.pris")
    assert c._input_wait is not None  # prøver igen om lidt
    # Blokeringen dukker op først (som ved en genstart): stadig ingen plan uden priser
    ha.states.async_set("sensor.zone_nu", "rød", {})
    await ha.async_block_till_done()
    assert not c.plans and calls == []
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    await c.async_replan()
    assert c.plans and c.plan_meta["inputs_ok"] and not c.plan_meta["safety"]
    assert calls == ["off"]  # blokeret nu: varmepumpen slukkes af den rigtige plan
    # Efter ventetiden planlægges der som før uden priser (nødplan)
    ha.states.async_remove("sensor.pris")
    c._started = time.time() - 300
    await c.async_replan()
    assert c.plan_meta["safety"] and c.plan_meta["missing"] == ["priser"]
    c._input_wait and c._input_wait()


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_replan_failure_follows_last_plan(ha: HomeAssistant) -> None:
    """Fejler planlægningen, styres der videre efter den seneste plan: en blokering slukker stadig, og der kommer én besked."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.zone_nu", "grøn", {})
    ha.states.async_set("climate.vp_onoff", "heat", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2,
                        data={**HOUSE, "price_entity": "sensor.pris", "block_now_entity": "sensor.zone_nu", "block_now_states": "rød"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp_onoff"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(call.data["hvac_mode"]))
    events = async_capture_events(ha, "varmemodel_varsel")
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    sid = next(iter(c.sources))
    c.settings[f"{sid}.hydro_control"] = True
    await c._on_replan(dt_util.now())
    assert c.plans[sid]["hours"] and c.plan_meta["inputs_ok"]
    updated = c.plan_meta["updated"]
    calls.clear()

    async def boom() -> None:
        raise RuntimeError("regnefejl")

    c.async_replan = boom
    ha.states.async_set("sensor.zone_nu", "rød", {})  # blokeringen udløser en planlægning, som fejler
    await ha.async_block_till_done()
    assert calls == ["off"]
    assert c.plan_meta["updated"] == updated and c.plan_meta["missing"] == ["fejl: regnefejl"] and not c.plan_meta["inputs_ok"]
    assert not events  # en enkelt fejl giver ingen besked
    c._plan_fail_since = time.time() - 1000
    await c._on_replan(dt_util.now())
    await c._on_replan(dt_util.now())
    await ha.async_block_till_done()
    assert len(events) == 1 and events[0].data["type"] == "drift" and "seneste" in events[0].data["kort"]
    # Planen virker igen: hændelsen er slut, og en ny fejl kan give en ny besked
    del c.async_replan
    await c._on_replan(dt_util.now())
    assert c._plan_fail_since is None and not c.air["varsel"]["drift"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_onoff_fallback_and_recheck(ha: HomeAssistant) -> None:
    """To tænd/sluk-entiteter: der skrives til den første, der svarer, og en entitet, der stadig viser det modsatte ved
    tjekket, får kommandoen direkte. Svarer ingen under en blokering, prøves der igen."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_sky", "heat", {})
    ha.states.async_set("climate.vp_lokal", "heat", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp_sky", "onoff_fallback": "climate.vp_lokal"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append((call.data["entity_id"], call.data["hvac_mode"])))
    events = async_capture_events(ha, "varmemodel_varsel")
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    sid = next(iter(c.sources))
    s = c.sources[sid]
    c.settings[f"{sid}.hydro_control"] = True
    # 1) Blokeret: sluk ad alle veje med det samme (den primære først) – ét skift – og et tjek er sat op
    await c._hydro_apply(s, True, True, [])
    assert calls == [("climate.vp_sky", "off"), ("climate.vp_lokal", "off")] and sid in c._hydro_pending
    flips = c.air[f"hydro:{sid}"]["flips"]
    assert flips == 1
    # 2) Skyen viser slukket, men varmepumpen kører stadig (lokalt): tjekket skriver direkte til den lokale – intet nyt skift
    ha.states.async_set("climate.vp_sky", "off", {})
    calls.clear()
    c._hydro_pending.pop(sid)()
    await c._hydro_apply(s, False, True, [], recheck=True)
    assert calls == [("climate.vp_lokal", "off")] and c.air[f"hydro:{sid}"]["flips"] == flips
    assert c.air[f"hydro:{sid}"]["fallback"]["entiteter"] == ["climate.vp_lokal"]
    # 3) Tredje forgæves tjek giver én besked
    await c._hydro_apply(s, False, True, [], recheck=True)
    assert not events
    await c._hydro_apply(s, False, True, [], recheck=True)
    await c._hydro_apply(s, False, True, [], recheck=True)
    await ha.async_block_till_done()
    assert len(events) == 1 and events[0].data["type"] == "drift" and "tænd/sluk" in events[0].data["titel"]
    # 4) Nu står begge som ønsket: hændelsen er slut
    ha.states.async_set("climate.vp_lokal", "off", {})
    await c._hydro_apply(s, False, True, [], recheck=True)
    assert c.air[f"hydro:{sid}"]["recheck_n"] == 0 and not c.air["varsel"]["drift"]
    # 5) Den primære svarer ikke: tænd via reserven
    ha.states.async_set("climate.vp_sky", "unavailable", {})
    calls.clear()
    await c._hydro_apply(s, True, False, [])
    assert calls == [("climate.vp_lokal", "heat")]
    # 6) Ingen svarer under en blokering: prøv igen om lidt
    ha.states.async_set("climate.vp_lokal", "unknown", {})
    c._hydro_pending.pop(sid, lambda: None)()
    calls.clear()
    await c._hydro_apply(s, True, True, [])
    assert calls == [] and sid in c._hydro_pending
    c._hydro_pending.pop(sid)()


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_hydro_returns_to_heat_mode(ha: HomeAssistant) -> None:
    """Står varmepumpen tændt i en anden tilstand end varme (fx »auto« efter en reserve-styring), sættes den tilbage til
    varme – fremløbsmålet gælder kun dér. Det tæller ikke som et skift."""
    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp", "auto", {"hvac_modes": ["off", "auto", "heat", "cool"]})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append(call.data["hvac_mode"]))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    sid = next(iter(c.sources))
    s = c.sources[sid]
    await c._hydro_apply(s, True, False, [])
    assert calls == ["heat"] and not c.air[f"hydro:{sid}"].get("flips")
    c._hydro_pending.pop(sid)()
    # En entitet uden »heat« blandt sine tilstande røres ikke
    ha.states.async_set("climate.vp", "auto", {"hvac_modes": ["off", "auto"]})
    calls.clear()
    await c._hydro_apply(s, True, False, [])
    assert calls == []


def test_source_state_restores_todays_figures() -> None:
    """Dagens COP-tal gemmes med kilden og vises straks efter en genstart – men kun samme dag."""
    from custom_components.varmemodel.coordinator import SourceState

    s = SourceState(sid="a", name="Kilde", kind="hydronic", conf={})
    s.today = {"cop": 2.5, "kwh_varme": 30.1, "kwh_el": 12.2}
    d = s.as_store()
    s2 = SourceState(sid="a", name="Kilde", kind="hydronic", conf={})
    s2.load(d)
    assert s2.today == s.today
    d["today"]["t"] -= 2 * 86400  # gemt i forgårs: bruges ikke
    s3 = SourceState(sid="a", name="Kilde", kind="hydronic", conf={})
    s3.load(d)
    assert s3.today == {}


async def test_diagnostics_has_replayable_plan_input(ha: HomeAssistant) -> None:
    """Diagnostikken har den seneste plans input uden entitets-id'er, og den kan genkøres til samme plan."""
    import json

    from custom_components.varmemodel.diagnostics import async_get_config_entry_diagnostics
    from custom_components.varmemodel.planner import plan_from_dump

    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    await c.async_replan()
    sid = next(iter(c.sources))
    d = json.loads(json.dumps(await async_get_config_entry_diagnostics(ha, e)))
    pi = d["sources"]["kilde_1"]["plan_input"]
    assert "sensor." not in json.dumps(pi) and pi["hours"][0]["h"] == 0 and len(pi["hours"]) == 24
    p = plan_from_dump(pi)
    assert [x["heat_kw"] for x in p.hours] == [x["heat_kw"] for x in c.plans[sid]["hours"]]


def test_day_health_counts_starts_short_runs_and_defrosts() -> None:
    """Dagens driftstal: starter, korte kørsler (stop inden for 20 min. efter start), afrimninger og driftstid."""
    from custom_components.varmemodel.coordinator import day_health

    t0 = 1_000_000.0  # midnat
    rows = {
        "c": [(t0 - 1800, "on"),            # kørte allerede ved midnat (ingen start i dag)
              (t0 + 3600, "off"),            # stop efter lang drift (starten ligger før data: længden er ukendt)
              (t0 + 7200, "on"), (t0 + 7200 + 300, "off"),   # start + kort kørsel (5 min.)
              (t0 + 9000, "unavailable"), (t0 + 9060, "off"),  # Modbus-udfald: intet skift
              (t0 + 10800, "on")],           # start, kører stadig
        "d": [(t0 - 1800, "off"), (t0 + 12000, "on"), (t0 + 12300, "off")],
        "v": [(t0 - 1800, "off")],
    }
    h = day_health(rows, "c", "v", "d", t0, t0 + 14400)
    assert h["starter"] == 2 and h["stop"] == 2 and h["korte_kørsler"] == 1 and h["afrimninger"] == 1 and h["varmt_vand"] == 0
    assert h["drift_min"] == round((3600 + 300 + 3600) / 60)
    assert day_health(rows, "c", None, None, t0, t0 + 14400)["afrimninger"] is None


async def test_low_flow_learns_only_when_its_own_target_is_written(ha: HomeAssistant) -> None:
    """Laveste fungerende fremløb læres kun, når søgningens eget mål er det, der står i varmepumpen – ikke mens planens
    forvarmning eller et værn (opstart/stopværn) holder det skrevne mål højere."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    rooms = [{"navn": "Gang", "margen": 0.3, "tendens": 0.0, "om_3t": 0.3}]
    c._low_rooms_sync = lambda _s: [dict(x) for x in rooms]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 24.0
    hs["low_hold_since"] -= 4 * 3600
    hs["low_raise"] = 11.0  # planen forvarmer: det skrevne mål er 35, ikke 24
    await c._low_flow(s, hs, 35.0)
    assert not hs.get("low_learned") and time.time() - hs["low_hold_since"] < 5
    hs["low_raise"] = 0.0
    hs["guard"] = {"mål": 27.0, "grund": "stopværn"}
    hs["low_hold_since"] -= 4 * 3600
    await c._low_flow(s, hs, 35.0)
    assert not hs.get("low_learned")
    hs["guard"] = None
    hs["low_hold_since"] -= 4 * 3600
    await c._low_flow(s, hs, 35.0)
    assert list((hs.get("low_learned") or {}).values()) == [24.0]


def test_partial_hour_patterns() -> None:
    """Delvist fri time: de blokerede kvarterer ligger samlet i den ene ende, og mindst to kvarterer er frie."""
    from custom_components.varmemodel.coordinator import partial_hour

    k = 7200
    q = lambda *js: {k + j * 900 for j in js}  # noqa: E731
    assert partial_hour(k, q(2, 3)) == (0.5, False)      # rødt fra xx:30: varme til xx:30
    assert partial_hour(k, q(0, 1)) == (0.5, True)       # rødt til xx:30: varme fra xx:30
    assert partial_hour(k, q(1, 2)) is None and partial_hour(k, q(0, 3)) is None and partial_hour(k, q(0, 2)) is None  # delt
    assert partial_hour(k, q(1, 2, 3)) is None and partial_hour(k, q(0, 1, 2)) is None  # kun ét frit kvarter
    assert partial_hour(k, q(0, 1, 2, 3)) is None and partial_hour(k, set()) is None


def test_quarter_prices_only_from_quarter_lists() -> None:
    """Priser pr. kvarter findes kun, når prislisten har flere rækker i samme time."""
    from datetime import datetime

    from custom_components.varmemodel.inputs import hourly_prices, quarter_prices

    parse = lambda s: datetime.fromisoformat(s)  # noqa: E731
    t = lambda h, m: int(datetime.fromisoformat(f"2026-11-25T{h:02d}:{m:02d}:00+01:00").timestamp())  # noqa: E731
    quarters = {"raw_today": [{"start": f"2026-11-25T06:{m:02d}:00+01:00", "value": v} for m, v in ((0, 2.0), (15, 2.2), (30, 3.4), (45, 3.6))]}
    assert quarter_prices(quarters, None, parse) == {t(6, 0): 2.0, t(6, 15): 2.2, t(6, 30): 3.4, t(6, 45): 3.6}
    assert hourly_prices(quarters, None, parse) == {t(6, 0): 2.8}
    hourly = {"raw_today": [{"start": f"2026-11-25T{h:02d}:00:00+01:00", "value": 2.0} for h in range(4)]}
    assert quarter_prices(hourly, None, parse) == {} and len(hourly_prices(hourly, None, parse)) == 4


def test_day_health_restart_after_defrost_is_not_a_start() -> None:
    """Dagens starter: genstarten lige efter et stop, der ikke tæller (4-vejsventilens skift ved en afrimning, skiftet efter
    varmt vand), er samme kørsel. En start længe efter et sådant stop er en rigtig start."""
    from custom_components.varmemodel.coordinator import day_health

    t0 = 1_000_000.0
    rows = {
        "c": [(t0 - 1800, "off"),
              (t0 + 600, "on"),                               # 1. start
              (t0 + 4000, "off"), (t0 + 4060, "on"),          # ventilskift ved afrimningens slut: samme kørsel
              (t0 + 9000, "off"),                             # stop, mens der laves varmt vand (tæller ikke som stop)
              (t0 + 9000 + 3600, "on"),                       # 2. start – en time senere
              (t0 + 15000, "off"), (t0 + 15900, "on")],       # rigtigt stop og 3. start
        "d": [(t0 - 1800, "off"), (t0 + 3800, "on"), (t0 + 3990, "off")],
        "v": [(t0 - 1800, "off"), (t0 + 8000, "on"), (t0 + 9100, "off")],
    }
    h = day_health(rows, "c", "v", "d", t0, t0 + 20000)
    assert h["starter"] == 3 and h["stop"] == 1 and h["afrimninger"] == 1 and h["varmt_vand"] == 1
    assert h["drift_min"] == round((3400 + 4940 + 2400 + 4100) / 60)


def test_comp_stops_shift_to_dhw_is_no_stop() -> None:
    """Skiftet til varmt vand: kompressoren stopper, varmt vand melder tændt nogle sekunder efter, og kompressoren starter
    igen efter 3–4 min. Det er hverken et stop, en kort kørsel (25/11 02:02: otte minutter efter en afrimning) eller en ny
    start. Et stop længe før varmt vand er et rigtigt stop."""
    from custom_components.varmemodel.coordinator import comp_stops, day_health

    t0 = 1_000_000.0
    rows = {
        "c": [(t0 - 1800, "on"), (t0 + 7324, "off"), (t0 + 7557, "on")],   # 02:02:04 → 02:05:57
        "d": [(t0 - 1800, "off"), (t0 + 6703, "on"), (t0 + 6860, "off")],   # afrimning 01:51:43–01:54:20
        "v": [(t0 - 1800, "off"), (t0 + 7328, "on")],                       # varmt vand melder tændt 02:02:08
    }
    starts: list[float] = []
    assert comp_stops(rows, "c", "v", "d", t0, starts=starts) == [] and starts == []
    h = day_health(rows, "c", "v", "d", t0, t0 + 9000)
    assert h["stop"] == 0 and h["korte_kørsler"] == 0 and h["starter"] == 0 and h["varmt_vand"] == 1
    # Samme stop, men varmt vand først fem minutter efter: et rigtigt stop (kort kørsel efter afrimningen)
    rows["v"][1] = (t0 + 7324 + 300, "on")
    assert [(x["t"], round(x["run"]), x["df"]) for x in comp_stops(rows, "c", "v", "d", t0)] == [(t0 + 7324, 464, "efter")]
    # Stop og varmt vand i samme sekund (rækkefølgen i historikken er tilfældig): intet stop
    rows["v"][1] = (t0 + 7324, "on")
    assert comp_stops(rows, "c", "v", "d", t0) == []


def test_start_k_from_log() -> None:
    """Startløftet før en kort kørsel læst af søgningens log (versioner før 0.25.1 gemte det ikke): løftet på den seneste
    startlinje før stoppet – sænket én gang, når starten siden kom igennem opstarten."""
    from custom_components.varmemodel.coordinator import start_k_from_log

    log = [[100, 26.0, 35.0, "start: retur + 3 °C"],
           [200, 30.0, 34.8, "kort kørsel (6 min.) – varmepumpens opstart skød fremløbet over målet; startløft nu retur + 5.1 °C"],
           [300, 23.0, 34.7, "start: retur + 3 °C (opstartsløft retur + 5.1 °C, lært af varmepumpens opstart)"],
           [400, 25.0, 34.8, "Gang er 0.6 °C under gulvet og ikke på vej op"]]
    assert start_k_from_log(log, 500, True, 3.0) == 4.85 and start_k_from_log(log, 500, False, 3.0) == 5.1
    assert start_k_from_log(log, 250, True, 3.0) == 3.0  # starten før havde intet lært løft
    assert start_k_from_log(log, 50, True, 3.0) is None and start_k_from_log([], 500, True, 3.0) is None
    assert start_k_from_log([[300, 23.0, 34.7, "start: retur + 3 °C (opstartsløft retur + 4,2 °C, lært …)"]], 500, False, 3.0) == 4.2
    assert start_k_from_log([[300, 23.0, 34.7, "start: retur + 3 °C (opstartsløft retur + 6 °C, lært …)"]], 500, True, 3.0) == 5.75


async def test_false_short_run_is_undone(ha: HomeAssistant) -> None:
    """En kort kørsel, der viser sig ikke at være en (varmt vand meldte tændt lige efter stoppet – stoppet er væk fra de
    rigtige stop ved næste gennemgang): startløftet går tilbage til det, der gjaldt før, og kørslen tælles ikke."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    c._low_rooms_sync = lambda _s: [{"navn": "Gang", "margen": 0.2, "tendens": 0.0, "om_3t": 0.2}]  # noqa: SLF001
    stops: list = []
    c._comp_stops_sync = lambda _s, _since: [dict(x) for x in stops]  # noqa: SLF001
    assert await c._low_flow(s, hs, 35.0) == 24.0
    now = time.time()
    hs.update(start_t=now - 7200, start_k=4.85, start_ok=True)
    c.air[f"flow:{s.sid}"] = {"hist": [[now - 3600, 35.0]]}
    stops[:] = [{"t": now - 60, "run": 464.0, "ret0": 29.3, "df": "efter"}]
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 6.7 and hs["start_short"] == 1 and hs["short_last"]["løft_før"] == 4.85  # 35 − 29,3 + 1
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 6.7  # stadig en kort kørsel: intet ændret
    stops[:] = []  # varmt vand meldte tændt: stoppet er ikke længere et rigtigt stop
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 4.85 and hs["start_short"] == 0 and hs["short_last"] is None
    assert c.flow_search(s)["startløft"] == 4.8 or c.flow_search(s)["startløft"] == 4.9
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 4.85  # og den læres ikke igen
    # Kunne stoppene ikke læses, røres løftet ikke
    stops[:] = [{"t": time.time() - 30, "run": 300.0, "ret0": 30.0, "df": None}]
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 6.0 and hs["short_last"]["løft_før"] == 4.85

    def boom(_s, _since):
        raise RuntimeError("ingen historik")

    c._comp_stops_sync = boom  # noqa: SLF001
    await c._low_flow(s, hs, 35.0)
    assert hs["start_k"] == 6.0 and hs["start_short"] == 1


async def test_last_short_run_is_rechecked_after_start(ha: HomeAssistant) -> None:
    """Efter opstart tjekkes den senest lærte korte kørsel én gang mod den gældende klassifikation: var den skiftet til
    varmt vand (lært af en ældre version, der ikke gemte løftet før), går startløftet tilbage til løftet ved den seneste
    start ifølge søgningens log. En rigtig kort kørsel røres ikke."""
    c, s = await _lv_entry(ha)
    sid = s.sid
    hs = c.air.setdefault(f"hydro:{sid}", {})
    t = time.time() - 4 * 3600
    legacy = {"start_k": 8.2, "start_short": 2, "start_ok": True, "start_t": t - 5 * 3600, "short_seen": t,  # to falske i samme kørsel
              "short_last": {"t": round(t), "min": 7.7, "mål": 35.0, "retur": 29.3, "efter_afrimning": True},
              "low_log": [[t - 5 * 3600, 23.0, 34.7, "start: retur + 3 °C (opstartsløft retur + 5.1 °C, lært af varmepumpens opstart)"],
                          [t - 3600, 34.8, 34.8, "Stue er 0.4 °C under gulvet og ikke på vej op"]]}
    asked: list = []

    def valid(_s, seen):
        asked.append(seen)
        return True

    hs.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in legacy.items()})
    c._short_checked.clear()  # noqa: SLF001
    c._short_valid_sync = valid  # noqa: SLF001
    await c.async_replan()
    assert asked == [t] and hs["start_k"] == 8.2 and hs["short_last"]["min"] == 7.7 and sid in c._short_checked  # noqa: SLF001
    await c.async_replan()
    assert asked == [t]  # kun én gang efter opstart
    c._short_checked.clear()  # noqa: SLF001
    c._short_valid_sync = lambda _s, _seen: False  # noqa: SLF001
    await c.async_replan()
    assert hs["start_k"] == 4.85 and hs["start_short"] == 0 and hs["short_last"] is None
    # Uden startlinje i loggen beholdes løftet (det falder af sig selv), men den falske korte kørsel vises ikke længere
    hs.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in legacy.items()})
    hs["low_log"] = []
    c._short_checked.clear()  # noqa: SLF001
    await c.async_replan()
    assert hs["start_k"] == 8.2 and hs["short_last"] is None and hs["start_short"] == 0
    # Kunne historikken ikke læses, prøves der igen ved næste planlægning
    hs.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in legacy.items()})
    c._short_checked.clear()  # noqa: SLF001

    def boom(_s, _seen):
        raise RuntimeError("optageren er ikke klar")

    c._short_valid_sync = boom  # noqa: SLF001
    await c.async_replan()
    assert hs["start_k"] == 8.2 and hs["short_last"] is not None and hs["start_short"] == 2 and sid not in c._short_checked  # noqa: SLF001


async def test_planned_stop_writes_only_the_primary(ha: HomeAssistant) -> None:
    """Et planlagt stop (ingen blokering) skrives kun til den primære tænd/sluk-entitet – reserven får det først, hvis den
    stadig viser det modsatte ved tjekket. Ved en blokering slukkes der ad alle veje med det samme."""
    h0 = int(time.time() // 3600 * 3600)
    ha.states.async_set("sensor.pris", "1.0", {"timer": _price_items(h0)})
    ha.states.async_set("sensor.ude", "-2.0", {"state_class": "measurement"})
    ha.states.async_set("climate.vp_lokal", "heat", {})
    ha.states.async_set("climate.vp_sky", "heat", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub({**HYD, "onoff_target": "climate.vp_lokal", "onoff_fallback": "climate.vp_sky"}, "source")])
    e.add_to_hass(ha)
    calls: list = []
    ha.services.async_register("climate", "set_hvac_mode", lambda call: calls.append((call.data["entity_id"], call.data["hvac_mode"])))
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    sid = next(iter(c.sources))
    s = c.sources[sid]
    c.settings[f"{sid}.hydro_control"] = True
    await c._hydro_apply(s, False, False, [])  # noqa: SLF001
    assert calls == [("climate.vp_lokal", "off")]
    c._hydro_pending.pop(sid)()  # noqa: SLF001
    # Blokering, og reserven viser slukket i forvejen: kun den, der viser tændt, får kommandoen
    calls.clear()
    ha.states.async_set("climate.vp_sky", "off", {})
    await c._hydro_apply(s, True, True, [])  # noqa: SLF001
    assert calls == [("climate.vp_lokal", "off")]
    c._hydro_pending.pop(sid)()  # noqa: SLF001


async def test_partial_hour_before_block_is_planned_and_stopped_at_red(ha: HomeAssistant) -> None:
    """Prisloftets kvarterer begynder midt i en time: for den vandbårne kilde er timens første halvdel fri (som fortsættelse
    af timen før) med prisen for de frie kvarterer; styringen er blokeret, når klokken når de røde kvarterer – også uden en
    »Blokering lige nu«-entitet – og »Plan nu« viser næste stop ved det røde kvarter."""
    from datetime import datetime as _dt

    loc = dt_util.get_default_time_zone()
    h0 = int(time.time() // 3600 * 3600)
    iso = lambda ts: _dt.fromtimestamp(ts, loc).isoformat()  # noqa: E731
    red0 = h0 + 2 * 3600 + 1800  # rødt fra xx:30 i planens tredje time, 10 timer frem
    ha.states.async_set("sensor.kvart", "40", {"kvarterer": [iso(red0 + j * 900) for j in range(38)]})
    c, s = await _lv_entry(ha, {"block_quarters_entity": "sensor.kvart"})
    # priser pr. kvarter: de røde kvarterer er dyre, de frie i samme time billigere
    ha.states.async_set("sensor.pris", "1.0", {"raw_today": [
        {"start": iso(h0 + j * 900), "value": 3.5 if red0 <= h0 + j * 900 < red0 + 38 * 900 else (2.1 if h0 + j * 900 >= h0 + 7200 else 1.0)}
        for j in range(96)]})
    await c.async_replan()
    hrs = c.plans[s.sid]["hours"]
    h2 = next(x for x in hrs if x["ts"] == h0 + 7200)
    assert [x["blocked"] for x in hrs[:5]] == [False, False, False, True, True]
    assert h2["free"] == 0.5 and h2["price"] == 2.1 and "free" not in hrs[1]
    assert hrs[1]["heat_kw"] > 0 and 0 < h2["heat_kw"] <= hrs[1]["heat_kw"] / 2 + 0.13
    assert c.red_spans == [(red0, red0 + 38 * 900)]
    # styringen: fri i timens første halvdel, blokeret i de røde kvarterer – uden »Blokering lige nu«
    assert c.live_blocked() is False
    assert c._blocked_now(h2, h0 + 7200 + 600) is False and c._blocked_now(h2, red0 + 60) is True  # noqa: SLF001
    assert c._blocked_now(hrs[1], red0 + 60) is False  # noqa: SLF001 – en hel time afgøres ikke af de røde perioder her
    st = next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("plan_now"))
    assert st.state == "heat" and st.attributes["next_off"] == iso(red0)
    assert st.attributes["plan"][2]["free"] == 0.5 and st.attributes["preload_window"]["til"] == iso(red0)
    # luft-luft og resten af huset regner stadig timen som blokeret (fælles timer)
    d = c.plan_inputs[s.sid][0]
    assert d[2].free == 0.5 and d[2].blocked is False and d[3].blocked is True


async def test_own_flow_periods_are_tracked(ha: HomeAssistant) -> None:
    """Perioderne med Varmemodels eget fremløb gemmes med varmekilden: ved en opdatering midt i en periode regnes den fra
    søgningens første logpunkt; kontakten lukker og åbner perioder; gamle perioder ryddes."""
    c, s = await _lv_entry(ha)
    f = c.air.setdefault(f"flow:{s.sid}", {})
    now = time.time()
    assert c._flow_own(s, now) == []  # noqa: SLF001 – Varmemodel styrer ikke fremløbet
    # Opdatering fra en ældre version, mens styringen er i gang: fra søgningens første logpunkt
    f.pop("own")
    c.settings[f"{s.sid}.flow_control"] = True
    c.air.setdefault(f"hydro:{s.sid}", {})["low_log"] = [[now - 30 * 3600, 28.0, 35.0, "start"], [now - 3600, 30.0, 35.0, "hævet"]]
    f["hist"] = [[now - 7200, 29.0]]
    assert c._flow_own(s, now) == [(now - 30 * 3600, None)]  # noqa: SLF001
    assert c._flow_own(s, now + 60) == [(now - 30 * 3600, None)]  # noqa: SLF001 – stadig samme periode
    c.settings[f"{s.sid}.flow_control"] = False
    assert c._flow_own(s, now + 900) == [(now - 30 * 3600, now + 900)]  # noqa: SLF001
    c.settings[f"{s.sid}.flow_control"] = True
    assert c._flow_own(s, now + 1800) == [(now - 30 * 3600, now + 900), (now + 1800, None)]  # noqa: SLF001
    # Perioder, der sluttede, før de gemte timer begynder, ryddes
    f["own"] = [[now - 200 * 86400, now - 150 * 86400], [now - 86400, None]]
    assert c._flow_own(s, now) == [(now - 86400, None)]  # noqa: SLF001
    # Fuld styring (tænd/sluk) sætter også fremløbet – uden et fremløbsmål styres der intet
    assert c._steers_flow(s) is True  # noqa: SLF001
    c.settings[f"{s.sid}.flow_control"] = False
    assert c._steers_flow(s) is False  # noqa: SLF001


async def test_learned_flow_ignores_hours_steered_by_varmemodel(ha: HomeAssistant) -> None:
    """Kalibreringen: husets eget fremløb (planens loft) læres kun af timer, hvor Varmemodel ikke selv satte fremløbet –
    25/11-2026 faldt loftet 1 °C på to døgn, fordi det lærte af søgningens egne lave fremløb, mens et rum lå under gulvet."""
    c, s = await _lv_entry(ha)
    h0 = int(time.time() // 3600 * 3600)
    cut = h0 - 60 * 3600
    hours = [Hour(ts=cut - (78 - i) * 3600, q=4.0, e=1.2, t_out=2 + i % 13, t_in=20.5, t_flow=35 - 0.5 * (2 + i % 13), comp=60, dhw=0)
             for i in range(78)]
    hours += [Hour(ts=cut + i * 3600, q=4.0, e=1.2, t_out=-2 + i % 5, t_in=20.5, t_flow=30.0, comp=60, dhw=0) for i in range(60)]
    real = c._hours_sync  # noqa: SLF001

    def fake() -> dict:
        out = real()
        out[s.sid] = hours
        return out

    c._hours_sync = fake  # noqa: SLF001
    await c.async_calibrate()
    echo = s.accepted["flow_learned"]["value"]
    assert echo < 34.0  # uden styring tæller alle timer med (som før)
    c.settings[f"{s.sid}.flow_control"] = True
    c.air.setdefault(f"flow:{s.sid}", {})["own"] = [[cut + 600, None]]
    s.accepted.pop("flow_learned")
    await c.async_calibrate()
    fl = s.accepted["flow_learned"]
    assert abs(fl["value"] - 35.0) < 0.05 and abs(fl["extra"]["slope"] + 0.5) < 0.01 and fl["extra"]["own_control_hours_left_out"] == 60
    # Når timerne uden egen styring er gledet ud af vinduet, beholdes den seneste værdi og hældning
    hours[:] = hours[78:]
    await c.async_calibrate()
    fl = s.accepted["flow_learned"]
    assert abs(fl["value"] - 35.0) < 0.05 and abs(fl["extra"]["slope"] + 0.5) < 0.01 and "egen styring" in fl["note"]


async def test_low_flow_at_the_ceiling_says_so(ha: HomeAssistant) -> None:
    """Søg laveste: beder et rum om mere, mens målet står ved loftet, loves ingen hævning (»Hæver til 36 °C kl. 04:15« stod
    der natten 25/11-2026, mens loftet var 34 °C) – status »loft« og teksten siger, at loftet holder; og når loftet er
    kommet under målet, skriver loggen, at målet følger loftet ned, ikke at det blev hævet."""
    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    rooms = [{"navn": "Stue", "t": 19.8, "gulv": 20.0, "margen": -0.2, "tendens": 0.0, "om_3t": -0.2}]
    c._low_rooms_sync = lambda _s: [dict(x) for x in rooms]  # noqa: SLF001
    await c._low_flow(s, hs, 35.0)  # noqa: SLF001 – start
    # Under loftet: hævningen loves som før
    hs.update(low=30.0, low_loft=34.4, low_t=time.time() - 60)
    assert await c._low_flow(s, hs, 34.4) == 30.0  # noqa: SLF001
    v = c.flow_search(s)
    assert v["status"] == "hæver" and v["tekst"].startswith("Hæver til 32 °C") and v["næste_ændring"]
    # Loftet er kommet under målet (ny kalibrering): målet følger med ned, og loggen siger det
    hs.update(low=34.8, low_t=time.time() - 3600)
    assert await c._low_flow(s, hs, 34.4) == 34.4  # noqa: SLF001
    assert hs["low"] == 34.4 and hs["low_why"].startswith("loftet er sænket til 34.4 °C – målet følger med (Stue er 0.2 °C under")
    assert hs["low_log"][-1][1:3] == [34.4, 34.4]
    v = c.flow_search(s)
    assert v["status"] == "loft" and v["næste_ændring"] is None
    assert v["tekst"].startswith("Holder loftet 34 °C – Stue er 0,2 °C under gulvet og ikke på vej op") and "Hæver" not in v["tekst"]
    # På vej under gulvet (ikke under endnu) ved loftet: samme besked med rummets tendens
    rooms[:] = [{"navn": "Stue", "t": 20.05, "gulv": 20.0, "margen": 0.05, "tendens": -0.03, "om_3t": -0.04}]
    assert await c._low_flow(s, hs, 34.4) == 34.4  # noqa: SLF001
    v = c.flow_search(s)
    assert v["status"] == "loft" and "Stue er på vej under gulvet" in v["tekst"]
    # Rummene er tilfredse: almindelig »holder«
    rooms[:] = [{"navn": "Stue", "t": 20.3, "gulv": 20.0, "margen": 0.3, "tendens": 0.0, "om_3t": 0.3}]
    await c._low_flow(s, hs, 34.4)  # noqa: SLF001
    assert c.flow_search(s)["status"] == "holder"


def test_flow_cap_note() -> None:
    """Planens begrundelse siger, når det er fremløbet – ikke varmepumpen – der gør, at behovet ikke kan dækkes."""
    from custom_components.varmemodel.coordinator import flow_cap_note

    w = ["behovet kan ikke dækkes i de frie timer"]
    hrs = [{"heat_kw": 3.91, "flow": 34.0}, {"heat_kw": 1.95, "flow": 34.0, "free": 0.5}, {"heat_kw": 0.0, "flow": None}]
    assert flow_cap_note(hrs, w, 5.71) == " – ved 34 °C fremløb tager gulvet højst 3.9 kW pr. time (varmepumpen kan 5.7 kW)"
    assert flow_cap_note(hrs, [], 5.71) == ""  # behovet dækkes: intet at forklare
    assert flow_cap_note(hrs, w, 4.0) == ""  # varmepumpen selv er grænsen
    assert flow_cap_note([{"heat_kw": 0.0, "flow": None}], w, 5.71) == ""  # ingen frie timer
    assert flow_cap_note([{"heat_kw": 3.0, "flow": None}], w, 5.71) == " – med planens fremløb tager gulvet højst 3.0 kW pr. time (varmepumpen kan 5.7 kW)"


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_priority_reaches_the_plan_and_preheats_above_the_ceiling(ha: HomeAssistant) -> None:
    """Prioritet (økonomi – balance – komfort) er husets valg i en dropdown: den følger med til planen sammen med, hvor
    højt fremløbet kan hæves (det fremløb, varmepumpens største effekt giver – højst varmepumpens eget maksimum), og hvor
    mange af graderne der er almindelig drift op til loftet. Vælger planen en hævning over loftet, skrives den til
    varmepumpen, søgningens visning følger med, og »Plan nu« viser de tre trins valg."""
    import json
    import math
    from pathlib import Path
    from unittest.mock import patch

    from custom_components.varmemodel import coordinator as cm

    c, s = await _lv_entry(ha)
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    c.settings[f"{s.sid}.flow_control"] = True
    writes: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: writes.append(call.data["temperature"]))
    sel = next(x for x in ha.states.async_all("select") if x.attributes.get("options") == ["economy", "balance", "comfort"])
    assert sel.state == "balance" and c.priority() == "balance"
    calls: list = []
    real = cm.make_plan

    def rec(*a, **k):
        calls.append(k)
        p = real(*a, **k)
        if k.get("priority") == "comfort":  # komfort: 2 °C over loftet
            p.flow_raise = float(k["flow_raise_free"] + 2)
            p.choices = [{"trin": x, "hævet": k["flow_raise_free"] + n, "kr": 40.0 + 5 * n, "kwh": 30.0, "el_kwh": 10.0, "rum": "Rum A",
                          "under": 0.4 - 0.1 * n, "temp": 19.6 + 0.1 * n, "ts": 1_795_000_000, "timer": 9 - n}
                         for n, x in enumerate(("economy", "balance", "comfort"))]
        return p

    with patch.object(cm, "make_plan", rec):
        await c.async_replan()
        hs = c.air[f"hydro:{s.sid}"]
        k = calls[-1]
        # uden kalibrering: lært fremløb 34 − 0,6·ude (−2 °C) = 35,2 = loftet; søgningen starter ved retur + 3 = 24 °C
        assert hs["low_loft"] == 35.2 and k["priority"] == "balance" and k["flow_raise_free"] == math.ceil(35.2 - 24)
        # højst det fremløb, varmepumpens største effekt giver gennem gulvet: 20,5 + 6 kW ÷ 0,25 kW/K = 44,5 °C
        assert hs["low_top"] == 44.5 and k["flow_raise_max"] == math.ceil(44.5 - 24)
        for key in ("since_flow", "last_change"):  # kommandoens fremløb ændres højst hvert kvarter: lad kvarteret være gået
            if c.commands[s.sid].get(key):
                c.commands[s.sid][key] -= 3600
        await ha.services.async_call("select", "select_option", {"entity_id": sel.entity_id, "option": "comfort"}, blocking=True)
        await ha.async_block_till_done()
    assert c.priority() == "comfort" and calls[-1]["priority"] == "comfort" and c.settings["priority"] == "comfort"
    assert ha.states.get(sel.entity_id).state == "comfort"
    # forvarmningen over loftet står i planens timer og skrives til varmepumpen (24 + 14 = 38 °C; loftet er 35,2)
    assert {x["flow"] for x in c.plans[s.sid]["hours"] if x["heat_kw"]} == {38.0} and writes[-1] == 38.0
    v = c.flow_search(s)
    assert v["forvarmer"] == 14.0 and v["mål_nu"] == 38.0 and v["loft"] == 35.2
    st = next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("plan_now"))
    pr = st.attributes["prioritet"]
    assert pr["valgt"] == "comfort" and [x["trin"] for x in pr["trin"]] == ["economy", "balance", "comfort"]
    assert pr["trin"][2]["hævet"] == 14 and pr["trin"][0]["kl"].startswith("20") and "ts" not in pr["trin"][0]
    # varmepumpens eget maksimum for fremløbsmålet er grænsen, når det ligger lavere
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 33, "min_temp": 20, "max_temp": 40})
    model = c.plan_inputs[s.sid][1]
    assert c._flow_top(s, model, 20.5) == 40.0  # noqa: SLF001
    # udgaven gemmes med tallene, så en opdatering kalibrerer ved opstart (en rettelse i kalibreringen virker straks)
    ver = json.loads((Path(cm.__file__).parent / "manifest.json").read_text(encoding="utf-8"))["version"]
    assert c._data()["sw"] == ver  # noqa: SLF001


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_ice_watch_sensor_reports_once(ha: HomeAssistant) -> None:
    """Isvagten: status og tal står i en føler på varmekilden. Bliver ydelsen ved med at være usædvanligt lav, kommer der
    én besked; grænserne for »lavt« holdes fast, mens det varer; og efter en normal periode kan en ny tilisning meldes."""
    ha.states.async_set("binary_sensor.afrimning", "off", {})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data=HOUSE,
                        subentries_data=[_sub({**HYD, "defrost": "binary_sensor.afrimning", "flow_temp": "sensor.frem"}, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    s = next(iter(c.sources.values()))
    events: list = []
    ha.bus.async_listen("varmemodel_varsel", lambda ev: events.append(ev.data))
    now = int(time.time())
    bounds = {"ren_low": 0.88, "snit_low": 0.86, "ren_normal": 1.01, "snit_normal": 0.98}
    cyc = [{"t": now - 3000 * (9 - i), "til": now - 3000 * (9 - i) + 2400, "min": 40, "ude": 0.5, "kw": 4.8, "ren": 0.8, "slut": 0.7,
            "snit": 0.78, "n": 6} for i in range(9)]
    w = {"status": "ice", "ref_n": 60, "low_run": 9, "low_hours": 6.0, "since": cyc[0]["t"], "bounds": bounds, "ren": 0.8, "snit": 0.78,
         "slut": 0.7, "latest": cyc[-1]["til"], "cycles": cyc[-8:], "defrost": {"n": 20, "gap_min": 48, "dur_min": 3.5, "last": now - 600},
         "updated": now}
    c._ice_sync = lambda _s: ({"123": [0.4, 0.15, 0.5, 33.0, 10]}, dict(w))  # noqa: SLF001
    await c._on_today(None)  # noqa: SLF001
    await ha.async_block_till_done()
    st = next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("ice_watch"))
    a = st.attributes
    assert st.state == "ice" and a["lave_i_træk"] == 9 and a["lav_i_timer"] == 6.0 and a["ren_pct"] == 80 and a["ren_lav_pct"] == 88
    assert a["ren_normal_pct"] == 101 and len(a["cyklusser"]) == 8 and all(x["lav"] for x in a["cyklusser"]) and a["cyklusser"][0]["ren_pct"] == 80
    assert a["afrimninger_24t"] == 20 and a["afrimning_hver_min"] == 48 and a["afrimning_varer_min"] == 3.5 and a["siden"].startswith("20")
    assert len(events) == 1 and events[0]["type"] == "drift" and "ise til" in events[0]["titel"] and "6 timer" in events[0]["kort"]
    assert "80 %" in events[0]["tekst"] and "ca. hver 48. minut" in events[0]["tekst"] and "3,5 min. ad gangen" in events[0]["tekst"]
    assert c.air[f"ice:{s.sid}"]["hold"] == bounds and s.slots["123"] == [0.4, 0.15, 0.5, 33.0, 10]
    await c._on_today(None)  # noqa: SLF001 – samme tilisning: ingen ny besked
    await ha.async_block_till_done()
    assert len(events) == 1
    # normal igen: grænserne slippes, og en ny tilisning meldes igen
    c._ice_sync = lambda _s: ({}, {**w, "status": "ok", "low_run": 0, "low_hours": 0.0, "since": None})  # noqa: SLF001
    await c._on_today(None)  # noqa: SLF001
    await ha.async_block_till_done()
    assert "hold" not in c.air[f"ice:{s.sid}"] and ha.states.get(st.entity_id).state == "ok" and len(events) == 1
    c._ice_sync = lambda _s: ({}, {**w, "status": "watch", "low_run": 3, "low_hours": 2.0})  # noqa: SLF001 – følg: vises, men ingen besked
    await c._on_today(None)  # noqa: SLF001
    await ha.async_block_till_done()
    assert ha.states.get(st.entity_id).state == "watch" and c.air[f"ice:{s.sid}"]["hold"] == bounds and len(events) == 1
    c._ice_sync = lambda _s: ({}, dict(w))  # noqa: SLF001
    await c._on_today(None)  # noqa: SLF001
    await ha.async_block_till_done()
    assert len(events) == 2
    # uden afrimningsføler er der ingen isvagt
    assert not [x for x in ha.states.async_all("sensor") if x.entity_id.endswith("ice_watch") and x.entity_id != st.entity_id]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_hot_water_plan_is_a_recommendation(ha: HomeAssistant) -> None:
    """Varmt vand: med en tankføler på den vandbårne kilde lægger Varmemodel en plan for varmt vand og viser den som en
    anbefaling (fra / normalt / højt) ved siden af det, varmepumpen faktisk står på. Brugerens tre temperaturer er
    indstillinger; »bad nu« giver »højt« med det samme. Varmemodel skriver intet til varmepumpen."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0 + 0.1 * (k % 5)} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.tank", "43.0", {"state_class": "measurement", "unit_of_measurement": "°C"})
    ha.states.async_set("switch.vv", "off", {})
    ha.states.async_set("sensor.vv_maal", "45.0", {})
    ha.states.async_set("input_boolean.bad", "off", {})
    hyd = {**HYD, "dhw_temp": "sensor.tank", "dhw": "binary_sensor.vv", "dhw_switch": "switch.vv", "dhw_target": "sensor.vv_maal",
           "dhw_boost": "input_boolean.bad"}
    calls: list = []
    for dom, srv in (("switch", "turn_on"), ("switch", "turn_off"), ("climate", "set_temperature"), ("water_heater", "set_temperature")):
        ha.services.async_register(dom, srv, lambda call: calls.append(call))
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"}, subentries_data=[_sub(hyd, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    s = next(iter(c.sources.values()))
    await c.async_replan()
    await ha.async_block_till_done()
    get = lambda: next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("hot_water"))  # noqa: E731
    st = get()
    a = st.attributes
    # varm tank: ingen opvarmning nu; varmepumpen står også på fra – enige
    assert st.state == "off" and a["tank"] == 43.0 and a["varmepumpen_nu"] == "off" and a["enig"] is True
    assert (a["mindst"], a["normalt"], a["højt"]) == (40.0, 45.0, 50.0) and a["prioritet"] == "balance" and len(a["plan"]) >= 20
    assert a["tank_model"]["opvarmning_grader_pr_min"] == 0.3 and a["tank_model"]["lært_af_timer"] == 0  # endnu intet lært: standardmodellen
    assert a["næste_opvarmning"] and a["næste_opvarmning"]["mål"] in (45.0, 50.0) and a["timer_under_mindst"] == 0
    nums = {x.entity_id.split(".")[1].split("_", 1)[1]: x.state for x in ha.states.async_all("number") if "hot_water" in x.entity_id}
    assert len(nums) == 3
    # kold tank: varm nu – varmepumpen står stadig på fra, så de er uenige
    ha.states.async_set("sensor.tank", "36.0", {"state_class": "measurement", "unit_of_measurement": "°C"})
    await c.async_replan()
    await ha.async_block_till_done()
    a = get().attributes
    assert get().state == "normal" and a["mål"] == 45.0 and a["plan"][0]["varmer"] and a["enig"] is False
    # varmepumpen står på til med mål 45: enige igen
    ha.states.async_set("switch.vv", "on", {})
    await c.async_replan()
    await ha.async_block_till_done()
    assert get().attributes["varmepumpen_nu"] == "normal" and get().attributes["enig"] is True
    # »mindst« sat langt ned: tanken holder hele perioden, ingen opvarmning
    c.settings["dhw_min"] = 25.0
    await c.async_replan()
    await ha.async_block_till_done()
    assert get().state == "off" and get().attributes["mindst"] == 25.0 and get().attributes["opvarmninger"] == 0
    # »bad nu«: højt med det samme
    ha.states.async_set("input_boolean.bad", "on", {})
    await c.async_replan()
    await ha.async_block_till_done()
    assert get().state == "high" and get().attributes["mål"] == 50.0 and get().attributes["bad_nu"] is True
    assert not calls and c.dhw[s.sid]["plan"]["state"] == "high"  # kun anbefaling: intet er skrevet til varmepumpen


async def test_hot_water_control_writes_the_plan(ha: HomeAssistant) -> None:
    """»Varmemodel styrer varmt vand«: planens fra / normalt / højt skrives til varmepumpen – målet først, så til; kun ved
    en forskel. En opvarmning i gang kører færdig; i en blokering er det fra (undtagen »bad nu«); målet holdes inden for
    det, entiteten tager imod; og følger varmepumpen ikke med, skrives der til reserven. Varmeplanen får at vide, hvor
    meget af timen varmt vand tager."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0 + 0.1 * (k % 5)} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.tank", "36.0", {"state_class": "measurement", "unit_of_measurement": "°C"})
    ha.states.async_set("switch.vv", "off", {})
    ha.states.async_set("binary_sensor.vv", "off", {})
    ha.states.async_set("sensor.prisstatus", "grøn")
    ha.states.async_set("climate.vv", "auto", {"temperature": 40.0, "min_temp": 40, "max_temp": 55})
    ha.states.async_set("water_heater.sky", "off", {"temperature": 40.0, "min_temp": 40, "max_temp": 54, "operation_list": ["off", "heat_pump"]})
    ha.states.async_set("input_boolean.bad", "off", {})
    hyd = {**HYD, "dhw_temp": "sensor.tank", "dhw": "binary_sensor.vv", "dhw_switch": "switch.vv", "dhw_target": "climate.vv",
           "dhw_boost": "input_boolean.bad", "dhw_fallback": "water_heater.sky"}
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2,
                        data={**HOUSE, "price_entity": "sensor.pris", "block_now_entity": "sensor.prisstatus", "block_now_states": "rød"},
                        subentries_data=[_sub(hyd, "source")])
    e.add_to_hass(ha)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    s = next(iter(c.sources.values()))
    calls: list = []
    for dom, srv in (("switch", "turn_on"), ("switch", "turn_off"), ("climate", "set_temperature"), ("water_heater", "set_temperature"),
                     ("water_heater", "set_operation_mode")):
        ha.services.async_register(dom, srv, lambda call: calls.append((call.domain, call.service, dict(call.data))))
    # hele døgnet er brugstid (fra = til og intet lært): kold tank → varm nu. Styringen er fra: intet skrives
    await c.async_replan()
    await ha.async_block_till_done()
    assert c.dhw[s.sid]["plan"]["state"] == "normal" and not calls and not c.dhw[s.sid]["busy"]
    names = {x.entity_id for x in ha.states.async_all("switch")} | {x.entity_id for x in ha.states.async_all("select")} | {x.entity_id for x in ha.states.async_all("time")}
    assert any(x.endswith("controls_hot_water") for x in names) and any(x.endswith("hot_water_priority") for x in names)
    assert sum(1 for x in names if "hot_water_is_used" in x) == 2
    # styringen til: først målet, så til
    await c.async_set_dhw_control(s, True)
    await ha.async_block_till_done()
    assert calls == [("climate", "set_temperature", {"entity_id": "climate.vv", "temperature": 45.0}), ("switch", "turn_on", {"entity_id": "switch.vv"})]
    d = c.dhw[s.sid]
    assert d["control"] and d["writes"] == 2 and d["last"]["wrote"] == ["mål 45 °C", "til"] and d["last"]["via"] == "primary"
    # varmeplanen får timens andel: opvarmningens minutter plus pausen omkring den
    assert d["busy"] and abs(d["busy"][d["plan"]["hours"][0]["ts"]] - min(0.9, (d["plan"]["hours"][0]["min"] + 10.0) / 60)) < 0.02
    # varmepumpen følger med: intet nyt at skrive
    calls.clear()
    ha.states.async_set("switch.vv", "on", {})
    ha.states.async_set("climate.vv", "auto", {"temperature": 45.0, "min_temp": 40, "max_temp": 55})
    ha.states.async_set("binary_sensor.vv", "on", {})
    await c.async_replan()
    await ha.async_block_till_done()
    assert not calls
    # midt i opvarmningen (tanken tættere på målet end startgrænsen): planen regner den færdig – der slukkes ikke
    ha.states.async_set("sensor.tank", "43.0", {"state_class": "measurement", "unit_of_measurement": "°C"})
    await c.async_replan()
    await ha.async_block_till_done()
    assert not calls and c.dhw[s.sid]["plan"]["state"] == "normal" and c.dhw[s.sid]["running"] and c.dhw[s.sid]["agree"]
    # opvarmningen er færdig: fra
    ha.states.async_set("binary_sensor.vv", "off", {})
    ha.states.async_set("sensor.tank", "45.5", {"state_class": "measurement", "unit_of_measurement": "°C"})
    await c.async_replan()
    await ha.async_block_till_done()
    assert calls == [("switch", "turn_off", {"entity_id": "switch.vv"})] and c.dhw[s.sid]["plan"]["state"] == "off"
    calls.clear()
    ha.states.async_set("switch.vv", "off", {})
    # blokeret lige nu og kold tank: der tændes ikke
    ha.states.async_set("sensor.prisstatus", "rød")
    ha.states.async_set("sensor.tank", "35.0", {"state_class": "measurement", "unit_of_measurement": "°C"})
    await c.async_replan()
    await ha.async_block_till_done()
    assert not calls
    # »bad nu« gælder også i en blokering: højt – men kun så højt, som varmepumpen tager imod (55), og kun én gang
    c.settings["dhw_high"] = 60.0
    ha.states.async_set("input_boolean.bad", "on", {})
    await ha.async_block_till_done()
    await c.async_replan()
    await ha.async_block_till_done()
    assert ("climate", "set_temperature", {"entity_id": "climate.vv", "temperature": 55.0}) in calls and calls[-1] == ("switch", "turn_on", {"entity_id": "switch.vv"})
    n = len(calls)
    ha.states.async_set("switch.vv", "on", {})
    ha.states.async_set("climate.vv", "auto", {"temperature": 55.0, "min_temp": 40, "max_temp": 55})
    await c.async_replan()
    await ha.async_block_till_done()
    assert len(calls) == n
    # »bad nu« fra, stadig blokeret: fra med det samme – også midt i en opvarmning
    ha.states.async_set("binary_sensor.vv", "on", {})
    ha.states.async_set("input_boolean.bad", "off", {})
    await ha.async_block_till_done()
    await c.async_replan()
    await ha.async_block_till_done()
    assert calls[-1] == ("switch", "turn_off", {"entity_id": "switch.vv"})
    # varmepumpen følger ikke med (kontakten står stadig på til ved tjekket): reserven får besked – og kun, hvis den
    # heller ikke viser det ønskede
    calls.clear()
    await c._dhw_apply(s, time.time(), recheck=True)
    assert calls == []  # reserven står allerede på fra
    ha.states.async_set("water_heater.sky", "heat_pump", {"temperature": 45.0, "min_temp": 40, "max_temp": 54, "operation_list": ["off", "heat_pump"]})
    await c._dhw_apply(s, time.time(), recheck=True)
    assert calls == [("water_heater", "set_operation_mode", {"entity_id": "water_heater.sky", "operation_mode": "off"})]
    assert c.dhw[s.sid]["last"]["via"] == "fallback" and c.dhw[s.sid]["fallback"]
    st = next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("hot_water"))
    assert st.attributes["styrer"] is True and st.attributes["seneste_skrivning"]["via"] == "fallback" and st.attributes["reserve_brugt"]
    # styringen fra igen: intet skrives, uanset planen
    calls.clear()
    await c.async_set_dhw_control(s, False)
    ha.states.async_set("sensor.prisstatus", "grøn")
    await c.async_replan()
    await ha.async_block_till_done()
    assert not calls and c.dhw[s.sid]["plan"]["state"] == "normal" and not c.dhw[s.sid]["busy"]


async def test_air_priority_min_fan_and_shared_steps(ha: HomeAssistant) -> None:
    """Luft-luft: kilden har sin egen prioritet (økonomi – balance – komfort; den gamle »Opstart« oversættes og dens
    entitet fjernes), en min. blæser, der former stigen, og trin uden egne målinger låner de andre blæseres tal –
    også starttrinnet lært med en anden maks. blæser."""
    from homeassistant.helpers import entity_registry as er

    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "19.0", {"state_class": "measurement"})
    ha.states.async_set("climate.ll", "off", {"fan_modes": ["Auto", "Low", "Mid", "High"], "preset_modes": ["none", "quiet"],
                                               "swing_modes": ["Auto", "Up"], "temperature": 20, "fan_mode": "Low"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub({**AIR, "climate": "climate.ll"}, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air,
                                                                 "minimum": 19.5, "maximum": 22.5}, subentry_type="room", title="Loft", unique_id=None))
    # den gamle »Opstart«-entitet fra før 0.29 ligger i registret
    old = er.async_get(ha).async_get_or_create("select", DOMAIN, f"{e.entry_id}_{air}_air_start", config_entry=e, suggested_object_id="ll_start_up")
    # … og tallet »Maks. fremløb over det lærte«, som også er fjernet i 0.29 (planen hæver selv efter prioriteten)
    old_fm = er.async_get(ha).async_get_or_create("number", DOMAIN, f"{e.entry_id}_flow_margin", config_entry=e, suggested_object_id="hus_flow_margin")
    for svc in ("set_hvac_mode", "set_fan_mode", "set_temperature", "set_preset_mode"):
        ha.services.async_register("climate", svc, lambda call: None)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    assert er.async_get(ha).async_get(old.entity_id) is None and er.async_get(ha).async_get(old_fm.entity_id) is None
    assert not any("flow_margin" in x.unique_id or "above_the_learned" in x.entity_id for x in er.async_entries_for_config_entry(er.async_get(ha), e.entry_id))
    c = e.runtime_data
    s = c.sources[air]
    c.rooms["sensor.rum_2"] = {"value": 0.4, "r2": 0.6, "extra": {"sources": {"Luft-luft": {"now": 0.7, "delayed": 0.0}}, "accuracy_6h_k": 0.3}}
    c._recent_sync = lambda s, a, b: ({}, {})
    c.settings[f"{air}.air_fan_day"] = c.settings[f"{air}.air_fan_night"] = "High"
    sel = lambda end: next(x for x in ha.states.async_all("select") if x.entity_id.endswith(end) and "luft_luft" in x.entity_id)  # noqa: E731
    pr = sel("priority")
    assert pr.state == "balance" and pr.attributes["options"] == ["economy", "balance", "comfort"]
    # den gamle »Opstart« oversættes: hurtig = komfort, rolig = balance – til brugeren vælger selv
    c.settings[f"{air}.air_start"] = "hurtig"
    assert c.air_setting(s, "air_priority") == "comfort"
    c.settings[f"{air}.air_start"] = "rolig"
    assert c.air_setting(s, "air_priority") == "balance"
    await ha.services.async_call("select", "select_option", {"entity_id": pr.entity_id, "option": "economy"}, blocking=True)
    await ha.async_block_till_done()
    assert c.air_setting(s, "air_priority") == "economy" and sel("priority").state == "economy"
    step = lambda: next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("ladder_step") or x.entity_id.endswith("air_step"))  # noqa: E731
    assert step().attributes["prioritet"] == "economy" and step().attributes["min_blæser"] is None
    assert len(step().attributes["trin_liste"]) == (1 + 3) * 7  # quiet + 3 blæsere på hver af 7 temperaturer
    # min. blæser: »quiet« og de lave blæsere udgår
    mf = sel("min_fan")
    assert mf.state == "quiet" and mf.attributes["options"] == ["quiet", "Low", "Mid", "High"]
    await ha.services.async_call("select", "select_option", {"entity_id": mf.entity_id, "option": "Mid"}, blocking=True)
    await ha.async_block_till_done()
    lst = step().attributes["trin_liste"]
    assert [(x["blæser"], x["setpunkt"]) for x in lst[:3]] == [("Mid", 19.5), ("High", 19.5), ("Mid", 20.0)]
    assert len(lst) == 2 * 7 and step().attributes["min_blæser"] == "Mid"
    # det, der er lært med en anden blæser, deles: forbrug og virkning på samme setpunkt over gulvet – og starttrinnet
    c.air[air]["w"] = {"Mid +0.5": 300, "Mid +2.0": 600}
    c.air[air]["eff"] = {"Mid +0.5": [5.0, 10.0, 12], "Mid +2.0": [3.0, 10.0, 12]}
    c.air[air]["start"] = {"0": "Mid +1.0"}
    c.air[air]["on"], c.air[air]["step"] = False, None
    c.settings[f"{air}.air_priority"] = "comfort"
    await c.async_replan()
    await ha.async_block_till_done()
    a = step().attributes
    by = {(x["blæser"], x["setpunkt"]): x for x in a["trin_liste"]}
    assert (by[("High", 20.0)]["w"], by[("High", 20.0)]["w_kilde"]) == (300, "delt") and by[("High", 20.0)]["virkning_kilde"] == "delt"
    assert by[("High", 20.0)]["grader_pr_kwh_el"] == 0.5 and by[("High", 20.0)]["målinger"] == 12
    assert (by[("High", 20.5)]["w"], by[("High", 20.5)]["w_kilde"]) == (400, "skøn") and by[("High", 20.5)]["virkning_kilde"] == "skøn"
    assert a["starttrin"] == {"0": "Mid +1.0"}
    if c.commands[air].get("on"):  # planen varmer (rummet er under gulvet): starter på den lærte temperatur, højeste blæser
        assert a["trin"] == next(k for k, x in enumerate(a["trin_liste"]) if (x["blæser"], x["setpunkt"]) == ("High", 20.5))


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_frost_price_and_ice_watch_reach_the_plan(ha: HomeAssistant) -> None:
    """Rimens pris: isvagten lærer, hvad en afrimning tager fra gulvvandet; sammen med afrimninger pr. kWh varme for hvert
    udebånd (kalibreringen) bliver det planens faktor – i rimvejr får huset mindre af den målte varme. Og bliver
    lamelfladen ikke ren (isvagten »følg« eller »is«), hæver planen ikke fremløbet over loftet, uanset prioriteten."""
    import math
    from unittest.mock import patch

    from custom_components.varmemodel import coordinator as cm

    ha.states.async_set("binary_sensor.afrimning", "off", {})
    c, s = await _lv_entry(ha, extra_source={"defrost": "binary_sensor.afrimning", "flow_temp": "sensor.frem"})
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    now = int(time.time())
    w = {"status": "ok", "ref_n": 60, "low_run": 0, "low_hours": 0.0, "since": None, "bounds": None, "ren": 1.0, "snit": 1.0, "slut": 0.9,
         "latest": now - 600, "cycles": [], "defrost": {"n": 4, "gap_min": 50, "dur_min": 3.5, "last": now - 600}, "updated": now,
         "defrost_new": [{"t": now - 3000, "min": 3.5, "q": 0.31, "flow": 15.0}, {"t": now - 6000, "min": 3.4, "q": 0.29, "flow": 15.0},
                         {"t": now - 2 * 86400, "min": 3.6, "q": 0.30, "flow": 15.0}, {"t": now - 400, "q": None}]}
    c.air.setdefault(f"ice:{s.sid}", {})["defq"] = {str(now - 40 * 86400): [0.9, 3.0]}  # ældre end 30 døgn: glemmes
    c._ice_sync = lambda _s: ({}, {**w, "defrost": dict(w["defrost"]), "defrost_new": list(w["defrost_new"])})  # noqa: SLF001
    for f in ("_cop_today_sync", "_health_sync"):
        if hasattr(c, f):
            setattr(c, f, lambda _s: None)
    await c._on_today(None)  # noqa: SLF001
    await ha.async_block_till_done()
    dq = c.air[f"ice:{s.sid}"]["defq"]
    assert len(dq) == 4 and dq[str(now - 400)] == [None, None] and str(now - 40 * 86400) not in dq
    assert s.ice["defrost"]["q"] == 0.30 and s.ice["defrost"]["q_n"] == 3 and s.ice["defrost"]["q_day"] == 0.6 and s.ice["defrost"]["q_day_n"] == 2
    assert c._frost_points(s) == []  # noqa: SLF001 – afrimninger pr. kWh varme er ikke lært endnu
    s.accepted["defrost"] = {"value": 0.5, "extra": {"per_kwh": [{"t_out": -1.0, "per_kwh": 0.25, "kwh": 80.0}, {"t_out": 8.0, "per_kwh": 0.0, "kwh": 80.0}],
                                                     "per_kwh_all": 0.125}}
    pts = c._frost_points(s)  # noqa: SLF001
    assert [a for a, _ in pts] == [-1.0, 8.0] and abs(pts[0][1] - (1 - 0.075) / (1 - 0.0375)) < 1e-3 and pts[1][1] > 1.03
    st = next(x for x in ha.states.async_all("sensor") if x.entity_id.endswith("ice_watch"))
    await c.async_replan()
    await ha.async_block_till_done()
    a = ha.states.get(st.entity_id).attributes
    assert a["afrimning_tager_kwh_varme"] == 0.30 and a["afrimning_tager_lært_af"] == 3 and a["rim_24t_kwh_varme"] == 0.6
    assert [x["ude"] for x in a["rim_faktor"]] == [-1.0, 8.0] and a["rim_faktor"][0]["pct"] < 100 < a["rim_faktor"][1]["pct"] and a["fremløb_holdes"] is False
    # planen: ude er −2 °C i testen – varmetimerne har rimens faktor, og begrundelsen siger det
    hrs = [x for x in c.plans[s.sid]["hours"] if x["heat_kw"]]
    assert hrs and all(x["frost"] == round(pts[0][1], 3) for x in hrs) and c.plan_inputs[s.sid][1].frost == tuple(pts)
    assert "rim: i de koldeste varmetimer får huset 4 % mindre" in c.plans[s.sid]["reason"]
    # isvagten melder »følg«: planen må ikke hæve fremløbet over loftet (kun op til det)
    calls: list = []
    real = cm.make_plan

    def rec(*a_, **k):
        calls.append(k)
        return real(*a_, **k)

    with patch.object(cm, "make_plan", rec):
        await c.async_replan()
        free = calls[-1]["flow_raise_free"]
        assert calls[-1]["flow_raise_max"] > free and not c.air[f"hydro:{s.sid}"].get("ice_hold")
        s.ice = {**s.ice, "status": "watch"}
        await c.async_replan()
        hs = c.air[f"hydro:{s.sid}"]
        assert calls[-1]["flow_raise_max"] == calls[-1]["flow_raise_free"] == free and hs["ice_hold"] and hs["low_top"] == hs["low_loft"]
        assert "isvagt: lamelfladen bliver ikke ren" in c.plans[s.sid]["reason"]
        assert ha.states.get(st.entity_id).attributes["fremløb_holdes"] is True
        s.ice = {**s.ice, "status": "ok"}
        await c.async_replan()
        assert calls[-1]["flow_raise_max"] > free and "ice_hold" not in c.air[f"hydro:{s.sid}"] and math.isclose(c.air[f"hydro:{s.sid}"]["low_top"], 44.5)


LIFT_TABLE = [[2.0, 3.18, 10], [3.0, 3.18, 55], [4.0, 3.59, 69], [5.0, 3.74, 138], [6.0, 4.25, 112], [7.0, 5.21, 92], [8.0, 6.52, 33],
              [9.0, 7.1, 23], [10.0, 7.6, 13], [11.0, 7.6, 10]]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_heat_up_follows_the_return_by_priority(ha: HomeAssistant) -> None:
    """Opvarmningen: på vej mod planens fremløb står målet prioritetens løft over returen og følger den op (økonomi: varmepumpens
    rolige område, balance: midt imellem), komfort går direkte. Loftet falder ikke, når returen dykker; varmt vand flytter det
    ikke; en målt effekt under den laveste stabile løfter 1 °C; dækker trinnets effekt ikke husets behov, bestemmer rummene; og
    uden lært tabel er alt som før."""
    c, s = await _lv_entry(ha, {"boost_entity": "input_boolean.fryser"})
    s.conf.update(compressor="binary_sensor.komp", defrost="binary_sensor.afrim", dhw="binary_sensor.vv", flow_temp="sensor.frem")
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    hs = c.air.setdefault(f"hydro:{s.sid}", {})
    for e, v in (("binary_sensor.afrim", "off"), ("binary_sensor.vv", "off"), ("sensor.frem", "26.0"), ("binary_sensor.komp", "on"),
                 ("input_boolean.fryser", "off")):
        ha.states.async_set(e, v)
    ha.states.async_set("sensor.vp_varme", "100.0", {"unit_of_measurement": "kWh"})
    calls: list = []
    ha.services.async_register("climate", "set_temperature", lambda call: calls.append(call.data["temperature"]))
    c._low_rooms_sync = lambda _s: []  # noqa: SLF001
    c._comp_stops_sync = lambda _s, _since: []  # noqa: SLF001
    now = time.time()
    hs.update(low=24.0, low_t=now, start_t=now - 3000, comp_on_t=now - 3000, low_loft=35.0, low_top=40.0, low_raise=11.0)
    plan = [{"flow": 35.0, "blocked": False}]
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 24, "min_temp": 20, "max_temp": 45})
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 35.0 and "appr" not in hs and c.flow_search(s)["opvarmning"] is None  # intet lært: direkte som før
    hs["lift"] = {"t": now, "tabel": LIFT_TABLE}
    s.accepted["min_power"], s.accepted["max_power"] = {"value": 3.16}, {"value": 5.7}
    c.settings["priority"] = "economy"
    ha.states.async_set("sensor.retur", "22.0")
    await c._flow_apply(s, plan, False)
    a = hs["appr"]
    assert calls[-1] == 26.0 and a["aktiv"] and a["løft"] == 4.0 and a["kw"] == 3.59 and a["v"] == 26.0 and a["dækker"]
    v = c.flow_search(s)
    assert v["mål_nu"] == 26.0 and v["tekst"].startswith("Opvarmning (økonomi): fremløbet holdes 4 °C over returen – ca. 3,6 kW – og følger den op mod 35 °C. Nu 26 °C.")
    assert "Forvarmer: 35 °C (søgningens 24 °C + 11 °C)" in v["tekst"]
    o = v["opvarmning"]
    assert o["aktiv"] and o["trin"] == "economy" and o["valg"]["economy"] == {"løft": 4.0, "kw": 3.59} and o["valg"]["balance"] == {"løft": 6.0, "kw": 4.25}
    assert o["fuld"] == {"løft": 10.0, "kw": 7.6} and o["tabel"] == LIFT_TABLE and o["loft_nu"] == 26.0 and o["mål"] == 35.0
    assert f"appr:{s.sid}" in c._hydro_pending  # noqa: SLF001 – returen følges igen om lidt
    # gulvet bliver varmere: loftet følger returen op – og falder ikke, når returen dykker (afrimning)
    ha.states.async_set("sensor.retur", "24.0")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 28.0
    ha.states.async_set("sensor.retur", "22.5")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 28.0 and hs["appr"]["v"] == 28.0
    # varmt vand: returen siger intet om gulvet – loftet flyttes ikke (heller ikke lige efter)
    ha.states.async_set("binary_sensor.vv", "on")
    ha.states.async_set("sensor.retur", "45.0")
    await c._flow_apply(s, plan, False)
    assert hs["appr"]["v"] == 28.0
    ha.states.async_set("binary_sensor.vv", "off")
    hs["dhw_off_t"] = time.time() - 100
    await c._flow_apply(s, plan, False)
    assert hs["appr"]["v"] == 28.0
    hs["dhw_off_t"] = 0
    ha.states.async_set("sensor.retur", "24.0")
    # den målte effekt ligger under varmepumpens laveste stabile (pumpen kører langsomt): løftet hæves 1 °C – højst 2
    hs["appr"]["q"] = [[time.time() - 900, 99.5]]  # 0,5 kWh varme på et kvarter = 2 kW
    await c._flow_apply(s, plan, False)
    assert hs["appr"]["trim"] == 1 and hs["appr"]["kw_målt"] == 2.0 and calls[-1] == 29.0
    assert "(+1 °C, fordi den målte effekt lå under varmepumpens laveste stabile)" in c.flow_search(s)["tekst"]
    hs["appr"]["q"] = [[time.time() - 900, 99.5]]
    await c._flow_apply(s, plan, False)
    assert hs["appr"]["trim"] == 1  # først igen efter et nyt kvarter
    # balance: 6 °C over returen (+ den ene grad); komfort: målet direkte, uden loft
    c.settings["priority"] = "balance"
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 31.0 and hs["appr"]["løft"] == 6.0
    c.settings["priority"] = "comfort"
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 35.0 and "appr" not in hs and c.flow_search(s)["opvarmning"]["aktiv"] is False
    # søgningens eget mål over loftet (rummene beder om mere): loftet gælder også det – når trinnets effekt dækker husets behov
    c.settings["priority"] = "economy"
    hs.update(low=31.0, low_raise=4.0, start_t=time.time() - 2000, comp_on_t=time.time() - 2000, need_kw=1.9)
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 28.0 and hs["appr"]["dækker"] and hs["appr"]["aktiv"]
    # … men dækker den ikke behovet med en fjerdedel i margen, bestemmer rummene selv (kun planens hævning holdes nede)
    hs["need_kw"] = 3.2
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 31.0 and hs["appr"]["dækker"] is False and c.flow_search(s)["opvarmning"]["rummene_bestemmer"] is True
    # »jeg fryser«: intet loft
    hs["need_kw"] = 1.9
    ha.states.async_set("input_boolean.fryser", "on")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 35.0 and "appr" not in hs
    ha.states.async_set("input_boolean.fryser", "off")
    # opstartens løft tæller med i loftet, så målet ikke falder, når opstarten er slut; et stop rydder det hele
    hs.update(low=24.0, low_raise=11.0, start_t=time.time(), comp_on_t=time.time(), start_k=5.1)
    ha.states.async_set("sensor.retur", "21.5")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 27.0 and hs["guard"] == {"mål": 27.0, "grund": "opstart"} and hs["appr"]["v"] == 27.0
    hs["comp_on_t"] = time.time() - 1500
    ha.states.async_set("sensor.frem", "27.0")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 27.0 and hs["guard"] is None  # 21,5 + 4 = 25,5 ville være et fald: loftet står på opstartens 27
    # løftet efter en afrimning (længe efter starten) hæver målet, mens det står på – men flytter ikke loftet
    hs.update(start_t=time.time() - 7200, df_off_t=time.time() - 700)
    hs["appr"]["start_t"] = hs["start_t"]
    await c._flow_apply(s, plan, False)
    assert hs["appr"]["v"] == 27.0
    ha.states.async_set("sensor.retur", "23.5")
    await c._flow_apply(s, plan, False)
    assert calls[-1] == 29.0 and hs["guard"] == {"mål": 29.0, "grund": "opstart"} and hs["appr"]["v"] == 27.5


async def test_economy_plans_with_the_heat_pumps_calm_power(ha: HomeAssistant) -> None:
    """Økonomi holder varmepumpen i sit rolige område – så regner planen også kun med den effekt. Balance og komfort regner med
    det, varmepumpen kan yde; uden lært løft er alt som før. Prisloftets hjælper står på status-føleren til fanen."""
    c, s = await _lv_entry(ha, {"price_cap_entity": "input_number.loft", "block_quarters_entity": "sensor.kvarterer"})
    ha.states.async_set("sensor.kvarterer", "0", {"kvarterer": []})
    c.settings[f"{s.sid}.soft_start"] = "lavest"
    s.accepted["min_power"], s.accepted["max_power"] = {"value": 3.16}, {"value": 5.7}
    c.settings["priority"] = "economy"
    await c.async_replan()
    assert c.plan_inputs[s.sid][1].p_max == 5.7 and "rolige område" not in c.plans[s.sid]["reason"]
    c.air.setdefault(f"hydro:{s.sid}", {})["lift"] = {"t": time.time(), "tabel": LIFT_TABLE}
    await c.async_replan()
    assert c.plan_inputs[s.sid][1].p_max == 3.59
    assert "økonomi: varmepumpen holdes i sit rolige område (ca. 3.6 kW)" in c.plans[s.sid]["reason"]
    assert all(x["heat_kw"] <= 3.59 + 1e-6 for x in c.plans[s.sid]["hours"])
    assert c.air[f"hydro:{s.sid}"]["need_kw"] > 0
    c.settings["priority"] = "comfort"
    await c.async_replan()
    assert c.plan_inputs[s.sid][1].p_max == 5.7 and "rolige område" not in c.plans[s.sid]["reason"]
    st = ha.states.get(er.async_get(ha).async_get_entity_id("sensor", DOMAIN, f"{c.entry.entry_id}_status"))
    assert st.attributes["følere"]["prisloft"] == "input_number.loft" and st.attributes["følere"]["kvarterer_over_loft"] == "sensor.kvarterer"


async def test_compare_with_even_operation_is_kept_per_day(ha: HomeAssistant) -> None:
    """Oversigten »Varmemodel mod jævn drift«: for hvert afsluttet døgn med priser i planloggen gemmes én række – målt el og kr
    mod samme varme leveret jævnt og jævnt med pause kl. 17–21; timer med varmt vand er ude for alle tre; et døgn, hvis sidste
    time mangler i målingerne, venter; og rækken regnes kun én gang."""
    from custom_components.varmemodel.planner import SourceModel

    c, s = await _lv_entry(ha)
    m = SourceModel(heat_loss_w_k=90, gains_w=200, p_max=6, p_min=3, cop=(4.5, 0.17, -0.07, 0.0), emission_kw_k=0.29)
    day0 = dt_util.start_of_local_day(dt_util.now() - timedelta(days=1))
    ts0 = int(day0.timestamp())
    log = c.plan_log.setdefault(s.sid, {})
    hours = []
    for i in range(24):
        ts = ts0 + i * 3600
        log[str(ts)] = {"p": 0.0, "e": 0.0, "pr": 4.0 if 17 <= i < 21 else 2.0, "b": False}
        hours.append(Hour(ts=ts, q=5.0 if i < 6 else 0.0, e=2.0 if i < 6 else 0.05, t_out=0.0, t_in=20.0, dhw=30.0 if i == 2 else 0.0))
    s.last_hours = hours[:20]
    c._compare_update(s, m, 20.5)  # noqa: SLF001 – døgnets sidste time er ikke målt endnu
    assert c.compare_view(s) is None
    s.last_hours = hours
    c._compare_update(s, m, 20.5)  # noqa: SLF001
    v = c.compare_view(s)
    y = v["i_går"]
    assert y["dato"] == day0.date().isoformat() and y["timer"] == 23 and y["varme_kwh"] == 25.0  # timen med varmt vand er ude
    assert y["el_kwh"] == round(5 * 2.0 + 18 * 0.05, 2) and y["kr"] == round(5 * 2.0 * 2.0 + 14 * 0.05 * 2.0 + 4 * 0.05 * 4.0, 2)
    assert y["jævn_kr"] > y["pause_kr"] > 0 and y["model_kr"] > 0 and y["ude"] == 0.0
    assert v["uge"]["dage"] == 1 and v["uge"]["kr"] == round(y["kr"], 1) and len(v["døgn"]) == 1
    before = dict(c.air[f"cmp:{s.sid}"])
    log[str(ts0)]["pr"] = 9.0
    c._compare_update(s, m, 20.5)  # noqa: SLF001 – regnet én gang: døgnet røres ikke igen
    assert c.air[f"cmp:{s.sid}"] == before
    st = ha.states.get(er.async_get(ha).async_get_entity_id("sensor", DOMAIN, f"{c.entry.entry_id}_{s.sid}_plan_cost"))
    assert st is not None


async def _air_ref_entry(ha: HomeAssistant) -> tuple:
    """Et hus med en luft-luft-kilde, hvis klimaentitet melder sin egen temperatur, et rum med gulv og et rum ved enheden."""
    now = time.time()
    h0 = int(now // 3600 * 3600)
    loc = dt_util.get_default_time_zone()
    from datetime import datetime as _dt

    items = [{"t": _dt.fromtimestamp(h0 + k * 3600, loc).strftime("%Y-%m-%dT%H"), "el": 1.0} for k in range(24)]
    ha.states.async_set("sensor.pris", "1.0", {"timer": items})
    ha.states.async_set("sensor.ude", "2.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_2", "19.0", {"state_class": "measurement"})
    ha.states.async_set("sensor.rum_3", "21.2", {"state_class": "measurement"})
    ha.states.async_set("climate.ll", "off", {**LL_ATTRS, "current_temperature": 21.0, "temperature": 20, "fan_mode": "Mid"})
    e = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={**HOUSE, "price_entity": "sensor.pris"},
                        subentries_data=[_sub(HYD, "source"), _sub({**AIR, "climate": "climate.ll"}, "source")])
    e.add_to_hass(ha)
    air = next(sid for sid, x in e.subentries.items() if x.data.get("source_type") == "air_air")
    from homeassistant.config_entries import ConfigSubentry
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Loft", "temperature": "sensor.rum_2", "heat_source": air,
                                                                 "minimum": 19.5, "maximum": 22.5}, subentry_type="room", title="Loft", unique_id=None))
    ha.config_entries.async_add_subentry(e, ConfigSubentry(data={"name": "Reposen", "temperature": "sensor.rum_3", "heat_source": air,
                                                                 "in_plan": False}, subentry_type="room", title="Reposen", unique_id=None))
    for svc in ("set_hvac_mode", "set_fan_mode", "set_temperature", "set_preset_mode"):
        ha.services.async_register("climate", svc, lambda call: None)
    assert await ha.config_entries.async_setup(e.entry_id)
    await ha.async_block_till_done()
    c = e.runtime_data
    c._recent_sync = lambda s, a, b: ({}, {})
    c.settings[f"{air}.air_fan_day"] = c.settings[f"{air}.air_fan_night"] = "High"
    c.settings[f"{air}.air_priority"] = "comfort"
    return c, c.sources[air], air


LL_ATTRS = {"fan_modes": ["Auto", "Mid", "HighMid", "High"], "preset_modes": ["none"], "swing_modes": ["Auto", "Up"],
            "min_temp": 16, "max_temp": 30, "target_temp_step": 0.5}


async def test_air_ladder_is_measured_from_the_sensor_at_the_unit(ha: HomeAssistant) -> None:
    """Luft-luft-stigen måles fra føleren ved enheden: laveste trin ligger 1,5 °C under den, setpunkterne følger føleren, og
    trinnets navn (blæser + niveau) følger trinnet. Op: blæseren først, så næste temperatur på højeste blæser. Ned: alle
    blæsere på temperaturen, før temperaturen falder – og et trin, der lige var for lidt, prøves ikke igen med det samme."""
    from types import SimpleNamespace

    from custom_components.varmemodel.air_control import DOWN_GAP, UP_EVERY
    from custom_components.varmemodel.planner import PlanRoom

    c, s, air = await _air_ref_entry(ha)
    p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [19.0] * 6})
    cold = [PlanRoom("Loft", 19.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    warm = [PlanRoom("Loft", 20.2, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    t = time.time()
    d = c._air_step(s, "climate.ll", p, cold, True, False, t)  # noqa: SLF001
    v = c.air[air]["view"]
    # føleren 21,0 °C → laveste trin 19,5 °C; toppen er rummets gulv + 3 °C; 7 temperaturer × 3 blæsere
    assert v["føler"] == "enheden" and v["føler_temp"] == 21.0 and v["laveste_under_føleren"] == 1.5 and v["top"] == 22.5
    assert [(x["blæser"], x["setpunkt"], x["niveau"]) for x in v["trin_liste"][:4]] == [("Mid", 19.5, -1.5), ("HighMid", 19.5, -1.5),
                                                                                       ("High", 19.5, -1.5), ("Mid", 20.0, -1.0)]
    assert len(v["trin_liste"]) == 21 and v["trin_liste"][-1]["setpunkt"] == 22.5 and v["trin_liste"][-1]["niveau"] == 1.5
    # en start begynder på højeste blæser
    assert d["step"] == 2 and (d["fan_mode"], d["setpoint"]) == ("High", 19.5) and c.air[air]["rung"] == "High +-1.5"
    # op fra en lav blæser: blæseren først …
    c.air[air].update(rung="Mid +-1.5", changed=t)
    seq = []
    for k in range(1, 4):
        d = c._air_step(s, "climate.ll", p, cold, True, False, t + k * UP_EVERY)  # noqa: SLF001
        seq.append((d["fan_mode"], d["setpoint"]))
    # … og fra højeste blæser den næste temperatur – stadig på højeste blæser
    assert seq == [("HighMid", 19.5), ("High", 19.5), ("High", 20.0)]
    assert "højeste blæser" in c.air[air]["view"]["grund"] and c.air[air]["not_enough"] == "High +-1.5" and c.air[air]["view"]["ikke_nok"] == 2
    # varmepumpen står nu, som stigen satte den, og føleren stiger: trinnet er det samme, setpunktet følger føleren
    t += 3 * UP_EVERY
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.3, "temperature": 20.0, "fan_mode": "High"})
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 60)  # noqa: SLF001
    assert d["setpoint"] == 20.0 and c.air[air]["view"]["kører_nu"] == "High +-1.0"  # 0,3 °C: inden for afrundingen
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.6, "temperature": 20.0, "fan_mode": "High"})
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 120)  # noqa: SLF001
    v = c.air[air]["view"]
    assert (d["fan_mode"], d["setpoint"]) == ("High", 20.5) and c.air[air]["rung"] == "High +-1.0" and v["kører_nu"] == "High +-1.0"
    assert v["trin_liste"][0]["setpunkt"] == 20.0 and len(v["trin_liste"]) == 18  # stigen er flyttet en halv grad op; toppen står fast
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.6, "temperature": 20.5, "fan_mode": "High"})
    # ned, når rummet har overskud: først blæseren på samme temperatur …
    c._air_step(s, "climate.ll", p, warm, True, False, t + 180)  # noqa: SLF001 – overskuddet begynder
    d = c._air_step(s, "climate.ll", p, warm, True, False, t + DOWN_GAP + 60)  # noqa: SLF001
    assert (d["fan_mode"], d["setpoint"]) == ("HighMid", 20.5)
    t2 = t + DOWN_GAP + 60
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.6, "temperature": 20.5, "fan_mode": "HighMid"})
    c._air_step(s, "climate.ll", p, warm, True, False, t2 + 60)  # noqa: SLF001
    d = c._air_step(s, "climate.ll", p, warm, True, False, t2 + DOWN_GAP)  # noqa: SLF001
    assert (d["fan_mode"], d["setpoint"]) == ("Mid", 20.5)
    # … og trinnet under (højeste blæser på temperaturen under) var ikke nok for lidt siden: det prøves ikke med det samme
    t3 = t2 + DOWN_GAP
    c._air_step(s, "climate.ll", p, warm, True, False, t3 + 60)  # noqa: SLF001
    d = c._air_step(s, "climate.ll", p, warm, True, False, t3 + DOWN_GAP)  # noqa: SLF001
    assert (d["fan_mode"], d["setpoint"]) == ("Mid", 20.5) and "ikke nok" in c.air[air]["view"]["grund"]
    d = c._air_step(s, "climate.ll", p, warm, True, False, t3 + 2 * DOWN_GAP)  # noqa: SLF001 – overskuddet blev ved
    assert (d["fan_mode"], d["setpoint"]) == ("High", 20.0) and c.air[air]["not_enough"] is None
    # slukket: hukommelsen nulstilles, og næste start begynder forfra
    c._air_step(s, "climate.ll", p, warm, False, False, t3 + 2 * DOWN_GAP + 60)  # noqa: SLF001
    assert c.air[air]["on"] is False and c.air[air]["not_enough"] is None


async def test_air_ladder_sensor_and_top_are_choices(ha: HomeAssistant) -> None:
    """Føleren ved enheden og stigens højeste setpunkt er valg på kilden. Vælges et rums føler, måles trinene fra den – og
    det lærte samles forfra, for trinenes niveauer betyder nu noget andet."""
    from types import SimpleNamespace

    from custom_components.varmemodel.planner import PlanRoom

    c, s, air = await _air_ref_entry(ha)
    ref = next(x for x in ha.states.async_all("select") if "luft_luft" in x.entity_id and x.attributes.get("options", [None])[0] == "enheden")
    assert ref.state == "enheden" and ref.attributes["options"] == ["enheden", "Loft", "Reposen"]
    top = next(x for x in ha.states.async_all("select") if "luft_luft" in x.entity_id and x.attributes.get("options", [None])[0] == "16.0")
    assert top.state == "22.5" and top.attributes["options"][-1] == "30.0"
    p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [19.0] * 6})
    cold = [PlanRoom("Loft", 19.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    c._air_step(s, "climate.ll", p, cold, True, False, time.time())  # noqa: SLF001
    c.air[air]["w"] = {"Mid +-1.5": 320}
    await ha.services.async_call("select", "select_option", {"entity_id": ref.entity_id, "option": "Reposen"}, blocking=True)
    await ha.async_block_till_done()
    assert c._air_ref(s) == (21.2, "Reposen", "sensor.rum_3")  # noqa: SLF001
    c._air_step(s, "climate.ll", p, cold, True, False, time.time())  # noqa: SLF001
    v = c.air[air]["view"]
    # 21,2 °C − 1,5 = 19,7 → 19,5 °C; det lærte er væk
    assert v["føler"] == "Reposen" and v["føler_temp"] == 21.2 and v["trin_liste"][0]["setpunkt"] == 19.5 and "Mid +-1.5" not in (c.air[air].get("w") or {})
    # er rummets føler væk i længere tid, bruges enhedens egen
    c.last_known.pop("sensor.rum_3", None)
    ha.states.async_set("sensor.rum_3", "unavailable")
    assert c._air_ref(s)[:2] == (21.0, "enheden")  # noqa: SLF001
    ha.states.async_set("sensor.rum_3", "21.2", {"state_class": "measurement"})
    # toppen: et højere setpunkt giver flere temperaturer (aldrig mere end 3 °C over laveste trin)
    await ha.services.async_call("select", "select_option", {"entity_id": top.entity_id, "option": "21.0"}, blocking=True)
    await ha.async_block_till_done()
    c._air_step(s, "climate.ll", p, cold, True, False, time.time())  # noqa: SLF001
    v = c.air[air]["view"]
    assert v["top"] == 21.0 and v["trin_liste"][-1]["setpunkt"] == 21.0 and len(v["trin_liste"]) == 4 * 3
    # uden nogen føler ved enheden: fra rummenes gulv
    ha.states.async_set("climate.ll", "off", {**LL_ATTRS, "temperature": 20, "fan_mode": "Mid"})
    c.settings[f"{air}.air_ref"] = "enheden"
    c._air_step(s, "climate.ll", p, cold, False, False, time.time())  # noqa: SLF001
    v = c.air[air]["view"]
    assert v["føler"] is None and v["trin_liste"][0]["setpunkt"] == 19.5 and v["trin_liste"][0]["niveau"] == 0.0


async def test_air_history_names_steps_by_distance_to_the_sensor(ha: HomeAssistant) -> None:
    """Forbrug og virkning pr. trin læres også af historikken – med samme navne som stigen: blæser + setpunktets afstand
    til føleren ved enheden (et rums føler, ellers klimaentitetens egen temperatur)."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.varmemodel import coordinator as co

    c, s, _air = await _air_ref_entry(ha)
    t0 = float(int(time.time() // 900 * 900) - 6 * 3600)
    utc = dt_util.utc_from_timestamp

    def row(ts: float, state: str, **attrs) -> SimpleNamespace:  # noqa: ANN003
        return SimpleNamespace(last_updated=utc(ts), state=state, attributes=attrs)

    clim = [row(t0, "heat", fan_mode="High", temperature=21.0, preset_mode="none", current_temperature=21.0),
            # kun enhedens temperatur opdateres: indstillingen er den samme
            row(t0 + 1800, "heat", fan_mode="High", temperature=21.0, preset_mode="none", current_temperature=21.0),
            row(t0 + 7200, "heat", fan_mode="HighMid", temperature=20.0, preset_mode="none", current_temperature=22.0),
            row(t0 + 14400, "off", fan_mode="HighMid", temperature=20.0, preset_mode="none", current_temperature=21.0)]
    room = [row(t0 - 60, "22.0"), row(t0 + 7200, "21.6")]
    stats = {"sensor.ll_w": [{"start": t0 + k * 300, "mean": 430.0 if k < 24 else 320.0} for k in range(48)]}
    with patch.object(co.history, "get_significant_states", return_value={"climate.ll": clim, "sensor.rum_3": room}), \
            patch.object(co, "statistics_during_period", return_value=stats):
        own = c._air_backfill_sync("climate.ll", "sensor.ll_w", None, 0.5, t0, t0 + 14400)  # noqa: SLF001
        by_room = c._air_backfill_sync("climate.ll", "sensor.ll_w", "sensor.rum_3", 0.5, t0, t0 + 14400)  # noqa: SLF001
    # enhedens egen temperatur: 21,0 − 21,0 = 0,0 og 20,0 − 22,0 = −2,0; de første 15 min. efter et skift tæller ikke
    assert sorted(own) == ["High +0.0", "HighMid +-2.0"] and len(own["High +0.0"]) == 21 and set(own["HighMid +-2.0"]) == {320.0}
    # rummets føler: 21,0 − 22,0 = −1,0 og 20,0 − 21,6 = −1,6 → −1,5
    assert sorted(by_room) == ["High +-1.0", "HighMid +-1.5"]
    # virkningen: samme navne (her uden rum med model – så ingen målinger, men trinene findes)
    with patch.object(co.history, "get_significant_states", return_value={"climate.ll": clim, "sensor.rum_3": room}), \
            patch.object(co, "statistics_during_period", return_value=stats):
        eff, stat = c._air_eff_backfill_sync("climate.ll", "sensor.ll_w", [], "sensor.rum_3", 0.5, "sensor.ude", None, None, None,  # noqa: SLF001
                                             t0, t0 + 14400, [], None, ["Mid", "HighMid", "High"])
    assert eff == {} and stat["kvarterer"] > 0 and stat["for_kort"] > 0


async def test_calibration_after_an_update_survives_a_reload(ha: HomeAssistant) -> None:
    """Efter en opdatering kalibreres der ved opstart. Genindlæses indgangen, før det er sket (fx en ændret indstilling),
    skal kalibreringen stadig køre: den gemte udgave er den, tallene sidst blev regnet med – ikke den, der sidst startede."""
    c, _s = await _lv_entry(ha)
    assert c._sw_cal == c._sw  # noqa: SLF001 – ny installation: intet at indhente
    c._sw_cal = "0.0.1"  # noqa: SLF001 – tallene er regnet med en ældre udgave
    assert c._data()["sw"] == "0.0.1"  # noqa: SLF001 – og det bliver stående, selv om der gemmes inden kalibreringen
    await c.async_calibrate()
    assert c._sw_cal == c._sw and c._data()["sw"] == c._sw  # noqa: SLF001


async def test_air_ladder_has_a_lowest_setpoint(ha: HomeAssistant) -> None:
    """Stigen har et laveste setpunkt – modstykket til det højeste: brugerens valg, men aldrig under det højeste gulv blandt
    de rum, kilden betjener (der skal mindst være så varmt ved enheden, som rummene skal holde). Ligger trinnets eget
    setpunkt under det (føleren ved enheden er kold, fx efter en lang blokering), holdes setpunktet på det laveste. Trinnet
    er det samme, og når føleren er nået op, tager trinnets eget setpunkt over. Det gælder altid og for alle prioriteter."""
    from types import SimpleNamespace

    from custom_components.varmemodel.planner import PlanRoom

    c, s, air = await _air_ref_entry(ha)
    p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [18.0] * 6})
    cold = [PlanRoom("Loft", 18.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    ok = [PlanRoom("Loft", 19.6, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    low = next(x for x in ha.states.async_all("select") if "luft_luft" in x.entity_id and x.attributes.get("options", [None])[0] == "19.5")
    assert low.state == "19.5" and low.attributes["options"][-1] == "30.0"  # ikke lavere end rummets gulv
    ha.states.async_set("climate.ll", "off", {**LL_ATTRS, "current_temperature": 18.0, "temperature": 20, "fan_mode": "Mid"})
    t = time.time()
    d = c._air_step(s, "climate.ll", p, cold, True, False, t)  # noqa: SLF001
    v = c.air[air]["view"]
    # føleren 18,0 °C → trinnets eget setpunkt ville være 16,5 °C; men rummet skal holde 19,5 °C: stigen starter ved det
    # laveste setpunkt, og spændet regnes derfra (19,5–22,5) – ingen kopier af samme setpunkt og blæser
    tl = v["trin_liste"]
    assert tl[0]["setpunkt"] == 19.5 and tl[-1]["setpunkt"] == 22.5 and v["laveste"] == 19.5
    pairs = [(x["blæser"], x["setpunkt"]) for x in tl]
    assert len(pairs) == len(set(pairs))
    assert d["setpoint"] >= 19.5 and v["løftet_til_laveste"] is None
    # varmepumpen står sådan nu: forbruget læres på det niveau, der køres
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 18.0, "temperature": 19.5, "fan_mode": "High"})
    c._air_step(s, "climate.ll", p, cold, True, False, t + 60)  # noqa: SLF001
    assert c.air[air]["view"]["kører_nu"] == "High +1.5"
    # det gælder også, når rummet er over sit gulv – det laveste setpunkt er en grænse, ikke en nødregel
    d = c._air_step(s, "climate.ll", p, ok, True, False, t + 120)  # noqa: SLF001
    assert d["setpoint"] >= 19.5 and c.air[air]["view"]["trin_liste"][0]["setpunkt"] == 19.5
    # føleren er nået op over det laveste setpunkt + 1,5 °C: trinnets eget setpunkt
    ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.6, "temperature": 19.5, "fan_mode": "High"})
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 180)  # noqa: SLF001
    assert c.air[air]["view"]["trin_liste"][0]["setpunkt"] == 20.0 and c.air[air]["view"]["løftet_til_laveste"] is None
    # brugeren kan sætte det højere (fx når et fjernt rum kræver varmere luft ved enheden) …
    await ha.services.async_call("select", "select_option", {"entity_id": low.entity_id, "option": "20.5"}, blocking=True)
    await ha.async_block_till_done()
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 240)  # noqa: SLF001
    assert d["setpoint"] == 20.5 and c.air[air]["view"]["laveste"] == 20.5
    # … men aldrig under rummenes gulv, og aldrig over stigens højeste setpunkt
    c.settings[f"{air}.air_sp_low"] = "17.0"
    assert c.air_setting(s, "air_sp_low") == "19.5"
    c.settings[f"{air}.air_sp_low"], c.settings[f"{air}.air_sp_top"] = "23.0", "21.0"
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 300)  # noqa: SLF001
    assert d["setpoint"] == 21.0
    # økonomi: samme grænse (prioriteten bestemmer tempoet, ikke hvad der er fysisk muligt)
    c.settings.pop(f"{air}.air_sp_low"); c.settings.pop(f"{air}.air_sp_top")
    c.settings[f"{air}.air_priority"] = "economy"
    ha.states.async_set("climate.ll", "off", {**LL_ATTRS, "current_temperature": 18.0, "temperature": 20, "fan_mode": "Mid"})
    c.air[air].update(on=False, rung=None)
    d = c._air_step(s, "climate.ll", p, cold, True, False, t + 360)  # noqa: SLF001
    assert d["setpoint"] == 19.5 and c.air[air]["view"]["trin_liste"][0]["setpunkt"] == 19.5


async def test_ventilation_follows_night_and_day_and_room_need(ha: HomeAssistant) -> None:
    """Ventilationen, der fordeler varmen: kører, når et af dens rum er under gulvet og føleren ved enheden er varmere end
    det koldeste rum (1 °C for at starte); højeste hastighed efter kildens nat/dag (»Ventilation maks. dag/nat«);
    slukket i blokerede timer; skifter højst hvert 5. minut. Varmemodel skriver kun, når kontakten er slået til."""
    from types import SimpleNamespace

    c, s, air = await _air_ref_entry(ha)
    ha.states.async_set("select.vent_mode", "Off", {"options": ["Auto", "Off", "On"]})
    ha.states.async_set("number.vent_speed", "1", {"min": 0, "max": 10})
    s.conf["assist_fan"], s.conf["assist_fan_speed"] = "select.vent_mode", "number.vent_speed"
    calls: list = []
    ha.services.async_register("select", "select_option", lambda call: calls.append(("mode", call.data["option"])))
    ha.services.async_register("number", "set_value", lambda call: calls.append(("speed", call.data["value"])))
    assert c.air_options(s, "air_vent_rooms") == ["Loft + Reposen", "Loft", "Reposen"]
    assert c.air_options(s, "air_vent_day") == [str(k) for k in range(1, 11)]
    assert c.air_setting(s, "air_vent_day") == "10" and c.air_setting(s, "air_vent_night") == "1"
    c.settings[f"{air}.air_vent_rooms"], c.settings[f"{air}.air_vent_day"] = "Loft", "4"
    room = lambda t: [SimpleNamespace(name="Loft", t_now=t, t_min=19.5)]  # noqa: E731
    a: dict = {}
    now = time.time()
    # under gulvet, gangen 1,5 °C varmere: til – dagens maks.
    v = c._vent_decide(s, a, room(19.0), {}, False, False, False, 20.5, now)  # noqa: SLF001
    assert v["til"] and v["maks"] == "4" and not v["styrer"]
    # nat: nattens maks.
    assert c._vent_decide(s, a, room(19.0), {}, False, False, True, 20.5, now)["maks"] == "1"  # noqa: SLF001
    # gangen ikke varm nok, rummet over gulvet, blokeret: fra
    assert not c._vent_decide(s, a, room(19.0), {}, False, False, False, 19.5, now)["til"]  # noqa: SLF001
    assert not c._vent_decide(s, a, room(19.9), {}, False, False, False, 21.0, now)["til"]  # noqa: SLF001
    assert not c._vent_decide(s, a, room(19.0), {}, True, False, False, 21.0, now)["til"]  # noqa: SLF001
    assert c._vent_decide(s, a, room(19.0), {}, True, True, False, 21.0, now)["til"]  # noqa: SLF001 – jeg fryser
    # forvarmning før en blokering: rummet skal også op over gulvet + forvarmningen
    assert c._vent_decide(s, a, room(19.9), {"Loft": 0.6}, False, False, False, 21.0, now)["til"]  # noqa: SLF001
    # skrives: til og hastighed
    await c._vent_apply(s, {"til": True, "maks": "4"})  # noqa: SLF001
    await ha.async_block_till_done()
    assert ("speed", 4.0) in calls and ("mode", "On") in calls
    # lige skiftet: et nyt skift venter
    ha.states.async_set("select.vent_mode", "On", {"options": ["Auto", "Off", "On"]})
    ha.states.async_set("number.vent_speed", "4", {"min": 0, "max": 10})
    v = c._vent_decide(s, c.air[air], room(19.9), {}, False, False, False, 21.0, time.time())  # noqa: SLF001
    assert v["til"] and "venter" in v["grund"]


async def test_air_start_with_all_rooms_below_the_floor_goes_straight_to_the_top_fan(ha: HomeAssistant) -> None:
    """Efter en lang blokering, når alle rum er under gulvet: starten går direkte på højeste blæser (powerful, når den er
    tilladt) på stigens laveste temperatur – også i økonomi."""
    from types import SimpleNamespace

    from custom_components.varmemodel.planner import PlanRoom

    c, s, air = await _air_ref_entry(ha)
    c.settings[f"{air}.air_priority"] = "economy"
    c.settings[f"{air}.air_fan_day"] = c.settings[f"{air}.air_fan_night"] = "powerful"
    p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [18.0] * 6})
    cold = [PlanRoom("Loft", 18.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    pw = {**LL_ATTRS, "preset_modes": ["none", "powerful"]}
    ha.states.async_set("climate.ll", "off", {**pw, "current_temperature": 18.0, "temperature": 20, "fan_mode": "Mid"})
    d = c._air_step(s, "climate.ll", p, cold, True, False, time.time())  # noqa: SLF001
    tl = c.air[air]["view"]["trin_liste"]
    assert d["step"] == next(k for k, x in enumerate(tl) if x["blæser"] == "powerful")
    assert d["preset"] == "powerful" and d["setpoint"] == tl[0]["setpunkt"]
    assert "starter på højeste blæser" in c.air[air]["view"]["grund"]
    # uden powerful på varmepumpen: højeste blæser
    c.air[air].update(on=False, rung=None)
    ha.states.async_set("climate.ll", "off", {**LL_ATTRS, "current_temperature": 18.0, "temperature": 20, "fan_mode": "Mid"})
    d = c._air_step(s, "climate.ll", p, cold, True, False, time.time())  # noqa: SLF001
    assert d["fan_mode"] == "High" and not d.get("preset") and all(x["blæser"] != "powerful" for x in c.air[air]["view"]["trin_liste"])


async def test_powerful_only_while_a_room_is_below_the_floor(ha: HomeAssistant) -> None:
    """Står stigen på powerful, og er intet rum under gulvet, går den straks til højeste blæser på samme temperatur."""
    from types import SimpleNamespace

    from custom_components.varmemodel.planner import PlanRoom

    c, s, air = await _air_ref_entry(ha)
    c.settings[f"{air}.air_priority"] = "economy"
    c.settings[f"{air}.air_fan_day"] = c.settings[f"{air}.air_fan_night"] = "powerful"
    pw = {**LL_ATTRS, "preset_modes": ["none", "powerful"]}
    p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [18.0] * 6})
    cold = [PlanRoom("Loft", 18.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    warm = [PlanRoom("Loft", 20.0, 19.5, 0.7, 0.0, 0.02, margin=0.3, t_max=22.5)]
    ha.states.async_set("climate.ll", "off", {**pw, "current_temperature": 18.0, "temperature": 20, "fan_mode": "Mid"})
    t = time.time()
    d = c._air_step(s, "climate.ll", p, cold, True, False, t)  # noqa: SLF001
    assert d["preset"] == "powerful"
    d = c._air_step(s, "climate.ll", p, warm, True, False, t + 60)  # noqa: SLF001
    assert d.get("preset") != "powerful" and d["fan_mode"] == "High" and "powerful slås fra" in c.air[air]["view"]["grund"]


async def test_economy_keeps_trying_a_lower_step(ha: HomeAssistant) -> None:
    """Økonomi går et trin ned, når alle rum er over målet og bliver ved med at være det (målt tendens) – også selv om
    overskuddet er mindre end rummodellens usikkerhed. Balance venter på usikkerheden."""
    from types import SimpleNamespace

    from custom_components.varmemodel.air_control import DOWN_GAP, DOWN_HOLD
    from custom_components.varmemodel.planner import PlanRoom

    for prio, goes_down in (("economy", True), ("balance", False)):
        c, s, air = await _air_ref_entry(ha)
        c.settings[f"{air}.air_priority"] = prio
        p = SimpleNamespace(hours=[{"blocked": False, "t_out": 2.0, "el_kw": None} for _ in range(6)], rooms={"Loft": [20.0] * 6})
        ok = [PlanRoom("Loft", 20.0, 19.5, 0.7, 0.0, 0.02, margin=0.7, t_max=22.5)]  # 0,5 over gulvet, usikkerhed 0,7
        ha.states.async_set("climate.ll", "heat", {**LL_ATTRS, "current_temperature": 21.0, "temperature": 20, "fan_mode": "High"})
        c._air_rise[air] = {"Loft": 0.0}  # noqa: SLF001
        p.hours[0]["el_kw"] = 5.0  # planens el-mål: stigens top – i økonomi ingen bund, når rummene er over målet
        t = time.time()
        first = c._air_step(s, "climate.ll", p, ok, True, False, t)["step"]  # noqa: SLF001
        c.air[air]["changed"] = t - DOWN_GAP - 10
        c._air_step(s, "climate.ll", p, ok, True, False, t + 1)  # noqa: SLF001
        d = c._air_step(s, "climate.ll", p, ok, True, False, t + DOWN_HOLD + 5)  # noqa: SLF001
        assert (d["step"] < first) is goes_down, prio
        await ha.config_entries.async_unload(c.entry.entry_id)
