"""Event platform for Jung Home rocker buttons."""

import logging
import time

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, device_slug, stable_unique_id
from .coordinator import JungHomeConfigEntry

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
    hass: HomeAssistant, entry: JungHomeConfigEntry, async_add_entities
):
    """Set up Jung Home event entities from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_events():
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
class JungHomeEventEntity(CoordinatorEntity, EventEntity):
    """Event entity for Jung Home button presses."""

    _attr_event_types = ["pressed", "depressed"]
    _attr_has_entity_name = True

    def __init__(self, coordinator, device, datapoint):
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
        # Availability is inherited from CoordinatorEntity (tracks the gateway
        # connection); don't pin it True or it stays "available" when the
        # gateway is down.
        self._last_press_time = 0
        self._last_value = self._get_state_from_datapoint(datapoint)

    @property
    def device_info(self):
        """Return device information for this event entity."""
        return {
            "identifiers": {(DOMAIN, device_slug(self._device))},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator (trigger event on press)."""
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]), None
        )
        if not device:
            return
        datapoint = next(
            (
                dp
                for dp in device.get("datapoints", [])
                if dp["id"] == self._datapoint["id"]
            ),
            None,
        )
        if not datapoint:
            return
        new_state = self._get_state_from_datapoint(datapoint)
        if new_state != self._last_value:
            now = time.time()
            if new_state is True:
                _LOGGER.debug(
                    "Triggering pressed event for %s at %s", self.entity_id, now
                )
                self._trigger_event("pressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
                self._last_press_time = now
            elif new_state is False:
                _LOGGER.debug(
                    "Triggering depressed event for %s at %s", self.entity_id, now
                )
                self._trigger_event("depressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
            self._last_value = new_state

    @property
    def state(self):
        """Return the timestamp of the last button event."""
        return getattr(self, "_attr_event_timestamp", None)

    def _get_state_from_datapoint(self, datapoint):
        """Extract state from datapoint values. Returns True if pressed."""
        for value in datapoint.get("values", []):
            if value["key"] in {"up_request", "down_request", "trigger_request"}:
                if value["value"] == "1":
                    return True
        return False
