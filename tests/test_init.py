"""Integration setup / entity / lifecycle tests for Jung Home."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome import async_unload_entry
from custom_components.junghome.const import DOMAIN, device_slug
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.junghome.event import JungHomeEventEntity
from custom_components.junghome.light import JungHomeLight
from custom_components.junghome.sensor import JungHomeQuantity
from custom_components.junghome.switch import JungHomeSocket, JungHomeSwitch

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
    self.ws_connected = True
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
    # Unknown unit ("?") -> unitless MEASUREMENT sensor (no unit) -> value floated.
    assert hass.states.get("sensor.boiler_status").state == "42.0"
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
    # Unknown datapoint id, a groups/scenes list frame, and a non-dict data frame
    # are all handled gracefully and must not disturb existing state.
    coordinator._handle_websocket_message(
        {"type": "datapoint", "data": {"id": "nope", "values": []}}
    )
    coordinator._handle_websocket_message({"type": "groups", "data": [{"id": "g"}]})
    coordinator._handle_websocket_message({"type": "datapoint", "data": "weird"})
    await hass.async_block_till_done()
    # Prior state survives the no-op frames.
    assert hass.states.get("light.hall_light").state == "on"


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


async def test_light_external_change_applied(
    hass: HomeAssistant, init_integration
) -> None:
    """Optimistic echo suppression was removed; the light trusts coordinator state.

    An external change pushed by the gateway is applied immediately, not
    suppressed in favour of the last commanded value.
    """
    coordinator = init_integration.runtime_data
    # Command brightness 255 (device raw 100).
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 255},
        blocking=True,
    )
    # An external brightness change (device raw 10) WINS now.
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
    assert hass.states.get("light.strip").attributes["brightness"] == round(
        10 * 255 / 100
    )
    # Command colour temp 3000K.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "color_temp_kelvin": 3000},
        blocking=True,
    )
    # An external colour-temp change (5000K) WINS now.
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
    assert hass.states.get("light.strip").attributes["color_temp_kelvin"] == 5000


async def test_diagnostics(hass: HomeAssistant, init_integration) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["device_count"] == len(DEVICES)
    assert diag["gateway_version"] == "1.5.0"
    assert diag["entry"]["data"][CONF_TOKEN] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"


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


async def test_migration_not_marked_done_on_failure(hass: HomeAssistant) -> None:
    """If migration raises at the top level, the entry isn't flagged migrated.

    Leaving ``stable_ids_migrated`` unset means setup retries the migration on the
    next load instead of silently skipping it forever.
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
            AsyncMock(return_value=DEVICES),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
        patch(
            "custom_components.junghome.er.async_entries_for_config_entry",
            side_effect=RuntimeError("boom"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Setup still succeeds, but the migration flag must NOT be set (so it retries).
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_host_change_triggers_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """A stored host change reloads the entry (the coordinator caches the host)."""
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: "9.9.9.9"}
        )
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)


async def test_token_only_change_no_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """A token-only update (host unchanged) must not trigger a reload."""
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: "newtok"}
        )
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_entity_availability_tracks_connection(
    hass: HomeAssistant, init_integration
) -> None:
    """available follows ws_connected OR last_update_success, on ALL platforms.

    All four platforms (light, socket+LED switch, sensor, event) must agree: a
    transient REST-poll miss with a live WebSocket must not knock the LED/event
    entities offline while the light/socket on the same device stay up.
    """
    coordinator = init_integration.runtime_data
    # One entity per platform, including the secondary LED switch and an event.
    entities = (
        "light.strip",
        "switch.boiler",
        "switch.button_a_status_led",
        "sensor.boiler_power",
        "event.button_a_up",
    )

    def states() -> set[str]:
        return {
            "unavailable" if hass.states.get(e).state == "unavailable" else "available"
            for e in entities
        }

    # WS up, REST failing -> everything available (WS is the liveness signal).
    coordinator.ws_connected = True
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states() == {"available"}

    # WS down, last REST poll ok -> still available (fallback).
    coordinator.ws_connected = False
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states() == {"available"}

    # Both down -> everything unavailable, together.
    coordinator.ws_connected = False
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states() == {"unavailable"}


async def test_websocket_message_guard_without_data(hass: HomeAssistant) -> None:
    """A datapoint frame arriving before the first refresh must not raise.

    The ``for device in self.data or []`` guard tolerates ``data`` being None.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = None
    # Must not raise despite data being None.
    coordinator._handle_websocket_message(
        {"type": "datapoint", "data": {"id": "x", "values": []}}
    )


async def test_failed_platform_unload_still_stops_coordinator(
    hass: HomeAssistant,
) -> None:
    """A failed platform unload must still stop the coordinator's WS task."""
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

    coordinator = entry.runtime_data
    with (
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        ),
        patch.object(coordinator, "stop", AsyncMock(wraps=coordinator.stop)) as stop,
    ):
        # Call the unload handler directly so the failed-platform-unload path is
        # exercised without leaving the entry half-torn-down in HA's state machine.
        result = await async_unload_entry(hass, entry)
        await hass.async_block_till_done()
    stop.assert_awaited()
    assert coordinator._ws_task is None
    # Unload reports failure (platforms didn't unload) but cleanup still happened.
    assert result is False
    # Tear down cleanly now that the WS task is stopped.
    await coordinator.async_shutdown()


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


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


