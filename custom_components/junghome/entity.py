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

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_slug, gateway_device_id
from .coordinator import JungHomeDataUpdateCoordinator
from .models import Datapoint, Device


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

    # Whether this entity's control path needs the live WebSocket. Commands
    # (turn on/off, brightness, position, target temperature, status LED) only
    # ever go out over the WebSocket — REST is poll-only — so a controllable
    # entity with the socket down cannot be actuated and must read unavailable.
    # Read-only entities (sensor/binary_sensor/event) leave this False and stay
    # available on the REST signal alone. Controllable platforms set it True.
    # Scenes are the one control path over REST, so the scene platform (which is
    # not a JungHomeEntity) keeps its own REST-only availability.
    _controllable_over_websocket = False

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

        Controllable entities additionally require a live WebSocket, because
        commands only travel over it: with the socket down they can report
        their last polled state but cannot be actuated, so they read
        unavailable rather than accept commands that would silently fail.
        ``ws_connected`` drives this and the connectivity diagnostic sensor;
        it never *grants* availability on its own.
        """
        if self._controllable_over_websocket and not self.coordinator.ws_connected:
            return False
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information, linking the entity to its Jung Home device.

        Every device is hung off the synthetic gateway (hub) device via
        ``via_device`` so the registry reflects the real "devices reached
        through the gateway" topology (the hub is registered up front in
        ``async_setup_entry``, so this reference always resolves).
        """
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, device_slug(self._device))},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version")
            or self.coordinator.gateway_version
            or "Unknown Version",
        }
        entry = self.coordinator.config_entry
        if entry is not None:
            info["via_device"] = (DOMAIN, gateway_device_id(entry))
        return info

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
        every entity of this entry, controllable or not. A push therefore can
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
