"""Diagnostik: kalibreringen og anonymiserede timetal (relative tider, ingen entitets-id'er)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import VarmemodelConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data

    def r(v: float | None, n: int = 2) -> float | None:
        return None if v is None else round(v, n)

    out: dict[str, Any] = {
        "house_roles": sorted(k for k, v in {**entry.data, **entry.options}.items() if v and k not in ("name", "migrated_source")),
        "rooms": len(c.rooms_cfg),
        "mode": c.mode,
        "status": c.status,
        "runs": c.runs[-10:],
        "rooms_fit": {f"rum_{i + 1}": c.rooms.get(rm["key"]) for i, rm in enumerate(c.rooms_cfg)},
        "sources": {},
    }
    for i, s in enumerate(c.sources.values()):
        t0 = s.last_hours[-1].ts if s.last_hours else 0
        out["sources"][f"kilde_{i + 1}"] = {
            "type": s.kind,
            "roles": sorted(k for k, v in s.conf.items() if v and k not in ("name", "legacy_ids")),
            "accepted": s.accepted,
            "fits": s.fits,
            # Timer med tid som "timer før seneste time" – kan bruges som testdata uden personlige oplysninger
            "hours": [
                {"h": (h.ts - t0) // 3600, "q": r(h.q), "e": r(h.e), "t_out": r(h.t_out, 1), "t_in": r(h.t_in, 1),
                 "t_flow": r(h.t_flow, 1), "t_ret": r(h.t_ret, 1), "comp": r(h.comp, 0), "dhw": r(h.dhw, 0),
                 "defrost": h.defrost, "setpoint": r(h.setpoint, 1), "offset": r(h.offset, 1)}
                for h in s.last_hours
            ],
        }
    return out
