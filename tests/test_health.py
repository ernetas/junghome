"""The gateway's health log (``GET /healthstatus/``) and its repair issues.

Wire shapes mirror the v2.1.3 middleware (``health_status_service.js``): an
append-only list since the middleware started, newest first, of
``{level, time, description, details}`` with an ISO-8601 UTC ``time``. The
descriptions below are the firmware's own strings.
"""

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_TOKEN, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.health import (
    HEALTH_CONDITIONS,
    HEALTH_STATUS_REFRESH_INTERVAL,
    ISSUE_BLUETOOTH_FAILURE,
    ISSUE_OUT_OF_SEQUENCE_NUMBERS,
    ISSUE_PROJECT_INCOMPLETE,
    ISSUE_PROJECT_MISSING,
    ISSUE_TIME_SYNC,
    HealthEntry,
    active_health_conditions,
    parse_health_status,
)
from tests.conftest import PRISTINE_DEVICES, _fake_run_websocket, bare_coordinator

HOST = "1.2.3.4"


def _entry(description: str, level: str = "ERROR", details: object = "d") -> dict:
    return {
        "level": level,
        "time": "2026-09-26T08:00:00.000Z",
        "description": description,
        "details": details,
    }


UP_AND_RUNNING = _entry(
    "Your JUNG HOME Gateway is up and running",
    "INFO",
    "You now have access to all the features without additional waiting time",
)
BT_CHIP = _entry("JUNG HOME Gateway Bluetooth Chip start failure")
BT_ADAPTER = _entry("was not able to start bluetooth adapter, details: ", details={})
OUT_OF_SN = _entry("out of sequence numbers")
TIME_SYNC = _entry(
    "JUNG HOME Gateway Time Sync Error", details="Last successful sync was 30 hours ago"
)
# Every failed sync logs this first (`time_error` set true,
# sys_event_handler.js:132 → configuration_service.js:207); a failure > 24 h
# after the last good sync then logs TIME_SYNC in the same handler (:154). So a
# > 24 h failure reads, newest first, TIME_SYNC, TIME_ERROR — the firmware pair.
TIME_ERROR = _entry(
    "time error",
    details="An error exists with the current time setting in your JUNG HOME "
    "Gateway. This could be due to a missing Internet connection or the time "
    "server cannot be reached",
)
PROJECT_MISSING = _entry("JUNG HOME Project missing", "WARN")
PROJECT_NOT_UPLOADED = _entry("project not_uploaded", "WARN")
PROJECT_INCOMPLETE = _entry("JUNG HOME project is incomplete", "WARN")
NEW_PROJECT = _entry("New Bluetooth Mesh Project", "INFO")
UNREACHABLE = _entry(
    "JUNG HOME Devices are unreachable",
    "WARN",
    "2 devices cannot be reached: Hall, Door. These devices might be powered off",
)


