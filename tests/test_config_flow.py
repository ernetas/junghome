"""Tests for the Jung Home config flow."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.config_flow import (
    CannotRegister,
    JungHomeConfigFlow,
    _cover_choices,
    _normalize_host,
)
from custom_components.junghome.const import (
    CONF_IDENTITY_ANCHOR,
    CONF_INVERTED_COVERS,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    entry_scope,
    gateway_device_id,
)
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator

# A single cover so the options flow has something to list. stable_unique_id =
# slug("Awning") + suffix("idawn-001") = "awning_001".
_COVERS = [
    {
        "id": "idawn",
        "type": "Position",
        "label": "Awning",
        "datapoints": [
            {
                "id": "idawn-001",
                "type": "level",
                "values": [{"key": "level", "value": "0"}],
            }
        ],
    }
]


def _flow(hass: HomeAssistant, host: str = "gw") -> JungHomeConfigFlow:
    flow = JungHomeConfigFlow()
    flow.hass = hass
    flow._host = host
    return flow


_REGISTER = "custom_components.junghome.config_flow.JungHomeConfigFlow._async_register"
_FETCH_SERIAL = (
    "custom_components.junghome.config_flow.JungHomeConfigFlow._async_fetch_serial"
)


@pytest.fixture(autouse=True)
def _no_rest_serial(request):
    """Default the REST serial lookup to 'unavailable' (legacy behaviour).

    `async_step_finish` and reconfigure now ask the gateway for its hardware
    serial over REST; an unstubbed call would open a real socket in every flow
    test. Tests exercising serial keying patch `_FETCH_SERIAL` themselves —
    an inner patch wins inside its `with` block — or opt out via the
    `real_serial_fetch` marker to drive the real HTTP path with aioclient_mock.
    """
    if request.node.get_closest_marker("real_serial_fetch") is not None:
        yield
        return
    with patch(_FETCH_SERIAL, AsyncMock(return_value=None)):
        yield


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


async def _choose(hass: HomeAssistant, result: dict, option: str) -> dict:
    """Pick an option from a menu step."""
    assert result["type"] == FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


def _host_suggested(result: dict) -> str | None:
    """Return the suggested_value pre-filled into a form's host field."""
    host_key = next(k for k in result["data_schema"].schema if k == CONF_HOST)
    return (host_key.description or {}).get("suggested_value")


async def test_user_menu_lists_both_methods(hass: HomeAssistant) -> None:
    """The user step is a menu offering both connection methods."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"app_approval", "password"}


async def test_user_flow_defaults_host_to_mdns(hass: HomeAssistant) -> None:
    """The manual host field is pre-filled with the mDNS default."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "app_approval")
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "app_approval"
    assert _host_suggested(result) == "junghome.local"


async def test_user_flow_invalid_host(hass: HomeAssistant) -> None:
    """A blank host is rejected with an error and re-shows the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "app_approval")
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
    result = await _choose(hass, result, "app_approval")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "1.2.3.4"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_host_aborts_when_gateway_discovered_under_hostname(
    hass: HomeAssistant,
) -> None:
    """Typing a discovered gateway's IP must not add it a second time.

    A zeroconf-discovered entry is keyed by its mDNS *hostname*, so the manual
    flow's `async_set_unique_id(host)` claims a different id for the same
    gateway and nothing aborts on unique_id alone.
    """
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "app_approval")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "1.2.3.4"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_password_host_aborts_when_discovered_under_hostname(
    hass: HomeAssistant,
) -> None:
    """The password step is the other `_async_apply_host` caller."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "password")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "1.2.3.4", "password": "secret"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_host_proceeds_when_no_entry_uses_it(
    hass: HomeAssistant,
) -> None:
    """A different host must still be accepted (the guard must not over-abort)."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "app_approval")
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="tok")), fetch, run_ws:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        result = await _advance_progress(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "5.6.7.8"


def _zeroconf_info(
    hostname: str = "junghome-abc.local.",
    host: str = "1.2.3.4",
    properties: dict | None = None,
) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=host,
        ip_addresses=[host],
        port=443,
        hostname=hostname,
        type="_junghome._tcp.local.",
        name="junghome._junghome._tcp.local.",
        properties=properties or {},
    )


_SERIAL_TXT = {
    "serial": "0000000084fb4b1b",
    "mac": "00:22:d1:05:96:02",
    "version": "2.1.3 Release (2840)",
}


async def test_zeroconf_discovery_starts_confirm(hass: HomeAssistant) -> None:
    """A discovered gateway offers a menu of connection methods.

    Without TXT identity records (pre-serial firmware), the serial/version
    placeholders fall back to a locale-neutral dash rather than rendering an
    empty gap in the dialog sentence.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info()
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "zeroconf_confirm"
    assert set(result["menu_options"]) == {"app_approval", "password"}
    assert result["description_placeholders"] == {
        "host": "1.2.3.4",
        "serial": "—",
        "version": "—",
    }


