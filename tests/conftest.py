"""Shared fixtures for the Jung Home test suite."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from copy import deepcopy
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

DEVICES = [
    {
        "id": "idlight1",
        "type": "OnOff",
        "label": "Hall Light",
        "datapoints": [
            {
                "id": "idlight1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            }
        ],
    },
    {
        "id": "idcolor1",
        "type": "ColorLight",
        "label": "Strip",
        "datapoints": [
            {
                "id": "idcolor1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "idcolor1-002",
                "type": "brightness",
                "values": [{"key": "brightness", "value": "50"}],
            },
            {
                "id": "idcolor1-004",
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": "2700"}],
            },
        ],
    },
    {
        "id": "iddim1",
        "type": "DimmerLight",
        "label": "Dimmer",
        "datapoints": [
            {
                "id": "iddim1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            },
            {
                "id": "iddim1-002",
                "type": "brightness",
                "values": [{"key": "brightness", "value": "30"}],
            },
        ],
    },
    {
        "id": "idblind1",
        "type": "PositionAndAngle",
        "label": "Bedroom Blind",
        "datapoints": [
            {
                "id": "idblind1-001",
                "type": "level",
                # device level 30% closed -> HA position 70 (open)
                "values": [{"key": "level", "value": "30"}],
            },
            {
                "id": "idblind1-002",
                "type": "angle",
                "values": [{"key": "angle", "value": "40"}],
            },
        ],
    },
    {
        "id": "idblind2",
        "type": "Position",
        "label": "Kitchen Shade",
        "datapoints": [
            {
                "id": "idblind2-001",
                "type": "level",
                "values": [{"key": "level", "value": "0"}],
            },
        ],
    },
    {
        "id": "idrtr1",
        "type": "Thermostat",
        "label": "Living Room",
        "datapoints": [
            {
                "id": "idrtr1-000",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "idrtr1-001",
                "type": "temperature_ctrl",
                "values": [
                    {"key": "temperature_ctrl", "value": "21.5"},
                    {"key": "temperature_ctrl_preset", "value": "comfort"},
                ],
            },
            {
                "id": "idrtr1-010",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "20.0"},
                    {"key": "quantity_label", "value": "Temperature "},
                    {"key": "quantity_unit", "value": "°C"},
                ],
            },
        ],
    },
    {
        "id": "idsock1",
        "type": "Socket",
        "label": "Boiler",
        "datapoints": [
            {
                "id": "idsock1-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
            {
                "id": "idsock1-010",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "5"},
                    {"key": "quantity_label", "value": "Power "},
                    {"key": "quantity_unit", "value": "W"},
                ],
            },
            {
                "id": "idsock1-099",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "42"},
                    {"key": "quantity_label", "value": "Status "},
                    {"key": "quantity_unit", "value": "?"},
                ],
            },
        ],
    },
    {
        "id": "idmeas1",
        "type": "Measurement",
        "label": "Hallway Sensor",
        "datapoints": [
            {
                "id": "idmeas1-010",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "120"},
                    {"key": "quantity_label", "value": "Illuminance "},
                    {"key": "quantity_unit", "value": "lux"},
                ],
            },
        ],
    },
    {
        "id": "idrock1",
        "type": "RockerSwitch",
        "label": "Button A",
        "datapoints": [
            {
                "id": "idrock1-00c",
                "type": "up_request",
                "values": [{"key": "up_request", "value": "0"}],
            },
            {
                "id": "idrock1-00d",
                "type": "down_request",
                "values": [{"key": "down_request", "value": "0"}],
            },
            {
                "id": "idrock1-00e",
                "type": "status_led",
                "values": [{"key": "status_led", "value": "0"}],
            },
        ],
    },
]


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


async def _fake_run_websocket(self: JungHomeDataUpdateCoordinator) -> None:
    """Stand in for the real WebSocket: present a fake socket, then park."""
    ws = AsyncMock()
    ws.closed = False
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
