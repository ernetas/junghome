"""Light / dimmer / color-light platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.light import JungHomeLight


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


def _color_light(
    coordinator: JungHomeDataUpdateCoordinator,
    *,
    parent_groups: list | None = None,
    color_temp: str = "3000",
) -> JungHomeLight:
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
                "values": [{"key": "color_temperature", "value": color_temp}],
            },
        ],
    }
    if parent_groups is not None:
        device["parent_groups"] = parent_groups
    return JungHomeLight(coordinator, device, device["datapoints"][0])


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


async def test_switch_echo_does_not_reset_brightness(
    hass: HomeAssistant, init_integration
) -> None:
    """A switch=on echo must not clobber the optimistic brightness (UI flicker).

    The gateway echoes switch-on and brightness as separate frames; the switch
    one arrives first, while coordinator data still holds the old brightness.
    Re-reading brightness on that frame would momentarily reset the slider.
    """
    coordinator = init_integration.runtime_data
    # Drag brightness up: HA turns the light on AND sets brightness optimistically.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200},
        blocking=True,
    )
    assert hass.states.get("light.strip").attributes["brightness"] == 200

    # The switch=on echo arrives FIRST; the brightness datapoint in coordinator
    # data still holds the old "50". This must NOT reset brightness to ~128.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idcolor1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == 200

    # The brightness echo then lands and is applied normally.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-002",
                "values": [{"key": "brightness", "value": "80"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == round(
        80 * 255 / 100
    )


async def test_plain_turn_on_waits_for_device_brightness(
    hass: HomeAssistant, init_integration
) -> None:
    """A plain turn-on clears the optimistic brightness and waits for the device.

    Without an explicit brightness the device restores its own level; the
    integration must not keep a stale/guessed value (which looked like the light
    jumping to 100%) — it clears brightness and applies what the device reports.
    """
    coordinator = init_integration.runtime_data
    # Set a high brightness via the slider (optimistic 200).
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200},
        blocking=True,
    )
    assert hass.states.get("light.strip").attributes["brightness"] == 200

    # A plain toggle-on must NOT keep 200; brightness is cleared, pending report.
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.strip"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes.get("brightness") is None

    # The device then reports its restored level; that is what shows.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idcolor1-002",
                "values": [{"key": "brightness", "value": "80"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == round(
        80 * 255 / 100
    )


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


async def test_dimmer_light_created(hass: HomeAssistant, init_integration) -> None:
    """A DimmerLight (switch + brightness, no color temp) becomes a brightness light."""
    state = hass.states.get("light.dimmer")
    assert state is not None
    assert state.attributes["supported_color_modes"] == ["brightness"]
    # 30% device brightness -> round(30 * 255 / 100) = 77
    # (light is off in the fixture, so brightness is reported but state is off)


async def test_dimmer_brightness_command(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    with patch.object(coordinator, "set_brightness", AsyncMock()) as sb:
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": "light.dimmer", "brightness": 255},
            blocking=True,
        )
    assert sb.called
    assert sb.call_args.args[1] == 100  # 255 HA -> 100 device


async def test_light_brightness_and_color_temp_are_clamped(hass: HomeAssistant) -> None:
    """Out-of-range gateway values are clamped to HA's contracts."""
    light = _color_light(_bare_coordinator(hass))
    # Device brightness 150 (>100) would scale to 383; clamp to 255.
    assert (
        light._get_brightness_from_datapoint(
            {"id": "x", "values": [{"key": "brightness", "value": "150"}]}
        )
        == 255
    )
    # A negative value clamps to 0.
    assert (
        light._get_brightness_from_datapoint(
            {"id": "x", "values": [{"key": "brightness", "value": "-10"}]}
        )
        == 0
    )
    # Color temp outside the advertised 2000-6500 K window is clamped.
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "9000"}]}
        )
        == 6500
    )
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "1000"}]}
        )
        == 2000
    )


async def test_light_uses_gateway_advertised_color_temp_range(
    hass: HomeAssistant,
) -> None:
    """The range the gateway advertises for the device's group wins over defaults."""
    coordinator = _bare_coordinator(hass)
    coordinator.groups = [
        {
            "id": "g1",
            "name": "Living room",
            "color_temperature_range": {"min": 2700, "max": 4000},
        }
    ]
    light = _color_light(coordinator, parent_groups=["g1"])
    assert light.min_color_temp_kelvin == 2700
    assert light.max_color_temp_kelvin == 4000
    # A read inside the advertised range is reported as-is.
    assert light.color_temp_kelvin == 3000


async def test_light_color_temp_range_falls_back_to_defaults(
    hass: HomeAssistant,
) -> None:
    """A missing or malformed advertised range leaves the module defaults in place."""
    coordinator = _bare_coordinator(hass)
    for groups in (
        [],  # no groups known yet
        [{"id": "g1", "name": "Living room"}],  # group without the capability
        [{"id": "g1", "color_temperature_range": "2700-4000"}],  # garbage
        [{"id": "g1", "color_temperature_range": {"min": 4000, "max": 2700}}],
        [{"id": "g1", "color_temperature_range": {"min": 4000, "max": 4000}}],
    ):
        coordinator.groups = groups
        light = _color_light(coordinator, parent_groups=["g1"])
        assert light.min_color_temp_kelvin == 2000, groups
        assert light.max_color_temp_kelvin == 6500, groups
    # A device in no group at all also keeps the defaults.
    light = _color_light(coordinator)
    assert (light.min_color_temp_kelvin, light.max_color_temp_kelvin) == (2000, 6500)


async def test_light_clamps_reads_to_the_gateway_range(hass: HomeAssistant) -> None:
    """Reads clamp to the resolved range, not to the module constants."""
    coordinator = _bare_coordinator(hass)
    coordinator.groups = [
        {"id": "g1", "color_temperature_range": {"min": 2700, "max": 4000}}
    ]
    # The initial read happens in __init__, so the range must already be resolved:
    # 6500 K sits inside the module defaults but above this fixture's 4000 K.
    light = _color_light(coordinator, parent_groups=["g1"], color_temp="6500")
    assert light.color_temp_kelvin == 4000
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "2000"}]}
        )
        == 2700
    )
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "3500"}]}
        )
        == 3500
    )


async def test_colortemp_light_without_brightness(hass: HomeAssistant) -> None:
    """A ColorLight exposing color_temp but no brightness still tracks color temp.

    Regression guard: the color_temp init used to be gated on _has_brightness, so
    such a device advertised COLOR_TEMP yet reported color_temp_kelvin == None.
    """
    coordinator = _bare_coordinator(hass)
    device = {
        "id": "ct",
        "type": "ColorLight",
        "label": "CT",
        "datapoints": [
            {
                "id": "ct-1",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "ct-4",
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": "3000"}],
            },
        ],
    }
    light = JungHomeLight(coordinator, device, device["datapoints"][0])
    assert light.color_mode == "color_temp"
    assert light.color_temp_kelvin == 3000
    assert light.min_color_temp_kelvin == 2000
    assert light.max_color_temp_kelvin == 6500
