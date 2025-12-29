import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Jung Home sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Fetch devices from the coordinator
    await coordinator.async_refresh()
    devices = coordinator.data

    # Create sensor entities for each device
    entities = []
    for device in devices:
        if device['type'] == 'Socket':  # Add devices with type "Socket"
            for datapoint in device.get('datapoints', []):
                if datapoint['type'] == 'quantity':
                    label = next((value['value'].strip() for value in datapoint['values'] if value['key'] == 'quantity_label'), None)
                    unit = next((value['value'] for value in datapoint['values'] if value['key'] == 'quantity_unit'), None)
                    if label and unit:
                        entities.append(JungHomeQuantity(coordinator, device, datapoint, label, unit))

    if entities:
        async_add_entities(entities, update_before_add=True)

class JungHomeQuantity(CoordinatorEntity, SensorEntity):
    """Representation of a Jung Home quantity."""

    def __init__(self, coordinator, device, datapoint, label, unit):
        """Initialize the quantity."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._name = f"{device.get('label', 'Jung Device')} {label}"
        self._unique_id = f"{device.get('id')}_{datapoint.get('id')}_{label.replace(' ', '_').lower()}"
        self._unit = unit
        self._value = self._get_value_from_datapoint(datapoint)
        self.entity_id = f"sensor.{self._unique_id}"  # Set the entity ID

    @property
    def name(self):
        """Return the name of the quantity."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the quantity."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the quantity."""
        return self._value

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of the quantity."""
        return self._unit

    @property
    def device_info(self):
        """Return device information about this quantity."""
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
        _LOGGER.debug("Handling coordinator update for quantity %s", self._name)
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]), None
        )
        if device:
            datapoint = next(
                (dp for dp in device.get('datapoints', []) if dp["id"] == self._datapoint["id"]), None
            )
            if datapoint:
                self._value = self._get_value_from_datapoint(datapoint)
                _LOGGER.debug("Updated state for quantity %s: %s", self._name, self._value)
                self.async_write_ha_state()

    def _get_value_from_datapoint(self, datapoint):
        """Extract the value of the quantity from its datapoint."""
        for value in datapoint.get('values', []):
            if value['key'] == 'quantity':
                return value['value']
        return None