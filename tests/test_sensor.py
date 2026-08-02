"""Numeric sensor platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.sensor import JungHomeQuantity
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
