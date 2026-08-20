"""Tests for the Jung Home data update coordinator."""

import asyncio
import json
import logging
import random
from datetime import timedelta
from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome.const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    scene_unique_id,
)
from custom_components.junghome.coordinator import (
    INITIAL_RECONNECT_DELAY,
    MAX_RECONNECT_FAILURES,
    STABLE_SESSION_SECONDS,
    JungHomeDataUpdateCoordinator,
    poll_interval_from_options,
)
from tests.conftest import _auto_reply_to_datapoint_commands


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # Absent -> default; in-range values pass through (floats truncated,
        # matching the int the options form stores).
        ({}, DEFAULT_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: 300}, 300),
        ({CONF_POLL_INTERVAL: 120.7}, 120),
        ({CONF_POLL_INTERVAL: "90"}, 90),
        # Out-of-range values are clamped, not trusted: a hand-edited 1 must
        # not hammer the gateway, a huge value must not disable the backstop.
        ({CONF_POLL_INTERVAL: 1}, MIN_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: 10**6}, MAX_POLL_INTERVAL_SECONDS),
        # Junk falls back to the default rather than failing entry setup.
        ({CONF_POLL_INTERVAL: "abc"}, DEFAULT_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: None}, DEFAULT_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: True}, DEFAULT_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: float("nan")}, DEFAULT_POLL_INTERVAL_SECONDS),
        ({CONF_POLL_INTERVAL: [60]}, DEFAULT_POLL_INTERVAL_SECONDS),
    ],
)
def test_poll_interval_from_options(stored: dict, expected: int) -> None:
    """The stored option is defaulted, coerced and clamped defensively."""
    assert poll_interval_from_options(stored) == expected


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


def _switch_device(value: str) -> dict:
    """One device with a single switch datapoint at the given value."""
    return {
        "id": "dev1",
        "label": "Lamp",
        "datapoints": [
            {
                "id": "dp-1",
                "type": "switch",
                "values": [{"key": "switch", "value": value}],
            },
            # A malformed id-less datapoint: the overlay application must skip
            # it rather than raise.
            {"type": "switch", "values": []},
        ],
    }


async def test_push_during_poll_wins_over_the_stale_snapshot(
    hass: HomeAssistant,
) -> None:
    """A push landing mid-poll must not be reverted by the poll's snapshot.

    The REST snapshot is generated before a push that races the response, so
    adopting it as-is briefly rolled the pushed value back until the next
    push or poll healed it (the switch visibly flicked off and on again).
    """
    coordinator = _coordinator(hass)
    coordinator.data = [_switch_device("0")]
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _slow_fetch(host: str, token: str) -> list[dict]:
        fetch_started.set()
        await release_fetch.wait()
        return [_switch_device("0")]  # snapshot predating the push

    with patch.object(coordinator, "_fetch_devices_from_api", _slow_fetch):
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await fetch_started.wait()
        # The light is switched on while the poll is in flight.
        coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {"id": "dp-1", "values": [{"key": "switch", "value": "1"}]},
            }
        )
        release_fetch.set()
        result = await poll

    assert result[0]["datapoints"][0]["values"] == [{"key": "switch", "value": "1"}]
    # The overlay is closed once the poll completes; later pushes with no poll
    # in flight are not recorded anywhere.
    assert coordinator._poll_push_overlay is None


