"""Shared base entity for Jung Home device platforms.

Every device-backed platform (light, switch, sensor, event, cover, climate)
repeated the same ``device_info``, ``available`` and coordinator-data lookups.
This base centralises them. The scene platform is intentionally *not* based on
it — scenes have no backing device.

``available`` keys off the coordinator's ``last_update_success`` signal, and
the lookup helpers return the same objects the inline ``next(...)`` calls did.
``device_info`` additionally links each device to the synthetic gateway (hub)
device via ``via_device``. Subclasses keep their own ``unique_id``/naming and
their own ``_handle_coordinator_update`` write logic (which intentionally
differs between platforms).
"""

from typing import Any, cast

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_slug, gateway_device_id
from .coordinator import JungHomeDataUpdateCoordinator
from .models import Datapoint, Device

# HA 2026.8 added ``via_device_id`` (the hub's registry id) to ``DeviceInfo``
# and 2026.9 deprecated the ``via_device`` identifier tuple. The deprecation
# report is what makes this more than a warning: it raises ``RuntimeError``
# when it cannot find an integration frame on the stack, which was exactly the
# case under the ``async_add_entities(..., update_before_add=True)`` the
# platforms used then, so one entity per startup failed to load (issue #207).
# Cores older than 2026.8 reject the new key as an unknown kwarg, hence the
# feature check rather than a version compare; the floor stays 2025.12.4.
VIA_DEVICE_ID_SUPPORTED = "via_device_id" in DeviceInfo.__optional_keys__


def entry_unloading(entry: ConfigEntry) -> bool:
    """Whether ``entry`` is being unloaded, so discovery must not add anything.

    A reload can start in the middle of a device-list adoption — the id-churn
    check and the capability watcher schedule one, and it runs eagerly up to
    its first suspension. By then the platforms are reset, but the discovery
    listeners are removed only when the unload finishes, so the same
    adoption's listener pass would add a new device's entities to the reset
    platform: an orphan bound to the dead coordinator, frozen at its first
    state, while the reloaded entry's own discovery finds the unique_id taken.
    The reloaded entry discovers everything afresh.
    """
    return entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS


def claim_new_entity(known: set[str], unique_id: str) -> bool:
    """Whether a platform should create an entity for ``unique_id`` now.

    ``known`` is the coordinator's shared per-platform set of unique_ids discovery
    has already added (``coordinator.known_unique_ids(domain)``); it guards against
    a duplicate add in the async window between scheduling an add and the entity
    landing in the registry. Returns ``True`` — and records the id — the first
    time an id is seen, ``False`` thereafter.

    Re-adding after a device is pruned is handled at the source, not here: the
    stale-device pruner calls ``coordinator.forget_device_unique_ids`` to drop a
    removed device's ids from these sets, so a device that reappears is a fresh
    id again and gets re-added. (Reconciling against the entity registry here
    instead would race the in-flight add and cause duplicate-add errors.)
    """
    if unique_id in known:
        return False
    known.add(unique_id)
    return True


