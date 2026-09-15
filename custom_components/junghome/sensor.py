"""Sensor platform for Jung Home (socket energy quantities)."""

import logging
import math
from dataclasses import dataclass, replace

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    Platform,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, is_presence_quantity, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity, claim_new_entity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0

_MEAS = SensorStateClass.MEASUREMENT
_TOTAL = SensorStateClass.TOTAL_INCREASING


@dataclass(frozen=True, kw_only=True)
class JungHomeQuantityDescription(SensorEntityDescription):
    """Describes one quantity the gateway reports on a ``quantity`` datapoint.

    A ``quantity`` datapoint carries a free-text ``quantity_label`` and a
    ``quantity_unit``; a description matches on both, normalised (stripped,
    lowercased). ``gateway_units`` are the unit spellings the description
    covers (they pick the classes and the Home Assistant unit);
    ``gateway_label`` is the English label the gateway attaches to the quantity
    and decides whether the entity is *named* by ``translation_key`` — the
    English translation is that very label, so an English install reads the
    same as before, while every other locale gets a translated name. A
    ``None`` label marks a unit-only description: the classes apply, but the
    raw label stays the name (it may be anything, and there is nothing correct
    to translate it to).

    ``entity_registry_enabled_default=False`` on the electrical diagnostics
    (voltage, current, frequency — chatty values that mostly clutter the
    recorder) only affects entities registered for the first time: Home
    Assistant never re-disables an entity that is already in the registry, so
    existing installs keep those sensors exactly as they are.
    """

    gateway_units: frozenset[str] = frozenset()
    gateway_label: str | None = None


