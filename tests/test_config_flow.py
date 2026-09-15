"""Tests for the Jung Home config flow."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

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
    CONF_SUPPRESS_DUPLICATE_PRESSES,
    CONF_TLS_FINGERPRINT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    entry_scope,
    gateway_device_id,
)
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.tls import fingerprint_ssl
from tests.conftest import FAKE_FINGERPRINT, PRISTINE_DEVICES
from tests.conftest import _fake_run_websocket as _fake_live_websocket

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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
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

    The reload is spied on, not stubbed out: a stub leaves the entry LOADED,
    which hides the real interleaving — the listener runs eagerly inside
    ``async_update_entry`` and has the entry in UNLOAD_IN_PROGRESS by the time
    the flow's post-update ``is not LOADED`` check ran, so the flow scheduled a
    second reload in production while this test (stubbed) counted one.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
    fetch, run_ws = _no_network()
    with fetch, run_ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload:
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
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
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
    aioclient_mock.get("https://5.6.7.9/api/junghome/version/", exc=aiohttp.ClientError)

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
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", status=401)

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
    aioclient_mock.get("https://5.6.7.9/api/junghome/version/", exc=TimeoutError)

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
    # The other fields were left untouched, so their defaults were stored.
    assert entry.options[CONF_POLL_INTERVAL] == DEFAULT_POLL_INTERVAL_SECONDS
    assert entry.options[CONF_SUPPRESS_DUPLICATE_PRESSES] is True


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
        # No covers field to show; the always-present fields remain.
        assert list(result["data_schema"].schema) == [
            CONF_POLL_INTERVAL,
            CONF_SUPPRESS_DUPLICATE_PRESSES,
        ]
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
        assert list(result["data_schema"].schema) == [
            CONF_POLL_INTERVAL,
            CONF_SUPPRESS_DUPLICATE_PRESSES,
        ]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 60}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_INVERTED_COVERS] == []


async def test_options_flow_turns_duplicate_suppression_off_and_reloads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Switching duplicate suppression off reloads the entry and takes effect.

    The event platform reads the option once at setup (like the cover
    platform reads its flags), so the update listener's reload is what
    applies it: after the save, one tap reported twice fires two clicks —
    the behaviour a user on older device firmware asks for.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="gw", data={CONF_HOST: "gw", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    clicks: list[str] = []

    @callback
    def _record(event) -> None:
        if event.data["subtype"] == "click":
            clicks.append(event.data["type"])

    hass.bus.async_listen(EVENT_BUTTON_ACTION, _record)
    # The conftest fake, not this file's: it reports the socket as connected,
    # which the event entities need to be available at all.
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_live_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = entry.runtime_data
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        field = next(
            key
            for key in result["data_schema"].schema
            if key == CONF_SUPPRESS_DUPLICATE_PRESSES
        )
        assert field.default() is True  # on unless the user turned it off
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SUPPRESS_DUPLICATE_PRESSES: False}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.options[CONF_SUPPRESS_DUPLICATE_PRESSES] is False
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not before  # the listener reloaded it
        # Let the rebuilt coordinator's (fake) WebSocket task start, so the
        # event entities are available (they need the socket).
        await asyncio.sleep(0)
        await hass.async_block_till_done()
        assert hass.states.get("event.button_a_up").state != "unavailable"

        # One tap as current firmware reports it: two press/release pairs.
        for value, gap in (("1", 0), ("0", 0.4), ("1", 0.5), ("0", 0.4)):
            freezer.tick(timedelta(seconds=gap))
            async_fire_time_changed(hass)
            entry.runtime_data._handle_websocket_message(
                {
                    "type": "datapoint",
                    "data": {
                        "id": "idrock1-00c",
                        "values": [{"key": "up_request", "value": value}],
                    },
                }
            )
            await hass.async_block_till_done()
        assert clicks == ["up", "up"]

        # Re-opening the form defaults to the stored value; an untouched
        # submit keeps it.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        field = next(
            key
            for key in result["data_schema"].schema
            if key == CONF_SUPPRESS_DUPLICATE_PRESSES
        )
        assert field.default() is False
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options[CONF_SUPPRESS_DUPLICATE_PRESSES] is False


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

    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
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


_LEARN = (
    "custom_components.junghome.config_flow.JungHomeConfigFlow._async_learn_fingerprint"
)
_OTHER_FINGERPRINT = "cd" * 32


async def _setup_failing(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set ``entry`` up against a gateway that cannot be reached (SETUP_RETRY)."""
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_from_api",
        AsyncMock(side_effect=aiohttp.ClientError("unreachable")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_zeroconf_does_not_move_a_healthy_entry(hass: HomeAssistant) -> None:
    """An mDNS packet must not redirect a LOADED, healthy entry (S1).

    The packet is unauthenticated and the serial/hostname it names are public
    (the gateway broadcasts them), so a forged announcement used to move the
    entry's host — and its next poll, token and all — to any address on the
    LAN, no user interaction involved. A healthy entry is talking to its
    gateway at the stored address right now: nothing to fix, nothing to
    trust. No reload, no request to the announced address, not even a
    certificate read.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    )
    entry.add_to_hass(hass)
    fetch, run_ws = _no_network()
    with fetch, run_ws:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.last_update_success

        with (
            patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn,
            patch.object(
                hass.config_entries,
                "async_reload",
                wraps=hass.config_entries.async_reload,
            ) as reload,
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "zeroconf"},
                data=_zeroconf_info(host="9.9.9.9"),
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert entry.data[CONF_HOST] == "1.2.3.4"
        learn.assert_not_called()
        reload.assert_not_called()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_zeroconf_moves_a_failing_entry_whose_certificate_matches(
    hass: HomeAssistant,
) -> None:
    """The legitimate case: the gateway got a new lease while HA could not reach it.

    The entry is in SETUP_RETRY at the old address, and the responder at the
    announced one presents the pinned certificate — which only the gateway
    can — so the host is adopted and the entry reloaded straight away rather
    than on its next retry timer.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "x",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    await _setup_failing(hass, entry)

    fetch, run_ws = _no_network()
    with (
        fetch,
        run_ws,
        patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info(host="9.9.9.9")
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    learn.assert_awaited_once_with("9.9.9.9")
    assert entry.data[CONF_HOST] == "9.9.9.9"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_zeroconf_refuses_a_failing_entry_when_the_certificate_differs(
    hass: HomeAssistant,
) -> None:
    """A failing entry still does not follow a packet to a stranger's address.

    The responder at the announced address presents a certificate other than
    the pinned one, so the announcement is refused: host unchanged, no
    reload, and (the point) no request that could carry the token.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "x",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    await _setup_failing(hass, entry)

    with (
        patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info(host="9.9.9.9")
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"
    assert entry.data[CONF_TLS_FINGERPRINT] == _OTHER_FINGERPRINT
    schedule_reload.assert_not_called()


async def test_zeroconf_ignores_an_announced_address_it_cannot_reach(
    hass: HomeAssistant,
) -> None:
    """A certificate read that fails leaves a failing entry untouched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "x",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    await _setup_failing(hass, entry)
    with patch(_LEARN, AsyncMock(side_effect=aiohttp.ClientError("refused"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info(host="9.9.9.9")
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"


async def test_zeroconf_moves_a_loaded_entry_whose_poll_is_failing(
    hass: HomeAssistant,
) -> None:
    """A loaded entry that has lost its gateway counts as failing too.

    The gateway moved while the entry was loaded: the coordinator's poll has
    failed (``last_update_success`` False). The certificate at the announced
    address matches, so the host is adopted and the entry's own update
    listener reloads it — exactly once.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "x",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=[])
    with (
        patch.object(JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", fetch),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        fetch.side_effect = aiohttp.ClientError("gone")
        await coordinator.async_refresh()
        assert not coordinator.last_update_success

        fetch.side_effect = None
        with (
            patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)),
            patch.object(
                hass.config_entries,
                "async_reload",
                wraps=hass.config_entries.async_reload,
            ) as reload,
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "zeroconf"},
                data=_zeroconf_info(host="9.9.9.9"),
            )
            await hass.async_block_till_done()
        assert result["reason"] == "already_configured"
        assert entry.data[CONF_HOST] == "9.9.9.9"
        reload.assert_called_once_with(entry.entry_id)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_zeroconf_unpinned_failing_entry_trusts_its_first_contact(
    hass: HomeAssistant,
) -> None:
    """An entry from before pinning that cannot reach its gateway follows the packet.

    It has no certificate to verify against, so this is the one case that
    still trusts the announcement — the same trust-on-first-use its next
    successful connect extends anyway (documented residual for legacy
    entries; a healthy unpinned entry is never moved, see above).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "x"},
    )
    entry.add_to_hass(hass)
    await _setup_failing(hass, entry)
    with (
        patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn,
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info(host="9.9.9.9")
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "9.9.9.9"
    learn.assert_not_called()  # nothing to compare against
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_zeroconf_same_address_reloads_a_retrying_entry(
    hass: HomeAssistant,
) -> None:
    """A gateway announcing itself at the stored address is retried at once.

    Parity with Home Assistant's own discovery handling for entries in
    SETUP_RETRY: the gateway just came back, so do not wait for the timer.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "x",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    await _setup_failing(hass, entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=_zeroconf_info(host="1.2.3.4")
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"
    schedule_reload.assert_called_once_with(entry.entry_id)


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
    """An IP change reaches a serial-keyed entry no matter how it was added.

    ...once the entry is actually failing at its stored address and the
    announced one presents its certificate: the serial in the packet is
    public and proves nothing on its own.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=_SERIAL_TXT["serial"],
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: _SERIAL_TXT["serial"],
            CONF_IDENTITY_ANCHOR: _SERIAL_TXT["serial"],
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    # Never set up (NOT_LOADED) is not "failing": nothing is adopted.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"

    await _setup_failing(hass, entry)
    with patch.object(hass.config_entries, "async_schedule_reload"):
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

    The announcement names the address the entry already talks to, which is
    what lets an entry with no pin yet trust its serial; the host stays.
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
        data=_zeroconf_info(host="1.2.3.4", properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_SERIAL] == _SERIAL_TXT["serial"]
    assert entry.data[CONF_HOST] == "1.2.3.4"
    assert entry.data[CONF_IDENTITY_ANCHOR] == "junghome-abc.local"
    assert gateway_device_id(entry) == hub_before
    assert entry_scope(entry) == scope_before


async def test_zeroconf_legacy_entry_is_not_rekeyed_from_an_unverifiable_packet(
    hass: HomeAssistant,
) -> None:
    """A healthy, unpinned legacy entry ignores a packet from another address.

    Migrating its identity onto a serial it cannot verify would let one forged
    packet re-key the entry (and lock reconfigure/rediscovery to the wrong
    serial). It migrates on the next same-address announcement — every HA
    restart produces one — or once pinned, on a certificate-verified one.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "junghome-abc.local"
    assert CONF_SERIAL not in entry.data
    assert entry.data[CONF_HOST] == "1.2.3.4"


async def test_zeroconf_pinned_legacy_entry_is_rekeyed_from_a_verified_packet(
    hass: HomeAssistant,
) -> None:
    """A pinned, hostname-keyed entry migrates when the announced address proves itself.

    Healthy, so the host stays; the responder at the new address presents the
    pinned certificate, so its serial is trusted and the identity migrates.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    with patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "zeroconf"},
            data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
        )
    assert result["reason"] == "already_configured"
    learn.assert_awaited_once_with("5.6.7.8")
    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_IDENTITY_ANCHOR] == "junghome-abc.local"
    assert entry.data[CONF_HOST] == "1.2.3.4"  # healthy (not failing): host kept

    # ...and with a certificate that does NOT match, nothing at all changes.
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-xyz.local",
        data={
            CONF_HOST: "1.2.3.5",
            CONF_TOKEN: "t",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
        },
    )
    other.add_to_hass(hass)
    with patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "zeroconf"},
            data=_zeroconf_info(
                hostname="junghome-xyz.local.",
                host="5.6.7.9",
                properties={**_SERIAL_TXT, "serial": "ser-xyz"},
            ),
        )
    assert result["reason"] == "already_configured"
    assert other.unique_id == "junghome-xyz.local"
    assert CONF_SERIAL not in other.data


