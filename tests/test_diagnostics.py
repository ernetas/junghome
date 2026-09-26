"""Diagnostics tests for Jung Home: what a downloadable report must not carry.

The entry-level and per-function-device diagnostics tests live in
``tests/test_init.py``; this file holds the hub (gateway) device cases and
the gateway's health log.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import (
    CONF_IDENTITY_ANCHOR,
    CONF_SERIAL,
    DOMAIN,
    gateway_device_id,
)
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from tests.conftest import _fake_run_websocket, find_device

HOST = "192.168.1.50"
HOSTNAME = "junghome-02005ec0ffee.local"
SERIAL = "02005EC0FFEE"


@pytest.mark.parametrize(
    ("unique_id", "data", "last_error"),
    [
        # Serial-keyed entry migrated from an mDNS-hostname unique_id: the
        # frozen anchor is the old hostname, and it sits in entry.data next to
        # the serial — so an error quoting either must be swept too.
        (
            SERIAL,
            {
                CONF_HOST: HOST,
                CONF_TOKEN: "tok",
                CONF_SERIAL: SERIAL,
                CONF_IDENTITY_ANCHOR: HOSTNAME,
            },
            f"Cannot connect to host {HOST}:443 (serial {SERIAL}, was {HOSTNAME})",
        ),
        # Legacy entry never rediscovered or reconfigured: no anchor (or
        # serial) in entry.data at all, so the anchor IS the unique_id — the
        # hostname — which nothing keyed on entry.data can find. The host is
        # the only thing such an entry's errors can quote.
        (
            HOSTNAME,
            {CONF_HOST: HOST, CONF_TOKEN: "tok"},
            f"Cannot connect to host {HOST}:443",
        ),
    ],
    ids=["migrated", "legacy"],
)
async def test_hub_device_diagnostics_do_not_leak_the_anchor(
    hass: HomeAssistant, unique_id: str, data: dict[str, str], last_error: str
) -> None:
    """The hub's report must not carry the host, the serial or the anchor.

    The hub device's identifier is ``gateway_<anchor>`` where the anchor is the
    host, the mDNS hostname or the serial — exactly the values ``TO_REDACT``
    strips from the entry dump — and the per-device report emitted it raw.
    The same values must not survive inside ``last_error`` either.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data=data)
    entry.add_to_hass(hass)
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
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data.last_error = last_error

    hub = find_device(hass, gateway_device_id(entry))
    assert hub is not None
    diag = await async_get_device_diagnostics(hass, entry, hub)

    dump = json.dumps(diag, default=str)
    for secret in (HOST, SERIAL, HOSTNAME):
        assert secret not in dump, secret
    assert diag["identifiers"] == ["gateway_**REDACTED**"]
    # The surrounding context survives, so the report is still debuggable.
    assert diag["last_error"].startswith("Cannot connect to host **REDACTED**:443")
    # The hub is not one of the gateway's functions, so it has no payload.
    assert diag["device"] is None
    assert diag["device_properties"] is None
    assert diag["node_software_revision"] is None
    assert diag["function_anchor"] is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_diagnostics_scrub_the_host_out_of_the_title(
    hass: HomeAssistant,
) -> None:
    """The entry title is free-form text and the flow fills it with the host.

    ``Jung Home (<host>)`` is what every entry created by the config flow is
    called, and ``TO_REDACT`` keys cannot reach it — so it re-leaked exactly
    the value the ``data`` redaction removes. It takes the same literal sweep
    as ``last_error`` and the raw frames; a user's own rename that quotes the
    serial is swept the same way.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        title=f"Jung Home ({HOST}) sn {SERIAL}",
        data={CONF_HOST: HOST, CONF_TOKEN: "tok", CONF_SERIAL: SERIAL},
    )
    entry.add_to_hass(hass)
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
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["title"] == "Jung Home (**REDACTED**) sn **REDACTED**"
    dump = json.dumps(diag, default=str)
    assert HOST not in dump
    assert SERIAL not in dump

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_diagnostics_carry_the_health_log_without_personal_data(
    hass: HomeAssistant,
) -> None:
    """The health log is in the report, minus the account and client names.

    Two of the gateway's fixed texts quote personal data (the myJUNG account
    the gateway was registered to, the name of each API client granted
    access) and any text can quote the host; device labels stay, as they do
    everywhere else in the report. ``health_conditions`` names what was raised
    as a repair issue — the unreachable-device list never is.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=HOST, data={CONF_HOST: HOST, CONF_TOKEN: "tok"}
    )
    entry.add_to_hass(hass)
    log = [
        {
            "level": "INFO",
            "time": "2026-09-26T08:03:00.000Z",
            "description": "Gateway registered to myJUNG Cloud",
            "details": "You have registered your Gateway to the myJUNG Account "
            "jane.doe@example.com",
        },
        {
            "level": "INFO",
            "time": "2026-09-26T08:02:00.000Z",
            "description": "New User Permission",
            "details": 'New User or System with the name "Jane\'s phone" has been '
            "granted access to your JUNG HOME Gateway. \n            If this "
            'was not you, please review your "access permissions" and revoke '
            "access if necessary.",
        },
        {
            "level": "WARN",
            "time": "2026-09-26T08:01:00.000Z",
            "description": "JUNG HOME Devices are unreachable",
            "details": "1 devices cannot be reached: Hall Light. Try again.",
        },
        {
            "level": "ERROR",
            "time": "2026-09-26T08:00:00.000Z",
            "description": "out of sequence numbers",
            "details": f"gateway {HOST} may lost its abillity",
        },
    ]
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_health_status_from_api",
            AsyncMock(return_value=log),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)
    for secret in (HOST, "jane.doe@example.com", "Jane's phone"):
        assert secret not in dump, secret
    health = diag["health_status"]
    assert [item["description"] for item in health] == [
        item["description"] for item in log
    ]
    assert health[0]["details"] == (
        "You have registered your Gateway to the myJUNG Account **REDACTED**"
    )
    assert health[1]["details"].startswith(
        'New User or System with the name "**REDACTED**" has been granted'
    )
    assert health[2]["details"] == log[2]["details"]
    assert health[3] == {
        "level": "ERROR",
        "time": "2026-09-26T08:00:00.000Z",
        "description": "out of sequence numbers",
        "details": "gateway **REDACTED** may lost its abillity",
    }
    assert diag["health_conditions"] == ["gateway_out_of_sequence_numbers"]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_diagnostics_before_any_health_read(
    hass: HomeAssistant,
) -> None:
    """A gateway whose log was never readable reports None, not an empty log."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=HOST, data={CONF_HOST: HOST, CONF_TOKEN: "tok"}
    )
    entry.add_to_hass(hass)
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
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["health_status"] is None
    assert diag["health_conditions"] == []

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