async def test_push_for_a_device_the_poll_discovers_survives_it(
    hass: HomeAssistant,
) -> None:
    """An unmatched push (brand-new device) still wins over the discovering poll.

    The push arrives before the poll has ever seen the device, so there is no
    stored datapoint to merge into — but the poll's snapshot of that new
    device may predate the push just the same.

    This also exercises the overlap-insurance path: the unmatched push
    immediately requests a second refresh (request_refresh, immediate
    debounce) while the first poll is still fetching. On the pinned HA that
    second refresh SERIALIZES behind the first (every refresh path takes the
    coordinator's debouncer lock — see the `_poll_push_overlay` comment), so
    true overlap cannot occur in production; the shared, refcounted overlay
    is retained as insurance against that private HA detail changing, and
    this test drives `_async_update_data` directly enough to keep the join
    logic honest (a naive per-poll dict was clobbered by the second poll
    opening it, losing the recorded push).
    """
    coordinator = _coordinator(hass)
    coordinator.data = []  # the device is not known yet
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _slow_fetch(host: str, token: str) -> list[dict]:
        fetch_started.set()
        await release_fetch.wait()
        return [_switch_device("0")]

    with patch.object(coordinator, "_fetch_devices_from_api", _slow_fetch):
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await fetch_started.wait()
        coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {"id": "dp-1", "values": [{"key": "switch", "value": "1"}]},
            }
        )
        release_fetch.set()
        result = await poll

    assert result[0]["datapoints"][0]["values"] == [{"key": "switch", "value": "1"}]
    # Let the push-triggered second refresh finish, then drain the debouncer
    # so its timer doesn't linger into teardown. The second refresh is the
    # last poll out, so it closes the shared overlay.
    await hass.async_block_till_done()
    assert coordinator._poll_push_overlay is None
    assert coordinator._polls_in_flight == 0
    await coordinator.async_shutdown()


async def test_poll_failure_discards_the_push_overlay(hass: HomeAssistant) -> None:
    """A failed poll closes the overlay: nothing to re-apply, nothing leaks."""
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
    assert coordinator._poll_push_overlay is None


async def test_functions_broadcast_supersedes_a_racing_polls_membership(
    hass: HomeAssistant,
) -> None:
    """A `functions` broadcast mid-poll must not be overwritten by the poll.

    The broadcast is the authoritative device list, sent on membership change;
    a poll whose fetch was already in flight carries an OLDER snapshot. The
    per-datapoint overlay only re-applies *values*, so adopting that snapshot
    used to resurrect removed devices and drop just-added ones until the next
    poll healed it. The poll must discard its snapshot in favour of the
    broadcast's list — including a value pushed for the new device after the
    broadcast — and must not advance `data_generation` a second time for the
    membership change listeners already counted.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [_switch_device("0")]
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _slow_fetch(host: str, token: str) -> list[dict]:
        fetch_started.set()
        await release_fetch.wait()
        return [_switch_device("0")]  # snapshot without the new device

    added_device = {
        "id": "dev2",
        "label": "New Socket",
        "datapoints": [
            {
                "id": "dp-2",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            }
        ],
    }
    with patch.object(coordinator, "_fetch_devices_from_api", _slow_fetch):
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await fetch_started.wait()
        # Membership changes mid-poll: the gateway broadcasts the full list.
        coordinator._handle_websocket_message(
            {"type": "functions", "data": [_switch_device("0"), added_device]}
        )
        generation_after_broadcast = coordinator.data_generation
        # The new device pushes a value before the poll returns.
        coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {"id": "dp-2", "values": [{"key": "switch", "value": "1"}]},
            }
        )
        release_fetch.set()
        result = await poll

    # The poll's stale snapshot was discarded: the broadcast's membership (and
    # the value pushed onto it) survive, and the generation did not advance a
    # second time for the same membership change.
    assert [d["id"] for d in result] == ["dev1", "dev2"]
    assert result[1]["datapoints"][0]["values"] == [{"key": "switch", "value": "1"}]
    assert coordinator.data_generation == generation_after_broadcast
    # The overlay closed normally despite the discarded snapshot.
    assert coordinator._poll_push_overlay is None


async def test_a_broadcast_before_the_fetch_does_not_discard_the_poll(
    hass: HomeAssistant,
) -> None:
    """Only a broadcast DURING the fetch supersedes it.

    A broadcast that was fully adopted before the poll's fetch even started is
    older than the fetch's snapshot, so the poll must adopt normally — the
    counter is snapshotted at fetch start, not at scheduling time.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [_switch_device("0")]
    coordinator._handle_websocket_message(
        {"type": "functions", "data": [_switch_device("0")]}
    )
    generation_after_broadcast = coordinator.data_generation

    with patch.object(
        coordinator,
        "_fetch_devices_from_api",
        AsyncMock(return_value=[_switch_device("1")]),
    ):
        result = await coordinator._async_update_data()

    # The poll adopted its own (fresher) snapshot and advanced the generation.
    assert result[0]["datapoints"][0]["values"] == [{"key": "switch", "value": "1"}]
    assert coordinator.data_generation == generation_after_broadcast + 1


