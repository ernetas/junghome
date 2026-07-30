"""The Jung Home integration."""

import logging
from collections.abc import Callable

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
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
from .models import Device

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

# Consecutive polls a device must be absent from before it is pruned. The
# gateway occasionally returns a partial device list on a single poll (notably
# right after a reload); pruning on the first miss would delete a live device's
# entities — and because identity is label-derived, the platform would then
# re-create them under whatever label the next poll reports, losing the user's
# entity_id/customisations. Requiring persistence rides out a transient blip.
STALE_DEVICE_PRUNE_MISSES = 3


def _capability_signature(device: Device) -> tuple[str | None, frozenset[str]]:
    """Fingerprint a device's capabilities: its function type + datapoint types.

    The platforms freeze an entity's supported features from the datapoints
    present when it is first built (a cover's tilt, a light's brightness/colour,
    a thermostat's heating action), and ``_discover_*`` is add-only — it never
    revisits a device it has already created. So this fingerprint changing on an
    existing device means its entities now carry a stale feature set, and the entry
    must be reloaded to rebuild them. Only the datapoint *types* matter here (not their
    values), so a normal value push never changes the signature.
    """
    return (
        device.get("type"),
        frozenset(
            dp_type
            for dp in device.get("datapoints", [])
            if (dp_type := dp.get("type"))
        ),
    )


def _register_capability_reload(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    coordinator: JungHomeDataUpdateCoordinator,
) -> None:
    """Reload the entry when an existing device's capability fingerprint changes.

    The platforms compute supported features once, from the datapoints present at
    entity construction, and discovery never revisits a device it has already
    created — so if the gateway later starts or stops exposing a datapoint for an
    existing device, the live entity keeps its stale features. This happens when a
    firmware update re-enumerates a device and its datapoints arrive across
    several polls, or when a channel like a blind's slat tilt is toggled visible
    in the JUNG HOME app: the gateway then reports the cover as PositionAndAngle
    with an ``angle`` datapoint (rather than Position), but a cover entity built
    moments earlier without it stays position-only until reloaded. A reload
    rebuilds every entity from the current datapoint set. See
    ``_capability_signature`` for what counts as a change (datapoint types, not
    their values, so a routine value push never triggers a reload).
    """
    capability_signatures: dict[str, tuple[str | None, frozenset[str]]] = {}
    reload_scheduled = False

    @callback
    def _reload_on_capability_change() -> None:
        nonlocal reload_scheduled
        if reload_scheduled or not coordinator.data:
            return
        changed = False
        for device in coordinator.data:
            slug = device_slug(device)
            signature = _capability_signature(device)
            previous = capability_signatures.get(slug)
            capability_signatures[slug] = signature
            # A brand-new device is picked up by the platforms' own discovery and
            # a vanished one by the stale-device prune; only a change to a device
            # already fingerprinted leaves an entity with stale features.
            if previous is not None and previous != signature:
                changed = True
        if changed:
            reload_scheduled = True
            _LOGGER.info(
                "Jung Home: a device's datapoint set changed; reloading the entry "
                "to rebuild entity capabilities (e.g. cover tilt, light colour)"
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)

    # Seed the baseline from the current devices (the first pass never reloads),
    # then watch for changes on subsequent refreshes.
    _reload_on_capability_change()
    entry.async_on_unload(coordinator.async_add_listener(_reload_on_capability_change))


def _make_stale_device_pruner(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    coordinator: JungHomeDataUpdateCoordinator,
) -> Callable[[], None]:
    """Build the callback that prunes devices the gateway no longer reports.

    Returned as a closure over a per-device count of consecutive polls each
    device has been missing, so pruning is debounced: a device must be absent
    for ``STALE_DEVICE_PRUNE_MISSES`` polls before it is removed. A single
    partial poll (which the gateway occasionally returns, notably right after a
    reload) therefore no longer destroys a live device's entities.
    """
    missing_polls: dict[str, int] = {}

    @callback
    def _prune_stale_devices() -> None:
        # Device membership only changes on a REST poll; a WebSocket push merges
        # datapoint values into existing devices and never adds or removes one.
        # Skip push dispatches so the miss counter below tracks poll cycles, not
        # the (frequent) pushes — otherwise a device transiently absent from one
        # poll would reach the miss threshold within seconds of pushes.
        if coordinator.pushed_datapoint_id is not None:
            return
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
            if not slugs:
                continue
            if slugs & current:
                missing_polls.pop(device_entry.id, None)  # present again; reset
                continue
            misses = missing_polls.get(device_entry.id, 0) + 1
            if misses < STALE_DEVICE_PRUNE_MISSES:
                missing_polls[device_entry.id] = misses
                continue
            missing_polls.pop(device_entry.id, None)
            # Forget this device's unique_ids from the platforms' discovery sets
            # BEFORE removing it, so if the gateway reports it again the platforms
            # treat it as new and re-create its entities (otherwise the stale
            # `known` ids would suppress the re-add until an entry reload).
            coordinator.forget_device_unique_ids(device_entry.id)
            dev_reg.async_update_device(
                device_entry.id, remove_config_entry_id=entry.entry_id
            )

    return _prune_stale_devices


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

    async def _async_stop_on_ha_shutdown(event: Event) -> None:
        """Close the WebSocket on a full HA shutdown, not just entry unload."""
        await coordinator.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_on_ha_shutdown)
    )

    # Prune HA devices the gateway no longer reports (quality-scale stale-devices).
    _prune_stale_devices = _make_stale_device_pruner(hass, entry, coordinator)
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

    _register_capability_reload(hass, entry, coordinator)

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