async def test_zeroconf_failing_legacy_entry_adopts_serial_and_host(
    hass: HomeAssistant,
) -> None:
    """A legacy entry that cannot reach its gateway adopts both from the packet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="junghome-abc.local",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    hub_before = gateway_device_id(entry)
    await _setup_failing(hass, entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "zeroconf"},
            data=_zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT),
        )
    assert result["reason"] == "already_configured"
    assert entry.unique_id == _SERIAL_TXT["serial"]
    assert entry.data[CONF_HOST] == "5.6.7.8"
    assert gateway_device_id(entry) == hub_before
    schedule_reload.assert_called_once_with(entry.entry_id)


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
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
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
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
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
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
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
    # A second address of the owner's gateway: the host check cannot see it,
    # the serial it answers with can.
    aioclient_mock.get("https://9.9.9.10/api/junghome/version/", json="1.5.0")
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-777")):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "9.9.9.10"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "1.2.3.4"  # unchanged
    assert entry.data[CONF_HOST] == "1.2.3.4"


@pytest.mark.real_serial_fetch
async def test_fetch_serial_over_rest(hass: HomeAssistant, aioclient_mock) -> None:
    """The REST helper parses the raw-string body and tolerates failures."""
    url = "https://gw/api/junghome/config/parameter/system_serial"
    aioclient_mock.get(url, json="0000000084fb4b1b")
    assert (
        await _flow(hass)._async_fetch_serial("gw", "tok", FAKE_FINGERPRINT)
        == "0000000084fb4b1b"
    )

    # Older firmware: parameter unknown -> 404 -> None.
    aioclient_mock.clear_requests()
    aioclient_mock.get(url, status=404)
    assert await _flow(hass)._async_fetch_serial("gw", "tok", FAKE_FINGERPRINT) is None

    # The middleware populates the value asynchronously after boot; an empty
    # string must read as "not known", not become a unique_id.
    aioclient_mock.clear_requests()
    aioclient_mock.get(url, json="")
    assert await _flow(hass)._async_fetch_serial("gw", "tok", FAKE_FINGERPRINT) is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, exc=aiohttp.ClientError())
    assert await _flow(hass)._async_fetch_serial("gw", "tok", FAKE_FINGERPRINT) is None


async def test_reauth_on_a_loaded_entry_reloads_via_the_listener(
    hass: HomeAssistant, init_integration
) -> None:
    """Reauth must store the token and reload without HA's deprecated helper.

    Only a *loaded* entry has update listeners, and that is the exact condition
    under which `async_update_reload_and_abort` reports "has an update listener
    and should use it for scheduling a reload" — deprecated in HA 2026.6, an
    error from 2026.12. Every other reauth test uses a bare `MockConfigEntry`
    that was never set up, so none of them can see it.

    The autouse `fail_on_home_assistant_deprecation_reports` fixture is what
    turns that report into a failure; this test is what provokes it.
    """
    entry = init_integration
    assert entry.update_listeners  # the precondition the deprecation keys on

    with (
        patch(_REGISTER, AsyncMock(return_value="fresh-tok")),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "fresh-tok"
    # The listener owns the reload, so the rebuilt coordinator carries the new
    # token — otherwise the next poll re-auth-fails in a loop.
    assert entry.runtime_data.config["token"] == "fresh-tok"
    # ...and it owns it alone: the flow's own reload (for an entry that never
    # loaded, below) must not stack a second one on top of the listener's.
    reload.assert_called_once_with(entry.entry_id)


async def test_reauth_recovers_an_entry_whose_setup_failed_on_auth(
    hass: HomeAssistant,
) -> None:
    """A token rejected at *setup* must be recovered by the reauth flow alone.

    A 401 on the first refresh raises ``ConfigEntryAuthFailed`` before
    ``async_setup_entry`` reaches the line that registers the update listener,
    so the entry sits in SETUP_ERROR with no listener at all — the state a user
    lands in after revoking Home Assistant in the app and restarting. Storing
    the fresh token then changed ``entry.data`` and nothing else: the flow
    reported success while the entry stayed SETUP_ERROR until a restart or a
    manual reload. The flow must schedule that reload itself here, exactly as
    reconfigure does for SETUP_RETRY.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "revoked"},
    )
    entry.add_to_hass(hass)

    async def _gateway(
        self: JungHomeDataUpdateCoordinator, host: str, token: str
    ) -> list:
        """Reject the revoked token; accept only the one reauth registers."""
        if token != "fresh-tok":
            raise aiohttp.ClientResponseError(Mock(), (), status=401)
        return []

    with (
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", _gateway
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert not entry.update_listeners

        # Drive the reauth flow Home Assistant itself opened on the auth
        # failure — the one behind the notification the user actually clicks.
        flow = next(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        with patch(_REGISTER, AsyncMock(return_value="fresh-tok")):
            result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
            result = await _advance_progress(hass, result)
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_TOKEN] == "fresh-tok"
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.config["token"] == "fresh-tok"
        reload.assert_called_once_with(entry.entry_id)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# TLS certificate pinning in the flows
# ---------------------------------------------------------------------------


async def test_reconfigure_probe_carries_no_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The reconfigure probe is the unauthenticated version read, token-free (S3).

    It used to be ``GET /functions`` with the token, sent to a freshly typed
    address before anything had established what answered there — and a 401
    from that address was accepted as "reachable", skipping straight past
    the serial check. Now the responder's certificate is read first (a bare
    handshake), the probe hits ``version`` without a token, and only the
    serial read — pinned — carries it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "secret-token"},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
    fetch, run_ws = _no_network()
    with fetch, run_ws, patch(_FETCH_SERIAL, AsyncMock(return_value=None)) as serial:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    calls = [
        (m[0].lower(), str(m[1]), (m[3] or {}).get("token"))
        for m in aioclient_mock.mock_calls
    ]
    assert calls == [("get", "https://5.6.7.8/api/junghome/version/", None)]
    # The serial read (the one request that does carry the token) went out
    # pinned to the certificate the address presented.
    serial.assert_awaited_once_with("5.6.7.8", "secret-token", FAKE_FINGERPRINT)
    assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT


