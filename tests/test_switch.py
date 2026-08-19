"""Switch / socket platform tests for Jung Home."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.switch import JungHomeSocket, JungHomeSwitch
from tests.conftest import bare_coordinator


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


async def test_socket_state_helper_reads_unknown_without_a_value(
    hass: HomeAssistant,
) -> None:
    """A socket datapoint with no switch value reads unknown, not off.

    Absent information is not "off": asserting off would put a fabricated state
    in history and fire every ``to: "off"`` automation. Same contract as the
    ``"NaN"`` case (see ``datapoint_bool``).
    """
    coordinator = bare_coordinator(hass)
    device = {"id": "d", "type": "Socket", "label": "Sock", "datapoints": []}
    socket = JungHomeSocket(coordinator, device, {"id": "d-1", "values": []})
    assert socket.is_on is None

    # And the status-LED helper likewise.
    led_dev = {"id": "r", "type": "RockerSwitch", "label": "R", "datapoints": []}
    led = JungHomeSwitch(coordinator, led_dev, {"id": "r-1", "values": []})
    assert led.is_on is None


async def test_switch_led_handle_update_missing_device_noops(
    hass: HomeAssistant,
) -> None:
    """JungHomeSwitch._handle_coordinator_update returns early when device is gone."""
    coordinator = bare_coordinator(hass)
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


async def test_unrelated_push_does_not_revert_optimistic_socket_state(
    hass: HomeAssistant, init_integration
) -> None:
    """An unrelated device's push must not flip a just-commanded socket back.

    Every push notifies every entity via ``async_update_listeners``, including
    the ``functions``-broadcast-adjacent reply that confirms this socket's own
    command. The socket used to re-read its own stored datapoint on ANY
    listener dispatch and reverted the optimistic state whenever that read
    predated the echo, so the switch visibly flipped off and then on again.
    ``_should_refresh`` guards this by gating a refresh on the pushed
    datapoint id matching the entity's own — this proves the guard still holds
    once a genuinely different device's push arrives right after this
    command's own confirmed echo.
    """
    coordinator = init_integration.runtime_data
    assert hass.states.get("switch.boiler").state == "on"

    # Command it off, simulating the gateway's normal confirmed echo (see
    # coordinator._send_datapoint_command) so the await completes instead of
    # racing COMMAND_REPLY_TIMEOUT.
    async def _confirm(message: dict) -> None:
        coordinator._dispatch_text_frame(
            json.dumps(
                {
                    "type": "datapoint",
                    "data": message["data"],
                    "message_id": message["message_id"],
                }
            )
        )

    with patch.object(
        coordinator, "send_websocket_message", AsyncMock(side_effect=_confirm)
    ):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.boiler"},
            blocking=True,
        )
    assert hass.states.get("switch.boiler").state == "off"

    # A push for a completely different device arrives right after.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.boiler").state == "off", (
        "an unrelated push reverted the optimistic socket state"
    )


def _two_device_coordinator(hass: HomeAssistant):
    """A bare coordinator holding two sockets, plus an entity for the first."""
    coordinator = bare_coordinator(hass)
    mine = {
        "id": "dev-a",
        "type": "Socket",
        "label": "Mine",
        "datapoints": [
            {
                "id": "dp-a",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            }
        ],
    }
    other = {
        "id": "dev-b",
        "type": "Socket",
        "label": "Other",
        "datapoints": [
            {
                "id": "dp-b",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            }
        ],
    }
    coordinator.data = [mine, other]
    entity = JungHomeSocket(coordinator, mine, mine["datapoints"][0])
    entity.hass = hass
    entity.entity_id = "switch.mine"
    return coordinator, entity


async def test_foreign_device_push_skips_the_state_write(hass: HomeAssistant) -> None:
    """Another device's push must not cost this entity a state-machine write.

    Every push notifies every entity; the write is skipped only when the push
    belongs to a different device AND this entity is already shown available
    (see ``JungHomeEntity._skip_foreign_device_push``). The push markers are
    set directly here, exactly as the coordinator holds them for the duration
    of one dispatch.
    """
    coordinator, entity = _two_device_coordinator(hass)
    hass.states.async_set("switch.mine", "on")

    # Foreign device's push while shown available -> skipped.
    coordinator.pushed_datapoint_id = "dp-b"
    coordinator.pushed_device_id = "dev-b"
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_not_called()

    # Own device's push -> writes.
    coordinator.pushed_datapoint_id = "dp-a"
    coordinator.pushed_device_id = "dev-a"
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_called_once()

    # No push marker (poll / broadcast / WS-drop dispatch) -> writes.
    coordinator.pushed_datapoint_id = None
    coordinator.pushed_device_id = None
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_called_once()


async def test_foreign_push_never_skipped_while_unavailable_or_unwritten(
    hass: HomeAssistant,
) -> None:
    """The skip must fail open whenever it could hide an availability change.

    A push proves the gateway alive (`last_update_success` was just set True),
    so an entity currently shown unavailable must write on this very dispatch
    to become available again — and an entity never written at all must take
    its first write. A pushed device without an id sets no device marker, so
    that also writes.
    """
    coordinator, entity = _two_device_coordinator(hass)
    coordinator.pushed_datapoint_id = "dp-b"
    coordinator.pushed_device_id = "dev-b"

    # Not yet in the state machine -> writes.
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_called_once()

    # Currently unavailable -> writes (the availability recovery).
    hass.states.async_set("switch.mine", "unavailable")
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_called_once()

    # Pushed device carries no id -> no device marker -> writes.
    hass.states.async_set("switch.mine", "on")
    coordinator.pushed_device_id = None
    with patch.object(entity, "async_write_ha_state") as write:
        entity._handle_coordinator_update()
    write.assert_called_once()


async def test_push_restores_availability_of_other_devices_entities(
    hass: HomeAssistant, init_integration
) -> None:
    """A push must make ALL entities available again, not just the pushed one.

    After a failed poll every entity reads unavailable. The next push sets
    ``last_update_success`` back to True for the whole entry, so entities of
    OTHER devices must write on that same dispatch too — the foreign-push skip
    is only allowed while an entity is already shown available. Skipping here
    would freeze them on "unavailable" until the next successful poll.
    """
    coordinator = init_integration.runtime_data
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("switch.boiler").state == "unavailable"
    assert hass.states.get("sensor.boiler_power").state == "unavailable"

    # A push for the LIGHT (a different device) arrives and proves the gateway
    # alive again.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.boiler").state != "unavailable"
    assert hass.states.get("sensor.boiler_power").state != "unavailable"


async def test_the_gateway_echo_still_updates_the_socket(
    hass: HomeAssistant, init_integration
) -> None:
    """The guard must not stop the real echo landing."""
    coordinator = init_integration.runtime_data
    socket_dp = next(
        dp["id"]
        for d in coordinator.data
        if d["label"] == "Boiler"
        for dp in d["datapoints"]
        if dp["type"] == "switch"
    )

    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": socket_dp, "values": [{"key": "switch", "value": "0"}]},
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.boiler").state == "off"


async def test_push_markers_do_not_survive_their_dispatch(
    hass: HomeAssistant, init_integration
) -> None:
    """The push markers must be cleared the moment the dispatch ends.

    ``_skip_foreign_device_push`` reads ``pushed_device_id`` and skips the
    state write for every entity of another device. That is only safe while
    the marker lives for exactly one dispatch: a marker that leaked past it
    would make every non-pushed device's entities stop writing state
    altogether — poll values would freeze and, worse, they would never go
    unavailable when the gateway died, because the availability guard only
    rescues the unavailable -> available direction.

    ``pushed_datapoint_id``'s reset is pinned by the event platform (a leaked
    marker fires phantom presses); this pins its sibling, which shipped
    without a guard of its own.
    """
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert coordinator.pushed_device_id is None
    assert coordinator.pushed_datapoint_id is None

    # With the markers cleared, the next failed poll reaches every entity —
    # including those of the device that pushed last.
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("switch.boiler").state == "unavailable"
    assert hass.states.get("sensor.boiler_power").state == "unavailable"


async def test_a_sibling_datapoints_push_does_not_revert_the_socket(
    hass: HomeAssistant, init_integration
) -> None:
    """A sibling datapoint's push must not re-read this socket's switch value.

    The device-scoped skip returns early for *other* devices, so the only
    dispatches still reaching ``_should_refresh`` are this device's own — and a
    socket has a sibling that pushes constantly: its power quantity.

    A single-datapoint socket cannot actually reach a stale read today: the
    command awaits the gateway's echo, and the reply falls through to the merge
    path, so ``coordinator.data`` already holds the confirmed value by the time
    the optimistic write runs. The divergent store is therefore *constructed*
    below. The guard is kept, and pinned here, because the same dispatch is
    genuinely reachable on a multi-datapoint entity — a light's brightness
    still holds its pre-command value while the switch echo is being
    dispatched, which is the flicker ``JungHomeEntity._should_refresh``
    documents — and because dropping it from one platform and not the others
    is exactly the asymmetry that hides the next regression.
    """
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.boiler"}, blocking=True
    )
    assert hass.states.get("switch.boiler").state == "off"

    coordinator = init_integration.runtime_data
    boiler = next(d for d in coordinator.data if d["label"] == "Boiler")
    switch_dp = next(dp for dp in boiler["datapoints"] if dp["type"] == "switch")
    power_dp = next(dp for dp in boiler["datapoints"] if dp["type"] == "quantity")
    # Re-open the pre-echo window: stored value back to the pre-command "on".
    switch_dp["values"] = [{"key": "switch", "value": "1"}]

    # The power reading pushes (same device, sibling datapoint).
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": power_dp["id"],
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "42.0"},
                    {"key": "quantity_label", "value": "Power "},
                    {"key": "quantity_unit", "value": "W"},
                ],
            },
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.boiler").state == "off"


async def test_nan_switch_reads_unknown_not_off(
    hass: HomeAssistant, init_integration
) -> None:
    """A `"NaN"` switch value must read unknown, never off.

    After three failed BT-Mesh reads the middleware's `onResponseFail` sets the
    state's value to `NaN`, and `composeDatapointByState` stringifies every
    value — so the literal `"NaN"` lands on the wire, and `/functions` keeps
    serving it (the firmware's function assembly has no reachability filter).
    Field-observed on a real gateway: a mesh outage produced summary lines
    naming up to 24 unreachable devices at once, and every reboot leaves a
    window before each node has been read for the first time.

    Read as off, that is indistinguishable from someone switching the load off:
    history and logbook record an off that never happened and every
    `to: "off"` automation fires.
    """
    coordinator = init_integration.runtime_data
    assert hass.states.get("switch.boiler").state == "on"

    socket_dp = next(
        dp["id"]
        for d in coordinator.data
        if d["label"] == "Boiler"
        for dp in d["datapoints"]
        if dp["type"] == "switch"
    )
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": socket_dp, "values": [{"key": "switch", "value": "NaN"}]},
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.boiler").state == "unknown"


async def test_nan_status_led_reads_unknown_not_off(
    hass: HomeAssistant, init_integration
) -> None:
    """The status LED takes the same contract as the socket."""
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00e",
                "values": [{"key": "status_led", "value": "NaN"}],
            },
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.button_a_status_led").state == "unknown"
