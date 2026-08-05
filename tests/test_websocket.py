"""Tests for the coordinator's WebSocket session handling."""

import asyncio
import json
from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from tests.conftest import _auto_reply_to_datapoint_commands, bare_coordinator


class _FakeMsg:
    def __init__(self, msg_type: aiohttp.WSMsgType, data: str = "") -> None:
        self.type = msg_type
        self.data = data


class _FakeWS:
    """Async-context-manager + async-iterator stand-in for a WebSocket."""

    def __init__(self, frames: list[_FakeMsg]) -> None:
        self._frames = frames
        self.closed = False
        self.close_code = 1000

    def __await__(self):
        """aiohttp's ws_connect result is awaitable as well as an async CM."""

        async def _resolve() -> "Self":
            return self

        return _resolve().__await__()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def __aiter__(self):
        for frame in self._frames:
            yield frame


async def test_run_websocket_processes_all_frame_types(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    text = aiohttp.WSMsgType.TEXT
    frames = [
        _FakeMsg(text, '{"type":"message","data":"hi"}'),
        _FakeMsg(text, '{"type":"version","data":"1.5.0"}'),
        _FakeMsg(text, '{"type":"datapoint","data":{"id":"x","values":[]}}'),
        _FakeMsg(text, "not json"),  # JSONDecodeError branch
        _FakeMsg(text, "[1, 2, 3]"),  # top-level list branch
        # Bare JSON scalar: json.loads -> str -> str.get raises AttributeError,
        # caught by the broad `except Exception`; the loop must continue.
        _FakeMsg(text, json.dumps("hi")),
        _FakeMsg(aiohttp.WSMsgType.ERROR, "boom"),  # -> raises, exits the loop
    ]
    session = Mock()
    session.ws_connect = Mock(return_value=_FakeWS(frames))
    # Record ws_connected at the moment of the connect-time resync.
    seen: dict[str, bool] = {}

    async def _record_refresh() -> None:
        seen["ws_connected"] = coordinator.ws_connected

    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", _record_refresh),
        pytest.raises(ConnectionError),
    ):
        await coordinator._run_websocket()

    assert coordinator.gateway_version == "1.5.0"
    # Lifecycle: ws_connected was True for the connect-time resync...
    assert seen["ws_connected"] is True
    # ...and is reset to False (with the socket cleared) in the finally block.
    assert coordinator.ws_connected is False
    assert coordinator.websocket is None


async def test_session_end_fails_pending_command_replies(hass: HomeAssistant) -> None:
    """The _run_websocket finally block sweeps in-flight command futures.

    A command's reply only ever arrives on the session that sent it, so the
    session ending (drop, gateway close, or entry unload cancelling the task)
    must fail the pending future immediately rather than leaving the command
    to sit out COMMAND_REPLY_TIMEOUT.
    """
    coordinator = bare_coordinator(hass)
    pending: asyncio.Future[dict] = hass.loop.create_future()
    coordinator._pending_replies["ha1"] = pending

    session = Mock()
    session.ws_connect = Mock(return_value=_FakeWS([]))  # closes immediately
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        pytest.raises(ConnectionError),  # clean close is routed to the failure path
    ):
        await coordinator._run_websocket()

    assert pending.done()
    assert isinstance(pending.exception(), HomeAssistantError)


