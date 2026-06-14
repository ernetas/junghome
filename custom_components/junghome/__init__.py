import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, datapoint_suffix, device_slug
from .coordinator import JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "switch", "sensor", "event"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Jung Home integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jung Home from a config entry."""
    host = entry.data.get("host")
    token = entry.data.get("token")

    # Initialize the coordinator with the host and token
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": host, "token": token}, entry
    )

    # Fetch initial data; raises ConfigEntryNotReady (retry) if the gateway is
    # unreachable, or ConfigEntryAuthFailed (reauth) if the token is rejected.
    await coordinator.async_config_entry_first_refresh()

    # Store the coordinator in hass data for later use
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}

    # Connect to the WebSocket for live updates
    await coordinator.start()

    # One-time migration of registry entries from the gateway's volatile device
    # ids to firmware-stable, label-based ids. Must run while the gateway's ids
    # still match the registry (i.e. before the platforms create new entities).
    if not entry.data.get("stable_ids_migrated"):
        _migrate_to_stable_ids(hass, entry, coordinator)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "stable_ids_migrated": True}
        )

    # Forward the setup to the appropriate platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _migrate_to_stable_ids(
    hass: HomeAssistant, entry: ConfigEntry, coordinator
) -> None:
    """
    Re-point existing id-based registry entries to label-based stable ids.

    The Jung HOME gateway exposes no hardware identifier, and it regenerates the
    random device id on firmware updates, which previously caused Home Assistant
    to create duplicate entities/devices (the old ones left greyed-out). This maps
    the currently-registered entries onto the new stable scheme so existing
    automations keep working and future firmware updates stop creating duplicates.
    """
    try:
        data = coordinator.data or []
        by_device = {}
        by_datapoint = {}
        for device in data:
            dev_id = device.get("id")
            if dev_id:
                by_device[dev_id] = device
            for dp in device.get("datapoints", []):
                dp_id = dp.get("id")
                if dp_id:
                    by_datapoint[dp_id] = device

        ent_reg = er.async_get(hass)
        migrated = 0
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            old_uid = entity.unique_id
            new_uid = None
            for dp_id, device in by_datapoint.items():
                prefix = f"{device['id']}_{dp_id}"
                if old_uid == prefix or old_uid.startswith(prefix + "_"):
                    trailing = old_uid[
                        len(prefix) :
                    ]  # "", "_switch", "_event", "_<label>"
                    new_uid = (
                        f"{device_slug(device)}_{datapoint_suffix(dp_id)}{trailing}"
                    )
                    break
            if not new_uid or new_uid == old_uid:
                continue
            existing = ent_reg.async_get_entity_id(entity.domain, DOMAIN, new_uid)
            if existing and existing != entity.entity_id:
                # A stable-id entity already exists (e.g. a leftover duplicate from
                # a previous firmware update); drop the stale entry rather than
                # collide on the new unique id.
                ent_reg.async_remove(entity.entity_id)
            else:
                ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_uid)
            migrated += 1

        dev_reg = dr.async_get(hass)
        for device_entry in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            new_identifiers = set()
            changed = False
            for domain, identifier in device_entry.identifiers:
                if domain == DOMAIN and identifier in by_device:
                    new_identifiers.add((DOMAIN, device_slug(by_device[identifier])))
                    changed = True
                else:
                    new_identifiers.add((domain, identifier))
            if changed:
                dev_reg.async_update_device(
                    device_entry.id, new_identifiers=new_identifiers
                )

        _LOGGER.info("Jung Home: migrated %s entities to firmware-stable ids", migrated)
    except Exception:
        _LOGGER.exception("Jung Home: failed to migrate registry to stable ids")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok and DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.stop()
        del hass.data[DOMAIN][entry.entry_id]

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload a config entry."""
    await async_unload_entry(hass, entry)
    return await async_setup_entry(hass, entry)
