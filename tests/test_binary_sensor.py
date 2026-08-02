"""Presence (occupancy) binary_sensor platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.binary_sensor import JungHomePresence
from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from tests.conftest import PRISTINE_DEVICES, _fake_run_websocket, bare_coordinator

# A presence detector to hang off the shared device list for the snapshot
# test. Presence is reported as a quantity datapoint with an *empty* unit (the
# detector's 0/1 flag), which is what makes the binary_sensor platform claim it
# instead of the numeric sensor platform.
PRESENCE_DEVICE = {
    "id": "idpres1",
    "type": "Measurement",
    "label": "Office Detector",
    "datapoints": [
        {
            "id": "idpres1-010",
            "type": "quantity",
            "values": [
                {"key": "quantity", "value": "1"},
                {"key": "quantity_label", "value": "Presence "},
                {"key": "quantity_unit", "value": ""},
            ],
        },
    ],
}


def _presence_device(value: str) -> dict:
    """A JUNG BWM-style detector: presence as a unit-less 0/1 quantity."""
    return {
        "id": "idbwm1",
        "type": "Measurement",
        "label": "Hallway Motion",
        "datapoints": [
            {
                "id": "idbwm1-001",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": value},
                    {"key": "quantity_label", "value": "Presence Detected"},
                    {"key": "quantity_unit", "value": ""},
                ],
            }
        ],
    }


async def test_presence_binary_sensor_states(hass: HomeAssistant) -> None:
    """Presence maps 1->on, 0->off, and NaN/missing to unknown (None)."""
    coordinator = bare_coordinator(hass)
    device = _presence_device("1")
    dp = device["datapoints"][0]
    sensor = JungHomePresence(coordinator, device, dp, "Presence Detected")
    assert sensor.is_on is True
    assert sensor.device_class == BinarySensorDeviceClass.OCCUPANCY

    extract = sensor._get_state_from_datapoint
    assert extract({"id": "x", "values": [{"key": "quantity", "value": "0"}]}) is False
    # "NaN" parses through float() but must read as unknown, not truthy.
    assert extract({"id": "x", "values": [{"key": "quantity", "value": "NaN"}]}) is None
    # A non-numeric value (unparseable) is unknown, not an error.
    assert extract({"id": "x", "values": [{"key": "quantity", "value": "?"}]}) is None
    assert extract({"id": "x", "values": []}) is None
    assert extract(None) is None


async def test_presence_binary_sensor_updates_on_push(hass: HomeAssistant) -> None:
    """A coordinator update re-reads the presence state from stored data."""
    coordinator = bare_coordinator(hass)
    device = _presence_device("0")
    dp = device["datapoints"][0]
    coordinator.data = [device]
    sensor = JungHomePresence(coordinator, device, dp, "Presence Detected")
    assert sensor.is_on is False
    dp["values"][0]["value"] = "1"  # motion detected
    with patch.object(sensor, "async_write_ha_state"):
        sensor._handle_coordinator_update()
    assert sensor.is_on is True


async def test_presence_discovered_and_split_from_numeric_sensor(
    hass: HomeAssistant,
) -> None:
    """A presence quantity becomes an occupancy binary_sensor, not a sensor."""
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_presence_device("1")]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="1.2.3.4",
            data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        bs = hass.states.get("binary_sensor.hallway_motion_presence_detected")
        assert bs is not None
        assert bs.state == "on"
        assert bs.attributes["device_class"] == "occupancy"
        # The same datapoint must NOT also surface as a numeric sensor.
        assert hass.states.get("sensor.hallway_motion_presence_detected") is None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_presence_binary_sensor_keeps_state_when_datapoint_gone(
    hass: HomeAssistant,
) -> None:
    """A vanished datapoint holds the last state rather than resetting to off.

    When the coordinator no longer carries the datapoint,
    ``_handle_coordinator_update`` must leave the previous reading untouched
    instead of writing ``None``/off — but it still writes HA state, so the
    entity's availability tracks the gateway on every update (matching the
    switch platform).
    """
    coordinator = bare_coordinator(hass)
    device = _presence_device("1")
    dp = device["datapoints"][0]
    sensor = JungHomePresence(coordinator, device, dp, "Presence Detected")
    assert sensor.is_on is True

    coordinator.data = []  # device/datapoint dropped from the latest poll
    with patch.object(sensor, "async_write_ha_state") as write_state:
        sensor._handle_coordinator_update()  # must not raise
    write_state.assert_called_once()  # availability still tracked
    assert sensor.is_on is True  # last-known state preserved, not cleared


async def test_presence_binary_sensor_discovered_on_any_device_type(
    hass: HomeAssistant,
) -> None:
    """Presence discovery matches on the datapoint, not the device type.

    ``binary_sensor.py`` is deliberately device-type-agnostic (every other
    platform gates on ``device['type']``). A presence quantity riding on a
    device whose type no other platform claims must still surface as an
    occupancy binary_sensor — and never as a numeric sensor.
    """
    device = {
        "id": "idbwm2",
        "type": "BWM",  # not claimed by light/switch/sensor/event/cover/climate
        "label": "Garage Detector",
        "datapoints": [
            {
                "id": "idbwm2-001",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "1"},
                    {"key": "quantity_label", "value": "Occupancy"},
                    {"key": "quantity_unit", "value": ""},
                ],
            }
        ],
    }
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
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="1.2.3.4",
            data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        bs = hass.states.get("binary_sensor.garage_detector_occupancy")
        assert bs is not None
        assert bs.state == "on"
        assert bs.attributes["device_class"] == "occupancy"
        # The unusual device type must not spawn a numeric sensor for it.
        assert hass.states.get("sensor.garage_detector_occupancy") is None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_presence_binary_sensor_rediscovery_is_idempotent(
    hass: HomeAssistant,
) -> None:
    """Re-running discovery for a known presence datapoint adds no duplicate.

    The discovery callback is a coordinator listener and re-fires on every
    update; a datapoint whose stable uid is already ``known`` must be skipped
    (the ``if uid not in known`` guard), so a fresh poll of the same device
    creates no second entity.
    """
    device = _presence_device("1")
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
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="1.2.3.4",
            data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)

        def _presence_entities() -> list:
            # Filter to occupancy sensors: the entry also has a gateway
            # connectivity binary_sensor, which is not a presence entity.
            return [
                e
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.domain == "binary_sensor"
                and e.original_device_class == BinarySensorDeviceClass.OCCUPANCY
            ]

        assert len(_presence_entities()) == 1

        # A REST poll re-runs discovery for the same (already-known) datapoint.
        entry.runtime_data.async_set_updated_data([device])
        await hass.async_block_till_done()

        assert len(_presence_entities()) == 1  # dedup guard: no duplicate added

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_all_binary_sensor_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every binary sensor entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.

    The shared device list carries no presence detector, so this one test adds
    one on top of it — otherwise the snapshot would only ever pin the gateway
    connectivity sensor and never the per-device presence entity.
    """
    entry = await init_platform(
        Platform.BINARY_SENSOR, [*PRISTINE_DEVICES, PRESENCE_DEVICE]
    )
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
