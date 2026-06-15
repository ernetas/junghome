"""The Jung Home integration."""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, datapoint_suffix, device_slug
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.EVENT,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Jung Home integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JungHomeConfigEntry) -> bool:
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

    # Expose the coordinator as runtime data for the platforms.
    entry.runtime_data = coordinator

    # Connect to the WebSocket for live updates
    await coordinator.start()

    # One-time migration of registry entries from the gateway's volatile device
    # ids to firmware-stable, label-based ids. Must run while the gateway's ids
    # still match the registry (i.e. before the platforms create new entities).
    if not entry.data.get("stable_ids_migrated"):
        if _migrate_to_stable_ids(hass, entry, coordinator):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "stable_ids_migrated": True}
            )

    # Forward the setup to the appropriate platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Prune HA devices the gateway no longer reports (quality-scale stale-devices).
    @callback
    def _prune_stale_devices() -> None:
        if not coordinator.data:
            return  # don't prune on an empty/failed poll
        current = {device_slug(d) for d in coordinator.data}
        dev_reg = dr.async_get(hass)
        for device_entry in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            slugs = {
                identifier
                for domain, identifier in device_entry.identifiers
                if domain == DOMAIN
            }
            if slugs and not (slugs & current):
                dev_reg.async_update_device(
                    device_entry.id, remove_config_entry_id=entry.entry_id
                )

    _prune_stale_devices()
    entry.async_on_unload(coordinator.async_add_listener(_prune_stale_devices))

    # Register the host-change reload listener only AFTER the migration's
    # async_update_entry flag-write above, so that write doesn't trigger a
    # mid-setup reload.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _migrate_to_stable_ids(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    coordinator: JungHomeDataUpdateCoordinator,
) -> bool:
    """Re-point existing id-based registry entries to label-based stable ids.

    The Jung HOME gateway exposes no hardware identifier, and it regenerates the
    random device id on firmware updates, which previously caused Home Assistant
    to create duplicate entities/devices (the old ones left greyed-out). This maps
    the currently-registered entries onto the new stable scheme so existing
    automations keep working and future firmware updates stop creating duplicates.

    Returns ``True`` on clean completion and ``False`` if any item (or the whole
    pass) failed, so the caller only marks the migration done when it fully
    succeeded. The body is idempotent (``new_uid == old_uid`` is skipped), so a
    re-run on the next setup is safe; per-item errors are isolated so one bad
    entity/device doesn't abort the rest of the batch.
    """
    had_error = False
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
            try:
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
                    # A stable-id entity already exists (e.g. a leftover duplicate
                    # from a previous firmware update); drop the stale entry rather
                    # than collide on the new unique id.
                    ent_reg.async_remove(entity.entity_id)
                else:
                    ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_uid)
                migrated += 1
            except Exception:
                had_error = True
                _LOGGER.exception(
                    "Jung Home: failed to migrate entity %s to a stable id",
                    entity.entity_id,
                )

        dev_reg = dr.async_get(hass)
        for device_entry in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            try:
                new_identifiers = set()
                changed = False
                for domain, identifier in device_entry.identifiers:
                    if domain == DOMAIN and identifier in by_device:
                        new_identifiers.add(
                            (DOMAIN, device_slug(by_device[identifier]))
                        )
                        changed = True
                    else:
                        new_identifiers.add((domain, identifier))
                if changed:
                    dev_reg.async_update_device(
                        device_entry.id, new_identifiers=new_identifiers
                    )
            except Exception:
                had_error = True
                _LOGGER.exception(
                    "Jung Home: failed to migrate device %s to a stable id",
                    device_entry.id,
                )

        _LOGGER.info("Jung Home: migrated %s entities to firmware-stable ids", migrated)
    except Exception:
        _LOGGER.exception("Jung Home: failed to migrate registry to stable ids")
        return False
    return not had_error


async def async_unload_entry(hass: HomeAssistant, entry: JungHomeConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # stop() is idempotent; call it unconditionally so a failed platform unload
    # doesn't leak the WebSocket reconnect loop.
    await entry.runtime_data.stop()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: JungHomeConfigEntry) -> None:
    """Reload when the stored host changes (e.g. zeroconf re-discovery).

    Registered as an update listener. The coordinator caches the host at
    construction, so a host change in the stored data only takes effect after a
    reload. Guard on an actual host change so reauth's token-only update (which
    already reloads via ``async_update_reload_and_abort``) doesn't trigger a
    redundant second reload.
    """
    coordinator = entry.runtime_data
    if coordinator.config.get("host") != entry.data.get("host"):
        await hass.config_entries.async_reload(entry.entry_id)