def _issue(
    hass: HomeAssistant, key: str, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


def _raised(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        condition.key
        for condition in HEALTH_CONDITIONS
        if _issue(hass, condition.key, entry) is not None
    }


def _active(*raw: dict) -> set[str]:
    entries = parse_health_status(list(raw))
    assert entries is not None
    return set(active_health_conditions(entries))


def test_parse_keeps_order_and_normalises_fields() -> None:
    """Newest first as sent; ``details`` may be an object, ``level`` any case."""
    assert parse_health_status({"not": "a list"}) is None
    assert parse_health_status(None) is None
    parsed = parse_health_status(
        [
            BT_ADAPTER,
            "junk",
            {"level": "ERROR", "description": 7},
            {"description": "no fields"},
            _entry("lower", "warn", None),
        ]
    )
    assert parsed == (
        HealthEntry(
            level="ERROR",
            time="2026-09-26T08:00:00.000Z",
            description="was not able to start bluetooth adapter, details: ",
            # A caught Error serialises to `{}` over the middleware IPC.
            details="{}",
        ),
        HealthEntry(level="", time="", description="no fields", details=""),
        HealthEntry(
            level="WARN",
            time="2026-09-26T08:00:00.000Z",
            description="lower",
            details="",
        ),
    )


def test_each_condition_is_raised_by_its_firmware_messages() -> None:
    """Every table message raises its condition; everything else raises nothing."""
    assert _active(BT_CHIP) == {ISSUE_BLUETOOTH_FAILURE}
    # The startup message ends in a space; the match ignores trailing space.
    assert _active(BT_ADAPTER) == {ISSUE_BLUETOOTH_FAILURE}
    assert _active({**BT_ADAPTER, "description": BT_ADAPTER["description"].rstrip()})
    assert _active(OUT_OF_SN) == {ISSUE_OUT_OF_SEQUENCE_NUMBERS}
    assert _active(TIME_SYNC, TIME_ERROR) == {ISSUE_TIME_SYNC}
    assert _active(PROJECT_MISSING) == {ISSUE_PROJECT_MISSING}
    assert _active(PROJECT_NOT_UPLOADED) == {ISSUE_PROJECT_MISSING}
    assert _active(PROJECT_INCOMPLETE) == {ISSUE_PROJECT_INCOMPLETE}
    # The middleware's `isDeviceOnline` list reads working push buttons as
    # unreachable: diagnostics only, never an issue. Nor is the generic
    # per-failure `time error` flag entry, or anything informational.
    assert (
        _active(
            UNREACHABLE,
            TIME_ERROR,
            _entry("btmesh error"),
            UP_AND_RUNNING,
            NEW_PROJECT,
        )
        == set()
    )
    assert _active() == set()


def test_a_new_project_clears_the_project_conditions_only_when_newer() -> None:
    """The log is newest first: a later import clears, an earlier one does not."""
    assert _active(NEW_PROJECT, PROJECT_MISSING, PROJECT_INCOMPLETE) == set()
    assert _active(PROJECT_NOT_UPLOADED, NEW_PROJECT) == {ISSUE_PROJECT_MISSING}
    # It clears nothing else: a dead chip stays dead until the gateway restarts.
    assert _active(NEW_PROJECT, BT_CHIP, OUT_OF_SN) == {
        ISSUE_BLUETOOTH_FAILURE,
        ISSUE_OUT_OF_SEQUENCE_NUMBERS,
    }


@asynccontextmanager
async def _running(
    hass: HomeAssistant, health: AsyncMock, time_error: AsyncMock | None = None
) -> AsyncIterator[MockConfigEntry]:
    """A running entry whose REST reads stay stubbed for the whole test."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=HOST, data={CONF_HOST: HOST, CONF_TOKEN: "tok"}
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_health_status_from_api", health
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_config_parameter_from_api",
            time_error or AsyncMock(return_value=True),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=HEALTH_STATUS_REFRESH_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_setup_raises_one_issue_per_condition_and_withdraws_on_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The setup read raises the issues; a restarted gateway's empty log withdraws them."""
    health = AsyncMock(
        return_value=[
            OUT_OF_SN,
            UNREACHABLE,
            PROJECT_INCOMPLETE,
            BT_CHIP,
            UP_AND_RUNNING,
        ]
    )
    async with _running(hass, health) as entry:
        assert _raised(hass, entry) == {
            ISSUE_BLUETOOTH_FAILURE,
            ISSUE_OUT_OF_SEQUENCE_NUMBERS,
            ISSUE_PROJECT_INCOMPLETE,
        }
        issue = _issue(hass, ISSUE_BLUETOOTH_FAILURE, entry)
        assert issue is not None
        assert issue.severity is ir.IssueSeverity.ERROR
        assert issue.translation_key == ISSUE_BLUETOOTH_FAILURE
        assert issue.translation_placeholders == {"host": HOST}
        assert not issue.is_fixable
        incomplete = _issue(hass, ISSUE_PROJECT_INCOMPLETE, entry)
        assert incomplete is not None
        assert incomplete.severity is ir.IssueSeverity.WARNING
        coordinator = entry.runtime_data
        assert coordinator.health.entries is not None
        assert len(coordinator.health.entries) == 5

        # Nothing changed: the periodic re-read keeps them (idempotently).
        await _tick(hass, freezer)
        assert health.await_count == 2
        assert len(_raised(hass, entry)) == 3

        # The gateway restarted: its log starts over.
        health.return_value = [UP_AND_RUNNING]
        await _tick(hass, freezer)
        assert _raised(hass, entry) == set()
        assert coordinator.health.conditions == frozenset()


async def test_a_new_project_withdraws_the_project_issue(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The log never shrinks; a newer import message is what clears the condition."""
    health = AsyncMock(return_value=[PROJECT_MISSING, UP_AND_RUNNING])
    async with _running(hass, health) as entry:
        assert _raised(hass, entry) == {ISSUE_PROJECT_MISSING}
        health.return_value = [NEW_PROJECT, PROJECT_MISSING, UP_AND_RUNNING]
        await _tick(hass, freezer)
        assert _raised(hass, entry) == set()


def test_a_later_missed_sync_round_clears_the_time_sync_condition() -> None:
    """Only the newest of the two time entries counts.

    The log is never pruned, so a > 24 h failure's entry stays in it after the
    clock recovers; a later single missed NTP round (< 24 h since the last good
    sync) logs only `time error` on top of it.
    """
    assert _active(TIME_ERROR, TIME_SYNC, TIME_ERROR) == set()
    # ... and a second > 24 h outage after that raises it again.
    assert _active(TIME_SYNC, TIME_ERROR, TIME_ERROR, TIME_SYNC, TIME_ERROR) == {
        ISSUE_TIME_SYNC
    }


async def test_time_sync_issue_follows_the_gateways_time_error_flag(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The log has no "recovered" message; `time_error` false withdraws the issue."""
    health = AsyncMock(return_value=[TIME_SYNC, TIME_ERROR, UP_AND_RUNNING])
    time_error = AsyncMock(return_value=True)
    async with _running(hass, health, time_error) as entry:
        assert _raised(hass, entry) == {ISSUE_TIME_SYNC}
        time_error.assert_awaited_with(HOST, "tok", "time_error")
        # An unreadable flag keeps what the log says.
        time_error.return_value = None
        await _tick(hass, freezer)
        assert _raised(hass, entry) == {ISSUE_TIME_SYNC}
        time_error.side_effect = aiohttp.ClientError("boom")
        await _tick(hass, freezer)
        assert _raised(hass, entry) == {ISSUE_TIME_SYNC}
        # Synchronised again: withdrawn although the entry is still logged.
        time_error.side_effect = None
        time_error.return_value = False
        await _tick(hass, freezer)
        assert _raised(hass, entry) == set()
        # The flag is only asked about while the log shows the condition.
        calls = time_error.await_count
        health.return_value = [UP_AND_RUNNING]
        await _tick(hass, freezer)
        assert time_error.await_count == calls


async def test_one_missed_sync_round_after_a_recovery_raises_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A stale > 24 h entry must not re-raise the issue on the next failed round.

    The issue says the clock has not synchronised for more than 24 hours; one
    missed NTP round after a recovery (`time_error` true again, but the last
    good sync is recent) is exactly what it must not claim.
    """
    health = AsyncMock(return_value=[TIME_SYNC, TIME_ERROR, UP_AND_RUNNING])
    time_error = AsyncMock(return_value=True)
    async with _running(hass, health, time_error) as entry:
        assert _raised(hass, entry) == {ISSUE_TIME_SYNC}
        time_error.return_value = False  # synchronised again
        await _tick(hass, freezer)
        assert _raised(hass, entry) == set()
        # Later, one missed round: only the generic entry is logged.
        health.return_value = [TIME_ERROR, TIME_SYNC, TIME_ERROR, UP_AND_RUNNING]
        time_error.return_value = True
        await _tick(hass, freezer)
        assert _raised(hass, entry) == set()
        # Still failing a day later: the firmware logs the pair again.
        health.return_value = [TIME_SYNC, TIME_ERROR, *health.return_value]
        await _tick(hass, freezer)
        assert _raised(hass, entry) == {ISSUE_TIME_SYNC}


async def test_an_unreadable_log_changes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """401/404, a transport error or a non-list body leave the raised issues alone."""
    health = AsyncMock(return_value=[BT_CHIP])
    async with _running(hass, health) as entry:
        assert _raised(hass, entry) == {ISSUE_BLUETOOTH_FAILURE}
        for outcome in (None, {"error": "Unauthorized"}):
            health.return_value = outcome
            await _tick(hass, freezer)
            assert _raised(hass, entry) == {ISSUE_BLUETOOTH_FAILURE}
        health.side_effect = TimeoutError
        await _tick(hass, freezer)
        assert _raised(hass, entry) == {ISSUE_BLUETOOTH_FAILURE}
        assert entry.runtime_data.last_update_success


async def test_issues_outlive_an_unload_and_go_with_a_disable_or_removal(
    hass: HomeAssistant,
) -> None:
    """Unload keeps the issues (the next setup re-derives them); disable and removal withdraw them."""
    health = AsyncMock(return_value=[OUT_OF_SN])
    async with _running(hass, health) as entry:
        assert _raised(hass, entry) == {ISSUE_OUT_OF_SEQUENCE_NUMBERS}
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert _raised(hass, entry) == {ISSUE_OUT_OF_SEQUENCE_NUMBERS}
        # The gateway restarted meanwhile: the next setup's first read withdraws.
        health.return_value = [UP_AND_RUNNING]
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _raised(hass, entry) == set()
        health.return_value = [OUT_OF_SN]
        await entry.runtime_data.async_fetch_health_status()
        assert _raised(hass, entry) == {ISSUE_OUT_OF_SEQUENCE_NUMBERS}
        # A disabled entry sets up no more: nothing would ever withdraw it.
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert _raised(hass, entry) == set()
        await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
        await hass.async_block_till_done()
        assert _raised(hass, entry) == {ISSUE_OUT_OF_SEQUENCE_NUMBERS}
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert _raised(hass, entry) == set()


async def test_an_ignored_issue_stays_ignored_across_a_reload(
    hass: HomeAssistant,
) -> None:
    """Deleting the issue on unload lost the user's "Ignore" on every reload."""
    health = AsyncMock(return_value=[OUT_OF_SN])
    async with _running(hass, health) as entry:
        issue_id = f"{ISSUE_OUT_OF_SEQUENCE_NUMBERS}_{entry.entry_id}"
        ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert health.await_count == 2  # re-derived by the new setup's read
        issue = _issue(hass, ISSUE_OUT_OF_SEQUENCE_NUMBERS, entry)
        assert issue is not None
        assert issue.active
        assert issue.dismissed_version is not None


async def test_an_ignored_issue_stays_ignored_across_an_ha_shutdown(
    hass: HomeAssistant,
) -> None:
    """HA's stop runs ``stop()`` without an unload; the registry must keep the dismissal.

    The issue registry stores a non-persistent issue's dismissal and restores
    it when the next start re-creates the issue — but only for an issue that
    was not deleted on the way down.
    """
    health = AsyncMock(return_value=[OUT_OF_SN])
    async with _running(hass, health) as entry:
        issue_id = f"{ISSUE_OUT_OF_SEQUENCE_NUMBERS}_{entry.entry_id}"
        ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert entry.runtime_data._closing
        issue = _issue(hass, ISSUE_OUT_OF_SEQUENCE_NUMBERS, entry)
        assert issue is not None
        assert issue.dismissed_version is not None


async def test_a_read_that_lands_after_unload_raises_nothing(
    hass: HomeAssistant,
) -> None:
    """A read still in flight when ``stop()`` runs writes nothing."""
    coordinator = bare_coordinator(hass)
    entry = coordinator.config_entry

    async def _read(*_args: object) -> list[dict]:
        await coordinator.stop()
        return [BT_CHIP]

    with patch.object(coordinator, "_fetch_health_status_from_api", _read):
        await coordinator.async_fetch_health_status()
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_BLUETOOTH_FAILURE}_{entry.entry_id}"
        )
        is None
    )
    assert coordinator.health.entries is None


