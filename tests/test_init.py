"""Integration setup / entity / lifecycle tests for Jung Home."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.diagnostics import (
    async_get_config_entry_diagnostics,
)

DEVICES = [
    {
        "id": "idlight1",
        "type": "OnOff",
        "label": "Hall Light",
        "datapoints": [
            {
                "id": "idlight1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            }
        ],
    },
    {
        "id": "idcolor1",
        "type": "ColorLight",
        "label": "Strip",
        "datapoints": [
            {
                "id": "idcolor1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "idcolor1-002",
                "type": "brightness",
                "values": [{"key": "brightness", "value": "50"}],
            },
            {
                "id": "idcolor1-004",
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": "2700"}],
            },
        ],
    },
    {
        "id": "iddim1",
        "type": "ColorLight",
        "label": "Dimmer",
        "datapoints": [
            {
                "id": "iddim1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            },
            {
                "id": "iddim1-002",
                "type": "brightness",
                "values": [{"key": "brightness", "value": "30"}],
            },
        ],
    },
    {
        "id": "idsock1",
        "type": "Socket",
        "label": "Boiler",
        "datapoints": [
            {
                "id": "idsock1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "idsock1-010",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "5"},
                    {"key": "quantity_label", "value": "Power "},
                    {"key": "quantity_unit", "value": "W"},
                ],
            },
            {
                "id": "idsock1-099",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "42"},
                    {"key": "quantity_label", "value": "Status "},
                    {"key": "quantity_unit", "value": "?"},
                ],
            },
        ],
    },
    {
        "id": "idrock1",
        "type": "RockerSwitch",
        "label": "Button A",
        "datapoints": [
            {
                "id": "idrock1-00c",
                "type": "up_request",
                "values": [{"key": "up_request", "value": "0"}],
            },
            {
                "id": "idrock1-00d",
                "type": "down_request",
                "values": [{"key": "down_request", "value": "0"}],
            },
            {
                "id": "idrock1-00e",
                "type": "status_led",
                "values": [{"key": "status_led", "value": "0"}],
            },
        ],
    },
]


async def _fake_run_websocket(self: JungHomeDataUpdateCoordinator) -> None:
    """Stand in for the real WebSocket: present a fake socket, then park."""
    ws = AsyncMock()
    ws.closed = False
    self.websocket = ws
    self.gateway_version = "1.5.0"
    await asyncio.Event().wait()


@pytest.fixture
async def init_integration(hass: HomeAssistant) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration with mocked gateway data and WebSocket."""
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
            AsyncMock(return_value=DEVICES),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_all_entity_types_created(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get("light.hall_light") is not None
    assert hass.states.get("light.strip").state == "on"
    assert hass.states.get("switch.boiler").state == "on"
    assert hass.states.get("sensor.boiler_power").state == "5.0"
    # Unknown unit -> no state class -> raw value passed straight through.
    assert hass.states.get("sensor.boiler_status").state == "42"
    assert hass.states.get("switch.button_a_status_led") is not None
    assert hass.states.get("event.button_a_up") is not None
    assert hass.states.get("event.button_a_down") is not None


async def test_light_commands(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 255, "color_temp_kelvin": 3000},
        blocking=True,
    )
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.hall_light"}, blocking=True
    )
    assert coordinator.websocket.send_str.called


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


async def test_state_update_via_websocket(
    hass: HomeAssistant, init_integration
) -> None:
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.hall_light").state == "on"
    # Unknown datapoint id and a groups/scenes list frame are handled gracefully.
    coordinator._handle_websocket_message(
        {"type": "datapoint", "data": {"id": "nope", "values": []}}
    )
    coordinator._handle_websocket_message({"type": "groups", "data": [{"id": "g"}]})
    coordinator._handle_websocket_message({"type": "datapoint", "data": "weird"})


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


async def test_colorlight_brightness_and_color_update(
    hass: HomeAssistant, init_integration
) -> None:
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-002",
                "values": [{"key": "brightness", "value": "80"}],
            },
        }
    )
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-004",
                "values": [{"key": "color_temperature", "value": "4000"}],
            },
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get("light.strip")
    assert state.attributes["color_temp_kelvin"] == 4000
    assert state.attributes["brightness"] == round(80 * 255 / 100)


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


async def test_light_echo_debounce(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    # Write brightness 255 (device raw 100); records the pending write.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 255},
        blocking=True,
    )
    # A transient echo with a *different* value is ignored (kept at 255).
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-002",
                "values": [{"key": "brightness", "value": "10"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == 255
    # The matching echo (raw 100) is accepted and clears the pending write.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-002",
                "values": [{"key": "brightness", "value": "100"}],
            },
        }
    )
    await hass.async_block_till_done()
    # Color temperature has the same debounce path.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "color_temp_kelvin": 3000},
        blocking=True,
    )
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-004",
                "values": [{"key": "color_temperature", "value": "5000"}],
            },
        }
    )
    await hass.async_block_till_done()
    # A transient colour-temp echo that differs from the write is debounced.
    assert hass.states.get("light.strip").attributes["color_temp_kelvin"] == 3000


async def test_diagnostics(hass: HomeAssistant, init_integration) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["device_count"] == len(DEVICES)
    assert diag["gateway_version"] == "1.5.0"
    assert diag["entry"]["data"][CONF_TOKEN] == "**REDACTED**"


async def test_stale_device_pruned(hass: HomeAssistant) -> None:
    """A registry device the gateway no longer reports is removed on setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    stale = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ghost_device")}
    )
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=DEVICES),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert dev_reg.async_get(stale.id) is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_legacy_unique_id_migrated(hass: HomeAssistant) -> None:
    """An old id-based entity is re-pointed to the label-based stable id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    # Pre-create a light entity under the old volatile-id unique_id scheme.
    ent_reg.async_get_or_create(
        Platform.LIGHT,
        DOMAIN,
        "idlight1_idlight1-001",
        config_entry=entry,
    )
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=DEVICES),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001")
        is not None
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