async def test_ws_error_message_frame_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A gateway `error:` message frame is surfaced at WARNING, not swallowed."""
    coordinator = bare_coordinator(hass)
    frames = [
        _FakeMsg(
            aiohttp.WSMsgType.TEXT,
            '{"type":"message","data":"error: could not set datapoint"}',
        ),
        _FakeMsg(aiohttp.WSMsgType.ERROR, "boom"),
    ]
    session = Mock()
    session.ws_connect = Mock(return_value=_FakeWS(frames))
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        pytest.raises(ConnectionError),
    ):
        await coordinator._run_websocket()
    assert "gateway reported an error" in caplog.text


async def test_ws_handshake_auth_failure_triggers_reauth(hass: HomeAssistant) -> None:
    """A 401 at the WS upgrade starts reauth and stops the reconnect loop."""
    coordinator = bare_coordinator(hass)
    err = aiohttp.WSServerHandshakeError(Mock(), (), status=401, message="unauthorized")
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_run_websocket",
            AsyncMock(side_effect=err),
        ),
        patch.object(coordinator.config_entry, "async_start_reauth") as reauth,
        patch("custom_components.junghome.coordinator.asyncio.sleep", AsyncMock()),
    ):
        await coordinator._websocket_loop()
    # Reauth was started exactly once and the loop exited (no endless reconnect).
    reauth.assert_called_once()


async def test_apply_gateway_version_updates_registry(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "some-device")},
    )
    assert device.sw_version is None

    coordinator.gateway_version = "1.5.0"
    coordinator._apply_gateway_version()

    assert registry.async_get(device.id).sw_version == "1.5.0"


async def test_apply_gateway_version_keeps_per_device_sw_version(
    hass: HomeAssistant,
) -> None:
    """A device reporting its OWN sw_version keeps it; others get the gateway's.

    Writing the gateway version unconditionally used to clobber a per-device
    version, making the registry disagree with ``device_info`` — this pins the
    fix (the branch was previously untested).
    """
    coordinator = bare_coordinator(hass)
    coordinator.data = [
        {"id": "d1", "label": "Versioned", "sw_version": "9.9-device"},
        {"id": "d2", "label": "Unversioned"},
    ]
    registry = dr.async_get(hass)
    versioned = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "versioned")},
    )
    unversioned = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "unversioned")},
    )

    coordinator.gateway_version = "1.5.0"
    coordinator._apply_gateway_version()

    assert registry.async_get(versioned.id).sw_version == "9.9-device"
    assert registry.async_get(unversioned.id).sw_version == "1.5.0"


async def test_apply_gateway_version_noop_without_version(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "some-device")},
    )
    # No version received yet — must not clobber the registry.
    coordinator._apply_gateway_version()
    assert registry.async_get(device.id).sw_version is None


async def test_send_raises_when_disconnected(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    coordinator.websocket = None
    # A dropped command must surface as an error, not a silent success.
    with pytest.raises(HomeAssistantError):
        await coordinator.send_websocket_message({"type": "datapoint"})


async def test_fetch_rejects_non_list_response(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get("https://gw/api/junghome/functions", json={"error": "boom"})
    coordinator = bare_coordinator(hass)
    with pytest.raises(UpdateFailed):
        await coordinator._fetch_devices_from_api("gw", "tok")


async def test_stop_cancels_task_and_closes_socket(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws
    await coordinator.stop()
    ws.close.assert_awaited()
    assert coordinator.websocket is None


async def test_websocket_loop_reconnects_with_backoff(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    calls: list[int] = []

    async def flaky(self: JungHomeDataUpdateCoordinator) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("drop")  # exercise the reconnect branch
        self._closing = True  # clean exit on the second attempt

    with (
        patch.object(JungHomeDataUpdateCoordinator, "_run_websocket", flaky),
        patch("custom_components.junghome.coordinator.asyncio.sleep", AsyncMock()),
    ):
        await coordinator._websocket_loop()

    assert len(calls) == 2


async def test_fetch_devices_from_api(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        "https://gw/api/junghome/functions", json=[{"id": "x", "datapoints": []}]
    )
    coordinator = bare_coordinator(hass)
    data = await coordinator._fetch_devices_from_api("gw", "tok")
    assert data == [{"id": "x", "datapoints": []}]


@pytest.mark.real_groups_fetch
async def test_fetch_groups_from_api(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        "https://gw/api/junghome/groups",
        json=[{"id": "g1", "name": "Kitchen"}, "not-a-dict"],
    )
    coordinator = bare_coordinator(hass)
    # Dict groups are kept; non-dict entries in the array are dropped.
    data = await coordinator._fetch_groups_from_api("gw", "tok")
    assert data == [{"id": "g1", "name": "Kitchen"}]


@pytest.mark.real_groups_fetch
async def test_fetch_groups_from_api_ignores_non_list(
    hass: HomeAssistant, aioclient_mock
) -> None:
    # A non-list response (e.g. an error object) yields no groups, never raises.
    aioclient_mock.get("https://gw/api/junghome/groups", json={"error": "boom"})
    coordinator = bare_coordinator(hass)
    assert await coordinator._fetch_groups_from_api("gw", "tok") == []


async def test_update_data_rejects_json_null_body(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A JSON `null` body fails the isinstance(list) check and raises.

    (There used to be a dead `response is None` branch downstream "for" this
    case — the fetch itself already rejects it, so a None can never reach
    `_async_update_data`.)
    """
    aioclient_mock.get(
        "https://gw/api/junghome/functions",
        text="null",
        headers={"Content-Type": "application/json"},
    )
    coordinator = bare_coordinator(hass)
    with pytest.raises(UpdateFailed):
        await coordinator._fetch_devices_from_api("gw", "tok")


