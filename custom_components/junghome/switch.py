import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Jung Home switches from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Fetch devices from the coordinator
    await coordinator.async_refresh()
    devices = coordinator.data

    # Create switch entities for each device
    entities = []
    for device in devices:
        if device['type'] == 'Socket':  # Add devices with type "Socket"
            for datapoint in device.get('datapoints', []):
                if datapoint['type'] == 'switch':
                    entities.append(JungHomeSocket(coordinator, device, datapoint))
        elif device.get('type') == 'RockerSwitch':
            for datapoint in device.get('datapoints', []):
                if datapoint.get('type') == 'status_led':
                    entities.append(JungHomeSwitch(coordinator, device, datapoint))

    if entities:
        async_add_entities(entities, update_before_add=True)

class JungHomeSocket(CoordinatorEntity, SwitchEntity):
    """Representation of a Jung Home socket."""

    def __init__(self, coordinator, device, datapoint):
        """Initialize the socket."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._name = device.get("label", "Jung Socket")
        self._unique_id = f"{device.get('id')}_{datapoint.get('id')}"  # Use device ID and datapoint ID
        self._is_on = self._get_state_from_datapoint(datapoint)
        self.entity_id = f"switch.{self._unique_id}"  # Set the entity ID

    @property
    def name(self):
        """Return the name of the socket."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the socket."""
        return self._unique_id

    @property
    def is_on(self):
        """Return the state of the socket."""
        return self._is_on

    @property
    def device_class(self):
        """Return the device class of the socket."""
        return "outlet"

    @property
    def device_info(self):
        """Return device information about this socket."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},  # Link to the device
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Handling coordinator update for socket %s", self._name)
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]), None
        )
        if device:
            datapoint = next(
                (dp for dp in device.get('datapoints', []) if dp["id"] == self._datapoint["id"]), None
            )
            if datapoint:
                self._is_on = self._get_state_from_datapoint(datapoint)
                _LOGGER.debug("Updated state for socket %s: %s", self._name, self._is_on)
                self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the socket on."""
        _LOGGER.debug("Turning on socket %s", self._name)
        await self.coordinator.turn_on_switch(self._datapoint["id"])
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the socket off."""
        _LOGGER.debug("Turning off socket %s", self._name)
        await self.coordinator.turn_off_switch(self._datapoint["id"])
        self._is_on = False
        self.async_write_ha_state()

    @property
    def should_poll(self):
        """No polling needed for this entity."""
        return False

    @property
    def available(self):
        """Return if the device is available."""
        return self.coordinator.last_update_success

    async def async_update(self):
        """Update the socket's state."""
        await self.coordinator.async_request_refresh()
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]), None
        )
        if device:
            datapoint = next(
                (dp for dp in device.get('datapoints', []) if dp["id"] == self._datapoint["id"]), None
            )
            if datapoint:
                self._is_on = self._get_state_from_datapoint(datapoint)
                self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint):
        """Extract the state of the socket from its datapoint."""
        for value in datapoint.get('values', []):
            if value['key'] == 'switch':
                return value['value'] == '1'
        return False

class JungHomeSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Jung Home status LED as a switch entity."""
    def __init__(self, coordinator, device, datapoint):
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._attr_name = f"{device.get('label', 'Jung Status LED')}_{datapoint.get('type', 'Unknown')}"
        self._attr_unique_id = f"{device.get('id')}_{datapoint.get('id')}_switch"
        self._attr_available = coordinator.last_update_success
        self._attr_is_on = self._get_state_from_datapoint(datapoint)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    def _get_state_from_datapoint(self, datapoint):
        for value in datapoint.get('values', []):
            if value['key'] == 'status_led':
                return value['value'] == '1'
        return False

    @property
    def is_on(self):
        return self._attr_is_on

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("Turning on switch %s", self._attr_name)
        await self.coordinator.set_status_led(self._datapoint["id"], True)

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("Turning off switch %s", self._attr_name)
        await self.coordinator.set_status_led(self._datapoint["id"], False)

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating switch for %s", self._attr_name)
        device = next((d for d in self.coordinator.data if d["id"] == self._device["id"]), None)
        if not device:
            return
        datapoint = next((dp for dp in device.get("datapoints", []) if dp["id"] == self._datapoint["id"]), None)
        if not datapoint:
            return
        new_state = self._get_state_from_datapoint(datapoint)
        if new_state != self._attr_is_on:
            self._attr_is_on = new_state
            self.async_write_ha_state()