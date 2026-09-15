"""Numeric sensor platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    CONF_HOST,
    CONF_TOKEN,
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import UNDEFINED
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.sensor import (
    JungHomeQuantity,
    quantity_description,
)
from tests.conftest import _fake_run_websocket, bare_coordinator


async def test_sensor_native_value_non_numeric_returns_none(
    hass: HomeAssistant, init_integration
) -> None:
    """A non-numeric value on a unitless MEASUREMENT sensor yields native_value None."""
    coordinator = init_integration.runtime_data
    # sensor.boiler_status is the unknown-unit ("?") MEASUREMENT sensor.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idsock1-099",
                "values": [{"key": "quantity", "value": "not-a-number"}],
            },
        }
    )
    await hass.async_block_till_done()
    # float("not-a-number") -> ValueError -> native_value None -> "unknown".
    assert hass.states.get("sensor.boiler_status").state == "unknown"


async def test_sensor_value_extractor_defensive(hass: HomeAssistant) -> None:
    """Sensor helpers return None for a missing value / None state."""
    coordinator = bare_coordinator(hass)
    device = {"id": "s", "type": "Socket", "label": "S", "datapoints": []}
    dp = {
        "id": "s-1",
        "values": [
            {"key": "quantity", "value": "5"},
            {"key": "quantity_label", "value": "P"},
            {"key": "quantity_unit", "value": "W"},
        ],
    }
    q = JungHomeQuantity(coordinator, device, dp, "P", "W")
    # No "quantity" key -> None.
    assert q._get_value_from_datapoint({"id": "x", "values": []}) is None
    # native_value is None when the stored value is None.
    q._value = None
    assert q.native_value is None
    # NaN/inf parse through float() but must not reach a numeric sensor's state.
    for bad in ("nan", "inf", "-inf"):
        q._value = bad
        assert q.native_value is None


async def test_measurement_sensor_created(
    hass: HomeAssistant, init_integration
) -> None:
    """A Measurement function's quantity surfaces as a sensor (lux -> illuminance)."""
    state = hass.states.get("sensor.hallway_sensor_illuminance")
    assert state is not None
    assert state.state == "120.0"
    assert state.attributes["unit_of_measurement"] == "lx"
    assert state.attributes["device_class"] == "illuminance"


async def test_sensor_native_value_rejects_nan(hass: HomeAssistant) -> None:
    """A NaN reading on a numeric sensor yields None (never pollutes statistics)."""
    coordinator = bare_coordinator(hass)
    device = {"id": "s", "type": "Socket", "label": "S", "datapoints": []}
    dp = {"id": "s-1", "values": [{"key": "quantity", "value": "nan"}]}
    # An unknown unit makes a numeric MEASUREMENT sensor; NaN must read as None.
    quantity = JungHomeQuantity(coordinator, device, dp, "Status", "?")
    assert quantity.native_value is None


async def test_all_sensor_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every sensor entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.SENSOR)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


def _measurement_device(unit: str | None, label: str = "Cycle Count") -> dict:
    """A Measurement device whose quantity carries `unit` (None = key absent)."""
    values: list[dict] = [
        {"key": "quantity", "value": "7"},
        {"key": "quantity_label", "value": label},
    ]
    if unit is not None:
        values.append({"key": "quantity_unit", "value": unit})
    return {
        "id": "idmeas1",
        "type": "Measurement",
        "label": "Boiler",
        "datapoints": [{"id": "idmeas1-001", "type": "quantity", "values": values}],
    }


