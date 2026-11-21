"""Varmemodel: regner husets, rummenes og varmekildernes tal ud af dine egne målinger."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_LEGACY_IDS,
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
PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR]

type VarmemodelConfigEntry = ConfigEntry[VarmemodelCoordinator]


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


async def async_setup_entry(hass: HomeAssistant, entry: VarmemodelConfigEntry) -> bool:
    coordinator = VarmemodelCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
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

