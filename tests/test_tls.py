"""TLS certificate pinning: the wire-level proof, and the repair flow.

The unit tests in ``test_coordinator.py`` / ``test_config_flow.py`` stub the
certificate learn and check *where* the fingerprint is learned, stored and
compared. This file is the other half: two real HTTPS servers on the
loopback interface, each with its own self-signed certificate — the
"gateway" and an "impostor" — and Home Assistant's own no-verify session,
exactly as the integration uses it. What is pinned by a real
``aiohttp.Fingerprint`` must reach the gateway and must be refused by the
impostor at the TLS handshake, i.e. before the request line, the ``token``
header, or anything else is written. The impostor's handler records every
request it ever sees, so "nothing was sent" is asserted, not assumed.
"""

import asyncio
import datetime
import hashlib
import importlib.util
import logging
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.config_flow import CannotRegister, JungHomeConfigFlow
from custom_components.junghome.const import CONF_SERIAL, CONF_TLS_FINGERPRINT, DOMAIN
from custom_components.junghome.coordinator import (
    ISSUE_TLS_MISMATCH,
    JungHomeDataUpdateCoordinator,
)
from custom_components.junghome.repairs import (
    TlsCertificateChangedFlow,
    async_create_fix_flow,
)
from custom_components.junghome.tls import (
    PROBE_DIGEST,
    async_learn_fingerprint,
    fingerprint_ssl,
    format_fingerprint,
    normalize_fingerprint,
)
from tests.conftest import FAKE_FINGERPRINT, _fake_run_websocket

TOKEN = "synthetic-token-1234"  # never a real credential


def _self_signed(directory: Path, name: str) -> tuple[Path, Path, str]:
    """Write a fresh self-signed certificate + key; return paths and SHA-256 hex."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "junghome.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    crt, pem = directory / f"{name}.crt", directory / f"{name}.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return crt, pem, digest


class _Server:
    """One HTTPS server on the loopback interface with its own certificate.

    Answers every JUNG HOME endpoint the integration touches with a minimal
    valid body and records ``(method, path, token header)`` for each request
    it actually receives.
    """

    def __init__(self, directory: Path, name: str) -> None:
        crt, key, self.fingerprint = _self_signed(directory, name)
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.load_cert_chain(str(crt), str(key))
        self.seen: list[tuple[str, str, str | None]] = []
        self.host = ""
        self._runner: web.AppRunner | None = None

    async def _handler(self, request: web.Request) -> web.Response:
        self.seen.append((request.method, request.path, request.headers.get("token")))
        path = request.path
        if path.endswith("/functions"):
            return web.json_response([])
        if path.endswith(("/register", "/register/by-password")):
            return web.json_response({"token": TOKEN})
        if path.endswith("/system_serial"):
            return web.json_response("00000000c0ffee42")
        if path.endswith("/version/"):
            return web.json_response("1.5.0")
        return web.json_response({}, status=404)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self.ssl_context)
        await site.start()
        port = self._runner.addresses[0][1]
        self.host = f"127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def servers(tmp_path: Path, socket_enabled: None) -> SimpleNamespace:
    """A ``gateway`` and an ``impostor`` server, with distinct certificates.

    ``socket_enabled`` (pytest-socket, as Home Assistant's own ``hass_client``
    uses it) lifts the harness's socket ban for the test; connections stay
    restricted to the loopback interface.
    """
    gateway, impostor = _Server(tmp_path, "gateway"), _Server(tmp_path, "impostor")
    await gateway.start()
    await impostor.start()
    assert gateway.fingerprint != impostor.fingerprint
    yield SimpleNamespace(gateway=gateway, impostor=impostor)
    await gateway.stop()
    await impostor.stop()


# ---------------------------------------------------------------------------
# tls.py against real sockets
# ---------------------------------------------------------------------------


@pytest.mark.real_tls_probe
async def test_learn_reads_the_digest_without_sending_a_request(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """The learn step yields the peer's SHA-256 and never gets past the handshake."""
    session = async_get_clientsession(hass, verify_ssl=False)
    learned = await async_learn_fingerprint(session, servers.gateway.host)
    assert learned == servers.gateway.fingerprint
    assert servers.gateway.seen == []  # no request line, no headers, nothing