async def test_zeroconf_confirm_shows_serial_and_firmware(
    hass: HomeAssistant,
) -> None:
    """The confirm dialog names the gateway it is about (serial + firmware).

    Both TXT records are advertised by every captured firmware generation
    (2.0.0 and 2.1.3 avahi service definitions), so a multi-gateway household
    can tell which gateway a discovery belongs to before approving it.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.MENU
    assert result["description_placeholders"] == {
        "host": "1.2.3.4",
        "serial": "0000000084fb4b1b",
        "version": "2.1.3 Release (2840)",
    }


async def test_zeroconf_confirm_app_approval_prefills_host(
    hass: HomeAssistant,
) -> None:
    """Discovered + approve-in-app: the host is pre-filled (not hidden) and the
    entry keeps the stable mDNS-hostname unique_id.
    """
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="tok-z")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info()
        )
        result = await _choose(hass, result, "app_approval")
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "app_approval"
        assert _host_suggested(result) == "1.2.3.4"  # discovered address, editable
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "tok-z",
            CONF_IDENTITY_ANCHOR: "junghome-abc.local",
        }
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "junghome-abc.local"


async def test_zeroconf_confirm_password_prefills_host(hass: HomeAssistant) -> None:
    """Discovered + network-key password: the host is pre-filled and setup is
    instant.
    """
    fetch, run_ws = _no_network()
    with patch(_REGISTER_PW, AsyncMock(return_value="pw-z")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info()
        )
        result = await _choose(hass, result, "password")
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "password"
        assert _host_suggested(result) == "1.2.3.4"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4", "password": "secret"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "pw-z",
            CONF_IDENTITY_ANCHOR: "junghome-abc.local",
        }
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "junghome-abc.local"


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


async def test_zeroconf_aborts_when_host_added_manually(hass: HomeAssistant) -> None:
    """A gateway already added manually (under a different unique_id) is skipped.

    Discovery assigns the mDNS hostname as the unique_id, which does not match the
    manual entry keyed on the host, so the unique_id abort does not fire; the
    host-based fallback check must still abort so the gateway is not offered twice.
    """
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info("junghome-abc.local."),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """Menu -> app approval -> host -> approved registration creates the entry."""
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="tok-123")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "tok-123",
            CONF_IDENTITY_ANCHOR: "1.2.3.4",
        }
        await hass.async_block_till_done()


async def test_register_shows_progress_while_pending(hass: HomeAssistant) -> None:
    """A registration still waiting on app approval shows the progress screen.

    Every real user sits in this state for up to 180 s, but a stubbed
    ``AsyncMock`` register resolves before the eager task's first ``done()``
    check, so the ``async_show_progress`` branch was never executed by the
    suite. Block the register on an event to force the pending path.
    """
    gate = asyncio.Event()

    async def _blocked_register(self: JungHomeConfigFlow) -> str:
        await gate.wait()
        return "tok-waited"

    fetch, run_ws = _no_network()
    with patch(_REGISTER, _blocked_register), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        # The task is parked on the gate: the flow must show progress.
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "waiting_for_approval"

        # Approval arrives; the flow advances to the entry.
        gate.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "tok-waited",
            CONF_IDENTITY_ANCHOR: "1.2.3.4",
        }
        await hass.async_block_till_done()


async def test_register_failed_form_allows_retry(hass: HomeAssistant) -> None:
    """The register-failed form's submit re-runs registration.

    Covers the resubmit branch of ``async_step_register_failed`` (a timed-out
    approval retried from the failure screen), which no test drove.
    """
    attempts = 0

    async def _flaky_register(self: JungHomeConfigFlow) -> str:
        # Yield once so the eager task parks, as a real HTTP register always
        # does at its first await. A mock that raises synchronously is done at
        # the flow's first check, and the manager's SHOW_PROGRESS_DONE
        # auto-advance then re-passes the ORIGINAL user_input into
        # register_failed — silently retrying without ever showing the form
        # (impossible with a real aiohttp call, so not worth handling).
        await asyncio.sleep(0)
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CannotRegister("not approved in time")
        return "tok-retry"

    fetch, run_ws = _no_network()
    with patch(_REGISTER, _flaky_register), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "register_failed"

        # Submitting the failure form retries; the second attempt succeeds.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "tok-retry",
            CONF_IDENTITY_ANCHOR: "1.2.3.4",
        }
        assert attempts == 2
        await hass.async_block_till_done()


async def test_reauth_flow(hass: HomeAssistant) -> None:
    """Reauth shows a confirm form first, then re-registers and stores the token.

    The confirm form matters: a reauth flow is created programmatically before
    the user has seen the notification, and registration opens the gateway's
    one 180 s approval window the moment it runs — starting it unattended
    burned that window before the user could approve.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    fetch, run_ws = _no_network()
    with patch(_REGISTER, AsyncMock(return_value="new-tok")) as register, fetch, run_ws:
        result = await entry.start_reauth_flow(hass)
        # No registration yet: the flow waits for the user on a confirm form.
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        register.assert_not_called()

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-tok"


