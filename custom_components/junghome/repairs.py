"""Repairs platform: the fix flow for a changed gateway certificate.

The coordinator pins the gateway's TLS certificate (``tls.py``) and raises the
``tls_certificate_changed`` issue when the responder at the stored address
presents a different one. Nothing re-learns the fingerprint on its own — an
impostor that could trigger a silent re-pin would defeat the pin — so this
flow is the only way forward, and it re-pins only after the user has
confirmed that the gateway was reset or replaced. It is the certificate
counterpart of the reauth flow for a rejected token.
"""

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_flow import async_fetch_serial
from .const import CONF_SERIAL, CONF_TLS_FINGERPRINT, DOMAIN
from .coordinator import ISSUE_TLS_MISMATCH
from .tls import async_learn_fingerprint, format_fingerprint


class TlsCertificateChangedFlow(RepairsFlow):
    """Re-pin the gateway's certificate once the user has vouched for it."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise the fix flow for one entry."""
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start at the confirm form."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show what changed; on confirm learn the new fingerprint and pin it.

        The fingerprint is learned *now*, at confirm time, not copied out of
        the issue: the issue records what a poll saw earlier, and what matters
        is what the gateway presents at the moment the user vouches for it.
        With the new certificate in hand the gateway is asked for its serial
        (the first request to carry the token to that certificate — which is
        what the user just agreed to) and a serial that contradicts the one
        recorded on the entry stops the flow: that is a *different* gateway,
        which reconfigure is for, not a re-pin. Firmware without the serial
        parameter, or a token the (reset) gateway no longer accepts, reads as
        "unknown" and proceeds — the reauth flow takes over from there.

        The new fingerprint is written and the entry reloaded explicitly: the
        entry's update listener reloads on host/token/options only, and an
        entry that failed its first refresh on the mismatch sits in
        SETUP_RETRY with no listener at all.
        """
        errors: dict[str, str] = {}
        entry = self._entry
        if user_input is not None:
            host = str(entry.data[CONF_HOST])
            session = async_get_clientsession(self.hass, verify_ssl=False)
            try:
                fingerprint = await async_learn_fingerprint(session, host)
            except (TimeoutError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                serial = await async_fetch_serial(
                    self.hass, host, str(entry.data.get(CONF_TOKEN, "")), fingerprint
                )
                recorded = entry.data.get(CONF_SERIAL)
                if recorded and serial and serial != recorded:
                    errors["base"] = "different_gateway"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_TLS_FINGERPRINT: fingerprint},
                    )
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_create_entry(data={})

        placeholders: dict[str, str] = {
            "host": str(entry.data.get(CONF_HOST, "")),
            "expected": format_fingerprint(
                str(entry.data.get(CONF_TLS_FINGERPRINT, ""))
            ),
            "observed": "?",
        }
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        if issue is not None and issue.translation_placeholders:
            placeholders.update(issue.translation_placeholders)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return the fix flow for one of this integration's issues.

    Only the certificate issue is fixable; its ``data`` names the entry. An
    issue whose entry is gone (removed while the issue stood) degrades to
    Home Assistant's plain confirm flow, which simply dismisses it.
    """
    entry_id = data.get("entry_id") if data else None
    entry = (
        hass.config_entries.async_get_entry(str(entry_id))
        if isinstance(entry_id, str)
        else None
    )
    if issue_id.startswith(ISSUE_TLS_MISMATCH) and entry is not None:
        return TlsCertificateChangedFlow(entry)
    return ConfirmRepairFlow()