async def test_periodic_reads_do_not_stack(hass: HomeAssistant) -> None:
    """A tick arriving while a read is still running skips its turn."""
    coordinator = bare_coordinator(hass)
    coordinator.health.refresh_running = True
    with patch.object(coordinator, "async_fetch_health_status", AsyncMock()) as fetch:
        await coordinator._async_refresh_health_status(None)  # type: ignore[arg-type]
    fetch.assert_not_awaited()
    coordinator.health.refresh_running = False
    with patch.object(coordinator, "async_fetch_health_status", AsyncMock()) as fetch:
        await coordinator._async_refresh_health_status(None)  # type: ignore[arg-type]
    fetch.assert_awaited_once()
    assert not coordinator.health.refresh_running


@pytest.mark.real_health_fetch
async def test_rest_reads(hass: HomeAssistant, aioclient_mock) -> None:
    """The two reads carry the token; any non-200 (401 included) reads as None."""
    coordinator = bare_coordinator(hass)
    base = "https://h/api/junghome"
    aioclient_mock.get(f"{base}/healthstatus/", json=[BT_CHIP])
    aioclient_mock.get(f"{base}/config/parameter/time_error", json=False)
    assert await coordinator._fetch_health_status_from_api("h", "t") == [BT_CHIP]
    assert (
        await coordinator._fetch_config_parameter_from_api("h", "t", "time_error")
        is False
    )
    assert all(call[3]["token"] == "t" for call in aioclient_mock.mock_calls)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/healthstatus/", status=401)
    aioclient_mock.get(f"{base}/config/parameter/time_error", status=404)
    assert await coordinator._fetch_health_status_from_api("h", "t") is None
    assert (
        await coordinator._fetch_config_parameter_from_api("h", "t", "time_error")
        is None
    )
