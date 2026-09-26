"""Light / dimmer / color-light platform tests for Jung Home."""

import json
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.light import (
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    JungHomeLight,
)
from custom_components.junghome.models import DeviceProperties
from tests.conftest import bare_coordinator


def _color_light(
    coordinator: JungHomeDataUpdateCoordinator,
    *,
    color_temp: str = "3000",
    brightness: str = "50",
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
                "values": [{"key": "brightness", "value": brightness}],
            },
            {
                "id": "c-4",
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": color_temp}],
            },
        ],
    }
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
    """A switch=on echo must not clobber the just-set brightness (UI flicker).

    The gateway echoes switch-on and brightness as separate frames; the switch
    one arrives first, while coordinator data still holds the old brightness.
    Re-reading brightness on that frame would momentarily reset the slider.
    """
    coordinator = init_integration.runtime_data
    # Drag brightness up: HA turns the light on AND sets brightness. 200 is
    # raw 78 on the device's 0-100 scale, which the gateway confirms and
    # which reads back as 199 (see test_brightness_round_trips_...).
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200},
        blocking=True,
    )
    assert hass.states.get("light.strip").attributes["brightness"] == 199

    # A switch=on echo carries no brightness; the brightness datapoint must
    # not be re-read on that frame (a stale snapshot would reset the slider).
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idcolor1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == 199

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
    """A plain off->on clears optimistic brightness; an already-on keeps it.

    Without an explicit brightness the device restores its own level on
    power-on; the integration must not keep a stale/guessed value across that
    transition (which looked like the light jumping to 100%) — it clears
    brightness and applies what the device reports. But a plain turn_on
    against an ALREADY-on dimmer changes nothing on the device and no push
    follows, so the held value (the device's current level) must be kept —
    clearing it left brightness unknown until the next poll for no reason.
    """
    coordinator = init_integration.runtime_data
    # Set a high brightness via the slider (200 -> raw 78 -> 199); light is on.
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200},
        blocking=True,
    )
    assert hass.states.get("light.strip").attributes["brightness"] == 199

    # Plain turn_on while already on: nothing changes device-side, keep 199.
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.strip"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes["brightness"] == 199

    # Off, then a plain on: this is the genuine power-on where the device
    # restores its own level — brightness is cleared, pending the report.
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.strip"}, blocking=True
    )
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


def _sent_value(coordinator: JungHomeDataUpdateCoordinator, dp_type: str) -> str:
    """Return the value of the last ``dp_type`` set the fake socket was sent."""
    frames = [
        json.loads(c.args[0]) for c in coordinator.websocket.send_str.call_args_list
    ]
    frame = next(f for f in reversed(frames) if f["data"].get("type") == dp_type)
    return str(frame["data"]["values"][0]["value"])


@pytest.mark.parametrize(
    ("requested", "sent"), [(6500, 6000), (1000, 2000), (3500, 3500)]
)
async def test_color_temp_request_is_clamped_before_sending(
    hass: HomeAssistant, init_integration, requested: int, sent: int
) -> None:
    """An out-of-range Kelvin request is clamped to the declared range first.

    `light.turn_on` does not validate `color_temp_kelvin` against the entity's
    min/max, and the gateway clamps every write to the device's range anyway
    (2000-6000 K until the device's own is known) — so 6500 K used to go out as 6500, come back confirmed as 6000, and the entity
    then showed 6500: above its own `max_color_temp_kelvin`, until the next
    push corrected it. What is sent must be what can be confirmed.
    """
    coordinator = init_integration.runtime_data
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "color_temp_kelvin": requested},
        blocking=True,
    )
    assert _sent_value(coordinator, "color_temperature") == str(sent)
    state = hass.states.get("light.strip")
    assert state.attributes["color_temp_kelvin"] == sent
    assert (
        state.attributes["color_temp_kelvin"]
        <= state.attributes["max_color_temp_kelvin"]
    )


