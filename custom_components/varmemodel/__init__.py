"""Varmemodel: regner husets, rummenes og varmekildernes tal ud af dine egne målinger."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_LEGACY_IDS,
    CONF_NAME,
    DEFAULT_NAME,
    CONF_MIGRATED_SOURCE,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    DOMAIN,
    HYDRONIC_KEYS,
    SOURCE_HYDRONIC,
    SUBENTRY_SOURCE,
)
from .coordinator import VarmemodelCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.TIME]

type VarmemodelConfigEntry = ConfigEntry[VarmemodelCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARDS_URL = "/varmemodel/varmemodel-cards.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Gør Varmemodels kort (anlæg, rum, varmekilder, energitiltag, hus) tilgængelige i brugerfladen."""
    if getattr(hass, "http", None) is None:
        return True
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        path = Path(__file__).parent / "frontend" / "varmemodel-cards.js"
        version = (await hass.async_add_executor_job(_manifest_version))
        await hass.http.async_register_static_paths([StaticPathConfig(CARDS_URL, str(path), True)])
        add_extra_js_url(hass, f"{CARDS_URL}?v={version}")
    except Exception:  # noqa: BLE001 – kortene må aldrig stoppe integrationen
        _LOGGER.exception("Kunne ikke registrere Varmemodels kort")
        return True

    async def _resource(_event: object = None) -> None:
        try:
            await _ensure_resource(hass, f"{CARDS_URL}?v={version}")
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Kunne ikke tilføje Varmemodels kort som dashboard-ressource; tilføj %s manuelt", CARDS_URL)

    if hass.is_running:
        hass.async_create_task(_resource())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _resource)
    return True


async def _ensure_resource(hass: HomeAssistant, url: str) -> None:
    """Kortene som dashboard-ressource (gemt tilstand), så de også indlæses i mobilappen før dashboardet tegnes.

    Findes ressourcen i en anden version, opdateres den; dubletter fjernes."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    data = hass.data.get(LOVELACE_DATA)
    resources = getattr(data, "resources", None)
    if resources is None or not hasattr(resources, "async_create_item"):
        return  # dashboards i YAML-tilstand: ressourcen skal tilføjes manuelt
    await resources.async_get_info()
    mine = [r for r in resources.async_items() if str(r.get("url", "")).split("?")[0] == CARDS_URL]
    if not mine:
        await resources.async_create_item({"res_type": "module", "url": url})
        return
    if mine[0].get("url") != url:
        await resources.async_update_item(mine[0]["id"], {"res_type": "module", "url": url})
    for extra in mine[1:]:
        await resources.async_delete_item(extra["id"])


def _manifest_version() -> str:
    import json

    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")).get("version", "0")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 1 (én varmepumpe) → version 2 (huset med varmekilder som underpunkter).

    Varmepumpens felter flyttes til en ny varmekilde. Dens sensorer beholder deres
    entitets-id'er og historik (``legacy_ids``)."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        merged = {**entry.data, **entry.options}
        src = {k: merged[k] for k in HYDRONIC_KEYS if merged.get(k) not in (None, "", [])}
        house = {k: v for k, v in merged.items() if k not in HYDRONIC_KEYS}
        sub = ConfigSubentry(
            data={CONF_SOURCE_NAME: entry.title, CONF_SOURCE_TYPE: SOURCE_HYDRONIC, CONF_LEGACY_IDS: True, **src},
            subentry_type=SUBENTRY_SOURCE, title=entry.title, unique_id=None,
        )
        hass.config_entries.async_add_subentry(entry, sub)
        house[CONF_MIGRATED_SOURCE] = sub.subentry_id
        house["name"] = "Hus"
        # Opsætningen er nu huset; varmepumpen er et underpunkt med det gamle navn
        hass.config_entries.async_update_entry(entry, title="Hus", data=house, options={}, version=2)
        _LOGGER.info("Varmemodel: opsætning flyttet til version 2 – varmepumpen er nu varmekilden '%s'", entry.title)
    return True


def _remove_retired(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> None:
    """Fjern entiteter, som integrationen ikke længere laver.

    0.7.0: timemodellen for COP. 0.8.0: LG's varmekurve, planens kurve-forskydning og »fremløb over kurven«.
    0.9.1: luft-luft-setpunkter og -margener (de flyder nu). 0.10.0: forvarmning (rummodellerne forudser faldet selv).
    0.29.0: luft-luft »Opstart« (afløst af kildens prioritet) og »Maks. fremløb over det lærte« (planen hæver selv efter
    prioriteten).
    """
    reg = er.async_get(hass)
    for e in er.async_entries_for_config_entry(reg, entry.entry_id):
        if (e.domain == "sensor" and e.unique_id.endswith(("_cop", "_curve", "_plan_shift"))) or (
                e.domain == "number" and e.unique_id.endswith(("_flow_over_curve", "_preload", "_air_sp_min", "_air_sp_max", "_air_sp_step",
                                                                "_air_down_margin", "_air_preload_max", "_flow_margin"))) or (
                e.domain == "select" and e.unique_id.endswith("_air_start")):
            reg.async_remove(e.entity_id)


def _ensure_devices(hass: HomeAssistant, entry: VarmemodelConfigEntry, coordinator: VarmemodelCoordinator) -> None:
    """Enhedstræet huset → varmekilde → rummene den varmer, bygget med enheds-id'er (via_device_id).

    Forældrene oprettes før børnene, så entiteterne kan pege på dem uden det udfasede via_device."""
    reg = dr.async_get(hass)
    svc = dr.DeviceEntryType.SERVICE
    house = reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)},
                                    name=entry.title or entry.data.get(CONF_NAME) or DEFAULT_NAME, manufacturer="Varmemodel",
                                    model="Hus", entry_type=svc)
    ids: dict[str | None, str] = {None: house.id}
    for s in coordinator.sources.values():
        d = reg.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=s.sid, identifiers={(DOMAIN, f"{entry.entry_id}_{s.sid}")},
                                    name=s.name, manufacturer="Varmemodel", model="Luft-vand" if s.kind == SOURCE_HYDRONIC else "Luft-luft",
                                    entry_type=svc, via_device_id=house.id)
        ids[s.sid] = d.id
    for r in coordinator.rooms_cfg:
        if not (sub := r.get("subentry_id")):
            continue
        src = coordinator.room_source(r)
        d = reg.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=sub, identifiers={(DOMAIN, f"{entry.entry_id}_{sub}")},
                                    name=r.get("name"), manufacturer="Varmemodel", model="Rum", entry_type=svc,
                                    via_device_id=ids.get(src.sid, house.id) if src is not None else house.id)
        ids[sub] = d.id
    coordinator.device_ids = ids


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> bool:
    coordinator = VarmemodelCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    _ensure_devices(hass, entry, coordinator)
    _remove_retired(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> None:
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device) -> bool:
    """Enheder for slettede rum og varmekilder må fjernes i brugerfladen."""
    return True

