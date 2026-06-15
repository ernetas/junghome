"""Tests for the Jung Home config flow."""

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.config_flow import (
    CannotRegister,
    JungHomeConfigFlow,
    _normalize_host,
)
from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


def _flow(hass: HomeAssistant, host: str = "gw") -> JungHomeConfigFlow:
    flow = JungHomeConfigFlow()
    flow.hass = hass
    flow._host = host
    return flow


_REGISTER = "custom_components.junghome.config_flow.JungHomeConfigFlow._async_register"


async def _fake_run_websocket(self: JungHomeDataUpdateCoordinator) -> None:
    self.websocket = AsyncMock()
    await asyncio.Event().wait()


_PROGRESS = (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE)


async def _advance_progress(hass: HomeAssistant, result: dict) -> dict:
    """Drive a flow through its waiting-for-approval progress steps."""
    for _ in range(10):  # cap iterations so a stuck flow fails instead of hanging
        if result["type"] not in _PROGRESS:
            break
        if result["type"] == FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


def _no_network():
    """Patch out the gateway REST + WebSocket so a setup/reload needs no network."""
    return (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    )


def test_normalize_host_strips_scheme_whitespace_and_slash():
    cases = {
        "192.168.1.10": "192.168.1.10",
        "  192.168.1.10  ": "192.168.1.10",
        "https://junghome.local": "junghome.local",
        "http://junghome.local/": "junghome.local",
        "HTTPS://Gateway/": "gateway",  # host is lower-cased (case-insensitive)
        "gateway/": "gateway",
    }
    for raw, expected in cases.items():
        assert _normalize_host(raw) == expected


async def test_user_flow_invalid_host(hass: HomeAssistant) -> None:
    """A blank host is rejected with an error and re-shows the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "   "}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}


async def test_user_flow_already_configured(hass: HomeAssistant) -> None:
    """A gateway already configured aborts the flow."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "1.2.3.4"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_discovery_starts_confirm(hass: HomeAssistant) -> None:
    """A discovered gateway pre-fills the host and asks to confirm."""
    info = ZeroconfServiceInfo(
        ip_address="1.2.3.4",
        ip_addresses=["1.2.3.4"],
        port=443,
        hostname="junghome.local.",
        type="_junghome._tcp.local.",
        name="junghome._junghome._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"


async def test_zeroconf_aborts_when_already_configured(hass: HomeAssistant) -> None:
    """Re-discovering an already-configured gateway aborts."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)
    info = ZeroconfServiceInfo(
        ip_address="1.2.3.4",
        ip_addresses=["1.2.3.4"],
        port=443,
        hostname="junghome.local.",
        type="_junghome._tcp.local.",
        name="junghome._junghome._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """Host + approved registration creates and sets up the entry."""
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="tok-123")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok-123"}
        await hass.async_block_till_done()


async def test_reauth_flow(hass: HomeAssistant) -> None:
    """Reauth re-registers and updates the stored token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="new-tok")), fetch, run_ws:
        result = await entry.start_reauth_flow(hass)
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-tok"


async def test_reconfigure_flow(hass: HomeAssistant) -> None:
    """Reconfigure updates the gateway host in place."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    fetch, run_ws = _no_network()
    with fetch, run_ws:
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"


async def test_reconfigure_reloads_once_and_keeps_unique_id(
    hass: HomeAssistant,
) -> None:
    """A reconfigure host change reloads exactly once and preserves unique_id.

    The host-change update listener does the single reload; the flow must not
    also schedule one (the old double-reload), and it must keep the entry's
    existing unique_id (e.g. a zeroconf hostname) rather than overwrite it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    fetch, run_ws = _no_network()
    with fetch, run_ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
            result = await entry.start_reconfigure_flow(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: "5.6.7.8"}
            )
            await hass.async_block_till_done()
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_HOST] == "5.6.7.8"
        reload.assert_called_once_with(entry.entry_id)
        assert entry.unique_id == "junghome.local"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_async_register_returns_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post("https://gw/api/junghome/register", json={"token": "abc"})
    assert await _flow(hass)._async_register() == "abc"


async def test_async_register_http_error(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post("https://gw/api/junghome/register", status=500)
    flow = _flow(hass)
    with pytest.raises(CannotRegister):
        await flow._async_register()
    assert flow._error == "register_failed"


async def test_async_register_missing_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post("https://gw/api/junghome/register", json={})
    with pytest.raises(CannotRegister):
        await _flow(hass)._async_register()


async def test_async_register_connection_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post("https://gw/api/junghome/register", exc=aiohttp.ClientError())
    flow = _flow(hass)
    with pytest.raises(CannotRegister):
        await flow._async_register()
    assert flow._error == "cannot_connect"


async def test_reconfigure_invalid_host(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1.2.3.4", data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "   "}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}


async def test_reconfigure_host_collision(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id="9.9.9.9", data={CONF_HOST: "9.9.9.9", CONF_TOKEN: "x"}
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1.2.3.4", data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "9.9.9.9"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_register_step_captures_failure(hass: HomeAssistant) -> None:
    """async_step_register routes a failed register task to the failure form."""
    flow = _flow(hass)

    async def boom() -> str:
        flow._error = "register_failed"  # what _async_register sets on failure
        raise CannotRegister("x")  # the exception the flow catches

    flow._register_task = hass.async_create_task(boom())
    await hass.async_block_till_done()
    result = await flow.async_step_register()
    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "register_failed"

    result = await flow.async_step_register_failed()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "register_failed"
    assert result["errors"] == {"base": "register_failed"}


async def test_register_failed_step_shows_form(hass: HomeAssistant) -> None:
    result = await _flow(hass).async_step_register_failed()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "register_failed"


async def test_reauth_confirm_captures_failure(hass: HomeAssistant) -> None:
    """async_step_reauth_confirm routes a failed task to the reauth failure form."""
    flow = _flow(hass)

    async def boom() -> str:
        flow._error = "register_failed"  # what _async_register sets on failure
        raise CannotRegister("x")  # the exception the flow catches

    flow._register_task = hass.async_create_task(boom())
    await hass.async_block_till_done()
    result = await flow.async_step_reauth_confirm()
    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "reauth_failed"

    result = await flow.async_step_reauth_failed()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_failed"
    assert result["errors"] == {"base": "register_failed"}


async def test_reauth_failed_step_shows_form(hass: HomeAssistant) -> None:
    result = await _flow(hass).async_step_reauth_failed()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_failed"
