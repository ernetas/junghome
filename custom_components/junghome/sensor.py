import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_slug, stable_unique_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    """Set up Jung Home sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known: set[str] = set()

    @callback
    def _discover_sensors():
        """Add entities for any sensors not yet created (handles devices added later)."""
        new_entities = []
        for device in coordinator.data or []:
            if device.get("type") == "Socket":
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "quantity":
                        label = next(
                            (
                                value["value"].strip()
                                for value in datapoint["values"]
                                if value["key"] == "quantity_label"
                            ),
                            None,
                        )
                        unit = next(
                            (
                                value["value"]
                                for value in datapoint["values"]
                                if value["key"] == "quantity_unit"
                            ),
                            None,
                        )
                        if label and unit:
                            entity = JungHomeQuantity(
                                coordinator, device, datapoint, label, unit
                            )
                            if entity.unique_id not in known:
                                known.add(entity.unique_id)
                                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_discover_sensors))


class JungHomeQuantity(CoordinatorEntity, SensorEntity):
    """Representation of a Jung Home quantity."""

    # Secondary entity on the device; HA prepends the device name, so the
    # entity_id becomes `sensor.<device>_<quantity>` (the label is no longer
    # baked into the entity name).
    _attr_has_entity_name = True

    def __init__(self, coordinator, device, datapoint, label, unit):
        """Initialize the quantity."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        self._attr_name = label
        self._name = f"{device.get('label', 'Jung Device')} {label}"  # for logging
        # Firmware-stable id derived from the label, not the volatile device id.
        self._unique_id = stable_unique_id(
            device, datapoint, label.replace(" ", "_").lower()
        )
        self._unit = unit
        self._value = self._get_value_from_datapoint(datapoint)

    @property
    def name(self):
        """Return the entity name (the measured quantity; HA adds the device)."""
        return self._attr_name

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
            "identifiers": {(DOMAIN, device_slug(self._device))},  # Link to the device
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
                (
                    dp
                    for dp in device.get("datapoints", [])
                    if dp["id"] == self._datapoint["id"]
                ),
                None,
            )
            if datapoint:
                self._value = self._get_value_from_datapoint(datapoint)
                _LOGGER.debug(
                    "Updated state for quantity %s: %s", self._name, self._value
                )
                self.async_write_ha_state()

    def _get_value_from_datapoint(self, datapoint):
        """Extract the value of the quantity from its datapoint."""
        for value in datapoint.get("values", []):
            if value["key"] == "quantity":
                return value["value"]
        return None
