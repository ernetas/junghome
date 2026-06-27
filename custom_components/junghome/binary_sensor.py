"""Binary sensor platform for Jung Home (presence / occupancy detectors).

JUNG presence/motion detectors ("BWM") are ``Measurement`` functions that, next
to their ambient ``Present Illuminance`` (lux) reading, expose detection as a
``quantity`` datapoint with an **empty unit** and a ``0``/``1`` value (label
``Presence Detected``). That is a boolean state, not a measurement, so it surfaces
here as an occupancy ``binary_sensor`` rather than a numeric sensor — the numeric
``sensor`` platform deliberately skips it (see ``is_presence_quantity``).
"""

import logging
import math

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, is_presence_quantity, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home binary sensors from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_binary_sensors() -> None:
        """Add entities for any presence sensors not yet created (handles late adds)."""
        new_entities: list[JungHomePresence] = []
        for device in coordinator.data or []:
            # Presence/occupancy is reported as a quantity datapoint (with an
            # empty unit); it can ride on a Measurement detector or, in
            # principle, any device, so match on the datapoint, not the type.
            for datapoint in device.get("datapoints", []):
                if datapoint.get("type") != "quantity":
                    continue
                raw_label = datapoint_value(datapoint, "quantity_label")
                if raw_label is None or not is_presence_quantity(raw_label):
                    continue
                label = raw_label.strip()
                uid = stable_unique_id(
                    device, datapoint, label.replace(" ", "_").lower()
                )
                if uid not in known:
                    known.add(uid)
                    new_entities.append(
                        JungHomePresence(coordinator, device, datapoint, label)
                    )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_binary_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_discover_binary_sensors))


class JungHomePresence(JungHomeEntity, BinarySensorEntity):
    """A Jung Home presence/occupancy detection (boolean quantity)."""

    # Secondary entity on the device (the detector also has a lux sensor), so HA
    # prepends the device name: entity_id `binary_sensor.<device>_presence_detected`.
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
        label: str,
    ) -> None:
        """Initialize the presence sensor."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        self._datapoint_id = datapoint["id"]
        self._attr_name = label
        self._name = f"{device.get('label', 'Jung Device')} {label}"  # for logging
        # Firmware-stable id derived from the label, not the volatile device id.
        self._attr_unique_id = stable_unique_id(
            device, datapoint, label.replace(" ", "_").lower()
        )
        self._is_on = self._get_state_from_datapoint(datapoint)

    @property
    def is_on(self) -> bool | None:
        """Return True if presence is detected (None if unknown)."""
        return self._is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        datapoint = self._find_datapoint(self._datapoint_id)
        if datapoint:
            self._is_on = self._get_state_from_datapoint(datapoint)
            self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: Datapoint | None) -> bool | None:
        """Extract the boolean presence state from a quantity datapoint.

        The value is a numeric string (``"0"``/``"1"``); anything non-finite
        (e.g. an uninitialised ``"NaN"``) reads as unknown rather than ``True``
        (``float("NaN") != 0`` is misleadingly truthy).
        """
        value = datapoint_value(datapoint, "quantity")
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric != 0 if math.isfinite(numeric) else None
