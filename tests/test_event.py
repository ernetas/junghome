"""RockerSwitch event platform tests for Jung Home."""

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.event import JungHomeEventEntity


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


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
    coordinator = _bare_coordinator(hass)
    device = {"id": "d", "type": "RockerSwitch", "label": "Btn", "datapoints": []}
    datapoint = {"id": "d-x", "type": "weird_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    # No matching translation key -> _attr_name is set to the raw dp type.
    assert entity._attr_name == "weird_request"


async def test_event_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    """_handle_coordinator_update returns early when the device is gone."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "gone", "type": "RockerSwitch", "label": "G", "datapoints": []}
    datapoint = {"id": "gone-c", "type": "up_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    # coordinator.data is [] so the device lookup yields None -> early return.
    with patch.object(entity, "async_write_ha_state") as write_state:
        entity._handle_coordinator_update()  # must not raise
    write_state.assert_called_once()
