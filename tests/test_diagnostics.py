"""Diagnostics tests for Jung Home: what a downloadable report must not carry.

The entry-level and per-function-device diagnostics tests live in
``tests/test_init.py``; this file holds the hub (gateway) device cases.
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
