"""Event platform for Jung Home rocker buttons."""

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import CONF_DEVICE_ID, CONF_TYPE, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    BUTTON_DATAPOINT_TYPES,
    CONF_SUBTYPE,
    EVENT_BUTTON_ACTION,
    datapoint_value,
    stable_unique_id,
)
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity, claim_new_entity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0

# Translation keys per rocker datapoint type. With `_attr_has_entity_name`, HA
# prepends the device name; the entity name itself comes from the
# `entity.event.*` translations (strings.json), so it's localisable rather than
# hardcoded. Shared with `device_trigger` (see BUTTON_DATAPOINT_TYPES) so a
# button side is named the same in both surfaces.
_EVENT_TRANSLATION_KEYS = BUTTON_DATAPOINT_TYPES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home event entities from a config entry."""
    coordinator = entry.runtime_data
    known = coordinator.known_unique_ids(Platform.EVENT)

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
                        if not claim_new_entity(known, uid):
                            continue
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
                self._fire_bus_event(event_type)
        self.async_write_ha_state()

    @callback
    def _fire_bus_event(self, event_type: str) -> None:
        """Re-emit this edge on the Home Assistant bus for device triggers.

        Device triggers can only attach to a bus event, not to an entity, so the
        edge is published a second time here (this mirrors how HA's own button
        integrations do it). Skipped for a datapoint type with no button side, and
        when the entity is not yet in the device registry — a device trigger is
        keyed on the device id, so an event without one would match nothing.
        """
        button_type = _EVENT_TRANSLATION_KEYS.get(self._datapoint.get("type", ""))
        device_entry = self.device_entry
        if button_type is None or device_entry is None:
            return
        self.hass.bus.async_fire(
            EVENT_BUTTON_ACTION,
            {
                CONF_DEVICE_ID: device_entry.id,
                CONF_TYPE: button_type,
                CONF_SUBTYPE: event_type,
                "entity_id": self.entity_id,
                "device_name": device_entry.name_by_user or device_entry.name,
            },
        )

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool:
        """Extract state from datapoint values. Returns True if pressed.

        Scoped to this datapoint's own type so bundled request keys don't merge.
        """
        return datapoint_value(datapoint, self._datapoint.get("type", "")) == "1"