async def test_event_unknown_datapoint_type_uses_name(hass: HomeAssistant) -> None:
    """A datapoint type with no translation key falls back to a plain name."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "d", "type": "RockerSwitch", "label": "Btn", "datapoints": []}
    datapoint = {"id": "d-x", "type": "weird_request", "values": []}
    entity = JungHomeEventEntity(coordinator, device, datapoint)
    # No matching translation key -> _attr_name is set to the raw dp type.
    assert entity._attr_name == "weird_request"


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


def _color_light(coordinator: JungHomeDataUpdateCoordinator) -> JungHomeLight:
    device = {
        "id": "c",
        "type": "ColorLight",
        "label": "C",
        "datapoints": [
            {
                "id": "c-1",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "c-2",
                "type": "brightness",
                "values": [{"key": "brightness", "value": "50"}],
            },
            {
                "id": "c-4",
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": "3000"}],
            },
        ],
    }
    return JungHomeLight(coordinator, device, device["datapoints"][0])


async def test_light_value_extractors_are_defensive(hass: HomeAssistant) -> None:
    """The light value extractors tolerate missing/garbage datapoints."""
    light = _color_light(_bare_coordinator(hass))
    # Missing datapoint -> safe defaults (0 / None), never an exception.
    assert light._get_brightness_from_datapoint(None) == 0
    assert light._get_color_temp_from_datapoint(None) is None
    # Unparseable values -> 0 / None.
    assert (
        light._get_brightness_from_datapoint(
            {"id": "x", "values": [{"key": "brightness", "value": "NaN"}]}
        )
        == 0
    )
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "NaN"}]}
        )
        is None
    )
    # No matching key -> defaults.
    assert light._get_brightness_from_datapoint({"id": "x", "values": []}) == 0
    assert light._get_color_temp_from_datapoint({"id": "x", "values": []}) is None
    # State helper with no switch key -> off.
    assert light._get_state_from_datapoint({"id": "x", "values": []}) is False


async def test_light_set_without_datapoints_warns_and_noops(
    hass: HomeAssistant,
) -> None:
    """Setting brightness/colour-temp on a light lacking those datapoints no-ops."""
    coordinator = _bare_coordinator(hass)
    device = {
        "id": "o",
        "type": "OnOff",
        "label": "O",
        "datapoints": [
            {"id": "o-1", "type": "switch", "values": [{"key": "switch", "value": "0"}]}
        ],
    }
    light = JungHomeLight(coordinator, device, device["datapoints"][0])
    assert light._brightness_datapoint_id is None
    assert light._color_temp_datapoint_id is None
    # No datapoint ids -> warn + return without sending anything (no websocket).
    with (
        patch.object(coordinator, "set_brightness", AsyncMock()) as sb,
        patch.object(coordinator, "set_color_temp", AsyncMock()) as sc,
    ):
        await light._set_brightness(100)
        await light._set_color_temp(3000)
    sb.assert_not_called()
    sc.assert_not_called()


async def test_brightness_floor_keeps_dim_on(hass: HomeAssistant) -> None:
    """A non-zero HA brightness never rounds to device raw 0 (which reads as off)."""
    light = _color_light(_bare_coordinator(hass))
    assert light._ha_to_raw_brightness(0) == 0
    # round(1 * 100 / 255) == 0 without the floor; the floor keeps it on at 1.
    assert light._ha_to_raw_brightness(1) == 1
    assert light._ha_to_raw_brightness(255) == 100


async def test_sensor_value_extractor_defensive(hass: HomeAssistant) -> None:
    """Sensor helpers return None for a missing value / None state."""
    coordinator = _bare_coordinator(hass)
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


async def _setup_with_registry(hass: HomeAssistant, prepare) -> MockConfigEntry:
    """Create an entry, let `prepare(entry)` seed the registries, then set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    prepare(entry)
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
    return entry


async def test_migration_repoints_device_identifier(hass: HomeAssistant) -> None:
    """A device registered under a volatile gateway id is re-pointed to the slug."""
    dev_reg = dr.async_get(hass)
    holder: dict[str, str] = {}

    def prepare(entry: MockConfigEntry) -> None:
        # Pre-create a device keyed on the volatile gateway id "idlight1".
        dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "idlight1")}
        )
        holder["id"] = dev.id

    entry = await _setup_with_registry(hass, prepare)
    # The migration rewrote the identifier to device_slug("Hall Light").
    migrated = dev_reg.async_get(holder["id"])
    assert migrated is not None
    assert (DOMAIN, device_slug(DEVICES[0])) in migrated.identifiers
    assert (DOMAIN, "idlight1") not in migrated.identifiers
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_removes_colliding_stable_id(hass: HomeAssistant) -> None:
    """An old id-based entity is dropped when its stable id already exists."""
    ent_reg = er.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        # A leftover entity already under the stable id...
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "hall_light_001", config_entry=entry
        )
        # ...and the old volatile-id entity that should migrate onto it.
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "idlight1_idlight1-001", config_entry=entry
        )

    entry = await _setup_with_registry(hass, prepare)
    # The colliding old entity was removed rather than renamed onto the existing id.
    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "idlight1_idlight1-001")
        is None
    )
    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001")
        is not None
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_per_item_error_leaves_flag_unset(
    hass: HomeAssistant,
) -> None:
    """A per-entity migration failure is isolated but still blocks the done flag."""
    ent_reg = er.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "idlight1_idlight1-001", config_entry=entry
        )

    with patch(
        "homeassistant.helpers.entity_registry.EntityRegistry.async_update_entity",
        side_effect=RuntimeError("boom"),
    ):
        entry = await _setup_with_registry(hass, prepare)

    # Setup succeeds, but the per-item error means the migration isn't marked done.
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
