"""Data update coordinator for Jung Home (REST polling + WebSocket push)."""

import asyncio
import json
import logging
import math
import random
from collections import deque
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_SCENE_RECALLED,
    device_slug,
    duplicate_slugs,
    scene_unique_id,
)
from .models import Device, Scene

_LOGGER = logging.getLogger(__name__)

# WebSocket reconnect backoff bounds (seconds).
INITIAL_RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60
# Small random addition to each reconnect wait, so multiple gateways/entries on
# the same network don't all retry in lockstep after a shared network blip.
RECONNECT_JITTER = 0.5
# Consecutive failed reconnects before raising a repair issue, following the core
# convention of bounding push failures. Below this the backoff has waited well
# under a minute in total, which an ordinary blip (gateway reboot, Wi-Fi hiccup)
# rides out silently; past it the gateway has been unreachable long enough that
# the user is unknowingly running on the 60 s REST poll and deserves to be told.
MAX_RECONNECT_FAILURES = 5
# How long a session must stay up before it counts as a genuine recovery rather
# than a flap. Resetting the backoff at the moment of connect made the escalation
# unreachable: a gateway that accepts the upgrade and drops us immediately (a
# reboot loop, a websocket server restart cycle, a client limit) would reconnect
# roughly once a second forever, never raising the repair issue and flapping every
# controllable entity. A session shorter than this is treated as a failed attempt.
STABLE_SESSION_SECONDS = 30
# Bound for the WebSocket handshake. Home Assistant's shared session carries
# aiohttp's default ClientTimeout(total=300, sock_connect=30), so a gateway that
# accepts the TCP connection and then says nothing parks the reconnect loop for a
# full five minutes: no retry, no failure counted, no progress toward the repair
# issue, and every controllable entity unavailable throughout. A gateway on the
# LAN either answers quickly or is not answering.
WS_CONNECT_TIMEOUT = 30
# Bound for a single outbound frame. `send_str` awaits the transport drain and
# has no timeout of its own, so a peer that stops reading can block the calling
# service call indefinitely. Short, because this is a LAN write of a few bytes.
WS_SEND_TIMEOUT = 10
# Bound for awaiting the gateway's confirmation of a datapoint set. A successful
# set is answered with a `datapoint` reply that echoes the request's
# `message_id` (firmware-verified, websocket-server-service.js); the middleware
# itself gives up waiting on the BT-Mesh node after
# `config.btmesh.response_timeout_ms` = 3000 ms (config.json) before the set
# rejects and the reply never comes, so this leaves comfortable headroom above
# that mesh-level bound for the WS round trip. Waiting longer would buy
# nothing: the api-server abandons its middleware IPC call at
# `middleware.command_timeout_ms` = 6000 ms (api-server config.json), and the
# middleware's own retry loop (3 attempts, 3 s apart) means a set that did not
# succeed on the first mesh attempt cannot answer inside that bound either —
# so every reply that will ever arrive does so within ~3.5 s. A rejected set
# produces only an uncorrelated `error:` message frame (no message_id to match
# against — see `_dispatch_text_frame`), so a rejection surfaces here as a
# timeout rather than the gateway's specific error text.
COMMAND_REPLY_TIMEOUT = 5
# Repair-issue translation key for that "live push is dead" state.
ISSUE_PUSH_FAILURE = "websocket_push_failure"

# Diagnostics: a bounded log of the most recent raw WebSocket frames so a
# downloadable report shows what the gateway actually sends (the connect-time
# handshake — version/message/functions/groups/scenes — plus live datapoint
# pushes) against how we parse it. Frames carry no secrets (the token is a
# connect header, never a frame body), but can be large, so each is truncated and
# only the most recent are kept.
WS_FRAME_LOG_SIZE = 60
WS_FRAME_MAX_CHARS = 2000

# Sanity bounds for a gateway-advertised colour-temperature range. Anything
# outside this is not a plausible tunable-white range and is treated as an
# unrecognised payload rather than trusted (a bogus range would otherwise be
# declared to Home Assistant, which enforces it against the user).
MIN_PLAUSIBLE_KELVIN = 1000
MAX_PLAUSIBLE_KELVIN = 20000

# Config entry carrying the coordinator as runtime_data.
type JungHomeConfigEntry = ConfigEntry[JungHomeDataUpdateCoordinator]