@pytest.mark.real_tls_probe
async def test_learn_reports_an_unreachable_host_as_a_client_error(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """A host with nothing listening is the ordinary cannot-connect error."""
    await servers.impostor.stop()
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(aiohttp.ClientError):
        await async_learn_fingerprint(session, servers.impostor.host)


async def test_learn_refuses_a_peer_that_accepts_the_probe_digest(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A "successful" probe (only a test double can produce one) is not a pin."""
    aioclient_mock.get("https://gw/api/junghome/version/", json="1.5.0")
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(aiohttp.ClientConnectionError):
        await async_learn_fingerprint(session, "gw")


@pytest.mark.real_tls_probe
async def test_pinned_request_reaches_the_gateway_and_not_the_impostor(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """The reviewer's feasibility probe, as a regression test.

    Through Home Assistant's own ``verify_ssl=False`` session (the very
    connector the integration shares): a request pinned to the gateway's
    digest completes against the gateway, and the same pin against the
    impostor raises ``ServerFingerprintMismatch`` with the impostor having
    received nothing — the token header included.
    """
    session = async_get_clientsession(hass, verify_ssl=False)
    pin = fingerprint_ssl(servers.gateway.fingerprint)
    headers = {"token": TOKEN}

    async with session.get(
        f"https://{servers.gateway.host}/api/junghome/functions",
        headers=headers,
        ssl=pin,
    ) as response:
        assert response.status == 200
    assert servers.gateway.seen == [("GET", "/api/junghome/functions", TOKEN)]

    with pytest.raises(aiohttp.ServerFingerprintMismatch) as excinfo:
        await session.get(
            f"https://{servers.impostor.host}/api/junghome/functions",
            headers=headers,
            ssl=pin,
        )
    assert excinfo.value.got.hex() == servers.impostor.fingerprint
    assert servers.impostor.seen == []


@pytest.mark.real_tls_probe
async def test_websocket_upgrade_is_refused_by_the_impostor_before_the_token(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """``ws_connect`` honours the same pin; the upgrade never leaves."""
    session = async_get_clientsession(hass, verify_ssl=False)
    with pytest.raises(aiohttp.ServerFingerprintMismatch):
        await session.ws_connect(
            f"wss://{servers.impostor.host}/ws",
            headers={"token": TOKEN},
            ssl=fingerprint_ssl(servers.gateway.fingerprint),
        )
    assert servers.impostor.seen == []


def test_fingerprint_helpers() -> None:
    """Normalisation, display form and the cached ``Fingerprint`` object."""
    digest = "ab" * 32
    assert normalize_fingerprint(digest) == digest
    assert normalize_fingerprint(digest.upper()) == digest
    assert normalize_fingerprint(format_fingerprint(digest)) == digest
    assert normalize_fingerprint(" " + digest + "\n") == digest
    for junk in (None, "", "ab" * 31, "zz" * 32, 42, b"ab" * 32):
        assert normalize_fingerprint(junk) is None
    assert format_fingerprint("abcd") == "AB:CD"
    assert format_fingerprint("") == ""
    # One object per digest, so aiohttp's connection pool keys stay stable.
    assert fingerprint_ssl(digest) is fingerprint_ssl(digest)
    assert fingerprint_ssl(digest).fingerprint == bytes.fromhex(digest)
    with pytest.raises(ValueError, match="SHA-256"):
        fingerprint_ssl("ab" * 16)  # MD5-length: aiohttp would refuse it too
    with pytest.raises(ValueError, match="hexadecimal"):
        fingerprint_ssl("not hex")
    assert len(PROBE_DIGEST) == 32


# ---------------------------------------------------------------------------
# The coordinator and the config flow end to end
# ---------------------------------------------------------------------------


def _entry(host: str, fingerprint: str | None) -> MockConfigEntry:
    data = {CONF_HOST: host, CONF_TOKEN: TOKEN, CONF_SERIAL: "00000000c0ffee42"}
    if fingerprint is not None:
        data[CONF_TLS_FINGERPRINT] = fingerprint
    return MockConfigEntry(domain=DOMAIN, unique_id="00000000c0ffee42", data=data)


@pytest.mark.real_tls_probe
async def test_coordinator_refuses_the_impostor_and_raises_the_issue(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """S1/S2: a pinned entry pointed at a stranger sends nothing and goes unavailable.

    The entry's stored host is the impostor's address (what a forged mDNS
    rewrite used to achieve); its pin is the real gateway's. The poll fails
    with ``UpdateFailed``, the certificate-changed repair issue is raised,
    and the impostor's log is still empty: the token never left.
    """
    entry = _entry(servers.impostor.host, servers.gateway.fingerprint)
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": entry.data[CONF_HOST], "token": TOKEN}, entry
    )
    with pytest.raises(UpdateFailed) as excinfo:
        await coordinator._async_update_data()
    assert excinfo.value.translation_key == "certificate_changed"
    assert servers.impostor.seen == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._tls_issue_id)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["observed"] == format_fingerprint(
        servers.impostor.fingerprint
    )
    # The stored pin is untouched: nothing re-learned behind the user's back.
    assert entry.data[CONF_TLS_FINGERPRINT] == servers.gateway.fingerprint


@pytest.mark.real_tls_probe
async def test_coordinator_polls_the_pinned_gateway(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """The same pin against the real gateway: an ordinary, successful poll."""
    entry = _entry(servers.gateway.host, servers.gateway.fingerprint)
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": entry.data[CONF_HOST], "token": TOKEN}, entry
    )
    assert await coordinator._async_update_data() == []
    assert servers.gateway.seen == [("GET", "/api/junghome/functions", TOKEN)]


@pytest.mark.real_tls_probe
async def test_legacy_entry_pins_on_its_first_successful_connect(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """TOFU for an entry created before pinning existed, with the real learn.

    The first poll learns the certificate of the CURRENT host with a bare
    handshake (the gateway's log shows no request for it), then fetches
    pinned to it; the digest lands in the entry once that fetch succeeded.
    """
    entry = _entry(servers.gateway.host, None)
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": entry.data[CONF_HOST], "token": TOKEN}, entry
    )
    assert await coordinator._async_update_data() == []
    assert entry.data[CONF_TLS_FINGERPRINT] == servers.gateway.fingerprint
    # Exactly one request reached the gateway: the learn was handshake-only.
    assert servers.gateway.seen == [("GET", "/api/junghome/functions", TOKEN)]


@pytest.mark.real_tls_probe
async def test_registration_is_refused_by_the_impostor_before_anything_is_sent(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """Both registration requests go only to the pinned certificate.

    ``register/by-password`` carries the network-key password — the one
    credential above the token — so the impostor must see neither it nor
    the approval request; the flow reports "cannot connect".
    """
    flow = JungHomeConfigFlow()
    flow.hass = hass
    flow._host = servers.impostor.host
    flow._fingerprint = servers.gateway.fingerprint
    with pytest.raises(CannotRegister):
        await flow._async_register_by_password("network-key")
    assert flow._error == "cannot_connect"
    with pytest.raises(CannotRegister):
        await flow._async_register()
    assert flow._error == "cannot_connect"
    assert servers.impostor.seen == []

    # ...and the same pin against the gateway itself registers normally.
    flow._host = servers.gateway.host
    assert await flow._async_register_by_password("network-key") == TOKEN
    assert servers.gateway.seen == [
        ("POST", "/api/junghome/register/by-password", None)
    ]


@pytest.mark.real_tls_probe
async def test_host_form_learns_the_gateway_certificate_before_registering(
    hass: HomeAssistant, servers: SimpleNamespace
) -> None:
    """``_async_apply_host`` pins with the real learn; nothing is sent to do so."""
    flow = JungHomeConfigFlow()
    flow.hass = hass
    flow.context = {}
    assert await flow._async_apply_host(servers.gateway.host) is None
    assert flow._fingerprint == servers.gateway.fingerprint
    assert servers.gateway.seen == []


# ---------------------------------------------------------------------------
# The repair flow
# ---------------------------------------------------------------------------


async def _raise_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> JungHomeDataUpdateCoordinator:
    """Set ``entry`` up so its first refresh hits a certificate mismatch."""
    mismatch = aiohttp.ServerFingerprintMismatch(
        bytes.fromhex(FAKE_FINGERPRINT), bytes.fromhex("cd" * 32), "1.2.3.4", 443
    )
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_from_api",
        AsyncMock(side_effect=mismatch),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue_id = f"{ISSUE_TLS_MISMATCH}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    return issue_id


async def test_repair_flow_re_pins_only_after_confirmation(
    hass: HomeAssistant,
) -> None:
    """The fix flow, driven through Home Assistant's repairs manager.

    Showing the form changes nothing. Confirming learns the certificate the
    gateway presents *now*, checks the serial against the recorded one on
    the newly pinned connection, stores the new pin, schedules the reload
    (a SETUP_RETRY entry has no update listener) and — via the manager —
    deletes the issue.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: "ser-1",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    issue_id = await _raise_issue(hass, entry)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None

    learn = AsyncMock(return_value="cd" * 32)
    serial = AsyncMock(return_value="ser-1")
    with (
        patch("custom_components.junghome.repairs.async_learn_fingerprint", learn),
        patch("custom_components.junghome.repairs.async_fetch_serial", serial),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"
        assert result["description_placeholders"] == {
            "host": "1.2.3.4",
            "expected": "AB:" * 31 + "AB",
            "observed": "CD:" * 31 + "CD",
        }
        # Nothing has happened yet.
        learn.assert_not_called()
        assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT

        result = await manager.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    learn.assert_awaited_once()
    serial.assert_awaited_once_with(hass, "1.2.3.4", TOKEN, "cd" * 32)
    assert entry.data[CONF_TLS_FINGERPRINT] == "cd" * 32
    schedule_reload.assert_called_once_with(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_repair_flow_refuses_a_different_gateway_and_an_unreachable_one(
    hass: HomeAssistant,
) -> None:
    """Confirming does not waive identity: a wrong serial keeps the old pin."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: "ser-1",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    issue_id = await _raise_issue(hass, entry)
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    assert isinstance(flow, TlsCertificateChangedFlow)
    flow.hass = hass
    flow.issue_id = issue_id

    with patch(
        "custom_components.junghome.repairs.async_learn_fingerprint",
        AsyncMock(side_effect=aiohttp.ClientError("refused")),
    ):
        result = await flow.async_step_confirm({})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    with (
        patch(
            "custom_components.junghome.repairs.async_learn_fingerprint",
            AsyncMock(return_value="cd" * 32),
        ),
        patch(
            "custom_components.junghome.repairs.async_fetch_serial",
            AsyncMock(return_value="ser-OTHER"),
        ),
    ):
        result = await flow.async_step_confirm({})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "different_gateway"}
    assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT


async def test_fix_flow_for_an_unknown_issue_or_missing_entry_is_a_plain_confirm(
    hass: HomeAssistant,
) -> None:
    """An issue whose entry is gone degrades to HA's dismiss-only flow."""
    flow = await async_create_fix_flow(
        hass, f"{ISSUE_TLS_MISMATCH}_x", {"entry_id": "x"}
    )
    assert not isinstance(flow, TlsCertificateChangedFlow)
    flow = await async_create_fix_flow(hass, "something_else", None)
    assert not isinstance(flow, TlsCertificateChangedFlow)


async def test_repair_flow_form_without_an_issue_still_renders(
    hass: HomeAssistant,
) -> None:
    """Placeholders fall back to the entry's own data when the issue is gone."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN, CONF_TLS_FINGERPRINT: "ab" * 32},
    )
    entry.add_to_hass(hass)
    flow = TlsCertificateChangedFlow(entry)
    flow.hass = hass
    flow.issue_id = "nope"
    result = await flow.async_step_init()
    assert result["type"] == FlowResultType.FORM
    assert result["description_placeholders"] == {
        "host": "1.2.3.4",
        "expected": "AB:" * 31 + "AB",
        "observed": "?",
    }


async def test_issue_is_withdrawn_when_the_gateway_answers_again(
    hass: HomeAssistant,
) -> None:
    """A loaded entry: mismatch raises the issue, the next good poll clears it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: TOKEN,
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
        registry = ir.async_get(hass)

        fetch.side_effect = aiohttp.ServerFingerprintMismatch(
            bytes.fromhex(FAKE_FINGERPRINT), bytes.fromhex("cd" * 32), "1.2.3.4", 443
        )
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert registry.async_get_issue(DOMAIN, coordinator._tls_issue_id)

        fetch.side_effect = None
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert registry.async_get_issue(DOMAIN, coordinator._tls_issue_id) is None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_learn_timeout_is_bounded(hass: HomeAssistant) -> None:
    """A peer that accepts TCP and never finishes the handshake times out."""
    session = async_get_clientsession(hass, verify_ssl=False)
    gate = asyncio.Event()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await gate.wait()

    with (
        patch.object(session, "_request", _hang),
        patch("custom_components.junghome.tls.LEARN_TIMEOUT", 0.01),
        pytest.raises(TimeoutError),
    ):
        await async_learn_fingerprint(session, "gw")
    gate.set()


# ---------------------------------------------------------------------------
# Floor safety and the pin's persistence rules
# ---------------------------------------------------------------------------


def test_repairs_platform_imports_without_the_2026_6_result_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repairs platform must import on the hacs.json floor (HA 2025.12.4).

    ``RepairsFlowResult`` exists only from HA 2026.6 (``repairs/models.py``);
    the platform annotates with it type-only. Load the module's source afresh
    with the name removed from ``homeassistant.components.repairs`` — what the
    floor core looks like — and it must still import. On the floor a platform
    ``ImportError`` is swallowed by core at DEBUG, and submitting the
    certificate issue then runs core's plain confirm flow, which deleted the
    issue without re-pinning: the repair silently did nothing, on six
    supported releases.
    """
    import homeassistant.components.repairs as repairs_component  # noqa: PLC0415

    import custom_components.junghome.repairs as junghome_repairs  # noqa: PLC0415

    # ``raising=False``: on the floor itself the name is already absent.
    monkeypatch.delattr(repairs_component, "RepairsFlowResult", raising=False)
    spec = importlib.util.spec_from_file_location(
        "custom_components.junghome._repairs_floor_probe",
        Path(junghome_repairs.__file__),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # raised ImportError before the fix
    assert issubclass(module.TlsCertificateChangedFlow, junghome_repairs.RepairsFlow)


async def test_learned_fingerprint_never_overwrites_a_pin_written_meanwhile(
    hass: HomeAssistant,
) -> None:
    """A stale coordinator's poll must not put its learned pin back over a newer one.

    A legacy entry learns X on its first poll and persists it. A reconfigure
    (or the repair flow) then writes Y — the certificate the user just
    confirmed for the new address — and reloads. Should a poll of the *old*
    coordinator complete between that write and its ``stop()``, the persist
    step must leave Y alone: writing X back would pin the new coordinator to
    the wrong certificate and raise ``tls_certificate_changed`` for the very
    certificate the user vouched for.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "1.2.3.4", "token": TOKEN}, entry
    )
    # The learn happens inside the (mocked) fetch, so take it explicitly: the
    # autouse stub answers FAKE_FINGERPRINT.
    await coordinator._async_ssl()
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_from_api",
        AsyncMock(return_value=[]),
    ):
        await coordinator._async_update_data()
        assert entry.data[CONF_TLS_FINGERPRINT] == FAKE_FINGERPRINT
        assert coordinator._learned_fingerprint == FAKE_FINGERPRINT

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TLS_FINGERPRINT: "cd" * 32}
        )
        await coordinator._async_update_data()
    assert entry.data[CONF_TLS_FINGERPRINT] == "cd" * 32


