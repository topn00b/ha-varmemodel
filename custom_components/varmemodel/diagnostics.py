"""Diagnostik: kalibreringen og anonymiserede timetal (relative tider, ingen entitets-id'er)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import VarmemodelConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    hours = c.last_hours
    t0 = hours[-1].ts if hours else 0

    def r(v: float | None, n: int = 2) -> float | None:
        return None if v is None else round(v, n)

    return {
        "configured_roles": sorted(k for k, v in {**entry.data, **entry.options}.items() if v and k not in ("name",)),
        "indoor_count": len(entry.data.get("indoor_temps") or entry.options.get("indoor_temps") or []),
        "mode": c.mode,
        "status": c.status,
        "accepted": c.accepted,
        "fits": c.fits,
        "runs": c.runs[-10:],
        # Timer med tid som "timer før seneste time" – kan bruges som testdata uden personlige oplysninger
        "hours": [
            {"h": (h.ts - t0) // 3600, "q": r(h.q), "e": r(h.e), "t_out": r(h.t_out, 1), "t_in": r(h.t_in, 1),
             "t_flow": r(h.t_flow, 1), "t_ret": r(h.t_ret, 1), "comp": r(h.comp, 0), "dhw": r(h.dhw, 0),
             "defrost": h.defrost, "setpoint": r(h.setpoint, 1), "offset": r(h.offset, 1)}
            for h in hours
        ],
    }
