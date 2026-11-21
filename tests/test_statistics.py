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

    entry = MockConfigEntry(domain=DOMAIN, title="Varmepumpe", data={
        "name": "Varmepumpe", "electricity_energy": "sensor.vp_el", "heat_energy": "sensor.vp_varme",
        "outdoor_temp": "sensor.ude", "indoor_temps": ["sensor.rum_1"], "flow_temp": "sensor.fremlob",
    })
    entry.add_to_hass(ha)
    assert await ha.config_entries.async_setup(entry.entry_id)
    await ha.async_block_till_done()
    c = entry.runtime_data
    await c.async_calibrate()
    await ha.async_block_till_done()

    assert len(c.last_hours) >= len(hours) - 30
    assert abs(c.accepted["heat_loss"]["value"] - H_TRUE) < 6
    assert abs(c.accepted["cop"]["value"] - (A + C * 35)) < 0.1
    assert c.accepted["cop"]["extra"]["c_source"] == "fit"
    assert c.status in ("partial", "ok")
