"""Tests for the Jung Home data update coordinator."""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import (
    INITIAL_RECONNECT_DELAY,
    MAX_RECONNECT_FAILURES,
    STABLE_SESSION_SECONDS,
    JungHomeDataUpdateCoordinator,
)


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


async def test_scene_recall_fires_event(hass: HomeAssistant) -> None:
    """A `scene` recall frame fires junghome_scene_recalled (not a datapoint)."""
    coordinator = _coordinator(hass)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_scene_recalled", events.append)
    coordinator._handle_websocket_message(
        {
            "type": "scene",
            "data": {
                "id": "id0001",
                "label": "Išjungti WC",
                "related_functions": [],
                "value": "0001",
            },
        }
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["scene_id"] == "id0001"
    assert events[0].data["label"] == "Išjungti WC"


async def test_scene_recall_without_id_is_ignored(hass: HomeAssistant) -> None:
    """A scene recall frame with no id fires no event."""
    coordinator = _coordinator(hass)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_scene_recalled", events.append)
    coordinator._handle_websocket_message(
        {"type": "scene", "data": {"label": "No id here"}}
    )
    await hass.async_block_till_done()
    assert events == []


class _EmptyWS:
    """A WebSocket that connects successfully and closes without any frames."""

    def __init__(self) -> None:
        self.closed = False
        self.close_code = 1000

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


async def _run_failing_loop(
    coordinator: JungHomeDataUpdateCoordinator, attempts: int
) -> None:
    """Drive `_websocket_loop` through exactly `attempts` failed reconnects."""
    calls: list[int] = []

    async def always_failing(self: JungHomeDataUpdateCoordinator) -> None:
        calls.append(1)
        if len(calls) >= attempts:
            self._closing = True  # exit the loop once we've failed enough
        raise ConnectionError("drop")

    with (
        patch.object(JungHomeDataUpdateCoordinator, "_run_websocket", always_failing),
        patch("custom_components.junghome.coordinator.asyncio.sleep", AsyncMock()),
    ):
        await coordinator._websocket_loop()

    assert len(calls) == attempts


async def test_repair_issue_raised_after_repeated_reconnect_failures(
    hass: HomeAssistant,
) -> None:
    """Sustained reconnect failure surfaces the silent REST-only degradation."""
    coordinator = _coordinator(hass)
    await _run_failing_loop(coordinator, MAX_RECONNECT_FAILURES)

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._push_failure_issue_id
    )
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "websocket_push_failure"
    assert issue.translation_placeholders == {
        "host": "h",
        "failures": str(MAX_RECONNECT_FAILURES),
    }


async def test_no_repair_issue_below_failure_threshold(hass: HomeAssistant) -> None:
    """An ordinary blip the backoff rides out must not nag the user."""
    coordinator = _coordinator(hass)
    await _run_failing_loop(coordinator, MAX_RECONNECT_FAILURES - 1)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, coordinator._push_failure_issue_id)
        is None
    )


class _HoldingWS:
    """A WebSocket that connects and stays open until `release` is set."""

    def __init__(self) -> None:
        self.closed = False
        self.close_code = 1000
        self.release = asyncio.Event()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> object:
        await self.release.wait()
        raise StopAsyncIteration


