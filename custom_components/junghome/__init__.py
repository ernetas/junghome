"""The Jung Home integration."""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    DATA_AREA_ASSIGNED,
    DOMAIN,
    datapoint_suffix,
    device_slug,
    gateway_device_id,
    gateway_device_info,
)
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.COVER,
    Platform.CLIMATE,
    Platform.SCENE,
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

    # Fetch room groups before the platforms create entities, so each device can
    # suggest its area at creation time. Best-effort: never blocks setup.
    await coordinator.async_fetch_groups()

    # One-time migration of registry entries from the gateway's volatile device
    # ids to firmware-stable, label-based ids. Must run while the gateway's ids
    # still match the registry (i.e. before the platforms create new entities).
    if not entry.data.get("stable_ids_migrated"):
        if _migrate_to_stable_ids(hass, entry, coordinator):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "stable_ids_migrated": True}
            )

    # Register the synthetic gateway (hub) device up front, before the platforms
    # create the per-function devices that link to it via ``via_device``. Creating
    # it here rather than lazily (via the connectivity sensor) guarantees it
    # already exists when those devices reference it, so Home Assistant never
    # takes its deprecated "non existing via_device" path.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **gateway_device_info(entry, coordinator.gateway_version),
    )

    # Forward the setup to the appropriate platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Connect to the WebSocket only after the platforms exist, so live pushes
    # (datapoint updates, scene broadcasts/recalls) flow to registered entities
    # rather than being dispatched into the void during setup. The initial state
    # already came from async_config_entry_first_refresh() above.
    await coordinator.start()

    # Prune HA devices the gateway no longer reports (quality-scale stale-devices).
    @callback
    def _prune_stale_devices() -> None:
        if not coordinator.data:
            return  # don't prune on an empty/failed poll
        current = {device_slug(d) for d in coordinator.data}
        # The synthetic gateway (hub) device never appears in the device list, so
        # keep it in the live set or it would be pruned on every refresh.
        current.add(gateway_device_id(entry))
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

    # Place devices in the Home Assistant area matching their gateway group.
    @callback
    def _assign_areas() -> None:
        """Auto-place devices that have no area yet, once each.

        Runs after the platforms have created their devices (and again on each
        refresh, so devices added at runtime are placed too). Two rules keep
        this from ever disturbing an existing setup:

        1. a device is placed only if it currently has **no** area, so an area
           the user chose is never overwritten; and
        2. every device is considered exactly **once** — the decision is
           recorded in the entry so that a device whose area the user later
           cleared on purpose is not silently re-placed on the next refresh.

        Area lookup is by name, so a group matching an existing area links to
        it rather than creating a duplicate.
        """
        if not coordinator.data:
            return  # nothing to place on an empty/failed poll
        area_by_slug = {
            device_slug(device): room
            for device in coordinator.data
            if (room := coordinator.area_for_device(device))
        }
        if not area_by_slug:
            return  # gateway reports no rooms (or none could be resolved)

        considered = set(entry.data.get(DATA_AREA_ASSIGNED, []))
        dev_reg = dr.async_get(hass)
        area_reg = ar.async_get(hass)
        newly_considered: set[str] = set()
        for device_entry in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            for domain, slug in device_entry.identifiers:
                if domain != DOMAIN or slug in considered:
                    continue
                area_name = area_by_slug.get(slug)
                if area_name is None:
                    continue  # no room for this device; reconsider it later
                if device_entry.area_id is None:
                    area = area_reg.async_get_or_create(area_name)
                    dev_reg.async_update_device(device_entry.id, area_id=area.id)
                # Recorded either way: a device the user had already placed is
                # settled too, and must not be auto-placed if later cleared.
                newly_considered.add(slug)

        if newly_considered:
            # A data-only update; the reload listener below ignores it (it acts
            # on host/options changes), so this does not cause a reload loop.
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    DATA_AREA_ASSIGNED: sorted(considered | newly_considered),
                },
            )

    _assign_areas()
    entry.async_on_unload(coordinator.async_add_listener(_assign_areas))

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
    """Reload when the stored host or the entry options change.

    Registered as an update listener. The coordinator caches the host and an
    options snapshot at construction, so a change to either only takes effect
    after a reload (options drive cover inversion; the host drives the API/WS
    target). Guard on an actual change so reauth's token-only update (which
    already reloads via ``async_update_reload_and_abort``) doesn't trigger a
    redundant second reload.
    """
    coordinator = entry.runtime_data
    host_changed = coordinator.config.get("host") != entry.data.get("host")
    options_changed = coordinator.options_snapshot != dict(entry.options)
    if host_changed or options_changed:
        await hass.config_entries.async_reload(entry.entry_id)
