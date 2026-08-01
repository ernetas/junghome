"""Switch / socket platform tests for Jung Home."""

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.switch import JungHomeSocket, JungHomeSwitch


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


async def test_switch_and_socket_commands(
    hass: HomeAssistant, init_integration
) -> None:
    for entity in ("switch.boiler", "switch.button_a_status_led"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity}, blocking=True
        )
    assert init_integration.runtime_data.websocket.send_str.called


async def test_status_led_update(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00e",
                "values": [{"key": "status_led", "value": "1"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.button_a_status_led").state == "on"


async def test_socket_state_helper_defaults_off(hass: HomeAssistant) -> None:
    """A socket datapoint without a switch value reads as off (helper fallback)."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "d", "type": "Socket", "label": "Sock", "datapoints": []}
    # No "switch" key in values -> _get_state_from_datapoint returns False.
    socket = JungHomeSocket(coordinator, device, {"id": "d-1", "values": []})
    assert socket.is_on is False

    # And the status-LED helper likewise defaults off without a status_led value.
    led_dev = {"id": "r", "type": "RockerSwitch", "label": "R", "datapoints": []}
    led = JungHomeSwitch(coordinator, led_dev, {"id": "r-1", "values": []})
    assert led.is_on is False


async def test_switch_led_handle_update_missing_device_noops(
    hass: HomeAssistant,
) -> None:
    """JungHomeSwitch._handle_coordinator_update returns early when device is gone."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "gone", "type": "RockerSwitch", "label": "G", "datapoints": []}
    datapoint = {"id": "gone-e", "type": "status_led", "values": []}
    entity = JungHomeSwitch(coordinator, device, datapoint)
    with patch.object(entity, "async_write_ha_state") as write_state:
        entity._handle_coordinator_update()  # must not raise
    write_state.assert_called_once()


async def test_command_failure_when_ws_down_surfaces(
    hass: HomeAssistant, init_integration
) -> None:
    """With the socket down, a command raises and optimistic state isn't applied."""
    coordinator = init_integration.runtime_data
    coordinator.websocket.closed = True  # simulate a dropped WebSocket
    assert hass.states.get("switch.boiler").state == "on"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.boiler"}, blocking=True
        )
    # The optimistic "off" must NOT have been written since the send failed.
    assert hass.states.get("switch.boiler").state == "on"


async def test_all_switch_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every switch entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.SWITCH)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