async def test_reauth_shows_progress_while_pending(hass: HomeAssistant) -> None:
    """A reauth registration still waiting on app approval shows progress."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    gate = asyncio.Event()

    async def _blocked_register(self: JungHomeConfigFlow) -> str:
        await gate.wait()
        return "tok-reauth"

    fetch, run_ws = _no_network()
    with patch(_REGISTER, _blocked_register), fetch, run_ws:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "waiting_for_approval"

        gate.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "tok-reauth"


async def test_reauth_failed_form_allows_retry(hass: HomeAssistant) -> None:
    """The reauth-failed form's submit retries immediately, with no re-confirm."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    attempts = 0

    async def _flaky_register(self: JungHomeConfigFlow) -> str:
        await asyncio.sleep(0)  # park once, as a real HTTP register does
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CannotRegister("not approved in time")
        return "tok-reauth-retry"

    fetch, run_ws = _no_network()
    with patch(_REGISTER, _flaky_register), fetch, run_ws:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_failed"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "tok-reauth-retry"
    assert attempts == 2


async def test_reconfigure_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """Reconfigure updates the gateway host in place."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
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
    hass: HomeAssistant, aioclient_mock
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
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
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


_REGISTER_PW = (
    "custom_components.junghome.config_flow."
    "JungHomeConfigFlow._async_register_by_password"
)


async def test_user_flow_password_success(hass: HomeAssistant) -> None:
    """Menu -> password -> host + password registers instantly."""
    fetch, run_ws = _no_network()
    with patch(_REGISTER_PW, AsyncMock(return_value="pw-tok")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4", "password": "secret"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "pw-tok",
            CONF_IDENTITY_ANCHOR: "1.2.3.4",
        }
        await hass.async_block_till_done()


async def test_user_flow_password_rejected(hass: HomeAssistant) -> None:
    """A wrong password re-shows the form with the invalid_auth error."""

    async def _reject(self, password):
        self._error = "invalid_auth"
        raise CannotRegister("Wrong password")

    with patch(_REGISTER_PW, _reject):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4", "password": "bad"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    # The host the user already typed is kept so they don't retype it on retry;
    # the password is not suggested back (it's a credential, and it was wrong).
    assert _host_suggested(result) == "1.2.3.4"


async def test_async_register_by_password_returns_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post(
        "https://gw/api/junghome/register/by-password", json={"token": "abc"}
    )
    assert await _flow(hass)._async_register_by_password("pw") == "abc"


async def test_async_register_by_password_wrong_password(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post("https://gw/api/junghome/register/by-password", status=401)
    flow = _flow(hass)
    with pytest.raises(CannotRegister):
        await flow._async_register_by_password("pw")
    assert flow._error == "invalid_auth"


async def test_async_register_by_password_http_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A non-200/401 status maps to register_failed."""
    aioclient_mock.post("https://gw/api/junghome/register/by-password", status=500)
    flow = _flow(hass)
    with pytest.raises(CannotRegister):
        await flow._async_register_by_password("pw")
    assert flow._error == "register_failed"


