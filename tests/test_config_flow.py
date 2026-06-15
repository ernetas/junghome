"""Tests for the Jung Home config flow."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.config_flow import _normalize_host
from custom_components.junghome.const import DOMAIN


def test_normalize_host_strips_scheme_whitespace_and_slash():
    cases = {
        "192.168.1.10": "192.168.1.10",
        "  192.168.1.10  ": "192.168.1.10",
        "https://junghome.local": "junghome.local",
        "http://junghome.local/": "junghome.local",
        "HTTPS://Gateway/": "Gateway",  # host casing is preserved
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
