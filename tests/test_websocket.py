"""Tests for the coordinator's WebSocket session handling."""

from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


class _FakeMsg:
    def __init__(self, msg_type: aiohttp.WSMsgType, data: str = "") -> None:
        self.type = msg_type
        self.data = data


class _FakeWS:
    """Async-context-manager + async-iterator stand-in for a WebSocket."""

    def __init__(self, frames: list[_FakeMsg]) -> None:
        self._frames = frames
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def __aiter__(self):
        for frame in self._frames:
            yield frame


def _coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


async def test_run_websocket_processes_all_frame_types(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    text = aiohttp.WSMsgType.TEXT
    frames = [
        _FakeMsg(text, '{"type":"message","data":"hi"}'),
        _FakeMsg(text, '{"type":"version","data":"1.5.0"}'),
        _FakeMsg(text, '{"type":"datapoint","data":{"id":"x","values":[]}}'),
        _FakeMsg(text, "not json"),  # JSONDecodeError branch
        _FakeMsg(text, "[1, 2, 3]"),  # top-level list branch
        _FakeMsg(aiohttp.WSMsgType.ERROR, "boom"),  # -> raises, exits the loop
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

    assert coordinator.gateway_version == "1.5.0"
    assert coordinator.websocket is None  # cleared in the finally block


async def test_apply_gateway_version_updates_registry(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "some-device")},
    )
    assert device.sw_version is None

    coordinator.gateway_version = "1.5.0"
    coordinator._apply_gateway_version()

    assert registry.async_get(device.id).sw_version == "1.5.0"


async def test_apply_gateway_version_noop_without_version(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "some-device")},
    )
    # No version received yet — must not clobber the registry.
    coordinator._apply_gateway_version()
    assert registry.async_get(device.id).sw_version is None


async def test_send_raises_when_disconnected(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator.websocket = None
    # A dropped command must surface as an error, not a silent success.
    with pytest.raises(HomeAssistantError):
        await coordinator.send_websocket_message({"type": "datapoint"})


async def test_fetch_rejects_non_list_response(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get("https://gw/api/junghome/functions", json={"error": "boom"})
    coordinator = _coordinator(hass)
    with pytest.raises(UpdateFailed):
        await coordinator._fetch_devices_from_api("gw", "tok")


async def test_stop_cancels_task_and_closes_socket(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws
    await coordinator.stop()
    ws.close.assert_awaited()
    assert coordinator.websocket is None


async def test_websocket_loop_reconnects_with_backoff(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
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
    coordinator = _coordinator(hass)
    data = await coordinator._fetch_devices_from_api("gw", "tok")
    assert data == [{"id": "x", "datapoints": []}]


async def test_update_data_returns_empty_on_none(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    with patch.object(
        coordinator, "_fetch_devices_from_api", AsyncMock(return_value=None)
    ):
        assert await coordinator._async_update_data() == []


async def test_all_command_methods_send(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws
    await coordinator.turn_on_switch("d")
    await coordinator.turn_off_switch("d")
    await coordinator.turn_on_light("d")
    await coordinator.turn_off_light("d")
    await coordinator.set_brightness("d", 50)
    await coordinator.set_color_temp("d", 3000)
    await coordinator.set_status_led("d", state=True)
    assert ws.send_str.await_count == 7
