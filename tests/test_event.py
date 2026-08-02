"""RockerSwitch event platform tests for Jung Home."""

from unittest.mock import patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.event import JungHomeEventEntity
from tests.conftest import bare_coordinator


async def test_event_pressed_and_depressed(
    hass: HomeAssistant, init_integration
) -> None:
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00c",
                "values": [{"key": "up_request", "value": "1"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.button_a_up").attributes["event_type"] == "pressed"
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00c",
                "values": [{"key": "up_request", "value": "0"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.button_a_up").attributes["event_type"] == "depressed"


async def test_event_fires_on_each_push_not_on_rest_reread(
    hass: HomeAssistant, init_integration
) -> None:
    """Fire-on-push: every WS edge fires (even repeats); REST re-reads do not."""
    coordinator = init_integration.runtime_data
    press_frame = {
        "type": "datapoint",
        "data": {"id": "idrock1-00c", "values": [{"key": "up_request", "value": "1"}]},
    }
    with patch.object(JungHomeEventEntity, "_trigger_event") as mock_trigger:
        # Two identical-value pushes: a level diff would coalesce these into a
        # single (or zero) events; fire-on-push fires each genuine edge.
        coordinator._handle_websocket_message(press_frame)
        coordinator._handle_websocket_message(press_frame)
        await hass.async_block_till_done()
        assert mock_trigger.call_count == 2
        assert [c.args[0] for c in mock_trigger.call_args_list] == [
            "pressed",
            "pressed",
        ]

        # A REST poll re-reads the same datapoint values, but the coordinator's
        # pushed-datapoint marker is None for non-WS updates, so nothing fires.
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
        assert mock_trigger.call_count == 2


async def test_event_unknown_datapoint_type_uses_name(hass: HomeAssistant) -> None:
    """A datapoint type with no translation key falls back to a plain name."""
    coordinator = bare_coordinator(hass)
    device = {"id": "d", "type": "RockerSwitch", "label": "Btn", "datapoints": []}
    datapoint = {"id": "d-x", "type": "weird_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    # No matching translation key -> _attr_name is set to the raw dp type.
    assert entity._attr_name == "weird_request"


async def test_event_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    """_handle_coordinator_update returns early when the device is gone."""
    coordinator = bare_coordinator(hass)
    device = {"id": "gone", "type": "RockerSwitch", "label": "G", "datapoints": []}
    datapoint = {"id": "gone-c", "type": "up_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    # coordinator.data is [] so the device lookup yields None -> early return.
    with patch.object(entity, "async_write_ha_state") as write_state:
        entity._handle_coordinator_update()  # must not raise
    write_state.assert_called_once()


async def test_fire_bus_event_skipped_without_device_entry(
    hass: HomeAssistant,
) -> None:
    """No bus event is emitted for an entity not yet in the device registry.

    A device trigger is keyed on the registry device id, so an event without
    one would match nothing; the early return must not raise either.
    """
    coordinator = bare_coordinator(hass)
    device = {"id": "d", "type": "RockerSwitch", "label": "Btn", "datapoints": []}
    datapoint = {"id": "d-c", "type": "up_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    entity.hass = hass
    fired: list[object] = []
    hass.bus.async_listen("junghome_button_action", fired.append)

    entity._fire_bus_event("pressed")  # device_entry is None: must no-op
    await hass.async_block_till_done()
    assert fired == []


async def test_all_event_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every event entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.EVENT)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