async def test_all_command_methods_send(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    ws = _auto_reply_to_datapoint_commands(coordinator)
    coordinator.websocket = ws
    # A matching stub datapoint so the confirmed reply merges instead of
    # falling into the unmatched-push path (which would schedule a refresh
    # this test never cleans up).
    coordinator.data = [
        {
            "id": "dev",
            "label": "Dev",
            "datapoints": [{"id": "d", "type": "switch", "values": []}],
        }
    ]
    await coordinator.turn_on_switch("d")
    await coordinator.turn_off_switch("d")
    await coordinator.turn_on_light("d")
    await coordinator.turn_off_light("d")
    await coordinator.set_brightness("d", 50)
    await coordinator.set_color_temp("d", 3000)
    await coordinator.set_status_led("d", state=True)
    assert ws.send_str.await_count == 7

    # Capture and decode every payload, keyed by datapoint type, to assert the
    # exact wire format the command builders produce.
    sent = [json.loads(call.args[0]) for call in ws.send_str.call_args_list]
    by_type = {msg["data"]["type"]: msg for msg in sent}

    # set_status_led(state=False would send "0"); state=True sends "1".
    led = by_type["status_led"]
    assert led["type"] == "datapoint"
    assert led["data"]["id"] == "d"
    assert led["data"]["values"] == [{"key": "status_led", "value": "1"}]

    # set_brightness(50) -> brightness datapoint with the raw value stringified.
    brightness = by_type["brightness"]
    assert brightness["data"]["values"] == [{"key": "brightness", "value": "50"}]

    # turn_on_light / turn_on_switch both send switch=1; turn_off sends switch=0.
    switch_values = [
        v
        for msg in sent
        if msg["data"]["type"] == "switch"
        for v in msg["data"]["values"]
    ]
    assert {"key": "switch", "value": "1"} in switch_values
    assert {"key": "switch", "value": "0"} in switch_values


async def test_status_led_off_sends_zero(hass: HomeAssistant) -> None:
    """set_status_led(False) sends the LED value field as "0"."""
    coordinator = bare_coordinator(hass)
    ws = _auto_reply_to_datapoint_commands(coordinator)
    coordinator.websocket = ws
    coordinator.data = [
        {
            "id": "dev",
            "label": "Dev",
            "datapoints": [{"id": "d", "type": "status_led", "values": []}],
        }
    ]
    await coordinator.set_status_led("d", state=False)
    sent = json.loads(ws.send_str.call_args.args[0])
    assert sent["data"]["values"] == [{"key": "status_led", "value": "0"}]


async def test_update_data_raises_update_failed_on_timeout(
    hass: HomeAssistant,
) -> None:
    """A fetch TimeoutError surfaces as UpdateFailed (not a bare TimeoutError)."""
    coordinator = bare_coordinator(hass)
    with (
        patch.object(
            coordinator, "_fetch_devices_from_api", AsyncMock(side_effect=TimeoutError)
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_update_data_5xx_maps_to_update_failed(hass: HomeAssistant) -> None:
    """A non-auth ClientResponseError (e.g. 500) surfaces as UpdateFailed."""
    coordinator = bare_coordinator(hass)
    err = aiohttp.ClientResponseError(Mock(), (), status=500)
    with (
        patch.object(
            coordinator, "_fetch_devices_from_api", AsyncMock(side_effect=err)
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_ws_handshake_non_auth_error_reconnects(hass: HomeAssistant) -> None:
    """A non-401/403 handshake error reconnects with backoff (not reauth)."""
    coordinator = bare_coordinator(hass)
    err = aiohttp.WSServerHandshakeError(Mock(), (), status=500, message="x")
    calls: list[int] = []

    async def flaky(self: JungHomeDataUpdateCoordinator) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise err
        self._closing = True  # clean exit on the second attempt

    with (
        patch.object(JungHomeDataUpdateCoordinator, "_run_websocket", flaky),
        patch("custom_components.junghome.coordinator.asyncio.sleep", AsyncMock()),
    ):
        await coordinator._websocket_loop()
    assert len(calls) == 2  # reconnected rather than giving up


async def test_send_raises_when_send_str_fails(hass: HomeAssistant) -> None:
    """A send failure on a live socket surfaces as HomeAssistantError."""
    coordinator = bare_coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    ws.send_str = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.websocket = ws
    with pytest.raises(HomeAssistantError):
        await coordinator.send_websocket_message({"type": "datapoint"})


async def test_ws_message_without_datapoint_id_is_ignored(hass: HomeAssistant) -> None:
    """A datapoint frame with a dict payload but no id is logged and ignored."""
    coordinator = bare_coordinator(hass)
    coordinator._handle_websocket_message(  # must not raise
        {"type": "datapoint", "data": {"values": []}}
    )


async def test_handle_message_non_dict_is_ignored(hass: HomeAssistant) -> None:
    """A non-dict message payload hits the defensive guard and is ignored."""
    coordinator = bare_coordinator(hass)
    coordinator._handle_websocket_message(["not-a-dict"])  # type: ignore[arg-type]


async def test_groups_broadcast_is_stored(hass: HomeAssistant) -> None:
    """A `groups` broadcast is cached for diagnostics (not consumed by entities)."""
    coordinator = bare_coordinator(hass)
    coordinator._handle_websocket_message(
        {"type": "groups", "data": [{"id": "g1", "name": "Living room"}, "junk"]}
    )
    # Non-dict items are filtered out.
    assert coordinator.groups == [{"id": "g1", "name": "Living room"}]
    # An unrelated list broadcast (e.g. devices) is just debug-logged, not stored.
    coordinator._handle_websocket_message({"type": "devices", "data": [{"id": "d"}]})
    assert coordinator.groups == [{"id": "g1", "name": "Living room"}]


async def test_ws_frame_log_truncates_large_frames(hass: HomeAssistant) -> None:
    """The diagnostics frame log keeps recent frames and truncates large ones."""
    coordinator = bare_coordinator(hass)
    coordinator._log_ws_frame("x" * 5000, None)
    coordinator._log_ws_frame("short", None)
    assert coordinator.ws_frame_log[-1] == "short"
    assert coordinator.ws_frame_log[0].endswith("…[truncated]")
    assert len(coordinator.ws_frame_log[0]) < 5000


async def test_ws_frame_log_keeps_latest_per_type(hass: HomeAssistant) -> None:
    """The latest frame of each type is kept IN FULL (handshake survives churn)."""
    coordinator = bare_coordinator(hass)
    # A large functions handshake frame: it exceeds the rolling-log truncation
    # cap, but the per-type store keeps it complete for direct wire comparison.
    big = '{"type":"functions","data":[' + ",".join(['{"id":"x"}'] * 500) + "]}"
    assert len(big) > 2000
    coordinator._log_ws_frame(big, "functions")
    coordinator._log_ws_frame('{"type":"version","data":"1.5.0"}', "version")
    coordinator._log_ws_frame("not json", None)  # unparseable -> not keyed by type
    by_type = coordinator.ws_last_frame_by_type
    # Per-type store: full, untruncated.
    assert by_type["functions"] == big
    assert by_type["version"] == '{"type":"version","data":"1.5.0"}'
    assert set(by_type) == {"functions", "version"}
    # Rolling log: same large frame is truncated there.
    assert coordinator.ws_frame_log[0].endswith("…[truncated]")


async def test_dispatch_parses_each_frame_once_and_logs_it(
    hass: HomeAssistant,
) -> None:
    """One `json.loads` per frame, shared with the diagnostics frame log.

    The frame logger used to decode every frame a second time just to read its
    type — doubled JSON work on the hottest path the integration has (a chatty
    gateway pushes several frames a second). The dispatcher's single parse now
    feeds both routing and the per-type diagnostics store.
    """
    coordinator = bare_coordinator(hass)
    frame = '{"type":"version","data":"1.5.0"}'
    with patch(
        "custom_components.junghome.coordinator.json.loads", wraps=json.loads
    ) as loads:
        coordinator._dispatch_text_frame(frame)
    assert loads.call_count == 1
    assert coordinator.ws_last_frame_by_type["version"] == frame
    assert coordinator.gateway_version == "1.5.0"


async def test_dispatch_logs_unparseable_and_untyped_frames(
    hass: HomeAssistant,
) -> None:
    """Malformed or oddly-typed frames still reach the rolling diagnostics log.

    An unparseable frame and a frame whose `type` is not a string must both be
    visible in diagnostics (that is the log's whole point when debugging a
    misbehaving gateway) without being keyed into the per-type store.
    """
    coordinator = bare_coordinator(hass)
    coordinator._dispatch_text_frame("not json")  # must not raise
    coordinator._dispatch_text_frame('{"type": 42, "data": {}}')
    assert list(coordinator.ws_frame_log) == ["not json", '{"type": 42, "data": {}}']
    assert coordinator.ws_last_frame_by_type == {}
