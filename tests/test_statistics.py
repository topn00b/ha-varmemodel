"""Hele vejen: timestatistik i databasen → kalibrering → sensorer."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.varmemodel.const import DOMAIN

from .test_calibration import A, C, H_TRUE, synth


def _meta(sid: str, unit: str, has_sum: bool, unit_class: str) -> dict:
    return {
        "mean_type": StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC,
        "has_sum": has_sum,
        "name": None,
        "source": "recorder",
        "statistic_id": sid,
        "unit_class": unit_class,
        "unit_of_measurement": unit,
    }


async def test_calibration_from_recorder_statistics(ha: HomeAssistant) -> None:
    hours = synth(days=50)
    for h in hours:  # større hus: timer uden kompressordata skal have ≥ 2,5 kWh for at tælle
        h.q *= 3
        h.e *= 3
    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    start = end - timedelta(hours=len(hours) - 1)
    q_sum = e_sum = 0.0
    qs, es, outs, ins, flows = [], [], [], [], []
    for i, h in enumerate(hours):
        t = start + timedelta(hours=i)
        q_sum += h.q
        e_sum += h.e
        qs.append({"start": t, "sum": q_sum, "state": q_sum})
        es.append({"start": t, "sum": e_sum, "state": e_sum})
        outs.append({"start": t, "mean": h.t_out, "min": h.t_out, "max": h.t_out})
        ins.append({"start": t, "mean": h.t_in, "min": h.t_in, "max": h.t_in})
        flows.append({"start": t, "mean": h.t_flow, "min": h.t_flow, "max": h.t_flow})
    async_import_statistics(ha, _meta("sensor.vp_varme", "kWh", True, "energy"), qs)
    async_import_statistics(ha, _meta("sensor.vp_el", "kWh", True, "energy"), es)
    async_import_statistics(ha, _meta("sensor.ude", "°C", False, "temperature"), outs)
    async_import_statistics(ha, _meta("sensor.rum_1", "°C", False, "temperature"), ins)
    async_import_statistics(ha, _meta("sensor.fremlob", "°C", False, "temperature"), flows)
    await async_wait_recording_done(ha)

    from homeassistant.config_entries import ConfigSubentryData

    entry = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={
        "name": "Hus", "outdoor_temp": "sensor.ude", "indoor_temps": ["sensor.rum_1"]}, subentries_data=[ConfigSubentryData(
        data={"name": "Luft-vand", "source_type": "hydronic", "electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme",
              "flow_temp": "sensor.fremlob"}, subentry_type="source", title="Luft-vand", unique_id=None)])
    entry.add_to_hass(ha)
    assert await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()
    c = entry.runtime_data
    await c.async_calibrate()
    await ha.async_block_till_done()

    s = c.hydronic
    assert len(s.last_hours) >= len(hours) - 30
    assert abs(s.accepted["heat_loss"]["value"] - 3 * H_TRUE) < 15
    # Timemodellen for COP er fjernet (0.7.0): kun stabil drift + ekstra el
    assert "cop" not in s.accepted
    assert c.status in ("partial", "ok")



async def test_backup_meter_scaled_where_main_missing(ha: HomeAssistant) -> None:
    """Den supplerende elmåler bruges, hvor hovedmåleren mangler, skaleret med forholdet i overlappet."""
    from homeassistant.config_entries import ConfigSubentryData

    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    n = 24 * 10
    start = end - timedelta(hours=n - 1)
    main, bak, outs = [], [], []
    ms = bs = 0.0
    for i in range(n):
        t = start + timedelta(hours=i)
        bs += 0.4  # skyen: 0,4 kWh i timen
        bak.append({"start": t, "sum": bs, "state": bs})
        outs.append({"start": t, "mean": 5.0, "min": 5.0, "max": 5.0})
        if i >= n - 24 * 4:  # hovedmåleren kun de sidste 4 døgn og 25 % højere
            ms += 0.5
            main.append({"start": t, "sum": ms, "state": ms})
    async_import_statistics(ha, _meta("sensor.ll_el", "kWh", True, "energy"), main)
    async_import_statistics(ha, _meta("sensor.ll_sky", "kWh", True, "energy"), bak)
    async_import_statistics(ha, _meta("sensor.ude", "°C", False, "temperature"), outs)
    await async_wait_recording_done(ha)
    entry = MockConfigEntry(domain=DOMAIN, title="Hus", version=2, data={"name": "Hus", "outdoor_temp": "sensor.ude"}, subentries_data=[
        ConfigSubentryData(data={"name": "LL", "source_type": "air_air", "electricity_energy": "sensor.ll_el",
                                 "electricity_backup": "sensor.ll_sky"}, subentry_type="source", title="LL", unique_id=None)])
    entry.add_to_hass(ha)
    assert await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()
    c = entry.runtime_data
    hours = await ha.async_add_executor_job(c._hours_sync)
    s = next(iter(c.sources.values()))
    assert abs(s.el_backup["factor"] - 1.25) < 0.01
    early = [h for h in hours[s.sid] if h.e is not None][:24]
    assert all(abs(h.e - 0.5) < 0.01 for h in early)