async def test_brightness_round_trips_through_the_confirmed_reply(
    hass: HomeAssistant, init_integration
) -> None:
    """The brightness shown is the gateway-confirmed level, not HA's request.

    The device has 0-100 steps: HA's 200 goes out as raw 78, and the reply the
    command awaits confirms 78 — which is 199 on HA's scale. Storing the
    request (200) after that reply overwrote the confirmed value the reply had
    already merged, so the entity disagreed with the very next push/poll.
    """
    coordinator = init_integration.runtime_data
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200},
        blocking=True,
    )
    assert _sent_value(coordinator, "brightness") == "78"
    assert hass.states.get("light.strip").attributes["brightness"] == round(
        78 * 255 / 100
    )


async def test_confirmed_reply_wins_over_the_requested_value(
    hass: HomeAssistant, init_integration
) -> None:
    """Whatever the gateway confirms is what the entity shows after a command.

    The fixture socket echoes requests verbatim, which cannot tell a re-read of
    the confirmed value from a stored request. Here the gateway confirms a
    different value than it was asked for (as it does when it clamps or the
    device adjusts), and that confirmed value must be what lands.
    """
    coordinator = init_integration.runtime_data
    adjusted = {"brightness": "60", "color_temperature": "3900"}

    def _reply_adjusted(raw: str) -> None:
        sent = json.loads(raw)
        data = sent["data"]
        if data.get("type") in adjusted:
            data = {
                **data,
                "values": [{"key": data["type"], "value": adjusted[data["type"]]}],
            }
        coordinator._dispatch_text_frame(
            json.dumps(
                {"type": "datapoint", "data": data, "message_id": sent["message_id"]}
            )
        )

    coordinator.websocket.send_str.side_effect = _reply_adjusted
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200, "color_temp_kelvin": 4000},
        blocking=True,
    )
    state = hass.states.get("light.strip")
    assert state.attributes["brightness"] == round(60 * 255 / 100)
    assert state.attributes["color_temp_kelvin"] == 3900


async def test_request_stands_in_when_the_reply_confirms_nothing(
    hass: HomeAssistant, init_integration
) -> None:
    """With no usable confirmed value, the (clamped) request is shown.

    The firmware answers every set with the re-read datapoint, so this is the
    defensive path: the datapoint holds nothing parseable (the gateway had
    given up on the node — "NaN") and the reply carries no values either.
    Leaving the entity blank after a command it just confirmed would be worse
    than the request.
    """
    coordinator = init_integration.runtime_data
    for dp_id, key in (
        ("idcolor1-002", "brightness"),
        ("idcolor1-004", "color_temperature"),
    ):
        coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {"id": dp_id, "values": [{"key": key, "value": "NaN"}]},
            }
        )
    await hass.async_block_till_done()
    assert hass.states.get("light.strip").attributes.get("color_temp_kelvin") is None

    def _reply_without_values(raw: str) -> None:
        sent = json.loads(raw)
        coordinator._dispatch_text_frame(
            json.dumps(
                {
                    "type": "datapoint",
                    "data": {"id": sent["data"]["id"]},
                    "message_id": sent["message_id"],
                }
            )
        )

    coordinator.websocket.send_str.side_effect = _reply_without_values
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "brightness": 200, "color_temp_kelvin": 6500},
        blocking=True,
    )
    state = hass.states.get("light.strip")
    assert state.attributes["brightness"] == 200
    assert state.attributes["color_temp_kelvin"] == 6000


async def test_light_value_extractors_are_defensive(hass: HomeAssistant) -> None:
    """The light value extractors tolerate missing/garbage datapoints."""
    light = _color_light(bare_coordinator(hass))
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
    # State helper with no switch key -> unknown, not off (see datapoint_bool).
    assert light._get_state_from_datapoint({"id": "x", "values": []}) is None