async def test_mismatch_is_logged_once_per_outage_not_per_coordinator(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The ERROR line survives coordinator rebuilds while the issue stands.

    A mismatch on the first refresh parks the entry in SETUP_RETRY, and every
    retry (up to one every ten minutes, for as long as the mismatch lasts)
    builds a fresh coordinator. Its once-per-instance flag is empty again, so
    the outage used to log a new ERROR per retry; the issue in the registry
    is the memory that spans the rebuilds. Once the issue is gone, a new
    mismatch is news again.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: "ser-1",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    issue_id = await _raise_issue(hass, entry)

    def _errors() -> int:
        return sum(
            1
            for record in caplog.records
            if record.levelno == logging.ERROR
            and "does not match the pinned one" in record.getMessage()
        )

    assert _errors() == 1
    mismatch = aiohttp.ServerFingerprintMismatch(
        bytes.fromhex(FAKE_FINGERPRINT), bytes.fromhex("cd" * 32), "1.2.3.4", 443
    )
    # The retry's fresh coordinator: same entry, same standing issue.
    retry = JungHomeDataUpdateCoordinator(
        hass, {"host": "1.2.3.4", "token": TOKEN}, entry
    )
    retry._report_fingerprint_mismatch(mismatch)
    retry._report_fingerprint_mismatch(mismatch)
    assert _errors() == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # Issue withdrawn (the gateway answered again, or the user fixed it): the
    # next mismatch is a new outage and is reported.
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    fresh = JungHomeDataUpdateCoordinator(
        hass, {"host": "1.2.3.4", "token": TOKEN}, entry
    )
    fresh._report_fingerprint_mismatch(mismatch)
    assert _errors() == 2


async def test_mismatch_issue_from_a_setup_retry_is_withdrawn_on_recovery(
    hass: HomeAssistant,
) -> None:
    """The pinned gateway answers again after a SETUP_RETRY: the issue goes.

    The coordinator that raised the issue was thrown away with the failed
    setup; the retry's coordinator never raised anything, so withdrawal keys
    on the registry, like the report side. Before, a transient impostor at
    startup left an ERROR repair on a working entry for good.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ser-1",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: "ser-1",
            CONF_TLS_FINGERPRINT: FAKE_FINGERPRINT,
        },
    )
    entry.add_to_hass(hass)
    issue_id = await _raise_issue(hass, entry)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        # What the retry timer does: set the entry up again.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