async def test_repair_issue_cleared_once_session_proves_stable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A session that *holds up* clears the issue and resets the counter.

    Recovery is judged on the session lasting `STABLE_SESSION_SECONDS`, not on
    the handshake succeeding — see the flapping test below for why.
    """
    coordinator = _coordinator(hass)
    coordinator.data = []
    await _run_failing_loop(coordinator, MAX_RECONNECT_FAILURES)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, coordinator._push_failure_issue_id)

    coordinator._closing = False
    ws = _HoldingWS()
    session = Mock()
    session.ws_connect = Mock(return_value=ws)
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
    ):
        # NB: not async_block_till_done — the session is deliberately still open,
        # so the task never completes. Yield just enough for it to reach the pump.
        task = hass.async_create_task(coordinator._run_websocket())
        for _ in range(5):
            await asyncio.sleep(0)
        # Still connected, but not yet proven stable.
        assert registry.async_get_issue(DOMAIN, coordinator._push_failure_issue_id)

        freezer.tick(timedelta(seconds=STABLE_SESSION_SECONDS + 1))
        async_fire_time_changed(hass)
        for _ in range(5):
            await asyncio.sleep(0)

        assert (
            registry.async_get_issue(DOMAIN, coordinator._push_failure_issue_id) is None
        )
        assert coordinator._reconnect_failures == 0
        assert coordinator._reconnect_delay == INITIAL_RECONNECT_DELAY

        coordinator._closing = True
        ws.release.set()
        await task


async def test_flapping_session_keeps_escalating(hass: HomeAssistant) -> None:
    """A connect that drops straight away is a failure, not a recovery.

    Resetting the backoff on connect made the escalation unreachable: the delay
    returned to 1 s before the doubling could apply and the failure counter never
    reached the repair-issue threshold, so a gateway in a reboot loop reconnected
    about once a second forever with nothing surfaced to the user.
    """
    coordinator = _coordinator(hass)
    coordinator.data = []
    coordinator._reconnect_failures = MAX_RECONNECT_FAILURES - 1
    coordinator._reconnect_delay = 8

    session = Mock()
    session.ws_connect = Mock(return_value=_EmptyWS())
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        pytest.raises(ConnectionError),
    ):
        await coordinator._run_websocket()

    # The instant close neither reset the backoff nor cleared the counter.
    assert coordinator._reconnect_delay == 8
    assert coordinator._reconnect_failures == MAX_RECONNECT_FAILURES - 1


async def test_clean_server_close_is_counted_as_a_failure(
    hass: HomeAssistant,
) -> None:
    """A gateway that closes the socket politely still escalates.

    `async for` simply ends on a clean close, so this used to return normally:
    no warning, no `last_error`, no failure count — and therefore never the
    repair issue that exists for exactly this silent degradation.
    """
    coordinator = _coordinator(hass)
    coordinator.data = []
    coordinator._closing = False
    session = Mock()
    session.ws_connect = Mock(return_value=_EmptyWS())
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        pytest.raises(ConnectionError, match="closed the WebSocket"),
    ):
        await coordinator._run_websocket()


async def test_stop_clears_the_repair_issue(hass: HomeAssistant) -> None:
    """Unloading while degraded must not strand the issue in the repairs UI."""
    coordinator = _coordinator(hass)
    coordinator.data = []
    await _run_failing_loop(coordinator, MAX_RECONNECT_FAILURES)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, coordinator._push_failure_issue_id)

    await coordinator.stop()

    assert registry.async_get_issue(DOMAIN, coordinator._push_failure_issue_id) is None


def _pushable_device() -> dict:
    """A device with one datapoint a WebSocket push can address."""
    return {
        "id": "dev1",
        "label": "Lamp",
        "datapoints": [{"id": "dev1-001", "type": "switch", "values": []}],
    }


def _push(datapoint_id: str = "dev1-001") -> dict:
    """A datapoint push frame for ``datapoint_id``."""
    return {
        "type": "datapoint",
        "data": {"id": datapoint_id, "values": [{"key": "switch", "value": "1"}]},
    }


async def test_push_notifies_listeners_without_rearming_the_poll(
    hass: HomeAssistant,
) -> None:
    """A push must notify listeners but leave the scheduled REST poll alone.

    Dispatching pushes through ``async_set_updated_data`` re-armed the refresh
    timer on every frame, so a gateway pushing faster than ``update_interval``
    deferred the poll indefinitely.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [_pushable_device()]
    notified = 0

    @callback
    def _listener() -> None:
        nonlocal notified
        notified += 1

    unsub = coordinator.async_add_listener(_listener)
    coordinator.last_update_success = False

    with patch.object(coordinator, "_schedule_refresh") as schedule:
        coordinator._handle_websocket_message(_push())
    unsub()

    assert schedule.call_count == 0, "a push must not re-arm the poll timer"
    # ...but everything async_set_updated_data used to provide still happens.
    assert notified == 1
    assert coordinator.last_update_success is True
    assert coordinator.data[0]["datapoints"][0]["values"] == [
        {"key": "switch", "value": "1"}
    ]


async def test_rest_poll_still_runs_under_a_continuous_push_stream(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The 60 s poll keeps firing on a gateway that pushes every 20 s.

    The poll is the only thing that discovers new devices, prunes removed ones,
    assigns areas and detects gateway id churn, so starving it breaks all four.
    """
    coordinator = _coordinator(hass)
    devices = [_pushable_device()]
    coordinator.data = devices

    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_async_update_data",
        AsyncMock(return_value=devices),
    ) as poll:
        # A listener is what makes the coordinator schedule refreshes at all.
        unsub = coordinator.async_add_listener(lambda: None)
        # Three minutes of traffic, one push every 20 s.
        for _ in range(9):
            freezer.tick(timedelta(seconds=20))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            coordinator._handle_websocket_message(_push())
            await hass.async_block_till_done()
        unsub()

    # Three minutes at a 60 s interval: at least two polls should have landed.
    assert poll.call_count >= 2, (
        f"pushes starved the REST poll (ran {poll.call_count} times in 3 minutes)"
    )


async def test_repeated_reconnect_failures_warn_only_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable gateway warns once, then drops to DEBUG.

    The loop retries forever, so warning on every attempt meant a gateway that
    stayed down produced a warning roughly once a minute indefinitely — the
    noise `log-when-unavailable` exists to prevent.
    """
    coordinator = _coordinator(hass)
    coordinator.data = []
    with caplog.at_level(logging.WARNING):
        await _run_failing_loop(coordinator, 5)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "disconnected" in r.message
    ]
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"


async def test_recovery_warns_once_and_rearms(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A stable reconnect logs the matching recovery and re-arms the warning."""
    coordinator = _coordinator(hass)
    coordinator._unavailable_logged = True

    coordinator._mark_session_stable(None)

    assert coordinator._unavailable_logged is False