async def test_light_set_without_datapoints_warns_and_noops(
    hass: HomeAssistant,
) -> None:
    """Setting brightness/colour-temp on a light lacking those datapoints no-ops."""
    coordinator = bare_coordinator(hass)
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
    light = _color_light(bare_coordinator(hass))
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
    light = _color_light(bare_coordinator(hass))
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
    # Color temp outside the default 2000-6000 K window is clamped (the
    # gateway middleware enforces the same range on writes — see light.py).
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "9000"}]}
        )
        == 6000
    )
    assert (
        light._get_color_temp_from_datapoint(
            {"id": "x", "values": [{"key": "color_temperature", "value": "1000"}]}
        )
        == 2000
    )


def _with_range(
    coordinator: JungHomeDataUpdateCoordinator,
    kelvin_range: tuple[int, int] | None,
    device_id: str = "c",
) -> None:
    """Hand the coordinator the verbose endpoint's range for one device."""
    coordinator.device_properties = MappingProxyType(
        {device_id: DeviceProperties(color_temp_range=kelvin_range)}
    )


async def test_light_kelvin_range_comes_from_the_device(hass: HomeAssistant) -> None:
    """The gateway clamps to the node's own CTL range; the entity declares it.

    2000-6000 K is only the middleware's constructor default: once it has read
    the node's Light CTL Temperature Range it clamps writes to that instead
    (ColorTemperatureState.js:94-103, :190-197). A 2700-6500 K fixture must
    not be offered 2000-2699 K, nor denied 6001-6500 K.
    """
    coordinator = bare_coordinator(hass)
    _with_range(coordinator, (2700, 6500))
    light = _color_light(coordinator, color_temp="6500")
    assert (light.min_color_temp_kelvin, light.max_color_temp_kelvin) == (2700, 6500)
    assert light.color_temp_kelvin == 6500  # not capped at the old 6000
    # Reads clamp to the device's window.
    for raw, expected in (("2000", 2700), ("9000", 6500), ("3500", 3500)):
        assert (
            light._get_color_temp_from_datapoint(
                {"id": "x", "values": [{"key": "color_temperature", "value": raw}]}
            )
            == expected
        ), raw


async def test_light_kelvin_range_falls_back_to_the_gateway_default(
    hass: HomeAssistant,
) -> None:
    """No properties, no range, or another device's range: 2000-6000 K."""
    coordinator = bare_coordinator(hass)
    for kelvin_range, device_id in (
        (None, "c"),  # the device is listed, its range is not usable
        ((2700, 6500), "other"),  # someone else's range
    ):
        _with_range(coordinator, kelvin_range, device_id)
        light = _color_light(coordinator, color_temp="9000")
        assert (light.min_color_temp_kelvin, light.max_color_temp_kelvin) == (
            DEFAULT_MIN_KELVIN,
            DEFAULT_MAX_KELVIN,
        ), kelvin_range
        assert light.color_temp_kelvin == DEFAULT_MAX_KELVIN
    coordinator.device_properties = MappingProxyType({})  # endpoint never answered
    light = _color_light(coordinator, color_temp="1000")
    assert light.min_color_temp_kelvin == DEFAULT_MIN_KELVIN
    assert light.color_temp_kelvin == DEFAULT_MIN_KELVIN


async def test_light_picks_up_a_range_learned_after_creation(
    hass: HomeAssistant, init_integration, entity_registry: er.EntityRegistry
) -> None:
    """A range that arrives once the entity exists reaches its state and registry.

    The verbose read can land after the light was created (a light added at
    runtime, a setup-time read that failed and is retried every interval).
    The range is read live, and the properties refresh's listener dispatch
    writes the new min/max — and re-clamps the shown value into it.
    """
    coordinator = init_integration.runtime_data
    state = hass.states.get("light.strip")
    assert state.attributes["max_color_temp_kelvin"] == DEFAULT_MAX_KELVIN
    assert state.attributes["color_temp_kelvin"] == 2700
    _with_range(coordinator, (3000, 6500), "idcolor1")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get("light.strip")
    assert state.attributes["min_color_temp_kelvin"] == 3000
    assert state.attributes["max_color_temp_kelvin"] == 6500
    assert state.attributes["color_temp_kelvin"] == 3000  # 2700 is out of range now
    capabilities = entity_registry.async_get("light.strip").capabilities
    assert capabilities["min_color_temp_kelvin"] == 3000
    assert capabilities["max_color_temp_kelvin"] == 6500


