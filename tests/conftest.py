"""Shared fixtures for the Jung Home test suite."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
            AsyncMock(return_value=DEVICES),
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
