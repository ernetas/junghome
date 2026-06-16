"""Tests for the Jung Home data update coordinator."""

import json
from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


def _coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    return JungHomeDataUpdateCoordinator(hass, {"host": "h", "token": "t"}, entry)


async def test_update_raises_auth_failed_on_401(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    err = aiohttp.ClientResponseError(Mock(), (), status=401)
    with (
        patch.object(coordinator, "_fetch_devices_from_api", side_effect=err),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()


async def test_update_raises_update_failed_on_client_error(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    with (
        patch.object(
            coordinator,
            "_fetch_devices_from_api",
            side_effect=aiohttp.ClientError("boom"),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_reload_scheduled_when_device_ids_change(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._device_ids = {"katilas": "idOLD"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed([{"id": "idNEW", "label": "Katilas"}])
    reload.assert_called_once()


async def test_no_reload_when_device_ids_stable(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._device_ids = {"katilas": "idSAME"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed(
            [{"id": "idSAME", "label": "Katilas"}]
        )
    reload.assert_not_called()


def _coordinator_with_ws(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws
    return coordinator


async def test_cover_climate_command_payloads(hass: HomeAssistant) -> None:
    """The new command methods build the expected datapoint set frames."""
    coordinator = _coordinator_with_ws(hass)
    await coordinator.set_level("dp-1", 75)
    await coordinator.move_level("dp-1", 0)
    await coordinator.set_angle("dp-2", 60)
    await coordinator.set_temperature("dp-3", 22.5)
    await coordinator.set_temperature_preset("dp-3", "eco")

    sent = [
        json.loads(c.args[0]) for c in coordinator.websocket.send_str.call_args_list
    ]
    assert sent[0]["data"]["values"] == [{"key": "level", "value": "75"}]
    assert sent[1]["data"]["values"] == [{"key": "level_move", "value": "0"}]
    assert sent[2]["data"]["values"] == [{"key": "angle", "value": "60"}]
    assert sent[3]["data"]["values"] == [{"key": "temperature_ctrl", "value": "22.5"}]
    assert sent[4]["data"]["values"] == [
        {"key": "temperature_ctrl_preset", "value": "eco"}
    ]


async def test_scenes_broadcast_full_new_deleted(hass: HomeAssistant) -> None:
    """scenes / scenes-new / scenes-deleted maintain the cached scene list."""
    coordinator = _coordinator(hass)
    coordinator._handle_scenes_broadcast(
        "scenes", [{"id": "id1", "label": "A"}, {"id": "id2", "label": "B"}]
    )
    assert {s["id"] for s in coordinator.scenes} == {"id1", "id2"}

    coordinator._handle_scenes_broadcast("scenes-new", [{"id": "id3", "label": "C"}])
    assert {s["id"] for s in coordinator.scenes} == {"id1", "id2", "id3"}

    coordinator._handle_scenes_broadcast(
        "scenes-deleted", [{"id": "id1", "label": "A"}]
    )
    assert {s["id"] for s in coordinator.scenes} == {"id2", "id3"}


class _FakeResponse:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._exc is not None:
            raise self._exc


async def test_activate_scene_posts_to_rest(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    session = Mock()
    session.post = Mock(return_value=_FakeResponse())
    with patch(
        "custom_components.junghome.coordinator.async_get_clientsession",
        return_value=session,
    ):
        await coordinator.activate_scene("id0002")
    url = session.post.call_args.args[0]
    assert url.endswith("/api/junghome/scenes/id0002")


async def test_activate_scene_raises_on_error(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    session = Mock()
    session.post = Mock(return_value=_FakeResponse(aiohttp.ClientError("boom")))
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.activate_scene("idX")