def _as_kelvin(raw: Any) -> int | None:
    """Coerce one end of a gateway range to Kelvin, or None if it isn't a number.

    Gateway numerics arrive as strings as often as numbers, so ``"2700"`` and
    ``2700`` are both accepted. ``bool`` is rejected explicitly (it is an ``int``
    subclass, and ``True`` is not a temperature).

    Every conversion below can raise on untrusted JSON, and none of them raise
    only ``ValueError``:

    - ``float()`` on a huge ``int`` raises ``OverflowError``. ``json.loads``
      parses integer literals at arbitrary precision, so a frame carrying a
      400-digit integer reaches this function as an ``int`` Python cannot
      represent as a float. (A huge *string* is safe — it becomes ``inf``.)
    - ``json.loads`` also accepts the bare ``NaN`` / ``Infinity`` literals, and
      ``round()`` rejects both: ``ValueError`` for NaN, ``OverflowError`` for
      infinity. ``math.isfinite`` screens them out first so the intent is
      explicit rather than incidental.

    Catching the union keeps a malformed frame a no-op here instead of an
    exception escaping into ``JungHomeLight.__init__`` and taking down the whole
    light platform.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        kelvin = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(kelvin):
        return None
    try:
        return round(kelvin)
    except (ValueError, OverflowError):  # pragma: no cover - isfinite guards it
        return None


def _parse_color_temp_range(raw: Any) -> tuple[int, int] | None:
    """Parse a gateway colour-temperature range, or None if unusable.

    Accepts ``{"min": 2700, "max": 6500}`` and ``[2700, 6500]``. Rejects
    non-numeric, reversed, zero-width and implausible ranges — the caller then
    falls back to the light platform's defaults.
    """
    if isinstance(raw, dict):
        low, high = raw.get("min"), raw.get("max")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        low, high = raw[0], raw[1]
    else:
        return None
    low_k, high_k = _as_kelvin(low), _as_kelvin(high)
    if low_k is None or high_k is None:
        return None
    # Reversed and zero-width ranges are both nonsense; Home Assistant would
    # reject (or mis-render) a min >= max colour-temperature entity.
    if low_k >= high_k:
        return None
    if low_k < MIN_PLAUSIBLE_KELVIN or high_k > MAX_PLAUSIBLE_KELVIN:
        return None
    return low_k, high_k


class JungHomeDataUpdateCoordinator(DataUpdateCoordinator[list[Device]]):
    """Class to manage fetching data from the Jung Home API."""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], config_entry: ConfigEntry
    ) -> None:
        """Initialize the coordinator."""
        self.config = config
        # Snapshot of the entry options at setup, so the update listener can tell
        # an options change (e.g. the inverted-covers set) from a token/host-only
        # update and reload exactly when the platforms need rebuilding.
        self.options_snapshot: dict[str, Any] = dict(config_entry.options)
        self.websocket: aiohttp.ClientWebSocketResponse | None = None
        self.ws_connected: bool = False
        # When the WebSocket last completed a connect (diagnostics only) — helps
        # tell "just dropped" from "has been down a while" in a downloaded report.
        self.ws_last_connected: datetime | None = None
        # Most recent REST/WebSocket failure, for diagnostics (never raised).
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        # The datapoint id whose WebSocket push is being dispatched right now, or
        # None for REST-poll-driven updates. Event entities read this to fire on
        # a genuine push edge rather than diffing snapshots (see event.py). It is
        # set only for the duration of one synchronous `async_update_listeners`
        # dispatch, so REST re-reads (which leave it None) never fire events.
        self.pushed_datapoint_id: str | None = None
        # Gateway firmware version, reported by the WebSocket "version" frame.
        self.gateway_version: str | None = None
        # Scene list, populated from the WebSocket `scenes` broadcasts (full list
        # on connect, `scenes-new` / `scenes-deleted` deltas on change). The scene
        # platform discovers from this; recall goes over REST because the
        # WebSocket `scene` command is unimplemented on the gateway.
        self.scenes: list[Scene] = []
        # Last `groups` broadcast (per-room capability metadata, e.g. which groups
        # advertise color_temperature_range). Read by `area_for_device` and
        # `color_temp_range_for_device`, and surfaced in diagnostics so the
        # capabilities we do not yet implement stay visible.
        self.groups: list[dict[str, Any]] = []
        # Unmapped quantity units the sensor platform has already warned about,
        # once per unit per entry (kept here so it resets on reload and is not
        # shared between two gateways' entries, unlike a module global).
        self.warned_quantity_units: set[str] = set()
        # Datapoint ids seen in pushes with no matching stored datapoint. Each
        # gets one WARNING and one refresh request (see the unmatched-push
        # branch); repeats log at DEBUG so a phantom id can't spam the log or
        # amplify polling.
        self._unmatched_push_ids: set[str] = set()
        # In-flight datapoint set commands, keyed by the `message_id` we tagged
        # them with, so the matching `datapoint` reply (see
        # `_resolve_pending_reply`) can resolve the future the sender is
        # awaiting instead of the send being fire-and-forget. Popped by
        # `_send_datapoint_command` whichever way the wait ends (reply or
        # COMMAND_REPLY_TIMEOUT), so this never accumulates stale entries.
        self._pending_replies: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._next_message_id = 0
        # While a REST poll is in flight, every pushed datapoint's merged keys
        # are recorded here (datapoint id -> merged keys) and re-applied over
        # the poll's snapshot before it is adopted — the snapshot was generated
        # before the push, so adopting it as-is briefly reverted the pushed
        # (or command-confirmed) value until the next push or poll healed it.
        # None outside a poll, so the steady state records nothing.
        #
        # On the pinned HA, polls can NOT actually overlap: every refresh path
        # — the 60 s schedule (`_handle_refresh_interval`), a manual
        # `async_refresh`, and the debounced `async_request_refresh` — takes
        # the same `_debounced_refresh.async_lock()` before calling
        # `_async_refresh` (verified in helpers/update_coordinator.py; an
        # earlier revision of this comment claimed the opposite from reading
        # `_async_refresh` alone and missing the lock in its callers). The
        # `_polls_in_flight` refcount and the shared-dict join below are kept
        # anyway as cheap insurance: the lock is a *private* HA implementation
        # detail, and if it ever changes, a replaced-per-poll overlay would
        # silently clobber pushes recorded for a still-running poll.
        self._poll_push_overlay: dict[str, dict[str, Any]] | None = None
        self._polls_in_flight = 0
        # Bounded log of recent raw WebSocket frames for diagnostics.
        self.ws_frame_log: deque[str] = deque(maxlen=WS_FRAME_LOG_SIZE)
        # Latest raw frame of each type, so the connect-time handshake
        # (functions/groups/scenes/version) is always present in diagnostics even
        # when the rolling log above has churned past it on a busy gateway.
        self.ws_last_frame_by_type: dict[str, str] = {}
        # Monotonic count of device-list adoptions: bumped by every successful
        # REST poll and every `functions` broadcast — the only two events that
        # can change device *membership*. Listeners that debounce on "consecutive
        # polls" (the stale-device pruner) compare this instead of counting raw
        # dispatches: pushes, scenes broadcasts and the WS-drop notification all
        # call async_update_listeners too, and counting those shrank the
        # pruner's 10-poll window during a WS flap or a scene-editing session
        # while a device was transiently missing from one poll.
        self.data_generation = 0
        # Stable-slug -> volatile device id, to detect firmware-update id changes.
        self._device_ids: dict[str, str] = {}
        # Per-platform (entity-domain -> unique_ids) sets shared with each
        # platform's discovery. They are the add-once duplicate guard; the stale
        # device pruner clears a removed device's ids from them (see
        # ``forget_device_unique_ids``) so a device that reappears is re-added.
        self._known_unique_ids: dict[str, set[str]] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._closing = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        # Consecutive failed reconnects; reset once a session proves stable (see
        # ``_mark_session_stable``), not merely on a successful handshake.
        self._reconnect_failures = 0
        # Whether the current outage has already produced its one WARNING, so the
        # retry loop degrades to DEBUG instead of warning once a minute forever.
        self._unavailable_logged = False
        # Repair-issue id, scoped to this entry so two gateways each report
        # their own outage instead of overwriting one shared issue.
        self._push_failure_issue_id = f"{ISSUE_PUSH_FAILURE}_{config_entry.entry_id}"
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Jung Home",
            update_interval=timedelta(minutes=1),
        )

    def _record_error(self, err: BaseException) -> None:
        """Remember the most recent REST/WebSocket failure for diagnostics."""
        self.last_error = str(err)
        self.last_error_at = dt_util.utcnow()

    async def _async_update_data(self) -> list[Device]:
        """Fetch data from the API."""
        _LOGGER.debug("Fetching new device data from Jung Home API")
        # Open the push overlay for the duration of the fetch: the response's
        # snapshot is generated before any push that races it, so those pushes
        # must win over the snapshot (see _apply_push_overlay). Joined, not
        # replaced, when another poll already opened it.
        if self._poll_push_overlay is None:
            self._poll_push_overlay = {}
        self._polls_in_flight += 1
        try:
            response = await self._fetch_devices_from_api(
                self.config["host"], self.config["token"]
            )
        except aiohttp.ClientResponseError as err:
            self._record_error(err)
            if err.status in (401, 403):
                # Token revoked/expired — trigger Home Assistant's reauth flow.
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except aiohttp.ClientError as err:
            self._record_error(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except TimeoutError as err:
            self._record_error(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            # Insurance path (see the `_poll_push_overlay` comment in
            # `__init__`: on the pinned HA every refresh path serializes on
            # the debouncer lock, so `_polls_in_flight` never actually
            # exceeds 1): should polls ever overlap again, leave the overlay
            # open while another poll is still fetching (this poll takes a
            # snapshot copy — applied synchronously below, so the
            # still-recording dict cannot mutate it mid-walk); the last poll
            # out closes it. On the failure paths above the collected
            # overlay is simply discarded: there is no snapshot to correct,
            # and a failed poll leaves `self.data` (with the pushes already
            # merged in) untouched.
            self._polls_in_flight -= 1
            if self._polls_in_flight == 0:
                overlay = self._poll_push_overlay
                self._poll_push_overlay = None
            else:
                overlay = dict(self._poll_push_overlay or {})

        _LOGGER.debug("API Response: %s", response)
        if overlay:
            self._apply_push_overlay(response, overlay)
        self._reload_if_device_ids_changed(response)
        # A fresh device list is about to be adopted (the base class stores the
        # return value before notifying listeners, so the counter is consistent
        # by dispatch time).
        self.data_generation += 1
        # The base class adopts this return value as `self.data` directly (it
        # does NOT route through `async_set_updated_data`) and then notifies
        # listeners.
        return response

    @callback
    def async_set_updated_data(self, data: list[Device]) -> None:
        """Adopt a fresh device list and notify listeners.

        Overridden to advance ``data_generation``: every caller of this method
        is adopting a poll-equivalent device list (the ``functions`` broadcast
        is the production caller; the per-datapoint push path deliberately
        avoids it — see ``_handle_websocket_message``), and the stale-device
        pruner debounces on that count rather than on raw dispatches. The REST
        poll adopts via the base class's ``self.data`` assignment instead, so
        ``_async_update_data`` advances the counter itself.
        """
        self.data_generation += 1
        super().async_set_updated_data(data)

    @staticmethod
    def _apply_push_overlay(
        devices: list[Device], overlay: dict[str, dict[str, Any]]
    ) -> None:
        """Re-apply pushes that raced an in-flight poll over its snapshot.

        A push reflects a state change the poll's snapshot may predate, and
        every gateway-side change emits a push — so for the datapoints it
        covers, the overlay always holds a value at least as fresh as the
        snapshot's. Without this, adopting the snapshot briefly reverted a
        value pushed (or confirmed back to a command) during the fetch, until
        the next push or poll set it right again. The key-by-key update
        mirrors the live merge in ``_handle_websocket_message`` exactly.
        """
        for device in devices:
            for datapoint in device.get("datapoints", []):
                dp_id = datapoint.get("id")
                if not dp_id or dp_id not in overlay:
                    continue
                cast("dict[str, Any]", datapoint).update(overlay[dp_id])

    def _reload_if_device_ids_changed(self, devices: list[Device]) -> None:
        """Reload the entry if the gateway regenerated its device ids.

        The gateway assigns new volatile device/datapoint ids on a firmware
        update; entities cache those ids, so without a reload they can no longer
        find their datapoint (state stops updating, commands target dead ids).
        unique_ids are label-based and survive the reload.

        Colliding slugs are skipped (see ``duplicate_slugs``): two devices whose
        labels slug identically would share ONE key in the map below, with the
        gateway's list order deciding which device's id it holds — so a mere
        order change between polls read as "the id changed" and scheduled a
        reload, every time, forever. Skipping them trades id-change detection
        for those (already-degraded) devices against that reload loop.
        """
        colliding = duplicate_slugs(devices)
        new_ids = {
            device_slug(d): d["id"]
            for d in devices
            if d.get("id") and device_slug(d) not in colliding
        }
        changed = any(
            self._device_ids.get(slug) not in (None, dev_id)
            for slug, dev_id in new_ids.items()
        )
        self._device_ids = new_ids
        if changed and self.config_entry is not None:
            _LOGGER.warning(
                "Jung Home gateway device ids changed (firmware update?); "
                "reloading the integration to re-resolve entities"
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _fetch_devices_from_api(self, host: str, token: str) -> list[Device]:
        """Fetch devices from the Jung Home API."""
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/functions"
        headers = {"token": f"{token}", "Content-Type": "application/json"}

        async with asyncio.timeout(30):
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

        # The functions endpoint must return a JSON array of device objects; an
        # error/object response would otherwise degrade into a list of dict keys
        # and crash the platforms downstream.
        if not isinstance(data, list):
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="invalid_response"
            )
        # Keep the full device payload so any firmware-stable identifier
        # (serial / address / etc.) is available for building unique IDs,
        # and is visible in the debug log above for inspection. This is the
        # trust boundary: untyped gateway JSON becomes the typed `Device` model.
        # Downstream code keeps defensive `.get(...)` access for malformed items.
        return cast("list[Device]", [d for d in data if isinstance(d, dict)])

    async def _fetch_groups_from_api(
        self, host: str, token: str
    ) -> list[dict[str, Any]]:
        """Fetch the gateway's groups (rooms) from the REST API.

        Groups also arrive over the WebSocket, but that connects only after the
        platforms are set up; fetching once here lets the first area-assignment
        pass at the end of setup place devices straight away, instead of waiting
        for the WebSocket handshake to deliver the groups a moment later.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/groups"
        headers = {"token": f"{token}", "Content-Type": "application/json"}
        async with asyncio.timeout(30):
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
        if not isinstance(data, list):
            return []
        return [g for g in data if isinstance(g, dict)]

    async def _fetch_scenes_from_api(self, host: str, token: str) -> list[Scene]:
        """Fetch the gateway's scenes from the REST API."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/scenes/"
        headers = {"token": f"{token}", "Content-Type": "application/json"}
        async with asyncio.timeout(30):
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
        if not isinstance(data, list):
            return []
        return cast("list[Scene]", [s for s in data if isinstance(s, dict)])

    async def async_fetch_scenes(self) -> None:
        """Populate ``self.scenes`` from REST, best-effort.

        Scenes otherwise arrive only in the WebSocket handshake, which connects
        *after* the platforms are set up — so `scene.*` entities did not exist at
        the end of setup, and never appeared at all if the WebSocket could not
        connect, even though every other platform keeps working on the REST poll.
        Fetching here means scenes are present as soon as setup finishes and
        survive a gateway whose WebSocket is unavailable.

        Best-effort for the same reason as the groups fetch: a scene list is not
        worth failing setup over, and the handshake delivers it moments later.
        """
        try:
            self.scenes = await self._fetch_scenes_from_api(
                self.config["host"], self.config["token"]
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not fetch Jung Home scenes: %s", err)

    async def async_fetch_groups(self) -> None:
        """Populate ``self.groups`` from REST, best-effort.

        Room grouping is a nice-to-have (it only drives placing a device in the
        matching area), so a failure here must never block setup or device
        polling — it just leaves the groups empty until the WebSocket handshake
        delivers them, at which point the next refresh places any device that
        was waiting on its room.
        """
        try:
            self.groups = await self._fetch_groups_from_api(
                self.config["host"], self.config["token"]
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # Best-effort, never fatal: the gateway being unreachable, slow, or
            # answering with a non-JSON body (ValueError covers
            # json.JSONDecodeError) just leaves the room list empty until the
            # WebSocket handshake delivers it. Any other exception is a bug here
            # rather than a gateway problem, so it is left to propagate.
            _LOGGER.debug("Could not fetch Jung Home groups: %s", err)

    def area_for_device(self, device: Device) -> str | None:
        """Return the room/area name for a device from its parent groups.

        Resolves the device's ``parent_groups`` ids against the groups list and
        returns the first group name found (a device is normally in one room).
        Returns ``None`` when the device has no group or none resolve to a name.

        Hardened exactly like ``color_temp_range_for_device`` below: groups and
        parent ids are untrusted gateway JSON, and an unhashable id (a list, a
        dict) must not raise ``TypeError`` out of the ``_assign_areas``
        coordinator listener. (HA's ``async_update_listeners`` does contain a
        raising listener — each callback runs in its own try/except and the
        rest still dispatch — but that containment logs a full traceback for
        what is merely malformed gateway data, on every refresh, and area
        assignment for the device silently stops happening.)
        """
        parents = device.get("parent_groups") or []
        if not isinstance(parents, (list, tuple)) or not parents:
            return None
        by_id: dict[Any, str] = {}
        for group in self.groups:
            if not isinstance(group, dict):
                continue
            group_id = group.get("id")
            if not isinstance(group_id, (str, int)) or isinstance(group_id, bool):
                continue
            name = group.get("name") or group.get("label")
            if name:
                # First occurrence wins on a duplicated id, matching the
                # documented order in color_temp_range_for_device.
                by_id.setdefault(group_id, str(name))
        for parent in parents:
            if not isinstance(parent, (str, int)) or isinstance(parent, bool):
                continue
            name = by_id.get(parent)
            if name is not None:
                return name
        return None

    def color_temp_range_for_device(self, device: Device) -> tuple[int, int] | None:
        """Return the (min, max) Kelvin range a device's groups advertise.

        **No firmware is known to send this.** Captured ``groups`` broadcasts
        (``disk_dump/ws-capture*/groups.json``, 14 real groups) carry only
        ``id`` / ``address`` / ``name`` / ``related_functions`` /
        ``function_types`` — there is no colour-temperature field, and the name
        ``color_temperature_range`` traces back to a speculative comment rather
        than a capture. Nothing wires this into an entity yet for exactly that
        reason; see the light-platform note in ``light.py``.

        It is kept because the ``groups`` broadcast is the only plausible source
        for a per-fixture range, and having the parser and its tests in place
        means confirming the field later is a one-line change instead of a
        design question. Both plausible encodings are accepted
        (``{"min": .., "max": ..}`` and ``[min, max]``); anything unrecognised
        or implausible is rejected rather than guessed at.

        Returns the range from the **first** parent group that advertises a
        usable one. That is arbitrary when a device sits in several groups with
        different ranges — it depends on the gateway's array order — so any
        future caller must decide whether first-wins, intersection or union is
        correct for its use. It is only defensible today because nothing
        consumes the result.
        """
        parents = device.get("parent_groups") or []
        # Untrusted gateway JSON: a non-list `parent_groups`, or an unhashable
        # group id, must not raise out of a caller's constructor.
        if not isinstance(parents, (list, tuple)) or not parents:
            return None
        by_id: dict[Any, dict[str, Any]] = {}
        for group in self.groups:
            if not isinstance(group, dict):
                continue
            group_id = group.get("id")
            if not isinstance(group_id, (str, int)) or isinstance(group_id, bool):
                continue
            # First occurrence wins, matching the documented order above; a
            # plain dict comprehension would silently keep the last duplicate.
            by_id.setdefault(group_id, group)
        for parent in parents:
            if not isinstance(parent, (str, int)) or isinstance(parent, bool):
                continue
            parent_group = by_id.get(parent)
            if parent_group is None:
                continue
            parsed = _parse_color_temp_range(
                parent_group.get("color_temperature_range")
            )
            if parsed is not None:
                return parsed
        return None

    def known_unique_ids(self, domain: str) -> set[str]:
        """Return the shared discovery ``known`` set for an entity domain.

        Each platform uses this instead of a private set so the stale-device
        pruner can reach into it (via ``forget_device_unique_ids``) and drop a
        removed device's ids — otherwise a device that reappears after being
        pruned would stay ``known`` and never get its entities re-created.
        """
        return self._known_unique_ids.setdefault(domain, set())

    def forget_device_unique_ids(self, device_id: str) -> None:
        """Drop a device's entity unique_ids from the discovery ``known`` sets.

        Called by the pruner just before it removes a device. Without this the
        per-platform ``known`` set keeps the id forever, so the platform would
        never re-add the entity if the gateway reported the device again (it
        would be missing until an entry reload). Looks the device's entities up
        in the registry and discards each id from the set for its domain.
        """
        entity_registry = er.async_get(self.hass)
        for entity in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        ):
            known = self._known_unique_ids.get(entity.domain)
            if known is not None:
                known.discard(entity.unique_id)

    async def activate_scene(self, scene_id: str) -> None:
        """Activate a scene via REST (the WebSocket scene command is unimplemented)."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        # scene_id comes from untrusted gateway JSON; percent-encode it so a
        # crafted id can't break out of the path segment (e.g. via ?/#//).
        safe_scene_id = quote(str(scene_id), safe="")
        url = f"https://{self.config['host']}/api/junghome/scenes/{safe_scene_id}"
        headers = {
            "token": f"{self.config['token']}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(30):
                async with session.post(url, headers=headers) as response:
                    response.raise_for_status()
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                # A revoked/expired token is permanent: reporting it as
                # "reconnecting, try again in a moment" left the user retrying a
                # scene forever with nothing prompting them to re-authenticate.
                # The REST poll and the WebSocket upgrade both drive reauth on
                # these statuses; this is the third path that can see one.
                _LOGGER.warning(
                    "Jung Home gateway rejected the token on scene recall "
                    "(HTTP %s); starting reauthentication",
                    err.status,
                )
                if self.config_entry is not None:
                    self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_send"
            ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_send"
            ) from err

    async def _websocket_loop(self) -> None:
        """Keep a WebSocket connection alive, reconnecting with backoff on drop.

        The gateway pushes state via WebSocket; without this loop a single
        network blip would silently stop live updates until the next command.
        """
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        while not self._closing:
            try:
                await self._run_websocket()
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                if err.status in (401, 403):
                    # A revoked/expired token is rejected at the WS upgrade.
                    # Reconnecting can't fix that, so stop and let Home Assistant
                    # drive reauth instead of hammering the gateway with a token
                    # it already refused. (The REST poll maps 401/403 to reauth
                    # too, but this surfaces it immediately.)
                    _LOGGER.warning(
                        "Jung Home WebSocket rejected the token (HTTP %s); "
                        "starting reauthentication",
                        err.status,
                    )
                    if self.config_entry is not None:
                        self.config_entry.async_start_reauth(self.hass)
                    return
                self._record_error(err)
                self._log_disconnected(err)
                self._note_reconnect_failure()
            except Exception as err:
                self._record_error(err)
                self._log_disconnected(err)
                self._note_reconnect_failure()
            if self._closing:
                break
            _LOGGER.debug(
                "Reconnecting to Jung Home WebSocket in %ss", self._reconnect_delay
            )
            await asyncio.sleep(
                self._reconnect_delay + random.uniform(0, RECONNECT_JITTER)  # noqa: S311
            )
            self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    def _log_disconnected(self, err: BaseException) -> None:
        """Log a dropped WebSocket once at WARNING, then at DEBUG.

        The reconnect loop retries forever, so warning on every attempt turned an
        unreachable gateway into a warning a minute for as long as it stayed down
        — exactly the noise the `log-when-unavailable` rule exists to prevent.
        The first drop is the newsworthy one; the rest repeat the same fact.
        ``_mark_session_stable`` clears the flag, so a genuine recovery followed
        by a genuine outage warns again.
        """
        if self._unavailable_logged:
            _LOGGER.debug("Jung Home WebSocket still disconnected: %s", err)
            return
        self._unavailable_logged = True
        _LOGGER.warning("Jung Home WebSocket disconnected: %s", err)

    def _note_reconnect_failure(self) -> None:
        """Count a failed reconnect and, past the threshold, tell the user.

        A dropped WebSocket degrades the integration to the 60 s REST poll: state
        still updates, so nothing looks broken, it just stops being live. Below
        ``MAX_RECONNECT_FAILURES`` that is an ordinary blip the backoff rides out
        silently. Past it, raise a repair issue so the degradation is visible
        rather than buried in a log warning. It is deliberately not fixable from
        the UI — only the gateway or the network coming back fixes it, and
        ``_run_websocket`` deletes the issue on the next successful connect.
        """
        self._reconnect_failures += 1
        if self._reconnect_failures < MAX_RECONNECT_FAILURES:
            return
        # Re-created on every further failure so the attempt count stays current
        # (and so a manually deleted issue comes back while the outage lasts).
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._push_failure_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PUSH_FAILURE,
            translation_placeholders={
                "host": str(self.config["host"]),
                "failures": str(self._reconnect_failures),
            },
        )

    def _fail_pending_replies(self) -> None:
        """Fail every in-flight command the moment its WebSocket session ends.

        The gateway sends a command's reply only to the socket that carried the
        request (``socket.send`` in ``websocket-server-service.js``), so once
        this session is gone the reply can never arrive — not even after a
        reconnect. Without this, each in-flight command sat out the full
        ``COMMAND_REPLY_TIMEOUT`` and then reported "did not confirm in time"
        when the truthful error is the connection loss (``cannot_send``, the
        same error an immediately-detected dead socket raises) — and an entry
        unload with a command in flight stalled the same way. Futures that are
        already done (reply raced the drop, or the timeout fired) are left
        alone; each command's ``finally`` still pops its own entry.
        """
        for future in self._pending_replies.values():
            if not future.done():
                future.set_exception(
                    HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="cannot_send"
                    )
                )

    def _resolve_pending_reply(self, message_id: str, reply_data: Any) -> None:
        """Resolve the future a command is awaiting, if `message_id` matches one.

        A no-op if nothing is pending under this id (already timed out, or an
        id we never sent) or the future was somehow already resolved — a
        malformed/duplicate frame must never raise
        ``asyncio.InvalidStateError`` out of the frame handler.
        """
        future = self._pending_replies.get(message_id)
        if future is not None and not future.done():
            future.set_result(reply_data if isinstance(reply_data, dict) else {})

    def _dispatch_text_frame(self, raw: str) -> None:
        """Parse one TEXT frame and route it to the right handler.

        Split out of ``_run_websocket`` so that function stays about the session
        lifecycle. Never raises: a malformed frame must not tear down an
        otherwise healthy connection.
        """
        _LOGGER.debug("Received WebSocket message: %s", raw)
        self._log_ws_frame(raw)
        try:
            data = json.loads(raw)
            # Every frame is `{"type": ..., "data": ...}`; a bare list or JSON
            # scalar ("hello", 42) is not a frame. Rejecting it here keeps it
            # a clean log line instead of an AttributeError in the catch-all.
            if not isinstance(data, dict):
                _LOGGER.error("Received non-object WebSocket message: %s", data)
                return
            message_id = data.get("message_id")
            if isinstance(message_id, str) and message_id:
                # Only a reply to one of OUR OWN datapoint sets/gets carries
                # message_id back (websocket-server-service.js never assigns it
                # to a broadcast), so this can only ever match something
                # `_send_datapoint_command` is awaiting. Resolving it here does
                # not short-circuit the frame: it still falls through to the
                # normal dispatch below, which merges `data.get("data")` into
                # `self.data` exactly like a push would — the confirmed value
                # replaces the optimistic one HA already wrote.
                self._resolve_pending_reply(message_id, data.get("data"))
            if data.get("type") == "version":
                self.gateway_version = data.get("data")
                _LOGGER.info(
                    "Jung Home gateway firmware version: %s", self.gateway_version
                )
                self._apply_gateway_version()
                return
            if data.get("type") == "message":
                text = data.get("data")
                if isinstance(text, str) and text.startswith("error:"):
                    # The gateway reports a rejected command (e.g. a bad set) as
                    # an `error:` message frame. There is no message_id
                    # correlation, but surfacing it at WARNING beats dropping it.
                    _LOGGER.warning("Jung Home gateway reported an error: %s", text)
                else:
                    _LOGGER.debug("Received message frame: %s", data)
                return
            self._handle_websocket_message(data)
        except json.JSONDecodeError as e:
            _LOGGER.error("Error decoding WebSocket message: %s", e)
        except Exception as e:
            _LOGGER.error("Unexpected error handling WebSocket message: %s", e)
            _LOGGER.error("Message content: %s", raw)

    async def _run_websocket(self) -> None:
        """Open one WebSocket session and pump messages until it closes."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"wss://{self.config['host']}/ws"
        headers = {"token": f"{self.config['token']}"}
        # Only the handshake is bounded — wrapping the `async with` below would
        # tear down a perfectly healthy session after WS_CONNECT_TIMEOUT. Once
        # connected, `heartbeat=30` is what detects a silently dead peer.
        async with asyncio.timeout(WS_CONNECT_TIMEOUT):
            ws = await session.ws_connect(url, headers=headers, heartbeat=30)
        async with ws:
            self.websocket = ws
            # Connected: resync state we may have missed while disconnected.
            # Logged at INFO (paired with the WARNING on disconnect) so the
            # drop/recover story is visible without enabling debug logging
            # during a long soak.
            #
            # The backoff and the failure counter are deliberately NOT reset
            # here. A successful upgrade proves nothing yet — a gateway stuck in
            # a reboot loop accepts the handshake and drops us straight away, and
            # resetting on connect made that flap immortal: the delay went back
            # to 1 s before the doubling could ever apply, and the counter never
            # reached MAX_RECONNECT_FAILURES so the repair issue never appeared.
            # `_mark_session_stable` below does the reset once the session has
            # actually lasted STABLE_SESSION_SECONDS.
            _LOGGER.info("Jung Home WebSocket connected")
            self.ws_connected = True
            self.ws_last_connected = dt_util.utcnow()
            cancel_stable = async_call_later(
                self.hass, STABLE_SESSION_SECONDS, self._mark_session_stable
            )
            await self.async_request_refresh()
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch_text_frame(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"WebSocket error frame: {msg}")
                # The gateway closed the socket cleanly: `async for` just ends,
                # without raising. Returning normally here would make the drop
                # invisible — no warning, no `last_error`, and no reconnect-failure
                # count, so a gateway that politely closes every session would
                # never raise the repair issue that exists for exactly this
                # silent degradation. Route it through the same failure path a
                # noisy drop takes.
                if not self._closing:
                    raise ConnectionError(
                        f"gateway closed the WebSocket (code {ws.close_code})"
                    )
            finally:
                cancel_stable()
                self.websocket = None
                self.ws_connected = False
                self._fail_pending_replies()
                self._notify_websocket_closed()

    @callback
    def _mark_session_stable(self, _now: datetime) -> None:
        """Treat the live session as a genuine recovery once it has held up.

        Fires ``STABLE_SESSION_SECONDS`` after a successful connect, and is
        cancelled if the session dies first — so a flapping gateway keeps
        escalating its backoff and keeps accumulating failures towards the repair
        issue, while a gateway that is actually back clears both (and the issue,
        a no-op when it was never raised).
        """
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        self._reconnect_failures = 0
        if self._unavailable_logged:
            # Pair the single WARNING above with a matching recovery line, so an
            # outage has a visible end without trawling debug logs.
            _LOGGER.warning("Jung Home WebSocket reconnected")
            self._unavailable_logged = False
        ir.async_delete_issue(self.hass, DOMAIN, self._push_failure_issue_id)

    def _notify_websocket_closed(self) -> None:
        """Push the WebSocket-down state to listeners after a live drop.

        Flips the gateway connectivity sensor to "off" immediately rather than
        lagging until the next REST poll (the reconnect path already refreshes on
        connect). Skipped while ``stop()`` is tearing the entry down: there the
        platforms are already being removed, so notifying would only run the
        prune/area listeners for no benefit.
        """
        if not self._closing:
            self.async_update_listeners()

    def _log_ws_frame(self, raw: str) -> None:
        """Record a raw WebSocket frame for diagnostics.

        The per-type store keeps the latest frame of each type IN FULL — it holds
        at most one frame per type, so it cannot grow unbounded, and keeping the
        connect-time handshake (functions/groups/scenes/version/message) complete
        makes it directly comparable to the raw wire format. The rolling buffer,
        which fills with high-frequency datapoint pushes, stays truncated.
        """
        frame_type = None
        try:
            frame_type = json.loads(raw).get("type")
        except (json.JSONDecodeError, AttributeError):
            pass
        if isinstance(frame_type, str):
            self.ws_last_frame_by_type[frame_type] = raw
        if len(raw) > WS_FRAME_MAX_CHARS:
            raw = raw[:WS_FRAME_MAX_CHARS] + "…[truncated]"
        self.ws_frame_log.append(raw)

    def _handle_websocket_message(self, message: dict[str, Any]) -> None:
        """Handle incoming WebSocket messages."""
        if not isinstance(message, dict):
            _LOGGER.error("Received WebSocket message is not a dictionary: %s", message)
            return

        data = message.get("data")
        msg_type = message.get("type")
        if isinstance(data, dict):
            if msg_type == "scene":
                # A scene was recalled (e.g. from a physical button). This is a
                # different frame from the `scenes` list broadcast: data is the
                # recalled scene object, not a datapoint. Without this branch it
                # would fall through to the datapoint lookup below and log a
                # spurious "no matching datapoint" warning.
                self._handle_scene_recall(data)
                return
            datapoint_id = data.get("id")
            if not datapoint_id:
                _LOGGER.error(
                    "Received WebSocket message without datapoint_id: %s", message
                )
                return
            if self._poll_push_overlay is not None:
                # A REST poll is in flight; record this push so the poll's
                # (older) snapshot cannot revert it. Recorded before the match
                # loop below on purpose: an unmatched push usually belongs to a
                # device the in-flight poll is about to discover, and its
                # snapshot values may predate this push just the same.
                overlay = self._poll_push_overlay.setdefault(datapoint_id, {})
                for key, value in data.items():
                    if key != "id":
                        overlay[key] = value
            updated = False
            for device in self.data or []:
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("id") == datapoint_id:
                        # Merge the pushed keys into the stored datapoint. The push
                        # carries arbitrary keys (typically `values`), so mutate via
                        # a dict view rather than the TypedDict.
                        dp_dict = cast("dict[str, Any]", datapoint)
                        for key, value in data.items():
                            if key != "id":
                                dp_dict[key] = value
                        _LOGGER.debug(
                            "Updated datapoint for device %s: %s",
                            device.get("id"),
                            datapoint,
                        )
                        updated = True
                        break
                if updated:
                    break
            if updated:
                # Flag the pushed datapoint for the duration of this dispatch so
                # event entities fire on the push itself. The dispatch below
                # notifies listeners synchronously, so the flag is valid for
                # exactly this push and is cleared immediately afterwards; REST
                # polls never set it and therefore never fire phantom events.
                self.pushed_datapoint_id = datapoint_id
                try:
                    # Deliberately NOT `async_set_updated_data`: that helper
                    # cancels the scheduled refresh and re-arms it a full
                    # `update_interval` from now, so a gateway that pushes more
                    # often than once a minute would defer the REST poll forever.
                    # The poll is the only thing that discovers new devices,
                    # prunes removed ones, assigns areas and detects gateway id
                    # churn, so starving it silently breaks all four.
                    #
                    # The merge above mutated the dicts already in `self.data`, so
                    # there is no new object to store. Setting `last_update_success`
                    # keeps the availability contract documented in `entity.py`
                    # (a push counts as proof the gateway is alive), and
                    # `async_update_listeners` gives the same synchronous
                    # notification the helper would have.
                    self.last_update_success = True
                    self.async_update_listeners()
                finally:
                    self.pushed_datapoint_id = None
            elif datapoint_id not in self._unmatched_push_ids:
                # An unmatched push usually means a device was just added in
                # the app and is pushing before the next poll has discovered
                # it. Request a (debounced) refresh on the FIRST sighting of an
                # unknown id — discovery then lands in seconds instead of up
                # to a minute — and warn once per id rather than per frame (a
                # new device pushing at 1 Hz used to warn 60 times before the
                # poll caught up).
                self._unmatched_push_ids.add(datapoint_id)
                _LOGGER.warning(
                    "No matching datapoint found for id %s; requesting a "
                    "refresh (a device may have just been added)",
                    datapoint_id,
                )
                self.hass.async_create_task(self.async_request_refresh())
            else:
                _LOGGER.debug("No matching datapoint found for id %s", datapoint_id)
        elif isinstance(data, list):
            if msg_type in ("scenes", "scenes-new", "scenes-deleted"):
                self._handle_scenes_broadcast(msg_type, data)
            elif msg_type == "groups":
                # Full groups list (on connect and on change). Carries per-room
                # capability metadata (area names, colour-temperature ranges) and
                # is surfaced in diagnostics.
                self.groups = [g for g in data if isinstance(g, dict)]
            elif msg_type == "functions":
                self._handle_functions_broadcast(data)
            else:
                _LOGGER.debug("Received %s broadcast (%d items)", msg_type, len(data))
        else:
            _LOGGER.warning(
                "Received WebSocket message with unknown data type: %s", message
            )

    def _handle_functions_broadcast(self, data: list[Any]) -> None:
        """Adopt a pushed ``functions`` list as if a REST poll had returned it.

        The gateway broadcasts the full, authoritative device list on connect
        and whenever it changes (captured frames match ``GET /functions/``
        exactly). Treating it as a poll result makes device add/remove
        push-driven — discovery, pruning, area assignment and the capability
        watcher all run on it via their coordinator listeners — instead of
        waiting up to a minute for the next poll.

        ``async_set_updated_data`` is correct HERE (and deliberately avoided in
        the per-datapoint push path above): this frame carries data as fresh
        and complete as a poll, so re-arming the poll timer a full interval out
        loses nothing — and the frame only arrives on membership change, so it
        cannot starve the poll the way per-value pushes did. No push marker is
        set, so event entities never read a broadcast as a button edge, and the
        unmatched-id memory resets because the authoritative list may have just
        added those devices.
        """
        devices = cast("list[Device]", [d for d in data if isinstance(d, dict)])
        _LOGGER.debug("Adopting functions broadcast (%d devices)", len(devices))
        self._unmatched_push_ids.clear()
        self._reload_if_device_ids_changed(devices)
        self.async_set_updated_data(devices)

    def _handle_scenes_broadcast(self, msg_type: str, data: list[Any]) -> None:
        """Update the cached scene list from a WebSocket scenes broadcast.

        The gateway pushes the full ``scenes`` list on connect and on change, and
        ``scenes-new`` / ``scenes-deleted`` deltas when scenes are added/removed
        in the app. The scene platform discovers from ``self.scenes`` and is
        notified via ``async_update_listeners`` so new scenes appear without a
        reload. (The WebSocket ``scene`` *command* is unimplemented on the
        gateway, so recall still goes over REST — see ``activate_scene``.)
        """
        items = cast("list[Scene]", [s for s in data if isinstance(s, dict)])
        if msg_type == "scenes":
            self.scenes = items
        elif msg_type == "scenes-new":
            by_id = {s.get("id"): s for s in self.scenes}
            for scene in items:
                by_id[scene.get("id")] = scene
            # De-duplicate by label, newest wins. Scene identity is the label
            # (ids regenerate on firmware updates), so a delta that assigned a
            # scene a new id would otherwise leave the old and new entries side
            # by side — and activation resolves the FIRST label match, which
            # could be the dead id. Scenes without a label can't back an entity
            # but are kept for diagnostics.
            by_label: dict[str, Scene] = {}
            unlabeled: list[Scene] = []
            for scene in by_id.values():
                label = scene.get("label")
                if label:
                    by_label[label] = scene
                else:
                    unlabeled.append(scene)
            self.scenes = [*by_label.values(), *unlabeled]
        else:  # scenes-deleted
            removed = {s.get("id") for s in items}
            self.scenes = [s for s in self.scenes if s.get("id") not in removed]
        self.async_update_listeners()

    def _handle_scene_recall(self, data: dict[str, Any]) -> None:
        """Fire a Home Assistant event when the gateway reports a scene recall.

        The gateway broadcasts ``{"type":"scene","data":{...scene...}}`` whenever a
        scene is activated — including by a physical button, not just by this
        integration. Re-emitting it on the HA event bus lets users automate on
        "scene X was recalled".
        """
        scene_id = data.get("id")
        label = data.get("label")
        if scene_id is None:
            _LOGGER.debug("Ignoring scene recall frame without an id: %s", data)
            return
        _LOGGER.debug("Scene recalled: %s (%s)", label, scene_id)
        event_data: dict[str, Any] = {"scene_id": scene_id, "label": label}
        if self.config_entry is not None:
            event_data["entry_id"] = self.config_entry.entry_id
            if isinstance(label, str) and label:
                # Resolve the scene entity backing this label, so the logbook
                # line links to it and automations can match on entity_id.
                # Best-effort: a scene not (yet) registered simply omits it.
                entity_id = er.async_get(self.hass).async_get_entity_id(
                    "scene", DOMAIN, scene_unique_id(self.config_entry, label)
                )
                if entity_id is not None:
                    event_data["entity_id"] = entity_id
        self.hass.bus.async_fire(EVENT_SCENE_RECALLED, event_data)

    def _apply_gateway_version(self) -> None:
        """Push the firmware version onto our devices in the registry.

        An entity's ``device_info`` is only read when it is first added, which
        may happen before the WebSocket ``version`` frame arrives. Update the
        registry directly so the device page shows the version without needing a
        reload. Combined with the ``device_info`` fallback this covers either
        ordering (entities created before or after the frame).

        The value written per device mirrors ``JungHomeEntity.device_info``
        exactly: a device that reports its **own** ``sw_version`` keeps it, and
        only devices without one (plus the synthetic gateway hub, which has no
        entry in the function list) fall back to the gateway version. Writing the
        gateway version unconditionally used to clobber a per-device version, so
        the two mechanisms disagreed whenever the gateway populated it.
        """
        if self.gateway_version is None or self.config_entry is None:
            return
        by_slug = {device_slug(d): d for d in (self.data or [])}
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            desired = self.gateway_version
            for domain, identifier in device.identifiers:
                if domain == DOMAIN and identifier in by_slug:
                    desired = by_slug[identifier].get("sw_version") or desired
                    break
            if device.sw_version != desired:
                registry.async_update_device(device.id, sw_version=desired)

    async def start(self) -> None:
        """Connect to the WebSocket.

        Initial device data is fetched separately during setup via
        async_config_entry_first_refresh() so that a failure aborts setup
        correctly (retry on connection error, reauth on a rejected token).
        """
        _LOGGER.debug("Starting coordinator: connecting to WebSocket")
        self._closing = False
        entry = self.config_entry
        if entry is None:  # pragma: no cover - an entry coordinator always has one
            return
        self._ws_task = entry.async_create_background_task(
            self.hass, self._websocket_loop(), name="junghome_ws"
        )

    async def stop(self) -> None:
        """Stop the coordinator and close the WebSocket connection."""
        _LOGGER.debug("Stopping coordinator and closing WebSocket")
        self._closing = True
        # Drop the degraded-push repair issue on the way out. It was only ever
        # deleted on a successful reconnect, so unloading, disabling or removing
        # the entry while degraded stranded it in the repairs UI forever, naming
        # a gateway that may no longer be configured. A no-op when unraised.
        ir.async_delete_issue(self.hass, DOMAIN, self._push_failure_issue_id)
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close()
        self.websocket = None

    async def send_websocket_message(self, message: dict[str, Any]) -> None:
        """Send a message via WebSocket."""
        _LOGGER.debug("Sending WebSocket message: %s", message)
        if self.websocket and not self.websocket.closed:
            try:
                async with asyncio.timeout(WS_SEND_TIMEOUT):
                    await self.websocket.send_str(json.dumps(message))
                _LOGGER.debug("WebSocket message sent successfully")
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="cannot_send"
                ) from err
        else:
            # The reconnect loop in _websocket_loop() will restore the connection,
            # but surface the failure now so the command isn't silently treated as
            # applied (callers optimistically update state only on success).
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_send"
            )

    async def _send_datapoint_command(
        self, datapoint_id: str, dp_type: str, values: list[dict[str, str]]
    ) -> None:
        """Set a datapoint and wait for the gateway to confirm it.

        Every command method below funnels through here. The frame is tagged
        with a ``message_id``; a successful set is answered with a matching
        ``datapoint`` reply that ``_dispatch_text_frame`` routes to
        ``_resolve_pending_reply``, which resolves the future this method
        awaits — turning what used to be fire-and-forget into a real,
        raiseable outcome. See ``COMMAND_REPLY_TIMEOUT`` for why a rejection
        (which the gateway cannot correlate back to this request) surfaces as
        a timeout rather than the gateway's own error text.

        The pending entry is always popped in ``finally``, whether the wait
        succeeded, timed out, or ``send_websocket_message`` raised first (e.g.
        no live socket) — so a send failure can never leak a future nothing
        will ever resolve.
        """
        self._next_message_id += 1
        message_id = f"ha{self._next_message_id}"
        message = {
            "type": "datapoint",
            "data": {"id": datapoint_id, "type": dp_type, "values": values},
            "message_id": message_id,
        }
        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending_replies[message_id] = future
        try:
            await self.send_websocket_message(message)
            try:
                async with asyncio.timeout(COMMAND_REPLY_TIMEOUT):
                    await future
            except TimeoutError as err:
                # Named here so it can be paired with the gateway's own
                # uncorrelated "error: ..." WARNING (logged by the message-frame
                # branch), which is the usual reason the reply never came.
                _LOGGER.warning(
                    "Jung Home gateway did not confirm the %s command for %s "
                    "within %s s",
                    dp_type,
                    datapoint_id,
                    COMMAND_REPLY_TIMEOUT,
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_timeout"
                ) from err
        finally:
            self._pending_replies.pop(message_id, None)

    async def turn_on_switch(self, datapoint_id: str) -> None:
        """Turn on the switch."""
        _LOGGER.debug("Turning on switch with datapoint_id: %s", datapoint_id)
        await self._send_datapoint_command(
            datapoint_id, "switch", [{"key": "switch", "value": "1"}]
        )

    async def turn_off_switch(self, datapoint_id: str) -> None:
        """Turn off the switch."""
        _LOGGER.debug("Turning off switch with datapoint_id: %s", datapoint_id)
        await self._send_datapoint_command(
            datapoint_id, "switch", [{"key": "switch", "value": "0"}]
        )

    async def turn_on_light(self, datapoint_id: str) -> None:
        """Turn on the light."""
        _LOGGER.debug("Turning on light with datapoint_id: %s", datapoint_id)
        await self._send_datapoint_command(
            datapoint_id, "switch", [{"key": "switch", "value": "1"}]
        )

    async def turn_off_light(self, datapoint_id: str) -> None:
        """Turn off the light."""
        _LOGGER.debug("Turning off light with datapoint_id: %s", datapoint_id)
        await self._send_datapoint_command(
            datapoint_id, "switch", [{"key": "switch", "value": "0"}]
        )

    async def set_brightness(self, datapoint_id: str, brightness: int) -> None:
        """Set the brightness of the light."""
        await self._send_datapoint_command(
            datapoint_id,
            "brightness",
            [{"key": "brightness", "value": str(brightness)}],
        )

    async def set_color_temp(self, datapoint_id: str, color_temp: int) -> None:
        """Set the color temperature of the light."""
        await self._send_datapoint_command(
            datapoint_id,
            "color_temperature",
            [{"key": "color_temperature", "value": str(color_temp)}],
        )

    async def set_status_led(self, datapoint_id: str, state: bool) -> None:
        """Set the status LED on (True) or off (False)."""
        value = "1" if state else "0"
        await self._send_datapoint_command(
            datapoint_id, "status_led", [{"key": "status_led", "value": value}]
        )

    async def set_level(self, datapoint_id: str, level: int) -> None:
        """Set a cover's position level (device scale 0-100)."""
        await self._send_datapoint_command(
            datapoint_id, "level", [{"key": "level", "value": str(level)}]
        )

    async def move_level(self, datapoint_id: str, direction: int) -> None:
        """Move/stop a cover via the ``level_move`` key.

        ``direction`` is the gateway's tri-state: ``1`` / ``-1`` to start moving,
        ``0`` to stop. (See ``cdb_types_datapoints.json``: ``level_move`` range
        ``["-1","0","1"]``.)
        """
        await self._send_datapoint_command(
            datapoint_id, "level", [{"key": "level_move", "value": str(direction)}]
        )

    async def set_angle(self, datapoint_id: str, angle: int) -> None:
        """Set a cover's slat angle (device scale 0-100)."""
        await self._send_datapoint_command(
            datapoint_id, "angle", [{"key": "angle", "value": str(angle)}]
        )

    async def set_temperature(self, datapoint_id: str, temperature: float) -> None:
        """Set a thermostat's target temperature (°C)."""
        await self._send_datapoint_command(
            datapoint_id,
            "temperature_ctrl",
            [{"key": "temperature_ctrl", "value": str(temperature)}],
        )

    async def set_temperature_preset(self, datapoint_id: str, preset: str) -> None:
        """Set a thermostat preset (``frost`` / ``eco`` / ``comfort``).

        These three are the only values the firmware accepts — it throws for
        anything else, including the ``none`` its own API descriptor
        advertises (``SetPointState.publishMode``), which would surface here
        as an uncorrelated error and a command-confirmation timeout.
        """
        await self._send_datapoint_command(
            datapoint_id,
            "temperature_ctrl",
            [{"key": "temperature_ctrl_preset", "value": preset}],
        )
