"""
Button, BinarySensor and Event entities for the Jung Home integration.

This module adapts coordinator datapoints into Home Assistant
`ButtonEntity`, `BinarySensorEntity` and `EventEntity` instances and
fires integration events on presses.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Double-click detection constants
DOUBLE_CLICK_WINDOW = 0.5
DOUBLE_CLICK_COUNT = 2

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[..., None],
) -> None:
    """
    Set up Jung Home button, binary_sensor and event entities.

    Entities are created from the coordinator data for RockerSwitch devices.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    await coordinator.async_refresh()
    devices = coordinator.data

    entities = []
    for device in devices:
        if device.get("type") != "RockerSwitch":
            continue
        for datapoint in device.get("datapoints", []):
            dp_type = datapoint.get("type")
            if dp_type in {"down_request", "up_request", "trigger_request"}:
                entities.append(
                    JungHomeEventEntity(coordinator, device, datapoint)
                )
    # Do not create JungHomeSwitch here; it will be handled in switch.py

    if entities:
        async_add_entities(entities, update_before_add=True)

# ------------------------------------------
# 🔹 BUTTON ENTITY (Stateless Press Action)
# ------------------------------------------
class JungHomeButton(CoordinatorEntity, ButtonEntity):
    """Representation of a Jung Home rocker switch as a button entity."""

    def __init__(
        self,
        coordinator: Any,
        device: Mapping[str, Any],
        datapoint: Mapping[str, Any],
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        label = device.get("label", "Jung Button")
        dp_type = datapoint.get("type", "Unknown")
        self._attr_name = f"{label}_{dp_type}"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_button"
        self._attr_available = coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information for this button."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    async def async_press(self) -> None:
        """Handle the button press (fires an event)."""
        _LOGGER.debug("Button %s pressed", self._attr_name)
        self.hass.bus.fire(
            "jung_home_button_press",
            {
                "entity_id": self.entity_id,
                "device_id": self._device["id"],
                "datapoint_id": self._datapoint["id"],
            },
        )

# ------------------------------------------
# 🔹 BINARY SENSOR ENTITY (Tracks Press State)
# ------------------------------------------
class JungHomeBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Jung Home button as a binary sensor (tracks press state)."""

    def __init__(
        self,
        coordinator: Any,
        device: Mapping[str, Any],
        datapoint: Mapping[str, Any],
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        label = device.get("label", "Jung Button")
        dp_type = datapoint.get("type", "Unknown")
        self._attr_name = f"{label}_{dp_type}_state"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_sensor"
        self._attr_device_class = "occupancy"
        self._attr_icon = "mdi:gesture-tap-button"
        self._attr_available = True
        # Always initialize to False if no state is available
        state = self._get_state_from_datapoint(datapoint)
        self._attr_is_on = state if isinstance(state, bool) else False
        self._last_press_time = 0
        self._press_count = 0
        self._last_value = self._get_state_from_datapoint(datapoint)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information for this sensor."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """
        Handle updated data from the coordinator.

        Check if button is pressed or released.
        """
        _LOGGER.debug("Updating binary sensor for %s", self._attr_name)
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]),
            None,
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
        if new_state == self._last_value:
            return

        now = time.time()
        if new_state is True:
            self._attr_is_on = True
            self.async_write_ha_state()
            self.hass.bus.fire(
                "jung_home_button_state_change",
                {
                    "entity_id": self.entity_id,
                    "device_id": self._device["id"],
                    "datapoint_id": self._datapoint["id"],
                    "state": "pressed",
                },
            )
            _LOGGER.debug("Fired state change event: pressed")

            # Double-click detection
            if now - self._last_press_time < DOUBLE_CLICK_WINDOW:
                self._press_count += 1
            else:
                self._press_count = 1

            self._last_press_time = now
            if self._press_count == DOUBLE_CLICK_COUNT:
                self.hass.bus.fire(
                    "jung_home_button_double_press",
                    {
                        "entity_id": self.entity_id,
                        "device_id": self._device["id"],
                        "datapoint_id": self._datapoint["id"],
                    },
                )
                _LOGGER.debug("Fired double press event for %s", self._attr_name)
                self._press_count = 0
        elif new_state is False:
            self._attr_is_on = False
            self.async_write_ha_state()
            self.hass.bus.fire(
                "jung_home_button_state_change",
                {
                    "entity_id": self.entity_id,
                    "device_id": self._device["id"],
                    "datapoint_id": self._datapoint["id"],
                    "state": "released",
                },
            )
            _LOGGER.debug("Fired state change event: released")

        self._last_value = new_state

    @property
    def is_on(self) -> bool:
        """Return True if button is pressed."""
        return bool(self._attr_is_on)

    def _get_state_from_datapoint(self, datapoint: Mapping[str, Any]) -> bool:
        """
        Extract state from datapoint values. Always returns True or False.

        Checks the request keys for explicit '1' (pressed) or '0' (released).
        """
        keys = {"up_request", "down_request", "trigger_request"}
        for value in datapoint.get("values", []):
            if value.get("key") in keys:
                if value.get("value") == "1":
                    return True
                if value.get("value") == "0":
                    return False
        return False

# ------------------------------------------
# 🔹 EVENT ENTITY (For UI Integration)
# ------------------------------------------
class JungHomeEventEntity(CoordinatorEntity, EventEntity):
    """Event entity for Jung Home button presses."""

    _attr_event_types: ClassVar[list[str]] = ["pressed", "depressed"]

    def __init__(
        self,
        coordinator: Any,
        device: Mapping[str, Any],
        datapoint: Mapping[str, Any],
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        label = device.get("label", "Jung Button")
        dp_type = datapoint.get("type", "Unknown")
        self._attr_name = f"{label}_{dp_type}_event"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_event"
        self._attr_icon = "mdi:gesture-tap-button"
        self._attr_available = True
        self._last_press_time = 0
        self._last_value = self._get_state_from_datapoint(datapoint)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information for this event entity."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator (trigger event on press)."""
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]),
            None,
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
                _LOGGER.debug("Triggering event for %s at %s", self._attr_name, now)
                self._trigger_event("pressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
                _LOGGER.debug("EventEntity state after trigger: %s", self.state)
                self._last_press_time = now
            elif new_state is False:
                _LOGGER.debug(
                    "Triggering depressed event for %s at %s",
                    self._attr_name,
                    now,
                )
                self._trigger_event("depressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
                _LOGGER.debug("EventEntity state after trigger: %s", self.state)
            self._last_value = new_state

    @property
    def state(self) -> Any | None:
        """Return the last event timestamp, or None if not available."""
        return getattr(self, "_attr_event_timestamp", None)

    def _get_state_from_datapoint(self, datapoint: Mapping[str, Any]) -> bool:
        """Extract state from datapoint values. Returns True if pressed."""
        keys = {"up_request", "down_request", "trigger_request"}
        for value in datapoint.get("values", []):
            if value.get("key") in keys and value.get("value") == "1":
                return True
        return False
