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
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    datapoint_value,
    gateway_device_id,
    gateway_device_info,
    is_presence_quantity,
    stable_unique_id,
)
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity, claim_new_entity
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
    known = coordinator.known_unique_ids(Platform.BINARY_SENSOR)

    # One gateway-level connectivity sensor per entry. Added once at setup (not a
    # per-device discovery) so the hub always exposes a reachability entity even
    # before any device is discovered.
    async_add_entities([JungHomeGatewayConnectivity(coordinator, entry)])

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
                unit = datapoint_value(datapoint, "quantity_unit")
                if raw_label is None or not is_presence_quantity(raw_label, unit):
                    continue
                label = raw_label.strip()
                uid = stable_unique_id(
                    device, datapoint, label.replace(" ", "_").lower()
                )
                if claim_new_entity(known, uid):
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
        if self._skip_foreign_device_push():
            return  # another device's push; nothing about this detector changed
        datapoint = self._find_datapoint(self._datapoint_id)
        if datapoint:
            self._is_on = self._get_state_from_datapoint(datapoint)
        # Write unconditionally (even when the datapoint is momentarily absent)
        # so the entity's availability tracks the gateway on every coordinator
        # update, matching the switch platform.
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


class JungHomeGatewayConnectivity(
    CoordinatorEntity[JungHomeDataUpdateCoordinator], BinarySensorEntity
):
    """Reports whether the gateway's live WebSocket link is up.

    This is a hub-level diagnostic entity, not backed by a JUNG *function*, so it
    is attached to a synthetic gateway device (shared by any future gateway-level
    entities) rather than to ``JungHomeEntity``.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "gateway_connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        entry: JungHomeConfigEntry,
    ) -> None:
        """Initialize the gateway connectivity sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{gateway_device_id(entry)}_connectivity"

    @property
    def available(self) -> bool:
        """Always available.

        A connectivity sensor that went unavailable when the gateway dropped
        would be useless — it must stay available to report ``off`` (offline).
        """
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the gateway WebSocket is connected."""
        return self.coordinator.ws_connected

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the synthetic gateway (hub) device."""
        return gateway_device_info(self._entry, self.coordinator.gateway_version)
