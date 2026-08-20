"""Shared fixtures for the Jung Home test suite."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import (
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str) -> Any:
    """Load a JSON document from ``tests/fixtures/``."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# The shared gateway payload, shaped exactly like ``GET /functions/`` returns
# it (and like the WS ``functions`` broadcast carries it). Kept as a JSON
# document so it reads as what it is — wire data, not Python — and can be
# diffed against real captures. Notable values: the Bedroom Blind's level is
# 30 (percent-closed, so HA position 70) and the Boiler's third quantity
# carries the unmapped unit "?" that pins the unmapped-unit warning.
DEVICES: list[dict] = load_json_fixture("functions.json")


# A deep copy of ``DEVICES`` taken at import time, before any test has run.
#
# ``DEVICES`` is handed to the coordinator by reference, and the coordinator
# merges WebSocket pushes straight into the device dicts it was given — so a
# test that turns a light on or drives a blind mutates the shared list for every
# test that follows it. That is harmless for tests asserting on structure (a
# device count, a slug), but the snapshot tests pin *values*, so they would
# otherwise pass or fail depending on execution order. Snapshot setups start
# from this pristine copy instead.
PRISTINE_DEVICES: list[dict] = deepcopy(DEVICES)


def bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    """Build a coordinator with an entry but no gateway data or WebSocket.

    The unit-level entity tests construct entities directly against this and
    hand-pick their device dicts; it replaces the identical private copy every
    platform test file used to carry. ``data`` starts as an empty list (not
    None) so lookups against ``coordinator.data`` see "gateway answered,
    device absent" rather than "never refreshed".
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


def _auto_reply_to_datapoint_commands(
    coordinator: JungHomeDataUpdateCoordinator,
) -> AsyncMock:
    """Build a fake WebSocket that immediately confirms any command it is sent.

    Command methods now await a `datapoint` reply echoing the `message_id` they
    sent (see `coordinator._send_datapoint_command`) instead of firing and
    forgetting, mirroring what the real gateway does
    (websocket-server-service.js: every set is answered with the re-read
    datapoint, tagged with the same message_id). Without this, every test that
    exercises a service call (turn_on, set_cover_position, ...) against a bare
    mocked socket would race COMMAND_REPLY_TIMEOUT and fail with a timeout
    error instead of completing.
    """
    ws = AsyncMock()
    ws.closed = False

    def _reply(raw: str) -> None:
        sent = json.loads(raw)
        message_id = sent.get("message_id")
        if sent.get("type") == "datapoint" and message_id:
            reply = json.dumps(
                {
                    "type": "datapoint",
                    "data": sent.get("data"),
                    "message_id": message_id,
                }
            )
            coordinator._dispatch_text_frame(reply)

    ws.send_str.side_effect = _reply
    return ws


async def _fake_run_websocket(self: JungHomeDataUpdateCoordinator) -> None:
    """Stand in for the real WebSocket: present a fake socket, then park."""
    ws = _auto_reply_to_datapoint_commands(self)
    self.websocket = ws
    self.ws_connected = True
    self.gateway_version = "1.5.0"
    # Mirror the real connect-time resync so listeners re-evaluate availability
    # now that the socket is up — controllable entities were added while
    # ws_connected was still False and would otherwise stay unavailable.
    self.async_update_listeners()
    await asyncio.Event().wait()


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return the snapshot fixture with Home Assistant's syrupy extension.

    Both ``syrupy`` and ``pytest_homeassistant_custom_component`` ship a plugin
    fixture named ``snapshot``, and which one wins depends on plugin
    registration order — which is not stable across machines (it bit CI on this
    very PR: locally the Home Assistant one won, on the runner syrupy's plain
    one did, so the extension's ``snapshots/`` directory was never consulted and
    every snapshot read as missing). Re-applying the extension from a conftest
    fixture settles it: conftest fixtures always take precedence over plugin
    fixtures, and re-wrapping an already-extended assertion is a no-op. This
    mirrors what Home Assistant core does in its own ``tests/conftest.py``.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
async def init_platform(
    hass: HomeAssistant,
) -> AsyncGenerator[Callable[..., Awaitable[MockConfigEntry]]]:
    """Return a factory that sets the integration up with a single platform.

    ``snapshot_platform`` refuses to snapshot a config entry that owns entities
    from more than one domain, so the snapshot tests load exactly one platform
    at a time by patching ``PLATFORMS`` for the duration of setup. Everything
    else (gateway devices, the faked WebSocket) matches ``init_integration``, so
    the snapshotted entities are the ones the rest of the suite exercises.

    The factory is awaited by the test and returns the entry; every entry it
    created is unloaded on teardown, with the coordinator patches still in place
    so the unload never reaches the real gateway.

    ``devices`` overrides the polled device list for callers that need a device
    ``PRISTINE_DEVICES`` does not carry (the presence detector, say), without
    perturbing the shared fixture every other test asserts against. Either way
    the list is deep-copied before the coordinator sees it, so the values a
    snapshot pins never depend on what an earlier test pushed.
    """
    entries: list[MockConfigEntry] = []
    fetch_devices = AsyncMock(return_value=deepcopy(PRISTINE_DEVICES))
    with (
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", fetch_devices
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):

        async def _setup(
            platform: Platform, devices: list[dict] | None = None
        ) -> MockConfigEntry:
            fetch_devices.return_value = deepcopy(
                PRISTINE_DEVICES if devices is None else devices
            )
            entry = MockConfigEntry(
                domain=DOMAIN,
                unique_id="1.2.3.4",
                data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
            )
            entry.add_to_hass(hass)
            with patch("custom_components.junghome.PLATFORMS", [platform]):
                await hass.config_entries.async_setup(entry.entry_id)
                await hass.async_block_till_done()
            entries.append(entry)
            return entry

        yield _setup

        for entry in entries:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


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
            AsyncMock(return_value=deepcopy(PRISTINE_DEVICES)),
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


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "real_groups_fetch: let the test run the real _fetch_groups_from_api "
        "(pair with aioclient_mock); by default it is stubbed to avoid a socket.",
    )
    config.addinivalue_line(
        "markers",
        "real_scenes_fetch: let the test run the real _fetch_scenes_from_api "
        "(pair with aioclient_mock); by default it is stubbed to avoid a socket.",
    )
    config.addinivalue_line(
        "markers",
        "real_serial_fetch: let the test run the real _async_fetch_serial "
        "(pair with aioclient_mock); by default it is stubbed to avoid a socket.",
    )
    config.addinivalue_line(
        "markers",
        "real_version_fetch: let the test run the real _fetch_config_parameter "
        "(pair with aioclient_mock); by default it is stubbed to avoid a socket.",
    )


@pytest.fixture(autouse=True)
def fail_on_home_assistant_deprecation_reports(caplog: pytest.LogCaptureFixture):
    """Fail any test that trips a Home Assistant deprecation report.

    ``homeassistant.helpers.frame.report_usage`` **logs**; it never calls
    ``warnings.warn``. So ``-W error::DeprecationWarning`` cannot see it, and a
    dated removal ("breaks in Home Assistant 2026.12") sits inside a fully green
    suite until the day it starts raising. That is exactly how pairing a
    reloading config-flow helper with an update listener — deprecated in HA
    2026.6 — survived unnoticed in the reauth path.

    The reports name the integration, so this catches ours and stays quiet for
    anything HA reports about itself.
    """
    yield
    offenders = [
        record.getMessage()
        for record in caplog.get_records("call")
        if record.name == "homeassistant.helpers.frame"
        and record.levelno >= logging.WARNING
    ]
    assert not offenders, "Home Assistant deprecation report(s): " + "; ".join(
        offenders
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the junghome custom integration in every test."""
    return


