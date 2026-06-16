"""Event platform for Jung Home rocker buttons."""

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

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
class JungHomeEventEntity(JungHomeEntity, EventEntity):
    """Event entity for Jung Home button presses."""

    _attr_event_types = ["pressed", "depressed"]
    _attr_device_class = EventDeviceClass.BUTTON

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        dp_type = datapoint.get("type", "Unknown")
        translation_key = _EVENT_TRANSLATION_KEYS.get(dp_type)
        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = dp_type
        self._attr_unique_id = stable_unique_id(device, datapoint, "event")
        # Icon comes from icons.json (icon-translations).

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
        # Fire only on a genuine WebSocket push for THIS datapoint. REST re-reads
        # (marker is None) and pushes for other datapoints skip the fire but still
        # write state below, so availability tracks the gateway connection without
        # ever emitting a phantom press.
        if self.coordinator.pushed_datapoint_id == self._datapoint["id"]:
            datapoint = self._find_datapoint(self._datapoint["id"])
            if datapoint:
                event_type = (
                    "pressed"
                    if self._get_state_from_datapoint(datapoint)
                    else "depressed"
                )
                _LOGGER.debug("Triggering %s event for %s", event_type, self.entity_id)
                self._trigger_event(event_type)
        self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool:
        """Extract state from datapoint values. Returns True if pressed.

        Scoped to this datapoint's own type so bundled request keys don't merge.
        """
        return datapoint_value(datapoint, self._datapoint.get("type", "")) == "1"
