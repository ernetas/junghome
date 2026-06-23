"""Sensor platform for Jung Home (socket energy quantities)."""

import logging
import math

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0

_MEAS = SensorStateClass.MEASUREMENT
_TOTAL = SensorStateClass.TOTAL_INCREASING

# Map a device-reported unit (normalised: stripped + lowercased) to
# (device_class, state_class, Home Assistant unit). Energy is TOTAL_INCREASING so
# it feeds the energy dashboard; the rest are MEASUREMENT. Unknown units fall
# back to a plain string sensor with no class.
_UNIT_MAP: dict[str, tuple[SensorDeviceClass | None, SensorStateClass, str]] = {
    "w": (SensorDeviceClass.POWER, _MEAS, UnitOfPower.WATT),
    "kw": (SensorDeviceClass.POWER, _MEAS, UnitOfPower.KILO_WATT),
    "wh": (SensorDeviceClass.ENERGY, _TOTAL, UnitOfEnergy.WATT_HOUR),
    "kwh": (SensorDeviceClass.ENERGY, _TOTAL, UnitOfEnergy.KILO_WATT_HOUR),
    "v": (SensorDeviceClass.VOLTAGE, _MEAS, UnitOfElectricPotential.VOLT),
    "a": (SensorDeviceClass.CURRENT, _MEAS, UnitOfElectricCurrent.AMPERE),
    "hz": (SensorDeviceClass.FREQUENCY, _MEAS, UnitOfFrequency.HERTZ),
    "°c": (SensorDeviceClass.TEMPERATURE, _MEAS, UnitOfTemperature.CELSIUS),
    "lux": (SensorDeviceClass.ILLUMINANCE, _MEAS, LIGHT_LUX),
    "lx": (SensorDeviceClass.ILLUMINANCE, _MEAS, LIGHT_LUX),
    # "%" is ambiguous (humidity, power factor, ...): keep the unit but assert no
    # device class rather than risk mislabelling.
    "%": (None, _MEAS, PERCENTAGE),
}

# Raw units we've already warned about, so each unmapped unit logs only once.
_warned_units: set[str] = set()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home sensors from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_sensors() -> None:
        """Add entities for any sensors not yet created (handles devices added later)."""
        new_entities: list[JungHomeQuantity] = []
        for device in coordinator.data or []:
            # Sockets expose energy quantities; Measurement functions (e.g. the
            # ambient readings on a presence detector) expose their own quantity
            # datapoints — both surface here as quantity sensors.
            if device.get("type") in ("Socket", "Measurement"):
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "quantity":
                        raw_label = datapoint_value(datapoint, "quantity_label")
                        label = raw_label.strip() if raw_label else None
                        unit = datapoint_value(datapoint, "quantity_unit")
                        if label and unit:
                            uid = stable_unique_id(
                                device, datapoint, label.replace(" ", "_").lower()
                            )
                            if uid not in known:
                                known.add(uid)
                                new_entities.append(
                                    JungHomeQuantity(
                                        coordinator, device, datapoint, label, unit
                                    )
                                )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_discover_sensors))


class JungHomeQuantity(JungHomeEntity, SensorEntity):
    """Representation of a Jung Home quantity."""

    # Secondary entity on the device; HA prepends the device name, so the
    # entity_id becomes `sensor.<device>_<quantity>` (the label is no longer
    # baked into the entity name).

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
        label: str,
        unit: str,
    ) -> None:
        """Initialize the quantity."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        self._attr_name = label
        self._name = f"{device.get('label', 'Jung Device')} {label}"  # for logging
        # Firmware-stable id derived from the label, not the volatile device id.
        self._attr_unique_id = stable_unique_id(
            device, datapoint, label.replace(" ", "_").lower()
        )
        mapped = _UNIT_MAP.get(unit.strip().lower())
        if mapped is not None:
            device_class, state_class, ha_unit = mapped
        else:
            # Unknown unit: expose a unitless measurement sensor (numeric, with
            # statistics) rather than a stateless string with an arbitrary unit.
            device_class, state_class, ha_unit = None, _MEAS, None
            if unit not in _warned_units:
                _warned_units.add(unit)
                _LOGGER.warning(
                    "Unmapped Jung Home quantity unit %r; exposing a unitless "
                    "measurement sensor",
                    unit,
                )
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = ha_unit
        self._value = self._get_value_from_datapoint(datapoint)

    @property
    def native_value(self) -> float | str | None:
        """Return the measured value (numeric when the unit has a state class)."""
        if self._value is None:
            return None
        if self._attr_state_class is not None:
            try:
                numeric = float(self._value)
            except (TypeError, ValueError):
                return None
            # Reject NaN/inf (which float() parses): they would pollute the
            # long-term-statistics / energy pipeline for a numeric sensor.
            return numeric if math.isfinite(numeric) else None
        # Unreachable: every sensor gets a state class (mapped, or MEASUREMENT for
        # an unknown unit), so this only exists to satisfy the return type.
        return self._value  # pragma: no cover

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Handling coordinator update for quantity %s", self._name)
        datapoint = self._find_datapoint(self._datapoint["id"])
        if datapoint:
            self._value = self._get_value_from_datapoint(datapoint)
            _LOGGER.debug("Updated state for quantity %s: %s", self._name, self._value)
            self.async_write_ha_state()

    def _get_value_from_datapoint(self, datapoint: Datapoint) -> str | None:
        """Extract the value of the quantity from its datapoint."""
        value = datapoint_value(datapoint, "quantity")
        return None if value is None else str(value)