async def test_async_register_by_password_connection_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A transport error maps to cannot_connect."""
    aioclient_mock.post(
        "https://gw/api/junghome/register/by-password", exc=aiohttp.ClientError()
    )
    flow = _flow(hass)
    with pytest.raises(CannotRegister):
        await flow._async_register_by_password("pw")
    assert flow._error == "cannot_connect"


async def test_async_register_by_password_missing_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A 200 with no token is treated as a failed registration."""
    aioclient_mock.post("https://gw/api/junghome/register/by-password", json={})
    with pytest.raises(CannotRegister):
        await _flow(hass)._async_register_by_password("pw")


async def test_password_flow_invalid_host(hass: HomeAssistant) -> None:
    """An empty/invalid host in the password step re-shows the form with an error.

    The host is validated before any network call, so no gateway is contacted.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _choose(hass, result, "password")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "   ", "password": "secret"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "password"
    assert result["errors"] == {"base": "invalid_host"}


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


async def test_reconfigure_rejects_unreachable_host(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A typo'd address must be caught on the form, not committed.

    Before connect-then-commit the new host was stored unverified and the
    mistake only surfaced later as a connect/reauth failure.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1.2.3.4", data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(
        "https://5.6.7.9/api/junghome/functions", exc=aiohttp.ClientError
    )

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "5.6.7.9"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    # The typo must not have been persisted.
    assert entry.data[CONF_HOST] == "1.2.3.4"


async def test_reconfigure_accepts_host_that_rejects_the_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A 401 still proves a gateway is reachable at the new address.

    The probe asserts reachability only: a rejected token means the address now
    points at a different gateway (or the token was revoked), which the reauth
    flow handles. Failing the form for it would strand the user on a screen that
    cannot fix it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1.2.3.4", data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", status=401)

    fetch, run_ws = _no_network()
    with fetch, run_ws:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"


async def test_reconfigure_rejects_host_that_times_out(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A hanging address fails the form rather than blocking the commit."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1.2.3.4", data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.9/api/junghome/functions", exc=TimeoutError)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "5.6.7.9"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_HOST] == "1.2.3.4"


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


def test_cover_choices_skips_malformed_and_falls_back_to_uid() -> None:
    """_cover_choices skips a Position without a level dp and labels blanks by uid."""
    data = [
        # No level datapoint -> skipped (mirrors cover.py discovery).
        {"id": "x", "type": "Position", "label": "Lbl", "datapoints": []},
        # Blank label -> the stable unique_id is used as the display label.
        {
            "id": "y",
            "type": "Position",
            "label": "",
            "datapoints": [{"id": "y-001", "type": "level", "values": []}],
        },
    ]
    assert _cover_choices(SimpleNamespace(data=data)) == {"y_001": "y_001"}


async def test_options_flow_lists_and_saves_inverted_covers(
    hass: HomeAssistant,
) -> None:
    """The options flow lists discovered covers and stores the chosen ids."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="gw", data={CONF_HOST: "gw", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    _, ws = _no_network()
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=_COVERS),
        ),
        ws,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_INVERTED_COVERS: ["awning_001"]}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_INVERTED_COVERS] == ["awning_001"]
    # The interval field was left untouched, so its default was stored.
    assert entry.options[CONF_POLL_INTERVAL] == DEFAULT_POLL_INTERVAL_SECONDS