async def test_reconfigure_unreadable_certificate_is_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """A host that cannot complete a TLS handshake fails the form, nothing sent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    with (
        patch(_LEARN, AsyncMock(side_effect=aiohttp.ClientError("refused"))),
        patch(_FETCH_SERIAL, AsyncMock(return_value=None)) as serial,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    serial.assert_not_called()
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reconfigure_different_certificate_requires_confirmation(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A new address with a different certificate is confirmed before use.

    The certificate is the only identity the flow can check without sending
    the token, and it differs from the pin — a different device, or the same
    gateway after a reset — so the user is asked. Nothing is stored and
    nothing but the handshake has gone to the address until they confirm; on
    confirm the new certificate becomes the pin and the usual commit (probe,
    serial check) runs against it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-orig",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-orig",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
    fetch, run_ws = _no_network()
    with (
        fetch,
        run_ws,
        patch(_FETCH_SERIAL, AsyncMock(return_value="ser-orig")) as serial,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure_certificate"
        assert result["description_placeholders"] == {
            "host": "5.6.7.8",
            "expected": "CD:" * 31 + "CD",
            "observed": "AB:" * 31 + "AB",
        }
        # Nothing committed, nothing sent beyond the handshake.
        assert entry.data[CONF_HOST] == "1.2.3.4"
        assert aioclient_mock.mock_calls == []
        serial.assert_not_called()

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"
    assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT
    serial.assert_awaited_once_with("5.6.7.8", "t", FAKE_FINGERPRINT)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_confirmed_certificate_still_checks_the_serial(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Confirming a new certificate does not waive the serial check."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-orig",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-orig",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
    with patch(_FETCH_SERIAL, AsyncMock(return_value="ser-OTHER")):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
        assert result["step_id"] == "reconfigure_certificate"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "different_gateway"}
    assert entry.data[CONF_HOST] == "1.2.3.4"
    assert entry.data[CONF_TLS_FINGERPRINT] == _OTHER_FINGERPRINT
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reconfigure_matching_certificate_needs_no_confirmation(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The same gateway (same certificate) at a new address commits directly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-orig",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-orig",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get("https://5.6.7.8/api/junghome/version/", json="1.5.0")
    fetch, run_ws = _no_network()
    with fetch, run_ws, patch(_FETCH_SERIAL, AsyncMock(return_value="ser-orig")):
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


async def test_manual_flow_refuses_to_move_an_entry_to_a_different_certificate(
    hass: HomeAssistant,
) -> None:
    """Typing an address that claims a configured serial is not enough.

    The serial comes from the responder itself, so any HTTPS host could
    claim it; the existing entry's host moves only if the responder presents
    the certificate that entry pinned. Here it does not: abort, untouched.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-777",
        data={
            CONF_HOST: "9.9.9.9",
            CONF_TOKEN: "t",
            CONF_SERIAL: "ser-777",
            CONF_IDENTITY_ANCHOR: "ser-777",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
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
    assert entry.data[CONF_HOST] == "9.9.9.9"
    assert entry.data[CONF_TLS_FINGERPRINT] == _OTHER_FINGERPRINT


async def test_manual_flow_pins_an_unpinned_existing_entry_it_finds(
    hass: HomeAssistant,
) -> None:
    """Rediscovering an unpinned entry by typing its address pins it too."""
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
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "1.2.3.4"
    assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT


async def test_host_form_fails_when_the_certificate_cannot_be_read(
    hass: HomeAssistant,
) -> None:
    """The pin is learned before registration; no handshake, no registration."""
    with (
        patch(_LEARN, AsyncMock(side_effect=TimeoutError)),
        patch(_REGISTER, AsyncMock(return_value="tok")) as register,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await _choose(hass, result, "app_approval")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "app_approval"
    assert result["errors"] == {"base": "cannot_connect"}
    register.assert_not_called()
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reauth_registers_against_the_pinned_certificate(
    hass: HomeAssistant,
) -> None:
    """Reauth re-registers with the gateway the entry pinned, not with whoever answers.

    The flow seeds its pin from the entry, so no certificate is learned and
    the registration request goes out pinned to the stored digest; the
    fresh token is stored next to the unchanged pin.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: "old",
            CONF_TLS_FINGERPRINT: _OTHER_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    seen: list[str | None] = []

    async def _register(self: JungHomeConfigFlow) -> str:
        seen.append(self._fingerprint)
        return "new-tok"

    fetch, run_ws = _no_network()
    with (
        patch(_REGISTER, _register),
        patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn,
        fetch,
        run_ws,
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert seen == [_OTHER_FINGERPRINT]
    learn.assert_not_called()
    assert entry.data[CONF_TOKEN] == "new-tok"
    assert entry.data[CONF_TLS_FINGERPRINT] == _OTHER_FINGERPRINT


async def test_reauth_of_an_unpinned_entry_pins_on_first_contact(
    hass: HomeAssistant,
) -> None:
    """A legacy entry with no pin learns one as part of the reauth registration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)

    async def _register(self: JungHomeConfigFlow) -> str:
        await self._async_ssl()  # what the real registration does first
        return "new-tok"

    fetch, run_ws = _no_network()
    with patch(_REGISTER, _register), fetch, run_ws:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _advance_progress(hass, result)
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-tok"
    assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT


async def test_registration_helpers_send_the_pin(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Both registration requests pass ``ssl=`` pinned to the learned digest.

    ``aioclient_mock`` does not record ``ssl``, so the session's request
    entry point is wrapped to capture it.
    """
    aioclient_mock.post("https://gw/api/junghome/register", json={"token": "abc"})
    aioclient_mock.post(
        "https://gw/api/junghome/register/by-password", json={"token": "pw"}
    )
    session = async_get_clientsession(hass, verify_ssl=False)
    original = session._request
    seen: list[object] = []

    async def _spy(method, url, **kwargs):
        seen.append(kwargs.get("ssl"))
        return await original(method, url, **kwargs)

    with patch.object(session, "_request", _spy):
        flow = _flow(hass)
        assert await flow._async_register() == "abc"
        assert await flow._async_register_by_password("pw") == "pw"
    assert seen == [fingerprint_ssl(FAKE_FINGERPRINT)] * 2
    assert flow._fingerprint == FAKE_FINGERPRINT


async def test_zeroconf_leaves_an_ignored_entry_alone(hass: HomeAssistant) -> None:
    """An ignored discovery stays ignored: no adoption, no reload, plain abort."""
    for unique_id, info in (
        (_SERIAL_TXT["serial"], _zeroconf_info(host="5.6.7.8", properties=_SERIAL_TXT)),
        ("junghome-abc.local", _zeroconf_info(host="5.6.7.8")),
    ):
        ignored = MockConfigEntry(
            domain=DOMAIN, unique_id=unique_id, source=config_entries.SOURCE_IGNORE
        )
        ignored.add_to_hass(hass)
        with patch(_LEARN, AsyncMock(return_value=FAKE_FINGERPRINT)) as learn:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "zeroconf"}, data=info
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert CONF_HOST not in ignored.data
        learn.assert_not_called()


async def test_finish_pins_even_when_no_step_learned_first(
    hass: HomeAssistant,
) -> None:
    """``async_step_finish`` never creates an entry without a pin.

    Every real path learns it in ``_async_apply_host``; this is the
    defensive fallback for a flow driven straight to ``finish``.
    """
    flow = _flow(hass, host="1.2.3.4")
    flow._token = "tok"
    flow.context = {"source": SOURCE_USER}
    result = await flow.async_step_finish()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT


@pytest.mark.real_project_fetch
async def test_forged_discovery_cannot_redirect_the_token_of_a_loaded_entry(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The reviewer's S1 reproduction, inverted: the token stays home.

    A serial-keyed entry is loaded and healthy at HOST_A (real REST path
    through ``aioclient_mock`` so every request's destination is recorded).
    One unauthenticated mDNS packet naming its (public) serial from HOST_B
    used to rewrite the host, reload the entry and send the token to
    HOST_B. Now: abort, host unchanged, no reload, and not a single request
    to HOST_B.
    """
    host_a, host_b, serial, token = "192.168.1.50", "192.168.1.66", "0022D1059602", "t"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=serial,
        data={
            CONF_HOST: host_a,
            CONF_TOKEN: token,
            CONF_SERIAL: serial,
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(f"https://{host_a}/api/junghome/functions", json=[])
    aioclient_mock.get(f"https://{host_a}/api/junghome/project/junghome", status=404)
    with patch.object(
        JungHomeDataUpdateCoordinator, "_run_websocket", _fake_live_websocket
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        info = _zeroconf_info(
            hostname="junghome-0022d1059602.local.",
            host=host_b,
            properties={"serial": serial, "version": "2.1.3"},
        )
        with patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "zeroconf"}, data=info
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert entry.data[CONF_HOST] == host_a
        reload.assert_not_called()
        assert [m for m in aioclient_mock.mock_calls if host_b in str(m[1])] == []
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
