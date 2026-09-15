"""Config flow for the Jung Home integration."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_IDENTITY_ANCHOR,
    CONF_INVERTED_COVERS,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    CONF_SUPPRESS_DUPLICATE_PRESSES,
    CONF_TLS_FINGERPRINT,
    DEFAULT_SUPPRESS_DUPLICATE_PRESSES,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    entry_anchor,
    stable_unique_id,
)
from .coordinator import (
    JungHomeConfigEntry,
    JungHomeDataUpdateCoordinator,
    poll_interval_from_options,
)
from .tls import (
    async_learn_fingerprint,
    fingerprint_ssl,
    format_fingerprint,
    normalize_fingerprint,
)

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


async def async_fetch_serial(
    hass: HomeAssistant, host: str, token: str, fingerprint: str
) -> str | None:
    """Best-effort fetch of the gateway's hardware serial over REST.

    ``GET /config/parameter/system_serial`` returns the raw serial string
    (the same cpuinfo-derived value the mDNS TXT record advertises; the
    firmware marks the parameter read-only). Requires a valid token, so the
    request is pinned to ``fingerprint`` — the caller has established which
    certificate it is willing to send the token to before asking. Returns
    None on any failure — an unreachable endpoint, older firmware (404), a
    rejected token, a certificate mismatch, or an empty value (the middleware
    populates it asynchronously after boot) — so every caller falls back to
    the legacy host-based keying rather than blocking on identity. Shared
    with the certificate repair flow (``repairs.py``).
    """
    session = async_get_clientsession(hass, verify_ssl=False)
    url = f"https://{host}/api/junghome/config/parameter/system_serial"
    headers = {"token": token}
    try:
        async with (
            asyncio.timeout(PROBE_TIMEOUT),
            session.get(
                url, headers=headers, ssl=fingerprint_ssl(fingerprint)
            ) as response,
        ):
            if response.status != 200:
                return None
            data = await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        _LOGGER.debug("Could not fetch gateway serial from %s: %s", host, err)
        return None
    if isinstance(data, str) and data.strip():
        return data.strip()
    return None


def _txt_property(properties: Mapping[str, Any], key: str) -> str | None:
    """Extract one string mDNS TXT property, or None when absent/empty.

    zeroconf hands TXT values through as str or bytes depending on the
    resolver path, so both are tolerated.
    """
    raw = properties.get(key)
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            return raw
    return None


def _serial_from_properties(properties: Mapping[str, Any]) -> str | None:
    """Extract the gateway hardware serial from mDNS TXT properties.

    The gateway's `_junghome._tcp` service carries `serial=<16 hex>` (verified
    against the firmware's avahi service definition, alongside `mac=` and
    `version=`). Returns None when absent/empty so callers can fall back to
    the legacy hostname keying for firmware that does not advertise it.
    """
    return _txt_property(properties, "serial")


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
    """Options: poll interval, duplicate-press suppression, inverted covers."""

    def _reconciled_cover_flags(self) -> tuple[dict[str, str], list[str]]:
        """Return (selectable covers, currently flagged) reconciled with reality.

        A flagged cover missing from the current poll may only be offline, so
        keep it (labelled by its uid) if its entity still exists — saving must
        not silently clear it. But a uid with no live cover *and* no registered
        entity is orphaned: its device was removed or relabelled, which changes
        the label-derived unique_id, so the platform registered a fresh cover
        and the stale one was pruned. The old code resurrected such orphans as
        a permanent, un-removable raw-slug row here; drop them so the list
        matches the covers that actually exist. Used by both the form build
        and the no-covers save path, so the two can never disagree about which
        flags survive.
        """
        entry: JungHomeConfigEntry = self.config_entry
        coordinator = getattr(entry, "runtime_data", None)
        choices = _cover_choices(coordinator) if coordinator is not None else {}
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
        return choices, current

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/persist the options.

        Every option is read by the coordinator or a platform at setup, so a
        change takes effect through the entry reload the update listener in
        ``__init__`` performs on any options change.
        """
        if user_input is not None:
            new_options: dict[str, Any] = {
                CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                CONF_SUPPRESS_DUPLICATE_PRESSES: bool(
                    user_input[CONF_SUPPRESS_DUPLICATE_PRESSES]
                ),
            }
            if CONF_INVERTED_COVERS in user_input:
                new_options[CONF_INVERTED_COVERS] = user_input[CONF_INVERTED_COVERS]
            else:
                # The covers selector was not part of the form (no covers to
                # flag — voluptuous fills the default for a shown-but-empty
                # field, so a missing key can only mean it wasn't shown). Its
                # absence is not a deselection: re-run the same reconciliation
                # the form build uses, so saving the interval keeps exactly
                # the flags the form would have offered — and still drops
                # orphaned ones.
                _, current = self._reconciled_cover_flags()
                new_options[CONF_INVERTED_COVERS] = current
            return self.async_create_entry(data=new_options)

        choices, current = self._reconciled_cover_flags()
        # The interval field is always shown — the covers selector only when
        # there is a cover to flag. (This step used to abort with "no covers",
        # which would now lock cover-less installs out of the poll interval.)
        schema_fields: dict[Any, Any] = {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=poll_interval_from_options(self.config_entry.options),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_POLL_INTERVAL_SECONDS,
                    max=MAX_POLL_INTERVAL_SECONDS,
                    step=1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            # Button duplicate suppression (event.py). Default on: current
            # device firmware reports every tap twice. Off is for older device
            # firmware whose users double-tap faster than the 1.2 s window.
            vol.Required(
                CONF_SUPPRESS_DUPLICATE_PRESSES,
                default=bool(
                    self.config_entry.options.get(
                        CONF_SUPPRESS_DUPLICATE_PRESSES,
                        DEFAULT_SUPPRESS_DUPLICATE_PRESSES,
                    )
                ),
            ): selector.BooleanSelector(),
        }
        if choices:
            options = [
                selector.SelectOptionDict(value=uid, label=label)
                for uid, label in sorted(choices.items(), key=lambda kv: kv[1].lower())
            ]
            schema_fields[vol.Optional(CONF_INVERTED_COVERS, default=current)] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema_fields)
        )


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
        # The gateway hardware serial, when known (mDNS TXT record). Manual
        # flows learn it over REST at finish time instead.
        self._serial: str | None = None
        # The gateway firmware version from the same TXT record; shown in the
        # discovery confirm dialog so multi-gateway households can tell which
        # gateway they are approving. Display-only, never part of identity.
        self._txt_version: str | None = None
        # The SHA-256 fingerprint of the certificate the gateway at ``_host``
        # presents, learned (a bare TLS handshake, nothing sent) before the
        # first request that carries anything — the registration password, or
        # the token — so that request is already pinned to it. Stored on the
        # entry as CONF_TLS_FINGERPRINT (see tls.py). Reauth seeds it from the
        # entry so the fresh registration goes only to the pinned gateway.
        self._fingerprint: str | None = None
        # Reconfigure: a new address whose certificate differs from the pinned
        # one is parked here while the user is asked to confirm it.
        self._pending_host: str | None = None
        self._pending_fingerprint: str | None = None

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
        # Pin before anything is sent: the registration that follows carries
        # the network-key password or receives the token, and both must reach
        # exactly the certificate learned here. A host that cannot complete a
        # TLS handshake is not a reachable gateway, so it fails the form now.
        self._fingerprint = None
        try:
            self._fingerprint = await self._async_learn_fingerprint(host)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Could not read the certificate of %s: %s", host, err)
            return "cannot_connect"
        return None

    async def _async_learn_fingerprint(self, host: str) -> str:
        """Learn the certificate fingerprint ``host`` presents (see tls.py).

        A patchable seam over the module helper, like ``_async_fetch_serial``:
        the flow tests stub it so no form submission opens a socket.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        return await async_learn_fingerprint(session, host)

    async def _async_ssl(self) -> aiohttp.Fingerprint:
        """Return the ``ssl=`` pin for a request to ``_host``.

        Learns the fingerprint first if no step has yet (the registration
        helpers are also driven directly by tests); every request this flow
        sends is pinned, without exception.
        """
        if self._fingerprint is None:
            self._fingerprint = await self._async_learn_fingerprint(self._host or "")
        return fingerprint_ssl(self._fingerprint)

    @callback
    def _async_update_and_reload(
        self,
        entry: JungHomeConfigEntry,
        *,
        data: dict[str, Any],
        unique_id: str | None = None,
        only_if_failing: bool = False,
    ) -> None:
        """Write ``data`` (and re-key, if asked) and get the entry reloaded once.

        The single place every flow-side entry update goes through, so they
        all reload the same way: a loaded entry carries an update listener
        (``__init__`` registers one at the end of every successful setup)
        that reloads on a host/token/options change, and HA's own
        ``updates=``/``..._reload_...`` helpers would stack a second reload on
        top of it (deprecated in HA 2026.6, an error from 2026.12). An entry
        that is not loaded has no listener, so the reload is scheduled here
        (which also cancels a pending retry timer) — for any non-loaded state
        when the user asked for the change (reconfigure), but with
        ``only_if_failing`` only for SETUP_RETRY/SETUP_ERROR: a discovery or
        a duplicate-detection update must not set up an entry Home Assistant
        has not loaded (disabled, or still starting) on its own initiative.
        Whether the listener will run is decided BEFORE the write: it is
        dispatched eagerly from inside ``async_update_entry`` and has already
        moved a loaded entry to UNLOAD_IN_PROGRESS by the time the call
        returns.
        """
        listener_will_reload = entry.state is ConfigEntryState.LOADED
        if unique_id is not None:
            changed = self.hass.config_entries.async_update_entry(
                entry, data=data, unique_id=unique_id
            )
        else:
            changed = self.hass.config_entries.async_update_entry(entry, data=data)
        if not changed or listener_will_reload:
            return
        if only_if_failing and entry.state not in (
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_ERROR,
        ):
            return
        self.hass.config_entries.async_schedule_reload(entry.entry_id)

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
        """Create the config entry once a token has been obtained.

        With a token in hand this is the first moment a *manual* flow can learn
        the gateway's hardware serial (REST `config/parameter/system_serial`) —
        discovery flows already carry it from the mDNS TXT record. When known,
        the serial becomes the entry's ``unique_id``: it is the only identifier
        that survives IP changes, re-provisioning and firmware updates, and it
        is what lets a later discovery update this entry's host and lets
        reconfigure refuse a different gateway. Firmware that does not expose
        the serial falls back to the legacy host keying.
        """
        serial = self._serial
        if self._fingerprint is None and self._host is not None:
            # Every path here learned the pin before registering; a stubbed
            # registration is the only way to arrive without one.
            self._fingerprint = await self._async_learn_fingerprint(self._host)
        if serial is None and self._host is not None and self._token is not None:
            serial = await self._async_fetch_serial(
                self._host, self._token, self._fingerprint
            )
        if serial is not None and serial != self.unique_id:
            # Typing the address of an already-configured gateway now surfaces
            # here (the host-based duplicate checks can't see through an IP
            # change): update that entry's host and bow out — but only if the
            # responder presents the certificate that entry pinned. The serial
            # came from the responder itself, so an arbitrary HTTPS host could
            # claim any serial; the certificate is what proves it is the same
            # gateway. An entry that never pinned adopts the host and the pin
            # together (its first pin, on user action). On a mismatch the
            # existing entry is left untouched: a genuinely regenerated
            # certificate is confirmed through reconfigure or the repair
            # issue, never by an unverified claim.
            await self.async_set_unique_id(serial)
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, serial
            )
            if existing is not None and existing.source != config_entries.SOURCE_IGNORE:
                pinned = normalize_fingerprint(existing.data.get(CONF_TLS_FINGERPRINT))
                if pinned in (None, self._fingerprint):
                    self._async_update_and_reload(
                        existing,
                        data={
                            **existing.data,
                            CONF_HOST: self._host,
                            CONF_TLS_FINGERPRINT: self._fingerprint,
                        },
                        only_if_failing=True,
                    )
                else:
                    _LOGGER.warning(
                        "%s reports the serial of the configured Jung Home "
                        "gateway but presents a different TLS certificate; "
                        "not adopting the address",
                        self._host,
                    )
            self._abort_if_unique_id_configured()
        # Freeze the identity anchor at creation (see const.entry_anchor): ids
        # derived from the entry (hub device, scene scope) must never change,
        # even if the unique_id is migrated later.
        data: dict[str, Any] = {
            CONF_HOST: self._host,
            CONF_TOKEN: self._token,
            CONF_IDENTITY_ANCHOR: self.unique_id or self._host,
            CONF_TLS_FINGERPRINT: self._fingerprint,
        }
        if serial is not None:
            data[CONF_SERIAL] = serial
        # Carry the host in the title so two gateways are distinguishable in
        # the entries list (every entry used to be titled just "Jung Home").
        # Existing entries keep their title; this only affects new entries.
        return self.async_create_entry(
            title=f"Jung Home ({self._host})",
            data=data,
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
        """Start reauth when the gateway rejects the stored token.

        The registration goes to the certificate the entry pinned: a
        rejected token is a reason to re-register with *that* gateway, never
        with whatever answers at the address now. An entry without a pin yet
        (created before pinning existed, never successfully connected since)
        learns one here — its first contact — like any first registration.
        """
        self._host = entry_data[CONF_HOST]
        self._fingerprint = normalize_fingerprint(entry_data.get(CONF_TLS_FINGERPRINT))
        self.context["title_placeholders"] = {"host": self._host}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-register with the gateway to obtain a fresh token.

        Shows a confirm form before the first registration attempt: reauth
        flows are created programmatically (the user has not even seen the
        notification yet), and ``_async_register`` opens the gateway's one
        180 s app-approval window the moment it runs — starting it unattended
        burned that window before the user could possibly approve, so the
        first attempt they actually saw was already the retry.
        """
        if self._register_task is None:
            if user_input is None:
                return self.async_show_form(
                    step_id="reauth_confirm", data_schema=vol.Schema({})
                )
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
        """Store the fresh token on the existing entry and get it reloaded.

        Deliberately ``async_update_and_abort``, not the ``..._reload_...``
        variant: a loaded entry carries an update listener (``__init__``
        registers one at the end of every successful setup), and combining
        the two is deprecated since HA 2026.6 and raises from 2026.12 — the
        raise lands *before* the scheduled reload, so the fresh token would be
        stored while the coordinator kept the rejected one, looping reauth
        until a restart. The reconfigure and zeroconf paths already
        update-and-let-the-listener-reload; this was the last one that did not.

        That listener only exists while the entry is LOADED, though. A token
        rejected on the *first* refresh raises ``ConfigEntryAuthFailed`` before
        setup ever reaches the line that registers it, leaving the entry in
        SETUP_ERROR with no listener — so the update below changed
        ``entry.data`` and nothing else, and the flow reported success while
        the entry stayed dead until a restart. Schedule the reload explicitly
        in that case, as reconfigure does for SETUP_RETRY; never for a loaded
        entry, where it would stack a second reload on the listener's. The
        state is read BEFORE the update: the listener is dispatched eagerly
        from inside ``async_update_entry`` and has already moved a loaded
        entry to UNLOAD_IN_PROGRESS by the time the update returns, so a
        check afterwards mistakes the listener's own reload for "not loaded".
        """
        entry = self._get_reauth_entry()
        listener_will_reload = entry.state is ConfigEntryState.LOADED
        # The pin the registration just went to: unchanged for a pinned
        # entry, learned on this first contact for one that had none.
        data_updates: dict[str, Any] = {CONF_TOKEN: self._token}
        if self._fingerprint is not None:
            data_updates[CONF_TLS_FINGERPRINT] = self._fingerprint
        result = self.async_update_and_abort(entry, data_updates=data_updates)
        if not listener_will_reload:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    async def async_step_reauth_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the failure reason and allow retrying the reauth."""
        if user_input is not None:
            # Pass the (non-None) input through so the retry starts
            # immediately — the user just pressed submit on the failure form;
            # showing the confirm form again would be a pointless extra click.
            return await self.async_step_reauth_confirm(user_input)
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

        The new address is checked before it is stored (connect-then-commit):
        a typo used to be accepted silently and only surfaced later as a
        confusing connect/reauth failure, with nothing tying it back to this
        edit. The order of the checks is the security-relevant part — nothing
        that carries the token goes out before the responder's identity is
        established:

        1. Its certificate is read (a bare TLS handshake, nothing sent). One
           that matches the entry's pin proves the same gateway (it holds
           the private key), and the flow commits. One that differs is a
           different device *or* a gateway whose certificate was regenerated
           — indistinguishable without sending the token — so the user is
           asked to confirm (``reconfigure_certificate``) before anything
           else happens. An entry with no pin yet trusts this first contact,
           exactly as it would on its next connect.
        2. Reachability is probed with the unauthenticated ``version``
           endpoint, pinned, no token.
        3. Only then the serial (a token-carrying read, pinned) — the
           belt-and-braces identity check against the recorded serial.
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
                try:
                    observed = await self._async_learn_fingerprint(host)
                except (TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("Could not read the certificate of %s: %s", host, err)
                    errors["base"] = "cannot_connect"
                else:
                    pinned = normalize_fingerprint(entry.data.get(CONF_TLS_FINGERPRINT))
                    if pinned is not None and observed != pinned:
                        self._pending_host = host
                        self._pending_fingerprint = observed
                        return await self.async_step_reconfigure_certificate()
                    errors = await self._async_commit_reconfigure(entry, host, observed)
                    if not errors:
                        return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_HOST_SCHEMA, {CONF_HOST: entry.data.get(CONF_HOST)}
            ),
            errors=errors,
        )

    async def async_step_reconfigure_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before trusting a new address that presents a different certificate.

        Reached only from ``async_step_reconfigure``. Until the user confirms,
        nothing has been sent to the new address but a TLS handshake; on
        confirm the flow commits with the new certificate as the pin, and the
        serial check inside the commit still refuses a responder that turns
        out to be a different gateway.
        """
        entry = self._get_reconfigure_entry()
        host, observed = self._pending_host, self._pending_fingerprint
        if host is None or observed is None:  # pragma: no cover - guarded by the caller
            return await self.async_step_reconfigure()
        if user_input is not None:
            errors = await self._async_commit_reconfigure(entry, host, observed)
            if not errors:
                return self.async_abort(reason="reconfigure_successful")
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(
                    STEP_HOST_SCHEMA, {CONF_HOST: host}
                ),
                errors=errors,
            )
        pinned = normalize_fingerprint(entry.data.get(CONF_TLS_FINGERPRINT)) or ""
        return self.async_show_form(
            step_id="reconfigure_certificate",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": host,
                "expected": format_fingerprint(pinned),
                "observed": format_fingerprint(observed),
            },
        )

    async def _async_commit_reconfigure(
        self, entry: JungHomeConfigEntry, host: str, fingerprint: str
    ) -> dict[str, str]:
        """Probe, identity-check and store a reconfigured address.

        Returns the form errors to show, empty on success (the entry has been
        updated and its reload arranged). ``fingerprint`` is the pin every
        request here goes to — the entry's own, or the one the user just
        confirmed for the new address.
        """
        if probe_error := await self._async_probe_host(host, fingerprint):
            return {"base": probe_error}
        # Identity check: any HTTPS responder passes the reachability probe,
        # so ask the gateway who it is. A mismatch against the recorded
        # serial means the address points at a DIFFERENT gateway — committing
        # it would only surface later as a confusing reauth, so fail the form
        # now instead.
        serial = await self._async_fetch_serial(
            host, entry.data.get(CONF_TOKEN, ""), fingerprint
        )
        recorded = entry.data.get(CONF_SERIAL)
        if recorded and serial and serial != recorded:
            return {"base": "different_gateway"}
        # An entry with no recorded serial (legacy, or firmware without the
        # endpoint) is migrated onto the serial we just learned — unless
        # another entry already owns it.
        conflict = (
            self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial)
            if serial is not None
            else None
        )
        if conflict is not None and conflict.entry_id != entry.entry_id:
            raise AbortFlow("already_configured")
        new_data = {**entry.data, CONF_HOST: host, CONF_TLS_FINGERPRINT: fingerprint}
        if serial is not None:
            # Freeze the anchor BEFORE the unique_id changes so the hub device
            # and scene ids stay put (see const.entry_anchor).
            new_data.setdefault(CONF_IDENTITY_ANCHOR, entry_anchor(entry))
            new_data[CONF_SERIAL] = serial
        self._async_update_and_reload(
            entry, data=new_data, unique_id=serial if serial is not None else None
        )
        return {}

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a gateway discovered via mDNS (_junghome._tcp).

        The TXT record carries the gateway's hardware serial, which is the
        preferred ``unique_id``: unlike the hostname or IP it survives network
        changes and identifies the *device*, so an IP change can update the
        stored host no matter how the entry was originally added. A discovery
        that matches an entry still keyed the legacy way (mDNS hostname, or
        the manually typed host) migrates that entry to the serial in place —
        freezing its identity anchor first so the hub device and scene ids do
        not change (see ``const.entry_anchor``).

        An mDNS packet is unauthenticated, though, and the serial it carries
        is public (the gateway broadcasts it): one forged packet naming a
        configured serial used to move that entry's host — and with it the
        next poll, token and all — to any address on the LAN. So a discovery
        never rewrites anything on its own authority; what it may change is
        decided by ``_async_adopt_discovery``.
        """
        self._host = discovery_info.host
        self._discovered = True
        hostname = (discovery_info.hostname or "").rstrip(".") or self._host
        self._serial = _serial_from_properties(discovery_info.properties)
        self._txt_version = _txt_property(discovery_info.properties, "version")
        if self._serial is not None:
            await self.async_set_unique_id(self._serial)
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, self._serial
            )
            if existing is not None:
                if existing.source != config_entries.SOURCE_IGNORE:
                    await self._async_adopt_discovery(existing, serial=None)
                return self.async_abort(reason="already_configured")
            # A legacy-keyed entry for this same gateway: adopt it onto the
            # serial rather than offering a duplicate discovery.
            legacy = self._async_find_legacy_entry(hostname)
            if legacy is not None:
                await self._async_adopt_discovery(legacy, serial=self._serial)
                return self.async_abort(reason="already_configured")
        else:
            # Firmware without a serial TXT record: legacy hostname keying.
            await self.async_set_unique_id(hostname)
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, hostname
            )
            if existing is not None:
                if existing.source != config_entries.SOURCE_IGNORE:
                    await self._async_adopt_discovery(existing, serial=None)
                return self.async_abort(reason="already_configured")
            # Also skip gateways added manually under a different unique id.
            if any(
                entry.data.get(CONF_HOST) in (self._host, hostname)
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
        self.context["title_placeholders"] = {"host": hostname}
        return await self.async_step_zeroconf_confirm()

    @callback
    def _async_find_legacy_entry(self, hostname: str) -> JungHomeConfigEntry | None:
        """Find an entry for this gateway still keyed by hostname/host.

        Entries carrying a recorded serial are skipped: their identity is
        known, and if it had matched this discovery the unique_id abort above
        would already have fired — a serial-keyed entry whose *stale host*
        happens to equal the discovered address must not be hijacked.
        """
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_SERIAL):
                continue
            if entry.unique_id in (hostname, self._host) or entry.data.get(
                CONF_HOST
            ) in (self._host, hostname):
                return entry
        return None

    @staticmethod
    def _entry_is_failing(entry: JungHomeConfigEntry) -> bool:
        """Whether ``entry`` cannot currently reach the gateway at its host.

        SETUP_RETRY/SETUP_ERROR, or loaded with a coordinator whose last poll
        failed. Anything else — loaded and polling fine, disabled, mid-setup
        or mid-unload — counts as "not failing", so it is never touched.
        """
        if entry.state in (ConfigEntryState.SETUP_RETRY, ConfigEntryState.SETUP_ERROR):
            return True
        if entry.state is not ConfigEntryState.LOADED:
            return False
        coordinator = getattr(entry, "runtime_data", None)
        return coordinator is not None and not coordinator.last_update_success

    async def _async_adopt_discovery(
        self, entry: JungHomeConfigEntry, *, serial: str | None
    ) -> None:
        """Apply what a discovery of an already-configured gateway may change.

        ``serial`` is set when ``entry`` is legacy-keyed and the discovery
        would migrate it onto the serial. Two rules, both there because the
        packet is unauthenticated:

        - **A healthy entry keeps its host.** It is talking to its gateway at
          the stored address right now (``last_update_success``), so there is
          nothing to fix and no reason to redirect the token. The discovered
          address is adopted only when the entry is *failing*
          (``_entry_is_failing``) — the gateway got a new DHCP lease while
          Home Assistant could not reach the old one, which is the feature
          this exists for.
        - **The discovered address must present the pinned certificate**
          before anything is trusted from it, host or identity. The
          certificate is what the packet cannot forge: only the gateway holds
          the private key behind the pin. A read of it is a bare TLS
          handshake — no request, no token — and is made only when there is
          something to change. An entry that has no pin yet cannot verify;
          it trusts the packet only in the failing case (its first contact
          then pins whatever answers, the same trust every first connect
          extends) and, for the identity migration, only when the packet
          names the very address the entry already talks to.

        The reload follows the same rules as every other flow-side update
        (``_async_update_and_reload``); a same-address discovery of an entry
        in SETUP_RETRY is also reloaded, as Home Assistant's own discovery
        handling does — the gateway just announced itself, so retry now.
        """
        stored_host = entry.data.get(CONF_HOST)
        same_host = stored_host == self._host
        failing = self._entry_is_failing(entry)
        adopt_host = not same_host and failing
        if not adopt_host and serial is None:
            if same_host and entry.state is ConfigEntryState.SETUP_RETRY:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            elif not same_host:
                _LOGGER.debug(
                    "Jung Home gateway announced at %s; keeping the healthy "
                    "entry on %s",
                    self._host,
                    stored_host,
                )
            return

        pinned = normalize_fingerprint(entry.data.get(CONF_TLS_FINGERPRINT))
        if same_host:
            verified = True
        elif pinned is not None:
            try:
                observed = await self._async_learn_fingerprint(self._host or "")
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "Ignoring discovery of Jung Home gateway at %s: %s",
                    self._host,
                    err,
                )
                return
            verified = observed == pinned
            if not verified:
                _LOGGER.warning(
                    "Ignoring an mDNS announcement for the configured Jung Home "
                    "gateway at %s: the responder there presents a TLS "
                    "certificate that does not match the pinned one",
                    self._host,
                )
                return
        else:
            verified = failing
        if not verified:
            _LOGGER.debug(
                "Ignoring discovery of Jung Home gateway at %s: the entry is "
                "healthy at %s and the announcement cannot be verified",
                self._host,
                stored_host,
            )
            return

        new_data = dict(entry.data)
        unique_id: str | None = None
        if serial is not None:
            _LOGGER.info(
                "Migrating Jung Home entry %s from unique_id %r to gateway serial",
                entry.entry_id,
                entry.unique_id,
            )
            # Freeze the anchor BEFORE the unique_id changes so the hub
            # device and scene ids stay put (see const.entry_anchor).
            new_data[CONF_IDENTITY_ANCHOR] = entry_anchor(entry)
            new_data[CONF_SERIAL] = serial
            unique_id = serial
        if adopt_host:
            _LOGGER.info(
                "Jung Home gateway moved to %s; updating the entry that could "
                "not reach it at %s",
                self._host,
                stored_host,
            )
            new_data[CONF_HOST] = self._host
        self._async_update_and_reload(
            entry, data=new_data, unique_id=unique_id, only_if_failing=True
        )

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose how to connect to the discovered gateway.

        The serial and firmware version come from the discovery TXT record
        (every captured firmware generation advertises both) and let a
        multi-gateway household tell which gateway this dialog is about. The
        "—" fallback is defensive, for firmware that omits the record.
        """
        return self.async_show_menu(
            step_id="zeroconf_confirm",
            menu_options=["app_approval", "password"],
            description_placeholders={
                "host": self._host or "",
                "serial": self._serial or "—",
                "version": self._txt_version or "—",
            },
        )

    async def _async_probe_host(self, host: str, fingerprint: str) -> str | None:
        """Check that a Jung Home gateway answers at ``host``.

        Returns an error key to show, or None when the address is usable.

        This deliberately tests **reachability only**: any HTTP reply proves a
        gateway is listening and the address is typed correctly, so a non-200
        is accepted here. The endpoint is the gateway's unauthenticated
        ``version`` — the probe carries **no token**. It used to hit
        ``/functions`` with the token, which handed the credential to
        whatever answered at a freshly typed address before its identity was
        checked; and the reply is pinned to ``fingerprint``, so even the
        token-less request goes only to the certificate the caller decided
        to trust.
        """
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/version/"
        try:
            async with (
                asyncio.timeout(PROBE_TIMEOUT),
                session.get(url, ssl=fingerprint_ssl(fingerprint)),
            ):
                # No raise_for_status(): a status-based failure still means the
                # host is reachable, which is all this probe asserts.
                return None
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Jung Home gateway probe failed for %s: %s", host, err)
            return "cannot_connect"

    async def _async_fetch_serial(
        self, host: str, token: str, fingerprint: str | None
    ) -> str | None:
        """Best-effort fetch of the gateway's hardware serial (``async_fetch_serial``).

        Kept as a method so the flow tests can stub it; the request carries
        the token, so it is pinned to ``fingerprint`` — the flow learns one
        before it ever gets here, and never sends the token without one.
        """
        if fingerprint is None:  # pragma: no cover - every caller pins first
            return None
        return await async_fetch_serial(self.hass, host, token, fingerprint)

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
            # Pinned: the token this returns must come from the certificate
            # learned before the request, not from whoever answers.
            ssl = await self._async_ssl()
            async with session.post(
                url, json={"user_name": REGISTER_USER_NAME}, timeout=timeout, ssl=ssl
            ) as response:
                if response.status != 200:
                    self._error = "register_failed"
                    raise CannotRegister(f"HTTP {response.status}")
                data = await response.json()
        except (TimeoutError, aiohttp.ClientError) as err:
            self._error = "cannot_connect"
            raise CannotRegister(str(err)) from err
        except ValueError as err:
            # A JSON-typed but malformed body: response.json() raises
            # json.JSONDecodeError (a ValueError), not a ClientError — without
            # this the flow died with "Unknown error occurred" instead of the
            # register_failed form.
            self._error = "register_failed"
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
            # Pinned before the password leaves: it is the network key, the
            # one credential above the token itself.
            ssl = await self._async_ssl()
            async with (
                asyncio.timeout(30),
                session.post(url, json={"password": password}, ssl=ssl) as response,
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
        except ValueError as err:
            # Same as _async_register: a malformed JSON body must land on the
            # failure form, not crash the flow.
            self._error = "register_failed"
            raise CannotRegister(str(err)) from err

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            self._error = "register_failed"
            raise CannotRegister("No token in response")
        return str(token)