async def test_a_superseded_poll_does_not_re_flag_id_churn(
    hass: HomeAssistant,
) -> None:
    """The superseded poll must skip the id-churn check, not just the adoption.

    On a firmware update the `functions` broadcast carries regenerated ids: it
    detects the churn, schedules the reload and rewrites `_device_ids` to the
    NEW ids. A poll whose fetch was already in flight returns the PRE-update
    list; running the churn check on it would detect "churn" a second time —
    scheduling a redundant reload AND clobbering `_device_ids` back to the
    stale ids, because the check overwrites the map with whatever list it is
    handed.
    """
    coordinator = _coordinator(hass)
    old_device = {"id": "idOLD", "label": "Lamp", "datapoints": []}
    new_device = {"id": "idNEW", "label": "Lamp", "datapoints": []}
    coordinator.data = [old_device]
    coordinator._device_ids = {"lamp": "idOLD"}
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def _slow_fetch(host: str, token: str) -> list[dict]:
        fetch_started.set()
        await release_fetch.wait()
        return [dict(old_device)]  # the pre-update snapshot

    with (
        patch.object(coordinator, "_fetch_devices_from_api", _slow_fetch),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await fetch_started.wait()
        coordinator._handle_websocket_message(
            {"type": "functions", "data": [new_device]}
        )
        release_fetch.set()
        await poll

    reload.assert_called_once()
    assert coordinator._device_ids == {"lamp": "idNEW"}


async def test_a_broadcast_that_fails_to_adopt_does_not_supersede_a_poll(
    hass: HomeAssistant,
) -> None:
    """The broadcast counter must only rise once the list is actually adopted.

    `_reload_if_device_ids_changed` runs before the adoption and can raise on a
    malformed frame — `device_slug` slugifies the label, which throws on a
    non-string — and `_dispatch_text_frame`'s catch-all swallows it. Counting
    the broadcast before that point would let such a frame suppress a racing
    poll that carried the fresher list, leaving stale membership for a full
    poll interval.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [_switch_device("0")]
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    added_device = {
        "id": "dev2",
        "label": "New Socket",
        "datapoints": [
            {
                "id": "dp-2",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            }
        ],
    }

    async def _slow_fetch(host: str, token: str) -> list[dict]:
        fetch_started.set()
        await release_fetch.wait()
        return [_switch_device("0"), added_device]  # the fresher list

    with patch.object(coordinator, "_fetch_devices_from_api", _slow_fetch):
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await fetch_started.wait()
        # A malformed broadcast: the label is not a string, so slugify raises
        # out of the id-churn check and the list is never adopted. Routed
        # through `_dispatch_text_frame`, whose catch-all swallows it exactly
        # as it would for a real frame off the wire.
        coordinator._dispatch_text_frame(
            json.dumps({"type": "functions", "data": [{"id": "dev1", "label": 7}]})
        )
        release_fetch.set()
        result = await poll

    assert [d["id"] for d in result] == ["dev1", "dev2"]


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


async def test_no_reload_when_duplicate_slug_order_flips(hass: HomeAssistant) -> None:
    """Colliding slugs must be skipped from the id-change map entirely.

    Two devices whose labels slug identically share one key in the slug->id
    map; the gateway's list order decides which id "wins". Without the
    duplicate_slugs guard, a mere order change between polls read as "the id
    changed (firmware update?)" and scheduled a reload — on every flip,
    forever. A non-colliding device's genuine id change must still reload.
    """
    coordinator = _coordinator(hass)
    lamp_a = {"id": "idA", "label": "Lamp 1"}
    lamp_b = {"id": "idB", "label": "Lamp-1"}  # both slug to lamp_1
    other = {"id": "idC", "label": "Katilas"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed([lamp_a, lamp_b, other])
        coordinator._reload_if_device_ids_changed([lamp_b, lamp_a, other])
        coordinator._reload_if_device_ids_changed([lamp_a, lamp_b, other])
    reload.assert_not_called()
    # The colliding slug is not tracked at all; the healthy device is.
    assert "lamp_1" not in coordinator._device_ids
    assert coordinator._device_ids == {"katilas": "idC"}

    # A genuine id change on the non-colliding device still reloads.
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed(
            [lamp_b, lamp_a, {"id": "idNEW", "label": "Katilas"}]
        )
    reload.assert_called_once()


def _coordinator_with_ws(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    coordinator = _coordinator(hass)
    coordinator.websocket = _auto_reply_to_datapoint_commands(coordinator)
    return coordinator


async def test_cover_climate_command_payloads(hass: HomeAssistant) -> None:
    """The new command methods build the expected datapoint set frames."""
    coordinator = _coordinator_with_ws(hass)
    # Matching stub data for every id these commands target, so the confirmed
    # reply the auto-reply mock echoes back finds a datapoint to merge into
    # (as a real command's reply always does — it targets an id the caller
    # just read off coordinator.data) instead of hitting the unmatched-push
    # path and scheduling a refresh the test never cleans up.
    coordinator.data = [
        {
            "id": "dev",
            "label": "Dev",
            "datapoints": [
                {
                    "id": "dp-1",
                    "type": "level",
                    "values": [{"key": "level", "value": "0"}],
                },
                {
                    "id": "dp-2",
                    "type": "angle",
                    "values": [{"key": "angle", "value": "0"}],
                },
                {
                    "id": "dp-3",
                    "type": "temperature_ctrl",
                    "values": [{"key": "temperature_ctrl", "value": "20"}],
                },
            ],
        }
    ]
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


async def test_command_reply_confirms_and_merges_the_read_back_value(
    hass: HomeAssistant,
) -> None:
    """A successful command reply merges into coordinator.data like a push.

    The gateway's reply carries the freshly re-read datapoint, not just an ack
    — awaiting it (rather than firing and forgetting) means the coordinator's
    stored state reflects the CONFIRMED value the instant the command method
    returns, before any caller-side optimistic write.
    """
    coordinator = _coordinator_with_ws(hass)
    coordinator.data = [
        {
            "id": "dev1",
            "label": "Blind",
            "datapoints": [
                {
                    "id": "dp-1",
                    "type": "level",
                    "values": [{"key": "level", "value": "10"}],
                }
            ],
        }
    ]
    await coordinator.set_level("dp-1", 75)
    assert coordinator.data[0]["datapoints"][0]["values"] == [
        {"key": "level", "value": "75"}
    ]


async def test_command_times_out_when_gateway_never_replies(
    hass: HomeAssistant,
) -> None:
    """A command the gateway silently drops surfaces as a real service error.

    Firmware-verified: a rejected datapoint set produces only an uncorrelated
    `error:` message frame (websocket-server-service.js), so there is nothing
    to await besides a timeout. Before this, the send was fire-and-forget and
    a rejected command looked identical to a successful one.
    """
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws  # accepts the send, never produces a reply

    with (
        patch("custom_components.junghome.coordinator.COMMAND_REPLY_TIMEOUT", 0.01),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await coordinator.turn_on_switch("dp-1")
    assert exc_info.value.translation_key == "command_timeout"
    # The pending entry must not leak once the wait gives up.
    assert coordinator._pending_replies == {}


async def test_uncorrelated_error_frame_does_not_resolve_a_pending_command(
    hass: HomeAssistant,
) -> None:
    """An `error:` message frame carries no message_id, so it must not be
    mistaken for the reply to whichever command happens to be in flight —
    that would misattribute a different command's failure. The pending
    command still only settles via COMMAND_REPLY_TIMEOUT.
    """
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws

    async def _send_then_inject_error(raw: str) -> None:
        coordinator._dispatch_text_frame(
            json.dumps({"type": "message", "data": "error: could not set datapoint"})
        )

    ws.send_str.side_effect = _send_then_inject_error

    with (
        patch("custom_components.junghome.coordinator.COMMAND_REPLY_TIMEOUT", 0.01),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await coordinator.turn_on_switch("dp-1")
    assert exc_info.value.translation_key == "command_timeout"


async def test_ws_drop_fails_inflight_commands_immediately(
    hass: HomeAssistant,
) -> None:
    """A dropped session fails in-flight commands now, not after the timeout.

    The gateway replies only to the socket that carried the request, so once
    the session is gone the confirmation can never arrive — waiting out
    COMMAND_REPLY_TIMEOUT would stall the service call (and entry unload) for
    the full 5 s and then blame the wrong thing ("did not confirm in time"
    instead of the connection loss).
    """
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws  # accepts the send, never produces a reply

    task = asyncio.ensure_future(coordinator.turn_on_switch("dp-1"))
    await asyncio.sleep(0)  # let the send land and register the future
    assert len(coordinator._pending_replies) == 1

    # An already-settled future (reply raced the drop) must be left alone.
    done_future: asyncio.Future[dict] = hass.loop.create_future()
    done_future.set_result({})
    coordinator._pending_replies["ha-done"] = done_future

    # No COMMAND_REPLY_TIMEOUT patch: the point is that this does NOT wait.
    coordinator._fail_pending_replies()
    with pytest.raises(HomeAssistantError) as exc_info:
        await task
    assert exc_info.value.translation_key == "cannot_send"
    assert done_future.result() == {}  # untouched by the sweep
    coordinator._pending_replies.pop("ha-done")
    assert coordinator._pending_replies == {}


async def test_concurrent_commands_do_not_cross_resolve(hass: HomeAssistant) -> None:
    """Two in-flight commands get distinct message_ids; replying to one must
    not resolve the other."""
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False
    sent_ids: list[str] = []

    def _capture(raw: str) -> None:
        sent_ids.append(json.loads(raw)["message_id"])

    ws.send_str.side_effect = _capture
    coordinator.websocket = ws
    coordinator.data = [
        {
            "id": "dev1",
            "label": "L",
            "datapoints": [
                {
                    "id": "dp-a",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "0"}],
                },
                {
                    "id": "dp-b",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "0"}],
                },
            ],
        }
    ]

    task_a = asyncio.ensure_future(coordinator.turn_on_switch("dp-a"))
    task_b = asyncio.ensure_future(coordinator.turn_on_switch("dp-b"))
    await asyncio.sleep(0)  # let both sends land and register their futures
    assert len(sent_ids) == 2
    assert len(coordinator._pending_replies) == 2

    # Reply only to the SECOND command sent; the first must remain pending.
    coordinator._dispatch_text_frame(
        json.dumps(
            {
                "type": "datapoint",
                "data": {"id": "dp-b", "values": [{"key": "switch", "value": "1"}]},
                "message_id": sent_ids[1],
            }
        )
    )
    await task_b
    assert not task_a.done()

    # Clean up: reply to the first so the test doesn't leak a pending task.
    coordinator._dispatch_text_frame(
        json.dumps(
            {
                "type": "datapoint",
                "data": {"id": "dp-a", "values": [{"key": "switch", "value": "1"}]},
                "message_id": sent_ids[0],
            }
        )
    )
    await task_a


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


async def test_scene_recall_event_carries_the_entity_id(
    hass: HomeAssistant,
) -> None:
    """A recall for a label with a registered scene entity links to it.

    The entity_id lets the logbook line deep-link and automations match on
    the entity rather than the (locale-specific) label.
    """
    coordinator = _coordinator(hass)
    entry = coordinator.config_entry
    registered = er.async_get(hass).async_get_or_create(
        "scene",
        DOMAIN,
        scene_unique_id(entry, "Movie Night"),
        config_entry=entry,
    )
    events = []
    hass.bus.async_listen(f"{DOMAIN}_scene_recalled", events.append)
    coordinator._handle_websocket_message(
        {"type": "scene", "data": {"id": "id0002", "label": "Movie Night"}}
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["entity_id"] == registered.entity_id


async def test_unexpected_error_in_frame_handler_is_contained(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug raised while handling one frame is logged, never propagated.

    The catch-all is the last line of defence for the WebSocket session: an
    exception escaping _dispatch_text_frame would tear down an otherwise
    healthy connection over a single bad frame.
    """
    coordinator = _coordinator(hass)
    with patch.object(
        coordinator, "_handle_websocket_message", side_effect=RuntimeError("boom")
    ):
        coordinator._dispatch_text_frame('{"type": "datapoint", "data": {}}')
    assert any(
        "Unexpected error handling WebSocket message" in r.getMessage()
        for r in caplog.records
    )


async def test_scenes_new_dedupes_by_label_keeping_newest(
    hass: HomeAssistant,
) -> None:
    """A scenes-new delta re-keying a label to a new id drops the old entry.

    Scene identity is the label; after a firmware update regenerates ids, the
    old and new entries would otherwise sit side by side and activation could
    resolve the dead id. Unlabeled scenes are kept (diagnostics only).
    """
    coordinator = _coordinator(hass)
    coordinator.scenes = [
        {"id": "old-id", "label": "Movie Night"},
        {"id": "keep-id", "label": "Dinner"},
    ]
    coordinator._handle_websocket_message(
        {
            "type": "scenes-new",
            "data": [
                {"id": "new-id", "label": "Movie Night"},
                {"id": "no-label"},
            ],
        }
    )
    by_label = {s.get("label"): s.get("id") for s in coordinator.scenes}
    assert by_label["Movie Night"] == "new-id"
    assert by_label["Dinner"] == "keep-id"
    assert {"id": "no-label"} in coordinator.scenes


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

    def __await__(self):
        """aiohttp's ws_connect result is awaitable as well as an async CM."""

        async def _resolve() -> "Self":
            return self

        return _resolve().__await__()

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

    def __await__(self):
        """aiohttp's ws_connect result is awaitable as well as an async CM."""

        async def _resolve() -> "Self":
            return self

        return _resolve().__await__()

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


async def test_send_is_bounded_by_a_timeout(hass: HomeAssistant) -> None:
    """A peer that stops reading must not hang the calling service call.

    `send_str` awaits the transport drain and has no timeout of its own, so
    without a bound a stalled gateway blocked the caller indefinitely.
    """
    coordinator = _coordinator(hass)
    ws = AsyncMock()
    ws.closed = False

    async def _never_returns(_data: str) -> None:
        await asyncio.Event().wait()

    ws.send_str = _never_returns
    coordinator.websocket = ws

    with patch("custom_components.junghome.coordinator.WS_SEND_TIMEOUT", 0.01):
        with pytest.raises(HomeAssistantError):
            await coordinator.send_websocket_message({"type": "x"})


async def test_connect_is_bounded_by_a_timeout(hass: HomeAssistant) -> None:
    """A gateway that accepts the socket then says nothing must not park the loop.

    The shared session's default is ClientTimeout(total=300), which would leave
    the reconnect loop stuck for five minutes with every controllable entity
    unavailable and no failure counted.
    """
    coordinator = _coordinator(hass)
    coordinator.data = []

    async def _never_connects(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()

    session = Mock()
    session.ws_connect = Mock(side_effect=_never_connects)
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch("custom_components.junghome.coordinator.WS_CONNECT_TIMEOUT", 0.01),
        pytest.raises(TimeoutError),
    ):
        await coordinator._run_websocket()


def _fuzz_frames(rng: random.Random, count: int) -> list[str]:
    """Adversarial frames: valid JSON of hostile shapes, plus raw junk.

    Deterministic (seeded) so a failure is reproducible. Shapes chosen to bait
    every parsing hazard the coordinator guards: huge integers (``float()``
    raises OverflowError on them), NaN/Infinity literals, wrong-typed ids and
    values (dict/list/bool where strings are expected), unhashable group ids,
    deep nesting, and non-JSON byte junk.
    """

    def junk_value(depth: int = 0) -> object:
        choices = [
            lambda: rng.randint(-(10**400), 10**400),
            lambda: rng.random() * 10**308,
            lambda: "x" * rng.randint(0, 500),
            lambda: None,
            lambda: rng.choice([True, False]),
            lambda: "\x00\U000107ff\U0001f600"[: rng.randint(0, 4)],
        ]
        if depth < 3:
            choices += [
                lambda: [junk_value(depth + 1) for _ in range(rng.randint(0, 4))],
                lambda: {
                    str(junk_value(depth + 1))[:20]: junk_value(depth + 1)
                    for _ in range(rng.randint(0, 4))
                },
            ]
        return rng.choice(choices)()

    frame_types = [
        "datapoint",
        "scene",
        "scenes",
        "scenes-new",
        "scenes-deleted",
        "groups",
        "functions",
        "message",
        "version",
        "devices-new",
        None,
        junk_value,
    ]
    frames: list[str] = []
    for _ in range(count):
        kind = rng.choice(frame_types)
        if callable(kind):
            kind = kind()
        frame: dict = {"type": kind, "data": junk_value()}
        if rng.random() < 0.5:
            frame["message_id"] = junk_value()
        if kind == "datapoint" and rng.random() < 0.7:
            frame["data"] = {
                "id": rng.choice(["dp1", "", None, 42, ["x"], {"a": 1}]),
                "type": junk_value(),
                "values": rng.choice(
                    [[{"key": junk_value(), "value": junk_value()}], junk_value(), []]
                ),
            }
        try:
            frames.append(json.dumps(frame, ensure_ascii=False))
        except (TypeError, ValueError):
            continue
    frames += ["", "{", "null", "[1,", '"str"', "\x00\x01", "NaN", "Infinity"]
    return frames


async def test_fuzz_dispatch_never_raises_or_corrupts(hass: HomeAssistant) -> None:
    """1 500 seeded adversarial frames: dispatch must never raise or corrupt.

    ``_dispatch_text_frame`` is the containment boundary for everything the
    wire can carry — this drives it with hostile shapes end to end, then
    checks the coordinator's structural invariants and that the group/area
    resolvers still cope with whatever the storm stored. The refresh paths
    are stubbed: unmatched fuzz ids would otherwise fan out thousands of
    debounced refresh tasks against a real socket.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [
        {
            "id": "dev1",
            "type": "OnOff",
            "label": "Dev",
            "datapoints": [
                {
                    "id": "dp1",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "0"}],
                }
            ],
        }
    ]
    rng = random.Random(20260803)  # noqa: S311 - deterministic fuzz seed
    with (
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
        patch.object(
            coordinator, "_fetch_devices_from_api", AsyncMock(return_value=[])
        ),
    ):
        for raw in _fuzz_frames(rng, 1500):
            coordinator._dispatch_text_frame(raw)  # must never raise

    assert coordinator._polls_in_flight == 0
    assert coordinator._poll_push_overlay is None
    assert coordinator._pending_replies == {}
    # The storm may have stored arbitrary garbage in groups/scenes; the
    # resolvers must still tolerate it together with malformed devices.
    for device in (
        {"id": "d", "parent_groups": ["g1", ["x"], {"y": 1}, True]},
        {"id": "d", "parent_groups": "not-a-list"},
        {"id": "d"},
    ):
        coordinator.area_for_device(device)
        coordinator.color_temp_range_for_device(device)
    await coordinator.async_shutdown()


async def test_command_futures_race_replies_and_session_drop(
    hass: HomeAssistant,
) -> None:
    """Eight concurrent commands, half confirmed, half killed by a drop.

    Every await must complete promptly with the truthful outcome — no command
    may hang toward COMMAND_REPLY_TIMEOUT once the session is gone, and the
    pending-reply registry must end empty either way.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [
        {
            "id": "dev1",
            "label": "Dev",
            "datapoints": [{"id": "dp1", "type": "switch", "values": []}],
        }
    ]
    ws = AsyncMock()
    ws.closed = False
    coordinator.websocket = ws

    async def run_one() -> str:
        try:
            await coordinator.turn_on_switch("dp1")
        except HomeAssistantError:
            return "failed"
        return "ok"

    tasks = [hass.async_create_task(run_one()) for _ in range(8)]
    await asyncio.sleep(0)
    for message_id in list(coordinator._pending_replies)[:4]:
        coordinator._dispatch_text_frame(
            json.dumps(
                {"type": "datapoint", "data": {"id": "dp1"}, "message_id": message_id}
            )
        )
    coordinator._fail_pending_replies()
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert outcomes.count("ok") == 4
    assert outcomes.count("failed") == 4
    assert coordinator._pending_replies == {}
    await coordinator.async_shutdown()


@pytest.mark.real_version_fetch
async def test_gateway_version_is_read_over_rest(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The device pages must show the GATEWAY's version, not the API's.

    The WebSocket handshake's `version` frame carries `api-junghome`'s own
    package version ("1.5.0"), which is the API contract — not the firmware.
    Stamping it as `sw_version` reported "1.5.0" on every device page for a
    gateway actually running 2.1.3 build 2840. The real value lives in the
    middleware's `version` topic, populated from the board controller's
    `MSG_SW_VERSION_IND`.
    """
    coordinator = _coordinator(hass)
    base = "https://h/api/junghome/config/parameter"
    aioclient_mock.get(f"{base}/version_release", json="2.1.3")
    aioclient_mock.get(f"{base}/version_build", json="2840")

    await coordinator.async_fetch_gateway_version()

    assert coordinator.gateway_version == "2.1.3 (2840)"
    # The API version is a separate number and must not be confused with it.
    assert coordinator.api_version is None


@pytest.mark.real_version_fetch
async def test_gateway_version_tolerates_missing_or_unread_parameters(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Anything short of a real reading must leave the version unset.

    `"0.0.0"` / `"0"` are the state DB's declared defaults: the middleware ships
    them until the board controller has answered, so they mean "not known yet",
    not "version 0". Firmware without the parameter 404s. Neither is worth
    failing setup over, and neither may be published as a version.
    """
    coordinator = _coordinator(hass)
    base = "https://h/api/junghome/config/parameter"

    # Older firmware: parameter unknown -> 404.
    aioclient_mock.get(f"{base}/version_release", status=404)
    await coordinator.async_fetch_gateway_version()
    assert coordinator.gateway_version is None

    # Middleware has not read the board yet -> the declared default.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/version_release", json="0.0.0")
    await coordinator.async_fetch_gateway_version()
    assert coordinator.gateway_version is None

    # A release with no build yet -> the release alone, not "2.1.3 (0)".
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/version_release", json="2.1.3")
    aioclient_mock.get(f"{base}/version_build", json="0")
    await coordinator.async_fetch_gateway_version()
    assert coordinator.gateway_version == "2.1.3"

    # A transport failure leaves the previously known value in place.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/version_release", exc=aiohttp.ClientError())
    await coordinator.async_fetch_gateway_version()
    assert coordinator.gateway_version == "2.1.3"

    # A non-string body is not a version either.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/version_release", json=213)
    await coordinator.async_fetch_gateway_version()
    assert coordinator.gateway_version == "2.1.3"

    # Re-reading the same version writes nothing: `_apply_gateway_version`
    # walks every registry row, and the stable-session hook calls this on
    # every reconnect.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/version_release", json="2.1.3")
    aioclient_mock.get(f"{base}/version_build", json="0")
    with patch.object(coordinator, "_apply_gateway_version") as apply:
        await coordinator.async_fetch_gateway_version()
    apply.assert_not_called()
    assert coordinator.gateway_version == "2.1.3"