async def test_options_flow_saves_poll_interval_and_coordinator_applies_it(
    hass: HomeAssistant,
) -> None:
    """A saved poll interval reaches the (rebuilt) coordinator's update_interval.

    Saving options reloads the entry via the update listener, and the new
    coordinator reads the stored interval at construction.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="gw", data={CONF_HOST: "gw", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    _, ws = _no_network()
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=_COVERS),
        ),
        ws,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.update_interval == timedelta(seconds=60)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 300}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_POLL_INTERVAL] == 300
    assert entry.runtime_data.update_interval == timedelta(seconds=300)


async def test_options_flow_without_covers_still_offers_the_interval(
    hass: HomeAssistant,
) -> None:
    """With no covers the flow no longer aborts: the interval stays reachable.

    The step used to abort with "no covers", which would now lock cover-less
    installs out of the poll interval; instead the form shows only the
    interval field, and saving must not invent (or clear) cover flags.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="gw", data={CONF_HOST: "gw", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    fetch, ws = _no_network()  # fetch returns [] -> no covers
    with fetch, ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        # Only the interval is in the schema; there is no covers field to show.
        assert list(result["data_schema"].schema) == [CONF_POLL_INTERVAL]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 120}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_POLL_INTERVAL] == 120
    assert entry.options[CONF_INVERTED_COVERS] == []


async def test_options_flow_keeps_offline_flagged_cover(hass: HomeAssistant) -> None:
    """A flagged cover the gateway isn't reporting is kept if its entity exists."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw",
        data={CONF_HOST: "gw", CONF_TOKEN: "t"},
        options={CONF_INVERTED_COVERS: ["ghost_001"]},
    )
    entry.add_to_hass(hass)
    # The cover's entity still exists in the registry (it's merely offline, so the
    # gateway isn't listing it right now), so the flag must be preserved.
    er.async_get(hass).async_get_or_create(
        "cover", DOMAIN, "ghost_001", config_entry=entry
    )
    fetch, ws = _no_network()  # no covers reported right now
    with fetch, ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Not aborted: the already-flagged "ghost" cover stays selectable.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_INVERTED_COVERS: ["ghost_001"]}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_INVERTED_COVERS] == ["ghost_001"]


async def test_options_flow_keeps_live_flagged_cover_selected(
    hass: HomeAssistant,
) -> None:
    """A flagged cover the gateway IS reporting stays selected by default."""
    cover_device = {
        "id": "idblind9",
        "type": "Position",
        "label": "Patio Awning",
        "datapoints": [
            {
                "id": "idblind9-001",
                "type": "level",
                "values": [{"key": "level", "value": "0"}],
            }
        ],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw",
        data={CONF_HOST: "gw", CONF_TOKEN: "t"},
        options={CONF_INVERTED_COVERS: ["patio_awning_001"]},
    )
    entry.add_to_hass(hass)
    _, ws = _no_network()
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[cover_device]),
        ),
        ws,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        # Submitting the form unchanged keeps the pre-selected live flag.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_INVERTED_COVERS] == ["patio_awning_001"]


async def test_options_flow_drops_orphaned_flagged_cover(hass: HomeAssistant) -> None:
    """A flag whose cover was removed/relabelled (no entity left) is dropped.

    The device's label-derived unique_id changed, so the old entity was pruned
    and no cover with this uid is registered any more. It must not resurface as
    a permanent raw-slug row in the options list.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gw",
        data={CONF_HOST: "gw", CONF_TOKEN: "t"},
        options={CONF_INVERTED_COVERS: ["orphan_001"]},
    )
    entry.add_to_hass(hass)
    fetch, ws = _no_network()  # no covers reported, and none registered
    with fetch, ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Orphan dropped -> no covers field at all (not a ghost row), and a
        # save through the covers-less form clears the orphaned flag rather
        # than carrying it forward blindly.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert list(result["data_schema"].schema) == [CONF_POLL_INTERVAL]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 60}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_INVERTED_COVERS] == []