# Energy is TOTAL_INCREASING so it feeds the energy dashboard; the rest are
# MEASUREMENT. Where the gateway can spell a unit two ways ("°C"/"C" — climate.py
# accepts a bare "c" for the ambient reading, so it is accepted here too —
# "lux"/"lx") one description covers both; where the *scale* differs (W/kW,
# Wh/kWh) each scale is its own description under the same translation key.
QUANTITY_DESCRIPTIONS: tuple[JungHomeQuantityDescription, ...] = (
    JungHomeQuantityDescription(
        key="power",
        translation_key="power",
        gateway_label="power",
        gateway_units=frozenset({"w"}),
        device_class=SensorDeviceClass.POWER,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    JungHomeQuantityDescription(
        key="power_kw",
        translation_key="power",
        gateway_label="power",
        gateway_units=frozenset({"kw"}),
        device_class=SensorDeviceClass.POWER,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
    ),
    JungHomeQuantityDescription(
        key="energy",
        translation_key="energy",
        gateway_label="energy",
        gateway_units=frozenset({"kwh"}),
        device_class=SensorDeviceClass.ENERGY,
        state_class=_TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    JungHomeQuantityDescription(
        key="energy_wh",
        translation_key="energy",
        gateway_label="energy",
        gateway_units=frozenset({"wh"}),
        device_class=SensorDeviceClass.ENERGY,
        state_class=_TOTAL,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_display_precision=0,
    ),
    JungHomeQuantityDescription(
        key="voltage",
        translation_key="voltage",
        gateway_label="voltage",
        gateway_units=frozenset({"v"}),
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    JungHomeQuantityDescription(
        key="current",
        translation_key="current",
        gateway_label="current",
        gateway_units=frozenset({"a"}),
        device_class=SensorDeviceClass.CURRENT,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
    ),
    JungHomeQuantityDescription(
        key="frequency",
        translation_key="frequency",
        gateway_label="frequency",
        gateway_units=frozenset({"hz"}),
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
    ),
    JungHomeQuantityDescription(
        key="temperature",
        translation_key="temperature",
        gateway_label="temperature",
        gateway_units=frozenset({"°c", "c"}),
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=_MEAS,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    JungHomeQuantityDescription(
        key="illuminance",
        translation_key="illuminance",
        gateway_label="illuminance",
        gateway_units=frozenset({"lux", "lx"}),
        device_class=SensorDeviceClass.ILLUMINANCE,
        state_class=_MEAS,
        native_unit_of_measurement=LIGHT_LUX,
        suggested_display_precision=0,
    ),
    JungHomeQuantityDescription(
        key="humidity",
        translation_key="humidity",
        gateway_label="humidity",
        gateway_units=frozenset({"%"}),
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=_MEAS,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    # "%" is ambiguous (humidity, power factor, ...): unless the label says
    # humidity, keep the unit but assert no device class rather than risk
    # mislabelling.
    JungHomeQuantityDescription(
        key="percentage",
        gateway_units=frozenset({"%"}),
        state_class=_MEAS,
        native_unit_of_measurement=PERCENTAGE,
    ),
)


def quantity_description(
    label: str, unit: str | None
) -> JungHomeQuantityDescription | None:
    """Return the description for a ``quantity`` datapoint, or None for an unknown unit.

    The unit selects the candidate descriptions. The label then decides the
    name: when it is the label the gateway uses for that quantity, the
    matching description (translated name) is returned as is; any other label
    on a known unit keeps today's behaviour — the classes and unit of the
    quantity, the raw label as the name, no translation key, and enabled by
    default. For a unit with a unit-only description ("%" for anything but
    humidity) that description is the base; otherwise the quantity's own is.
    """
    norm_label = label.strip().casefold()
    norm_unit = (unit or "").strip().casefold()
    candidates = [d for d in QUANTITY_DESCRIPTIONS if norm_unit in d.gateway_units]
    for description in candidates:
        if description.gateway_label == norm_label:
            return description
    if not candidates:
        return None
    base = next((d for d in candidates if d.gateway_label is None), candidates[0])
    return replace(
        base,
        translation_key=None,
        name=label,
        gateway_label=None,
        entity_registry_enabled_default=True,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home sensors from a config entry."""
    coordinator = entry.runtime_data
    known = coordinator.known_unique_ids(Platform.SENSOR)

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
                        unit = datapoint_value(datapoint, "quantity_unit")
                        # Presence/occupancy (an empty-unit 0/1 flag) is a boolean
                        # state owned by the binary_sensor platform — skip it here
                        # so the two never double-expose the same datapoint. The
                        # unit is passed so a *measured* quantity whose label merely
                        # contains a keyword (e.g. "Motion Light Level" in lux) is
                        # NOT skipped and still becomes a numeric sensor.
                        if is_presence_quantity(raw_label, unit):
                            continue
                        label = raw_label.strip() if raw_label else None
                        # A unit is NOT required. An unrecognised unit already
                        # becomes a unitless measurement sensor below, so refusing
                        # a datapoint that simply has no unit was inconsistent —
                        # and it fell through binary_sensor too (that platform
                        # only claims presence-ish labels), so a labelled
                        # unit-less quantity such as a counter or an index got no
                        # entity at all and no log line. A label is still required:
                        # it is part of the unique_id (and, for an unknown
                        # quantity, the entity's name).
                        if label:
                            uid = stable_unique_id(
                                device, datapoint, label.replace(" ", "_").lower()
                            )
                            if claim_new_entity(known, uid):
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

    entity_description: JungHomeQuantityDescription

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
        label: str,
        unit: str | None,
    ) -> None:
        """Initialize the quantity."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        self._name = f"{device.get('label', 'Jung Device')} {label}"  # for logging
        # Firmware-stable id derived from the label, not the volatile device id.
        # The qualifier is the raw label, never the description key: the
        # unique_id must read the same before and after a quantity became a
        # described one, or every existing entity would be re-keyed.
        self._attr_unique_id = stable_unique_id(
            device, datapoint, label.replace(" ", "_").lower()
        )
        description = quantity_description(label, unit)
        if description is None:
            # Unknown or absent unit: expose a unitless measurement sensor
            # (numeric, with statistics) rather than a stateless string with an
            # arbitrary unit.
            description = JungHomeQuantityDescription(
                key="quantity", name=label, state_class=_MEAS
            )
            cleaned = (unit or "").strip()
            if not cleaned:
                # A quantity that genuinely carries no unit (a count, an index)
                # is expected, not a mapping gap — don't warn about it.
                _LOGGER.debug(
                    "Jung Home quantity %r has no unit; exposing a unitless "
                    "measurement sensor",
                    label,
                )
            elif cleaned not in coordinator.warned_quantity_units:
                # Warn once per unit per entry (the set lives on the
                # coordinator, so it resets on reload and is not shared
                # between two gateways' entries).
                coordinator.warned_quantity_units.add(cleaned)
                _LOGGER.warning(
                    "Unmapped Jung Home quantity unit %r; exposing a unitless "
                    "measurement sensor",
                    unit,
                )
        self.entity_description = description
        self._value = self._get_value_from_datapoint(datapoint)

    @property
    def native_value(self) -> float | None:
        """Return the measured value as a number.

        Every quantity is numeric (each description carries a state class,
        the unknown-unit fallback included), so anything that does not parse
        as a finite float reads as unknown: NaN/inf — which ``float()`` happily
        parses — would otherwise pollute the long-term-statistics / energy
        pipeline.
        """
        if self._value is None:
            return None
        try:
            numeric = float(self._value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._skip_foreign_device_push():
            return  # another device's push; nothing about this quantity changed
        _LOGGER.debug("Handling coordinator update for quantity %s", self._name)
        datapoint = self._find_datapoint(self._datapoint["id"])
        if datapoint:
            self._value = self._get_value_from_datapoint(datapoint)
            _LOGGER.debug("Updated state for quantity %s: %s", self._name, self._value)
        # Write unconditionally (even when the datapoint is momentarily absent)
        # so the entity's availability tracks the gateway on every coordinator
        # update that reaches here (see `_skip_foreign_device_push`), matching
        # the switch platform.
        self.async_write_ha_state()

    def _get_value_from_datapoint(self, datapoint: Datapoint) -> str | None:
        """Extract the value of the quantity from its datapoint."""
        value = datapoint_value(datapoint, "quantity")
        return None if value is None else str(value)
