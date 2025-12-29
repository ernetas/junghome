import json
import logging
import time
from homeassistant.components.button import ButtonEntity
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.event import EventEntity
from homeassistant.util import dt as dt_util
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Jung Home button, binary_sensor, and event entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    await coordinator.async_refresh()
    devices = coordinator.data

    entities = []
    for device in devices:
        if device.get('type') == 'RockerSwitch':
            for datapoint in device.get('datapoints', []):
                dp_type = datapoint.get('type')
                if dp_type in {'down_request', 'up_request', 'trigger_request'}:
                    entities.append(JungHomeEventEntity(coordinator, device, datapoint))
    # Do not create JungHomeSwitch here; it will be handled in switch.py

    if entities:
        async_add_entities(entities, update_before_add=True)

# ------------------------------------------
# 🔹 BUTTON ENTITY (Stateless Press Action)
# ------------------------------------------
class JungHomeButton(CoordinatorEntity, ButtonEntity):
    """Representation of a Jung Home rocker switch as a button entity."""

    def __init__(self, coordinator, device, datapoint):
        """Initialize the button."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._attr_name = f"{device.get('label', 'Jung Button')}_{datapoint.get('type', 'Unknown')}"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_button"
        self._attr_available = coordinator.last_update_success

    @property
    def device_info(self):
        """Return device information for this button."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    async def async_press(self):
        """Handle the button press (fires an event)."""
        _LOGGER.debug("Button %s pressed", self._attr_name)
        self.hass.bus.fire(
            "jung_home_button_press",
            {
                "entity_id": self.entity_id,
                "device_id": self._device["id"],
                "datapoint_id": self._datapoint["id"]
            }
        )

# ------------------------------------------
# 🔹 BINARY SENSOR ENTITY (Tracks Press State)
# ------------------------------------------
class JungHomeBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Jung Home button as a binary sensor (tracks press state)."""

    def __init__(self, coordinator, device, datapoint):
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._attr_name = f"{device.get('label', 'Jung Button')}_{datapoint.get('type', 'Unknown')}_state"
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
    def device_info(self):
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
        """Handle updated data from the coordinator (check if button is pressed or released)."""
        _LOGGER.debug("Updating binary sensor for %s", self._attr_name)
        device = next((d for d in self.coordinator.data if d["id"] == self._device["id"]), None)
        if not device:
            return

        datapoint = next((dp for dp in device.get("datapoints", []) if dp["id"] == self._datapoint["id"]), None)
        if not datapoint:
            return

        new_state = self._get_state_from_datapoint(datapoint)
        if new_state != self._last_value:
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
                        "state": "pressed"
                    }
                )
                _LOGGER.debug("Fired state change event: pressed")
                # Double-click detection
                if now - self._last_press_time < 0.5:
                    self._press_count += 1
                else:
                    self._press_count = 1
                self._last_press_time = now
                if self._press_count == 2:
                    self.hass.bus.fire(
                        "jung_home_button_double_press",
                        {
                            "entity_id": self.entity_id,
                            "device_id": self._device["id"],
                            "datapoint_id": self._datapoint["id"]
                        }
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
                        "state": "released"
                    }
                )
                _LOGGER.debug("Fired state change event: released")
            self._last_value = new_state

    @property
    def is_on(self):
        """Return True if button is pressed."""
        return bool(self._attr_is_on)

    def _get_state_from_datapoint(self, datapoint):
        """Extract state from datapoint values. Always returns True or False."""
        for value in datapoint.get('values', []):
            if value['key'] in {'up_request', 'down_request', 'trigger_request'}:
                if value['value'] == '1':
                    return True
                elif value['value'] == '0':
                    return False
        return False

# ------------------------------------------
# 🔹 EVENT ENTITY (For UI Integration)
# ------------------------------------------
class JungHomeEventEntity(CoordinatorEntity, EventEntity):
    """Event entity for Jung Home button presses."""

    _attr_event_types = ["pressed", "depressed"]

    def __init__(self, coordinator, device, datapoint):
        """Initialize the event entity."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._attr_name = f"{device.get('label', 'Jung Button')}_{datapoint.get('type', 'Unknown')}_event"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_event"
        self._attr_icon = "mdi:gesture-tap-button"
        self._attr_available = True
        self._last_press_time = 0
        self._last_value = self._get_state_from_datapoint(datapoint)

    @property
    def device_info(self):
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
        device = next((d for d in self.coordinator.data if d["id"] == self._device["id"]), None)
        if not device:
            return
        datapoint = next((dp for dp in device.get("datapoints", []) if dp["id"] == self._datapoint["id"]), None)
        if not datapoint:
            return
        new_state = self._get_state_from_datapoint(datapoint)
        if new_state != self._last_value:
            now = time.time()
            if new_state is True:
                _LOGGER.debug(f"Triggering event for {self._attr_name} at {now}")
                self._trigger_event("pressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
                _LOGGER.debug(f"EventEntity state after trigger: {self.state}")
                self._last_press_time = now
            elif new_state is False:
                _LOGGER.debug(f"Triggering depressed event for {self._attr_name} at {now}")
                self._trigger_event("depressed")
                self._attr_event_timestamp = dt_util.now()
                self.async_write_ha_state()
                _LOGGER.debug(f"EventEntity state after trigger: {self.state}")
            self._last_value = new_state

    @property
    def state(self):
        # Show the last event timestamp as state if available
        return getattr(self, '_attr_event_timestamp', None)

    def _get_state_from_datapoint(self, datapoint):
        """Extract state from datapoint values. Returns True if pressed."""
        for value in datapoint.get('values', []):
            if value['key'] in {'up_request', 'down_request', 'trigger_request'}:
                if value['value'] == '1':
                    return True
        return False
