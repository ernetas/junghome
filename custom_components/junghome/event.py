"""Event platform for Jung Home rocker buttons."""

import logging
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_slug, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0

# Translation keys per rocker datapoint type. With `_attr_has_entity_name`, HA
# prepends the device name; the entity name itself comes from the
# `entity.event.*` translations (strings.json), so it's localisable rather than
# hardcoded.
_EVENT_TRANSLATION_KEYS = {
    "up_request": "up",
    "down_request": "down",
    "trigger_request": "press",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home event entities from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_events() -> None:
        """Add entities for any events not yet created (handles devices added later)."""
        new_entities = []
        for device in coordinator.data or []:
            if device.get("type") == "RockerSwitch":
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") in {
                        "down_request",
                        "up_request",
                        "trigger_request",
                    }:
                        uid = stable_unique_id(device, datapoint, "event")
                        if uid in known:
                            continue
                        known.add(uid)
                        new_entities.append(
                            JungHomeEventEntity(coordinator, device, datapoint)
                        )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_events()
    entry.async_on_unload(coordinator.async_add_listener(_discover_events))


# ------------------------------------------
# 🔹 EVENT ENTITY (For UI Integration)
# ------------------------------------------
class JungHomeEventEntity(
    CoordinatorEntity[JungHomeDataUpdateCoordinator], EventEntity
):
    """Event entity for Jung Home button presses."""

    _attr_event_types = ["pressed", "depressed"]
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: dict[str, Any],
        datapoint: dict[str, Any],
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        dp_type = datapoint.get("type", "Unknown")
        translation_key = _EVENT_TRANSLATION_KEYS.get(dp_type)
        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = dp_type
        self._attr_unique_id = stable_unique_id(device, datapoint, "event")
        # Icon comes from icons.json (icon-translations).

    @property
    def available(self) -> bool:
        """Return if the device is available.

        Matches the light/socket/LED entities: a live WebSocket link is the
        primary availability signal, falling back to the last REST poll, so an
        event entity doesn't go unavailable on a transient REST-poll miss while
        the WebSocket (which actually delivers its presses) is still connected.
        """
        return self.coordinator.ws_connected or self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for this event entity."""
        return {
            "identifiers": {(DOMAIN, device_slug(self._device))},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version")
            or self.coordinator.gateway_version
            or "Unknown Version",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire an event when this datapoint is pushed over the WebSocket.

        Press detection keys off the coordinator's per-push marker rather than
        diffing snapshots. The gateway broadcasts a ``datapoint`` frame on every
        genuine press/release edge, whereas REST polls (and the full-list resync
        frames) re-read the same values without setting the marker. So every real
        edge fires exactly once — including rapid same-value taps that a level
        diff would coalesce — and a re-read never fires a phantom press.
        """
        if self.coordinator.pushed_datapoint_id != self._datapoint["id"]:
            self.async_write_ha_state()
            return
        device = next(
            (
                d
                for d in self.coordinator.data or []
                if d.get("id") == self._device["id"]
            ),
            None,
        )
        datapoint = next(
            (
                dp
                for dp in (device or {}).get("datapoints", [])
                if dp.get("id") == self._datapoint["id"]
            ),
            None,
        )
        if not datapoint:  # pragma: no cover - marker implies the push just matched it
            self.async_write_ha_state()
            return
        # EventEntity records the event type and timestamp itself.
        event_type = (
            "pressed" if self._get_state_from_datapoint(datapoint) else "depressed"
        )
        _LOGGER.debug("Triggering %s event for %s", event_type, self.entity_id)
        self._trigger_event(event_type)
        self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: dict[str, Any]) -> bool:
        """Extract state from datapoint values. Returns True if pressed.

        Scoped to this datapoint's own type so bundled request keys don't merge.
        """
        for value in datapoint.get("values", []):
            if (
                value.get("key") == self._datapoint.get("type")
                and value.get("value") == "1"
            ):
                return True
        return False