async def test_reconfigure_reloads_an_entry_stuck_in_setup_retry(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A gateway that is failing to set up picks the new host up immediately.

    The host-change update listener is registered on the last line of a
    *successful* setup, so it does not exist in SETUP_RETRY — which is the usual
    state to reconfigure from. Without an explicit reload the new host sat unused
    until Home Assistant's retry timer next fired, up to 10 minutes later.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    # Setup fails, leaving the entry in SETUP_RETRY (and with no update listener).
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_from_api",
        AsyncMock(side_effect=aiohttp.ClientError("unreachable")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not entry.update_listeners

    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
    fetch, run_ws = _no_network()
    with (
        fetch,
        run_ws,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_zeroconf_ip_change_updates_the_stored_host(
    hass: HomeAssistant,
) -> None:
    """Re-discovery at a new address updates the entry (discovery-update-info).

    Covers the `updates={CONF_HOST: ...}` path on a *loaded* entry, which the
    other zeroconf abort tests never reach.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    )
    entry.add_to_hass(hass)

    info = ZeroconfServiceInfo(
        ip_address="9.9.9.9",
        ip_addresses=["9.9.9.9"],
        port=443,
        hostname="junghome-abc.local.",
        type="_junghome._tcp.local.",
        name="junghome._junghome._tcp.local.",
        properties={},
    )
    fetch, run_ws = _no_network()
    with fetch, run_ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=info
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "9.9.9.9"


# ---------------------------------------------------------------------------
# Serial-based entry identity
# ---------------------------------------------------------------------------


async def test_zeroconf_with_serial_keys_entry_on_serial(
    hass: HomeAssistant,
) -> None:
    """A discovery carrying the TXT serial keys the new entry on it.

    The serial is the only identifier that survives IP changes and
    re-provisioning; the identity anchor is frozen at creation.
    """
    fetch, run_ws = _no_network()
    with patch(_REGISTER_PW, AsyncMock(return_value="tok-s")), fetch, run_ws:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "zeroconf"},
            data=_zeroconf_info(properties=_SERIAL_TXT),
        )
        result = await _choose(hass, result, "password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4", "password": "pw"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_SERIAL] == _SERIAL_TXT["serial"]
    assert entry.data[CONF_IDENTITY_ANCHOR] == _SERIAL_TXT["serial"]


async def test_zeroconf_serial_rediscovery_updates_host(
    hass: HomeAssistant,
) -> None:
    """An IP change reaches a serial-keyed entry no matter how it was added."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_SERIAL_TXT["serial"],
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: _SERIAL_TXT["serial"],
            CONF_IDENTITY_ANCHOR: _SERIAL_TXT["serial"],
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "5.6.7.8"


async def test_zeroconf_adopts_legacy_hostname_keyed_entry(
    hass: HomeAssistant,
) -> None:
    """A hostname-keyed entry is migrated onto the serial, identity intact.

    The critical assertion is the last pair: the hub-device identifier and the
    scene unique_id scope must be EXACTLY what they were before the migration,
    or the re-keying would orphan the hub device and every scene entity.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    hub_before = gateway_device_id(entry)
    scope_before = entry_scope(entry)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_SERIAL] == _SERIAL_TXT["serial"]
    assert entry.data[CONF_HOST] == "5.6.7.8"
    assert entry.data[CONF_IDENTITY_ANCHOR] == "junghome-abc.local"
    assert gateway_device_id(entry) == hub_before
    assert entry_scope(entry) == scope_before


async def test_zeroconf_adopts_legacy_manual_host_keyed_entry(
    hass: HomeAssistant,
) -> None:
    """A manually added, host-keyed entry is adopted onto the serial too."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_IDENTITY_ANCHOR] == "1.2.3.4"


async def test_zeroconf_does_not_hijack_serial_keyed_entry_with_stale_host(
    hass: HomeAssistant,
) -> None:
    """A DIFFERENT gateway's discovery must not adopt a serial-keyed entry
    whose stale recorded host happens to equal the discovered address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-other",
        data={
            CONF_HOST: "1.2.3.4",  # stale: now used by another gateway
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-other",
            CONF_IDENTITY_ANCHOR: "ser-other",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(properties=_SERIAL_TXT),  # different serial
    )
    # Not adopted: a fresh discovery flow is offered instead.
    assert result["type"] == FlowResultType.MENU
    assert entry.unique_id == "ser-other"
    assert entry.data[CONF_HOST] == "1.2.3.4"
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_manual_flow_upgrades_to_serial_when_rest_provides_it(
    hass: HomeAssistant,
) -> None:
    """A manual entry learns its serial over REST once a token exists."""
    fetch, run_ws = _no_network()
    with (
        patch(_REGISTER, AsyncMock(return_value="tok-m")),
        patch(_FETCH_SERIAL, AsyncMock(return_value="ser-777")),
        fetch,
        run_ws,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "ser-777"
    assert entry.data[CONF_SERIAL] == "ser-777"
    assert entry.data[CONF_IDENTITY_ANCHOR] == "ser-777"


async def test_manual_flow_detects_existing_gateway_by_serial(
    hass: HomeAssistant,
) -> None:
    """Typing the (new) IP of an already-configured gateway updates that
    entry's host and aborts, instead of creating a duplicate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-777",
        data={
            CONF_HOST: "9.9.9.9",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-777",
            CONF_IDENTITY_ANCHOR: "ser-777",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(_REGISTER, AsyncMock(return_value="tok")),
        patch(_FETCH_SERIAL, AsyncMock(return_value="ser-777")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
        result = await _advance_progress(hass, result)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"


async def test_reconfigure_rejects_a_different_gateway(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A live responder with the WRONG serial fails the form, not later."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-orig",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-orig",
            CONF_IDENTITY_ANCHOR: "ser-orig",
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-OTHER")):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "different_gateway"}
    assert entry.data[CONF_HOST] == "1.2.3.4"  # unchanged
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reconfigure_accepts_matching_serial(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The same gateway at a new address reconfigures cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-orig",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-orig",
            CONF_IDENTITY_ANCHOR: "ser-orig",
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
    fetch, run_ws = _no_network()
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-orig")), fetch, run_ws:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_migrates_a_legacy_entry_to_serial(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Reconfiguring a legacy entry records the serial it just learned,
    freezing the identity anchor so hub/scene ids stay put."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    hub_before = gateway_device_id(entry)
    scope_before = entry_scope(entry)
    aioclient_mock.get("https://5.6.7.8/api/junghome/functions", json=[])
    fetch, run_ws = _no_network()
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-777")), fetch, run_ws:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "ser-777"
    assert entry.data[CONF_SERIAL] == "ser-777"
    assert entry.data[CONF_IDENTITY_ANCHOR] == "1.2.3.4"
    assert gateway_device_id(entry) == hub_before
    assert entry_scope(entry) == scope_before
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_to_an_already_configured_gateway_aborts(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Reconfiguring a legacy entry onto a gateway another entry already owns
    (by serial) aborts instead of creating a unique_id collision."""
    owner = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-777",
        data={
            CONF_HOST: "9.9.9.9",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-777",
            CONF_IDENTITY_ANCHOR: "ser-777",
        },
    )
    owner.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://9.9.9.9/api/junghome/functions", json=[])
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-777")):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "9.9.9.9"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "1.2.3.4"  # unchanged


@pytest.mark.real_serial_fetch
async def test_fetch_serial_over_rest(hass: HomeAssistant, aioclient_mock) -> None:
    """The REST helper parses the raw-string body and tolerates failures."""
    url = "https://gw/api/junghome/config/parameter/system_serial"
    aioclient_mock.get(url, json="0000000084fb4b1b")
    assert await _flow(hass)._async_fetch_serial("gw", "tok") == "0000000084fb4b1b"

    # Older firmware: parameter unknown -> 404 -> None.
    aioclient_mock.clear_requests()
    aioclient_mock.get(url, status=404)
    assert await _flow(hass)._async_fetch_serial("gw", "tok") is None

    # The middleware populates the value asynchronously after boot; an empty
    # string must read as "not known", not become a unique_id.
    aioclient_mock.clear_requests()
    aioclient_mock.get(url, json="")
    assert await _flow(hass)._async_fetch_serial("gw", "tok") is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, exc=aiohttp.ClientError())
    assert await _flow(hass)._async_fetch_serial("gw", "tok") is None