@pytest.fixture(autouse=True)
def mock_groups_fetch(request):
    """Keep the setup-time REST groups fetch off the network.

    ``async_setup_entry`` fetches the gateway's room groups over REST (see
    ``async_fetch_groups``) before the platforms are set up. Entity/lifecycle
    tests don't stub that call, so without this fixture they would open a real
    socket, which the Home Assistant test harness rejects. Default it to an
    empty list. Tests that assert on room -> area behaviour patch
    ``_fetch_groups_from_api`` themselves (that inner patch wins inside its
    ``with`` block), and the tests that cover the REST body opt out with
    ``@pytest.mark.real_groups_fetch``.
    """
    if request.node.get_closest_marker("real_groups_fetch") is not None:
        yield
        return
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_groups_from_api",
        AsyncMock(return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_version_fetch(request):
    """Keep the setup-time REST gateway-version read off the network.

    The mirror of ``mock_groups_fetch``: ``async_setup_entry`` also reads
    ``config/parameter/version_release`` (and ``version_build``) before the hub
    device is registered, so every device page carries the gateway's software
    version. Defaults to "parameter unavailable", which is exactly what an older
    firmware returns, so entity/lifecycle tests behave as they always did.
    """
    if request.node.get_closest_marker("real_version_fetch") is not None:
        yield
        return
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_config_parameter",
        AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_scenes_fetch(request):
    """Keep the setup-time REST scenes fetch off the network.

    The mirror of ``mock_groups_fetch``: ``async_setup_entry`` also fetches the
    gateway's scenes over REST before the platforms are set up, so that scenes
    exist even when the WebSocket never connects. Same defaulting, same opt-out.
    """
    if request.node.get_closest_marker("real_scenes_fetch") is not None:
        yield
        return
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_scenes_from_api",
        AsyncMock(return_value=[]),
    ):
        yield