class JungHomeEntity(CoordinatorEntity[JungHomeDataUpdateCoordinator]):
    """Base for entities backed by a Jung Home device."""

    _attr_has_entity_name = True

    # Whether this entity's function needs the live WebSocket. Commands (turn
    # on/off, brightness, position, target temperature, status LED) only ever
    # go out over the WebSocket, and button edges only ever arrive over it —
    # REST is a poll of last-known values — so with the socket down a
    # controllable entity cannot be actuated and an event entity cannot hear a
    # press. Both must read unavailable rather than look live while inert or
    # deaf. Pure state readers (sensor/binary_sensor) leave this False and stay
    # available on the REST signal alone; the controllable platforms and the
    # event platform set it True. Scenes are the one control path over REST,
    # so the scene platform (which is not a JungHomeEntity) keeps its own
    # REST-only availability.
    _needs_websocket = False

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
    ) -> None:
        """Initialise with the coordinator and the device this entity belongs to."""
        super().__init__(coordinator)
        self._device = device

    @property
    def available(self) -> bool:
        """Return if the device is available.

        The REST poll (default every 60 s — options-configurable — with a
        30 s timeout) is an independent, bounded reachability probe, and every
        WebSocket push also sets ``last_update_success`` True directly (the
        push path deliberately does NOT go through ``async_set_updated_data``,
        which would re-arm the poll timer and starve the poll — see
        ``_handle_websocket_message``). So it reads True while either the poll
        succeeds or pushes arrive, and flips False within about one poll
        interval plus the 30 s timeout once the gateway is truly gone.

        Availability deliberately does *not* OR in ``ws_connected``: that flag
        can stay stale-True on a half-open socket the heartbeat hasn't torn down
        yet, and OR-ing it would mask a failing REST poll — leaving entities
        "available" with frozen values long after the gateway vanished (which
        silently fabricated energy readings; see issue #120).

        Entities whose function needs the socket (``_needs_websocket``)
        additionally require a live WebSocket. Commands only travel over it:
        a controllable entity with the socket down could report its last
        polled state but not be actuated, so it reads unavailable rather than
        accept commands that would silently fail. Button edges only *arrive*
        over it (a REST poll re-reads the same values and fires nothing — see
        ``event.py``): an event entity with the socket down is deaf, so it
        reads unavailable rather than let an automation wait on a release it
        can never report (the shipped blueprint aborts a gesture on exactly
        that transition). ``ws_connected`` drives this and the connectivity
        diagnostic sensor; it never *grants* availability on its own.
        """
        if self._needs_websocket and not self.coordinator.ws_connected:
            return False
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information, linking the entity to its Jung Home device.

        Every device is hung off the synthetic gateway (hub) device so the
        registry reflects the real "devices reached through the gateway"
        topology: by registry id (``via_device_id``) on cores that know the
        key, by identifier tuple (``via_device``) on older ones. The hub is
        registered up front in ``async_setup_entry``, so both forms resolve.

        The identity is the label slug (``identifiers``) and nothing else —
        the registry resolves a device by identifier OR connection, so the
        slug must be the only key ``async_get_or_create`` can match on. When
        the coordinator resolved the function's hardware identity from the
        gateway's project export (``coordinator.node_identities``), the node's
        Bluetooth address is added as ``serial_number``: informational, shown
        on the device page, shared by every function of a multi-function node,
        never matched on. The ``CONNECTION_BLUETOOTH`` connection the primary
        function's device also carries is deliberately NOT set here: a
        relabelled function registers under a new slug while the old device
        is still live, and a connection here made the registry resolve the
        new slug to the old device by connection and merge the two (the old
        entity then lived on forever and the pruner never fired). The
        coordinator writes the connection instead, once the row exists and
        only when no other live device of the entry holds it
        (``link_node_identity`` from ``async_added_to_hass``, and
        ``apply_node_identities`` when identities resolve or a device is
        removed) — see ``_write_node_identity`` for the rule.

        No literal fallbacks, and no ``None`` either: an unknown label, type
        or version is left out, so the registry keeps whatever it already
        holds for that row (a ``None`` would clear it — the gateway version
        ``_apply_gateway_version`` wrote on an earlier run, say) and a new
        device is named after the entry, as HA does for any nameless device —
        rather than a made-up "Unknown Model" pinned as if the gateway had
        said it.
        """
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, device_slug(self._device))},
            "manufacturer": "Jung",
        }
        if label := self._device.get("label"):
            info["name"] = label
        if model := self._device.get("type"):
            info["model"] = model
        if version := (
            self._device.get("sw_version") or self.coordinator.gateway_version
        ):
            info["sw_version"] = version
        identity = self.coordinator.node_identity_for(self._device)
        if identity is not None and identity.mac is not None:
            info["serial_number"] = identity.mac
        # Whichever link key this core lacks is absent from its DeviceInfo
        # TypedDict (``via_device_id`` before 2026.8, ``via_device`` from
        # 2026.9), so both are set through a plain dict view to keep mypy
        # happy on every supported version.
        hub_registry_id = self.coordinator.gateway_device_registry_id
        if VIA_DEVICE_ID_SUPPORTED and hub_registry_id is not None:
            cast("dict[str, Any]", info)["via_device_id"] = hub_registry_id
        elif (entry := self.coordinator.config_entry) is not None:
            cast("dict[str, Any]", info)["via_device"] = (
                DOMAIN,
                gateway_device_id(entry),
            )
        return info

    async def async_added_to_hass(self) -> None:
        """Link the device to its radio now that its registry row exists.

        See ``device_info``: the Bluetooth connection is written by the
        coordinator, never registered through ``device_info``.
        """
        await super().async_added_to_hass()
        if (device_entry := self.device_entry) is not None:
            self.coordinator.link_node_identity(device_entry.id, self._device)

    def _current_device(self) -> Device | None:
        """Return this entity's device from the latest coordinator data."""
        return next(
            (
                d
                for d in self.coordinator.data or []
                if d.get("id") == self._device["id"]
            ),
            None,
        )

    def _find_datapoint(self, datapoint_id: str) -> Datapoint | None:
        """Return a datapoint by id from this entity's current device data."""
        device = self._current_device()
        if device is None:
            return None
        return next(
            (dp for dp in device.get("datapoints", []) if dp.get("id") == datapoint_id),
            None,
        )

    def _should_refresh(self, datapoint_id: str) -> bool:
        """Whether the attribute backed by ``datapoint_id`` should refresh now.

        The gateway sends each datapoint change as its own WebSocket frame, so on
        a push only the pushed datapoint's attribute should be re-read. Refreshing
        a *sibling* attribute here would read a not-yet-updated (stale) snapshot —
        e.g. a switch=on echo arriving before the brightness echo would momentarily
        reset the brightness slider to the old value (a UI flicker). On a REST poll
        (``pushed_datapoint_id`` is None) every datapoint is fresh, so refresh all.
        """
        pushed = self.coordinator.pushed_datapoint_id
        return pushed is None or pushed == datapoint_id

    @callback
    def _skip_foreign_device_push(self) -> bool:
        """Whether this dispatch is another device's push and the write can be skipped.

        Every coordinator dispatch notifies every entity, so on a gateway with
        chatty per-datapoint pushes (a socket reporting power once a second)
        each frame used to trigger a state-machine write for EVERY entity of
        the entry — N writes for one changed value. A per-datapoint push
        mutates exactly one datapoint dict of exactly one device, so entities
        of every *other* device can skip their write — under one guard:

        **Availability.** During a push dispatch the coordinator has just set
        ``last_update_success = True``, and ``ws_connected`` is necessarily
        True (the push arrived on the live session, and the flag is only
        cleared when that session ends) — so ``available`` computes True for
        every entity of this entry, socket-dependent or not. A push therefore can
        never make an entity UNavailable, but it can make one available again
        (a failed poll marked everything unavailable; the push proves the
        gateway alive). The skip is allowed only while the state machine
        already shows this entity as available; an entity currently
        unavailable — or not yet written at all — always writes, so the
        recovery propagates on this very dispatch instead of waiting for the
        next poll.

        Scoped to the *device*, not to a set of the entity's own datapoint
        ids, on purpose: entities read sibling datapoints beyond the ones
        whose ids they store (climate refreshes its ambient temperature from
        the device's ``quantity`` datapoint on every update), so an id-set
        scope would wrongly starve those. Everything else fails open: poll,
        ``functions``-broadcast, scenes and WS-drop dispatches carry no push
        marker, and a pushed device without an ``id`` sets no marker either —
        in all those cases every entity writes exactly as before. Entities
        whose state is NOT a pure function of their device's datapoints plus
        availability (the gateway connectivity sensor, scenes) do not inherit
        from this base and are untouched.
        """
        pushed_device = self.coordinator.pushed_device_id
        if pushed_device is None or pushed_device == self._device.get("id"):
            return False
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE
