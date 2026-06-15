"""Config flow for the Jung Home integration."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The gateway blocks the register request until the user approves it in the app.
# Its server-side timeout is 180s (register_timeout_ms); give the client a little
# more so the server's own timeout/response wins.
REGISTER_TIMEOUT = 190
REGISTER_USER_NAME = "Home Assistant"

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


class CannotRegister(Exception):
    """Raised when the gateway does not return a token."""


def _normalize_host(host: str) -> str:
    """Normalise a user-entered host (scheme/whitespace/slash/case).

    Hosts and hostnames are case-insensitive, so lower-casing keeps a manually
    entered hostname and the lower-case mDNS hostname from looking like two
    different gateways.
    """
    host = host.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    return host.rstrip("/").lower()


class JungHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jung Home."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str | None = None
        self._token: str | None = None
        self._error: str = "register_failed"
        self._register_task: asyncio.Task[str] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the gateway host, then start registration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = _normalize_host(user_input[CONF_HOST])
            if not self._host:
                errors["base"] = "invalid_host"
            else:
                await self.async_set_unique_id(self._host)
                self._abort_if_unique_id_configured()
                return await self.async_step_register()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the user to approve the access request in the Jung Home app."""
        if self._register_task is None:
            self._register_task = self.hass.async_create_task(self._async_register())

        if not self._register_task.done():
            return self.async_show_progress(
                step_id="register",
                progress_action="waiting_for_approval",
                progress_task=self._register_task,
            )

        try:
            self._token = self._register_task.result()
        except CannotRegister:
            self._register_task = None
            return self.async_show_progress_done(next_step_id="register_failed")

        self._register_task = None
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the config entry once a token has been obtained."""
        return self.async_create_entry(
            title="Jung Home",
            data={CONF_HOST: self._host, CONF_TOKEN: self._token},
        )

    async def async_step_register_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the failure reason and allow the user to retry."""
        if user_input is not None:
            return await self.async_step_register()
        return self.async_show_form(
            step_id="register_failed",
            data_schema=vol.Schema({}),
            errors={"base": self._error},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when the gateway rejects the stored token."""
        self._host = entry_data[CONF_HOST]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-register with the gateway to obtain a fresh token."""
        if self._register_task is None:
            self._register_task = self.hass.async_create_task(self._async_register())

        if not self._register_task.done():
            return self.async_show_progress(
                step_id="reauth_confirm",
                progress_action="waiting_for_approval",
                progress_task=self._register_task,
            )

        try:
            self._token = self._register_task.result()
        except CannotRegister:
            self._register_task = None
            return self.async_show_progress_done(next_step_id="reauth_failed")

        self._register_task = None
        return self.async_show_progress_done(next_step_id="reauth_finish")

    async def async_step_reauth_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the fresh token on the existing entry and reload it."""
        return self.async_update_reload_and_abort(
            self._get_reauth_entry(),
            data_updates={CONF_TOKEN: self._token},
        )

    async def async_step_reauth_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the failure reason and allow retrying the reauth."""
        if user_input is not None:
            return await self.async_step_reauth_confirm()
        return self.async_show_form(
            step_id="reauth_failed",
            data_schema=vol.Schema({}),
            errors={"base": self._error},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user update the gateway address (e.g. after an IP change).

        The existing token still works for the same gateway at a new address; if
        it points at a different gateway, the next refresh triggers reauth.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = _normalize_host(user_input[CONF_HOST])
            if not host:
                errors["base"] = "invalid_host"
            elif any(
                other.entry_id != entry.entry_id and other.data.get(CONF_HOST) == host
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            else:
                # Update the stored host and let the `add_update_listener` reload
                # the entry exactly once (the host change makes its guard pass).
                # Using async_update_reload_and_abort here would schedule a second,
                # redundant reload on top of the listener's. The entry keeps its
                # existing unique_id (the manual host or the zeroconf hostname) so
                # a later mDNS rediscovery still matches it instead of surfacing a
                # duplicate.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_HOST: host}
                )
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, {CONF_HOST: entry.data.get(CONF_HOST)}
            ),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a gateway discovered via mDNS (_junghome._tcp)."""
        self._host = discovery_info.host
        hostname = (discovery_info.hostname or "").rstrip(".") or self._host
        # Stable per-gateway id; update the stored host if its IP changed.
        await self.async_set_unique_id(hostname)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})
        # Also skip gateways already added manually under a different unique id.
        if any(
            entry.data.get(CONF_HOST) in (self._host, hostname)
            for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")
        self.context["title_placeholders"] = {"host": hostname}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of a discovered gateway, then register."""
        if user_input is None:
            return self.async_show_form(
                step_id="zeroconf_confirm",
                description_placeholders={"host": self._host or ""},
            )
        return await self.async_step_register()

    async def _async_register(self) -> str:
        """POST the registration request and return the issued token.

        Blocks until the user approves the request in the app or the gateway
        times out (~180s).
        """
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{self._host}/api/junghome/register"
        timeout = aiohttp.ClientTimeout(total=REGISTER_TIMEOUT)
        try:
            async with session.post(
                url, json={"user_name": REGISTER_USER_NAME}, timeout=timeout
            ) as response:
                if response.status != 200:
                    self._error = "register_failed"
                    raise CannotRegister(f"HTTP {response.status}")
                data = await response.json()
        except (TimeoutError, aiohttp.ClientError) as err:
            self._error = "cannot_connect"
            raise CannotRegister(str(err)) from err

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            self._error = "register_failed"
            raise CannotRegister("No token in response")
        return str(token)