@pytest.mark.parametrize(
    ("requested", "sent"), [(7000, 6500), (6300, 6300), (2000, 2700)]
)
async def test_color_temp_request_is_clamped_to_the_device_range(
    hass: HomeAssistant, init_integration, requested: int, sent: int
) -> None:
    """Writes clamp to the device's own window, not the 2000-6000 K default."""
    coordinator = init_integration.runtime_data
    _with_range(coordinator, (2700, 6500), "idcolor1")
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.strip", "color_temp_kelvin": requested},
        blocking=True,
    )
    assert _sent_value(coordinator, "color_temperature") == str(sent)
    assert hass.states.get("light.strip").attributes["color_temp_kelvin"] == sent


async def test_colortemp_light_without_brightness(hass: HomeAssistant) -> None:
    """A ColorLight exposing color_temp but no brightness still tracks color temp.

    Regression guard: the color_temp init used to be gated on _has_brightness, so
    such a device advertised COLOR_TEMP yet reported color_temp_kelvin == None.
    """
    coordinator = bare_coordinator(hass)
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
    assert light.max_color_temp_kelvin == 6000


async def test_all_light_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every light entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.LIGHT)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.parametrize(
    ("raw", "expected_pct"),
    [("50", 50), ("50.0", 50), ("49.6", 50), ("100.0", 100)],
)
async def test_decimal_brightness_is_parsed(
    hass: HomeAssistant, raw: str, expected_pct: int
) -> None:
    """A decimal-formatted brightness must not read as 0.

    Gateway numerics arrive as strings, and `int("50.0")` raises — which fell
    through to brightness 0, showing the lamp at 0 % with nothing logged. Cover,
    climate and sensor all parse via float(); this brings light into line.
    """
    light = _color_light(bare_coordinator(hass), brightness=raw)
    assert light.brightness == round(expected_pct * 255 / 100)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "", "abc"])
async def test_unparseable_brightness_falls_back_to_zero(
    hass: HomeAssistant, raw: str
) -> None:
    """Genuinely unusable values still degrade safely rather than raise."""
    assert _color_light(bare_coordinator(hass), brightness=raw).brightness == 0


@pytest.mark.parametrize(("raw", "expected"), [("2700", 2700), ("2700.0", 2700)])
async def test_decimal_color_temp_is_parsed(
    hass: HomeAssistant, raw: str, expected: int
) -> None:
    """A decimal-formatted Kelvin value must not blank the colour temperature."""
    light = _color_light(bare_coordinator(hass), color_temp=raw)
    assert light.color_temp_kelvin == expected


@pytest.mark.parametrize("raw", ["nan", "inf", "abc"])
async def test_unparseable_color_temp_is_none(hass: HomeAssistant, raw: str) -> None:
    """An unusable Kelvin value yields None rather than raising."""
    light = _color_light(bare_coordinator(hass), color_temp=raw)
    assert light.color_temp_kelvin is None


async def test_nan_switch_reads_unknown_not_off(
    hass: HomeAssistant, init_integration
) -> None:
    """A `"NaN"` switch value must leave the light unknown, never off.

    Same contract as the socket (see `datapoint_bool`): the gateway sends
    `"NaN"` once it has given up reading a node, and reading that as off puts a
    state the user never caused into history.
    """
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.hall_light").state == "on"

    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idlight1-001",
                "values": [{"key": "switch", "value": "NaN"}],
            },
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("light.hall_light").state == "unknown"