@pytest.mark.parametrize("unit", [None, "", "   "])
async def test_quantity_without_a_unit_still_gets_a_sensor(
    hass: HomeAssistant, unit: str | None
) -> None:
    """A labelled quantity with no unit must not vanish.

    sensor.py required a unit, and binary_sensor only claims presence-ish labels,
    so a unit-less quantity (a counter, an index) fell through both platforms —
    no entity, no log. An *unrecognised* unit already became a unitless
    measurement sensor, so refusing an absent one was inconsistent.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_measurement_device(unit)]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.boiler_cycle_count")
    assert state is not None, "a unit-less quantity produced no entity"
    assert state.state == "7.0"
    # Unitless measurement: numeric with statistics, but no unit or device class.
    assert state.attributes.get("unit_of_measurement") is None
    assert state.attributes.get("state_class") == "measurement"
    assert state.attributes.get("device_class") is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_presence_labelled_quantity_still_goes_to_binary_sensor(
    hass: HomeAssistant,
) -> None:
    """The split point is unchanged: presence labels are not numeric sensors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.5",
        data={CONF_HOST: "1.2.3.5", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    device = _measurement_device(None, label="Presence Detected")
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[device]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.boiler_presence_detected") is None
    assert hass.states.get("binary_sensor.boiler_presence_detected") is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


_MEAS = SensorStateClass.MEASUREMENT
_TOTAL = SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize(
    ("label", "unit", "translation_key", "device_class", "ha_unit"),
    [
        ("Power ", "W", "power", SensorDeviceClass.POWER, UnitOfPower.WATT),
        ("power", " kW ", "power", SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
        ("Energy", "kWh", "energy", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
        ("Energy", "Wh", "energy", SensorDeviceClass.ENERGY, UnitOfEnergy.WATT_HOUR),
        ("Voltage", "V", "voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
        ("Current", "A", "current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
        ("Frequency", "Hz", "frequency", SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
        ("Temperature ", "°C", "temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("Temperature", "C", "temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("Illuminance ", "lux", "illuminance", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX),
        ("Illuminance", "lx", "illuminance", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX),
        ("Humidity", "%", "humidity", SensorDeviceClass.HUMIDITY, PERCENTAGE),
    ],
)  # fmt: skip
def test_known_quantity_gets_a_translated_description(
    label: str,
    unit: str,
    translation_key: str,
    device_class: SensorDeviceClass,
    ha_unit: str,
) -> None:
    """The gateway's own label + unit for a quantity selects its description.

    Matching is on the normalised label and unit (the gateway pads labels with
    a trailing space); the description names the entity through its
    translation key, so it carries no literal name. Energy is the one
    TOTAL_INCREASING quantity (it feeds the energy dashboard); the electrical
    diagnostics (V/A/Hz) are the only ones disabled for new registrations.
    """
    description = quantity_description(label, unit)
    assert description is not None
    assert description.translation_key == translation_key
    assert description.name is UNDEFINED
    assert description.device_class is device_class
    assert description.native_unit_of_measurement == ha_unit
    assert description.state_class is (_TOTAL if translation_key == "energy" else _MEAS)
    assert description.suggested_display_precision is not None
    assert description.entity_registry_enabled_default is (
        translation_key not in ("voltage", "current", "frequency")
    )


@pytest.mark.parametrize(
    ("label", "unit", "device_class", "ha_unit"),
    [
        # A known unit under a label that is not the quantity's: classes from
        # the unit, the raw label as the name (today's behaviour).
        ("Heating Power", "W", SensorDeviceClass.POWER, UnitOfPower.WATT),
        # ... and never disabled by default, even for a diagnostic unit.
        ("Battery Voltage", "V", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
        # "%" is ambiguous: anything but humidity keeps the unit, no class.
        ("Power Factor", "%", None, PERCENTAGE),
    ],
)  # fmt: skip
def test_unknown_label_on_a_known_unit_keeps_the_raw_label(
    label: str, unit: str, device_class: SensorDeviceClass | None, ha_unit: str
) -> None:
    """A label the gateway does not use for the quantity is not translated."""
    description = quantity_description(label, unit)
    assert description is not None
    assert description.translation_key is None
    assert description.name == label
    assert description.device_class is device_class
    assert description.native_unit_of_measurement == ha_unit
    assert description.state_class is _MEAS
    assert description.entity_registry_enabled_default is True


@pytest.mark.parametrize("unit", ["?", "", "   ", None])
def test_unknown_unit_has_no_description(unit: str | None) -> None:
    """An unknown or absent unit is the caller's unitless fallback."""
    assert quantity_description("Status", unit) is None


def _socket_device(*quantities: tuple[str, str, str]) -> dict:
    """A Socket "Boiler" whose quantity datapoints carry (label, unit, value)."""
    return {
        "id": "idsock1",
        "type": "Socket",
        "label": "Boiler",
        "datapoints": [
            {
                "id": f"idsock1-{index:03d}",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": value},
                    {"key": "quantity_label", "value": label},
                    {"key": "quantity_unit", "value": unit},
                ],
            }
            for index, (label, unit, value) in enumerate(quantities, start=10)
        ],
    }


async def test_known_quantity_is_named_by_translation(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_platform
) -> None:
    """A known quantity is named through `entity.sensor.<key>.name`.

    In German the gateway's "Power " becomes "Leistung", while the unique_id
    is exactly what it was when the raw label was the name — the identity
    must not move with the naming. (The entity_id of a *new* registration
    follows Home Assistant's own rule — native-language object ids for the
    locales it lists — and an existing registration's entity_id is sticky
    either way, so it is deliberately not pinned here.) A W-unit quantity
    under any other label keeps that label as its name.
    """
    hass.config.language = "de"
    await init_platform(
        Platform.SENSOR,
        [_socket_device(("Power ", "W", "5"), ("Heating Power", "W", "3"))],
    )

    entity_id = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, "boiler_010_power"
    )
    assert entity_id is not None
    power = hass.states.get(entity_id)
    assert power is not None
    assert power.attributes["friendly_name"] == "Boiler Leistung"
    assert power.attributes["device_class"] == "power"
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.translation_key == "power"

    heating = hass.states.get("sensor.boiler_heating_power")
    assert heating is not None
    assert heating.attributes["friendly_name"] == "Boiler Heating Power"
    assert heating.attributes["device_class"] == "power"
    entry = entity_registry.async_get("sensor.boiler_heating_power")
    assert entry is not None
    assert entry.unique_id == "boiler_011_heating_power"
    assert entry.translation_key is None


async def test_electrical_diagnostics_disabled_only_for_new_registrations(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_platform
) -> None:
    """Voltage/current/frequency start disabled — unless already registered.

    `entity_registry_enabled_default=False` only applies the first time an
    entity is registered. An install that already has the voltage sensor
    (registered before the descriptions existed, enabled) must keep it
    enabled and reporting; the current and frequency sensors it never had
    are registered disabled, with no state.
    """
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        "boiler_011_voltage",
        suggested_object_id="boiler_voltage",
    )
    entry = await init_platform(
        Platform.SENSOR,
        [
            _socket_device(
                ("Power ", "W", "5"),
                ("Voltage", "V", "230"),
                ("Current", "A", "0.5"),
                ("Frequency", "Hz", "50"),
            )
        ],
    )

    assert hass.states.get("sensor.boiler_power").state == "5.0"
    voltage = entity_registry.async_get("sensor.boiler_voltage")
    assert voltage is not None
    assert voltage.disabled_by is None
    assert hass.states.get("sensor.boiler_voltage").state == "230.0"
    for object_id in ("boiler_current", "boiler_frequency"):
        registered = entity_registry.async_get(f"sensor.{object_id}")
        assert registered is not None, object_id
        assert registered.config_entry_id == entry.entry_id
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(f"sensor.{object_id}") is None
