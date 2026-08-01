"""Config flow for the Jung Home integration."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_TOKEN, Platform
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_INVERTED_COVERS, DOMAIN, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# The gateway blocks the register request until the user approves it in the app.
# Its server-side timeout is 180s (register_timeout_ms); give the client a little
# more so the server's own timeout/response wins.
REGISTER_TIMEOUT = 190
REGISTER_USER_NAME = "Home Assistant"

# Reachability check on the reconfigure form. Kept short: the user is sitting in
# front of the dialog, and an unreachable host should fail fast enough that
# retyping it feels immediate.
PROBE_TIMEOUT = 10

# The gateway's generic mDNS hostname (also its TLS certificate CN). Offered as
# the default host so most users need not look up the gateway's IP. It only
# resolves if the network maps it; the reliable name is the per-device mDNS
# hostname (junghome-<mac>.local) that discovery supplies, and an IP always works.
MDNS_DEFAULT_HOST = "junghome.local"

_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# Host-only schema, reused by the app-approval and reconfigure steps.
STEP_HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})
# Host + network-key password, for instant setup.
STEP_HOST_PASSWORD_SCHEMA = vol.Schema(
    {vol.Required(CONF_HOST): str, vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}
)


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


def _cover_choices(coordinator: JungHomeDataUpdateCoordinator) -> dict[str, str]:
    """Map cover stable unique_id -> device label for the options selector.

    Mirrors cover.py discovery (Position/PositionAndAngle with a level datapoint)
    so the options flow lists exactly the covers the platform creates.
    """
    choices: dict[str, str] = {}
    for device in coordinator.data or []:
        if device.get("type") not in ("Position", "PositionAndAngle"):
            continue
        level_dp = next(
            (dp for dp in device.get("datapoints", []) if dp.get("type") == "level"),
            None,
        )
        if level_dp is None:
            continue
        uid = stable_unique_id(device, level_dp)
        choices[uid] = device.get("label") or uid
    return choices


class JungHomeOptionsFlow(config_entries.OptionsFlow):
    """Options: flag covers whose reported position is inverted (e.g. awnings)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/persist the set of inverted covers."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_INVERTED_COVERS: user_input.get(CONF_INVERTED_COVERS, [])}
            )

        entry: JungHomeConfigEntry = self.config_entry
        coordinator = getattr(entry, "runtime_data", None)
        choices = _cover_choices(coordinator) if coordinator is not None else {}
        # Reconcile the stored flags against reality. A flagged cover missing from
        # the current poll may only be offline, so keep it (labelled by its uid)
        # if its entity still exists — saving must not silently clear it. But a
        # uid with no live cover *and* no registered entity is orphaned: its
        # device was removed or relabelled, which changes the label-derived
        # unique_id, so the platform registered a fresh cover and the stale one
        # was pruned. The old code resurrected such orphans as a permanent,
        # un-removable raw-slug row here; drop them so the list matches the
        # covers that actually exist.
        registered_covers = {
            registry_entry.unique_id
            for registry_entry in er.async_entries_for_config_entry(
                er.async_get(self.hass), entry.entry_id
            )
            if registry_entry.domain == Platform.COVER
        }
        current: list[str] = []
        for uid in entry.options.get(CONF_INVERTED_COVERS, []):
            if uid in choices:
                current.append(uid)
            elif uid in registered_covers:
                choices.setdefault(uid, uid)
                current.append(uid)
            # A uid in neither set is orphaned: intentionally left out of both
            # lists here, so it is removed from the stored option on save.
        if not choices:
            return self.async_abort(reason="no_covers")

        options = [
            selector.SelectOptionDict(value=uid, label=label)
            for uid, label in sorted(choices.items(), key=lambda kv: kv[1].lower())
        ]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_INVERTED_COVERS, default=current
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class JungHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jung Home."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: JungHomeConfigEntry,
    ) -> JungHomeOptionsFlow:
        """Return the options flow handler."""
        return JungHomeOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str | None = None
        self._token: str | None = None
        self._error: str = "register_failed"
        self._register_task: asyncio.Task[str] | None = None
        # True once the host is known from discovery, so the method steps skip
        # asking for it again.
        self._discovered: bool = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick how to connect to the gateway.

        Gateways are normally found automatically (mDNS) and shown on the
        Integrations page. This menu is the manual fallback and offers the two
        ways to obtain a token: approving the request in the app, or entering the
        gateway's network-key password for an instant connection.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=["app_approval", "password"],
        )

    async def async_step_app_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect by approving the access request in the Jung Home app.

        Shows the host field (pre-filled with the discovered address when the
        gateway was found automatically) so the user can confirm or edit it.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_apply_host(user_input[CONF_HOST])
            if error is None:
                return await self.async_step_register()
            errors["base"] = error

        return self.async_show_form(
            step_id="app_approval",
            data_schema=self._host_schema(user_input),
            errors=errors,
        )

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect instantly using the gateway's network-key password.

        Shows the host field (pre-filled with the discovered address when the
        gateway was found automatically) alongside the password.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_apply_host(user_input[CONF_HOST])
            if error is not None:
                errors["base"] = error
            else:
                try:
                    self._token = await self._async_register_by_password(
                        user_input[CONF_PASSWORD]
                    )
                except CannotRegister:
                    errors["base"] = self._error
                else:
                    return await self.async_step_finish()

        return self.async_show_form(
            step_id="password",
            data_schema=self._password_schema(user_input),
            errors=errors,
        )

    async def _async_apply_host(self, raw_host: str) -> str | None:
        """Normalise the host from a form and claim identity where needed.

        Returns an error key to show, or None on success. A discovered gateway
        already set its unique_id to the stable mDNS hostname during discovery, so
        we keep that and only record the confirmed (or edited) host; a manually
        entered host becomes the unique_id and aborts if already configured.
        """
        host = _normalize_host(raw_host)
        if not host:
            return "invalid_host"
        # unique_id alone does not catch every duplicate: a gateway discovered
        # over mDNS is keyed by its *hostname*, so typing that same gateway's IP
        # here would claim a different unique_id and add it a second time. Match
        # on the stored host too — the same check `async_step_zeroconf` and
        # `async_step_reconfigure` already apply from the other direction.
        if any(
            entry.data.get(CONF_HOST) == host for entry in self._async_current_entries()
        ):
            raise AbortFlow("already_configured")
        self._host = host
        # Populate the {host} flow_title placeholder for later steps.
        self.context["title_placeholders"] = {"host": host}
        if not self._discovered:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
        return None

    def _host_default(self, user_input: dict[str, Any] | None) -> str:
        """Return the host to pre-fill: retyped value, else discovered, else mDNS.

        Order: a value the user just typed (kept across a retry), then the
        discovered address, then the generic mDNS default.
        """
        if user_input and user_input.get(CONF_HOST):
            return str(user_input[CONF_HOST])
        if self._discovered and self._host:
            return self._host
        return MDNS_DEFAULT_HOST

    def _host_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Host-only form with the host pre-filled."""
        return self.add_suggested_values_to_schema(
            STEP_HOST_SCHEMA, {CONF_HOST: self._host_default(user_input)}
        )

    def _password_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Host + password form with the host pre-filled.

        The password is never suggested back — it is a credential, and a retry is
        usually *because* it was wrong.
        """
        return self.add_suggested_values_to_schema(
            STEP_HOST_PASSWORD_SCHEMA, {CONF_HOST: self._host_default(user_input)}
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
        self.context["title_placeholders"] = {"host": self._host}
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

        The new address is probed before it is stored (connect-then-commit): a
        typo used to be accepted silently and only surfaced later as a confusing
        connect/reauth failure, with nothing tying it back to this edit.
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
            elif probe_error := await self._async_probe_host(
                host, entry.data.get(CONF_TOKEN, "")
            ):
                errors["base"] = probe_error
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
                STEP_HOST_SCHEMA, {CONF_HOST: entry.data.get(CONF_HOST)}
            ),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a gateway discovered via mDNS (_junghome._tcp)."""
        self._host = discovery_info.host
        self._discovered = True
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
        """Let the user choose how to connect to the discovered gateway."""
        return self.async_show_menu(
            step_id="zeroconf_confirm",
            menu_options=["app_approval", "password"],
            description_placeholders={"host": self._host or ""},
        )

    async def _async_probe_host(self, host: str, token: str) -> str | None:
        """Check that a Jung Home gateway answers at ``host``.

        Returns an error key to show, or None when the address is usable.

        This deliberately tests **reachability only**: any HTTP reply proves a
        gateway is listening and the address is typed correctly, so a 401/403 is
        accepted here. A rejected token means the address now points at a
        different gateway (or the token was revoked), which the reauth flow
        already handles on the next refresh — failing the form for it would just
        strand the user on a screen that cannot fix it.
        """
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/functions"
        headers = {"token": token, "Content-Type": "application/json"}
        try:
            async with (
                asyncio.timeout(PROBE_TIMEOUT),
                session.get(url, headers=headers),
            ):
                # No raise_for_status(): a status-based failure still means the
                # host is reachable, which is all this probe asserts.
                return None
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Jung Home gateway probe failed for %s: %s", host, err)
            return "cannot_connect"

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

    async def _async_register_by_password(self, password: str) -> str:
        """Exchange the gateway's network-key password for a token immediately.

        Unlike ``_async_register`` this does not block on in-app approval: the
        gateway's ``register/by-password`` endpoint returns a token right away,
        or ``401`` if the password is wrong.
        """
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{self._host}/api/junghome/register/by-password"
        try:
            async with (
                asyncio.timeout(30),
                session.post(url, json={"password": password}) as response,
            ):
                if response.status == 401:
                    self._error = "invalid_auth"
                    raise CannotRegister("Wrong password")
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
