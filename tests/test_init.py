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
    await c.async_replan()
    await ha.async_block_till_done()
    step = next(s for s in ha.states.async_all("sensor") if s.entity_id.endswith("ladder_step") or s.entity_id.endswith("air_step"))
    assert step.state == "0" and len(step.attributes["trin_liste"]) == 1 + 3 + 6  # quiet, 3 blæsere, setpunkt 20,5–23
    assert not calls  # kun anbefaling, så længe kontakten er slået fra
    sel = {s.entity_id: s for s in ha.states.async_all("select")}
    day = next(s for k, s in sel.items() if k.endswith("max_fan_day"))
    assert day.state == "High" and day.attributes["options"] == ["Low", "Mid", "High"]
    sw = next(s for s in ha.states.async_all("switch") if s.entity_id.endswith("controls"))
    await ha.services.async_call("switch", "turn_on", {"entity_id": sw.entity_id}, blocking=True)
    await ha.async_block_till_done()
    assert ("hvac", "heat") in calls and ("preset", "quiet") in calls
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
    assert calls == [26.0]  # retur 22 + 4
    hs["start_t"] = time.time() - 1800  # to kvarterer inde
    ha.states.async_set("climate.vp_flow", "heat", {"temperature": 26, "min_temp": 20, "max_temp": 45})
    await c._flow_apply(s, hours, False)
    assert calls[-1] == 30.0


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
    assert order[:2] == [("flow", 26.0), ("mode", "heat")]  # retur 21,5 + 4 → 26 (trin 1), før tænding
    assert c.air[f"hydro:{s.sid}"]["soft_now"] == 26.0


async def _lv_entry(ha: HomeAssistant, extra_house: dict | None = None):
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
                        subentries_data=[_sub({**HYD, "flow_target": "climate.vp_flow", "return_temp": "sensor.retur"}, "source")])
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
    assert await c._low_flow(s, hs, 35.0) == 26.0  # retur 21,5 + 4 → 26 (afrundet)
    rooms[:] = [{"navn": "Gang", "margen": 0.2, "tendens": 0.0, "om_3t": 0.2}]
    assert await c._low_flow(s, hs, 35.0) == 26.0 and hs["low_status"] == "holder"
    rooms[:] = [{"navn": "Gang", "margen": 0.2, "tendens": -0.05, "om_3t": 0.05}]
    assert await c._low_flow(s, hs, 35.0) == 26.0 and hs["low_status"] == "hæver"  # venter en time
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
