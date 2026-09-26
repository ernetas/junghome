"""Data update coordinator for Jung Home (REST polling + WebSocket push)."""

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import quote

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INVERTED_COVERS,
    CONF_POLL_INTERVAL,
    CONF_TLS_FINGERPRINT,
    DATA_AREA_ASSIGNED,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    EVENT_SCENE_RECALLED,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    WEBSOCKET_OUTAGE_REPAIR_AFTER,
    device_slug,
    duplicate_slugs,
    scene_unique_id,
)
from .health import (
    HEALTH_CONDITIONS,
    HEALTH_STATUS_REFRESH_INTERVAL,
    ISSUE_TIME_SYNC,
    TIME_ERROR_PARAMETER,
    HealthCondition,
    HealthState,
    active_health_conditions,
    health_issue_ids,
    parse_health_status,
)
from .models import (
    DOUBLED_BUTTON_FIRMWARE,
    Device,
    DeviceProperties,
    FunctionAnchor,
    NodeIdentity,
    Scene,
    parse_device_properties,
    parse_devices_verbose,
    parse_function_anchors,
    parse_project_export,
    sanitize_devices,
)
from .tls import (
    async_learn_fingerprint,
    fingerprint_ssl,
    format_fingerprint,
    normalize_fingerprint,
)

_LOGGER = logging.getLogger(__name__)

# WebSocket reconnect backoff bounds (seconds).
INITIAL_RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60
# Small random addition to each reconnect wait, so multiple gateways/entries on
# the same network don't all retry in lockstep after a shared network blip.
RECONNECT_JITTER = 0.5
# The repair issue for a dead push channel is raised on the first failed
# reconnect once the outage has lasted WEBSOCKET_OUTAGE_REPAIR_AFTER (const.py,
# with the rationale for the value): elapsed time since the outage began, not
# a count of attempts — a count reached five in ~15-20 s of backoff, inside
# every ordinary gateway reboot. Below the threshold the blip rides out
# silently; past it the gateway has been unreachable long enough that the user
# is unknowingly running on the REST poll alone and deserves to be told.
#
# How long a session must stay up before it counts as a genuine recovery rather
# than a flap. Resetting the backoff at the moment of connect made the escalation
# unreachable: a gateway that accepts the upgrade and drops us immediately (a
# reboot loop, a websocket server restart cycle, a client limit) would reconnect
# roughly once a second forever, never raising the repair issue and flapping every
# controllable entity. A session shorter than this is treated as a failed attempt
# and does not end the outage, so its clock keeps running through the flap.
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
# timeout rather than the gateway's specific error text. (Were the firmware
# ever to echo the message_id on that frame, `_reject_pending_reply` fails
# the command at once instead.)
COMMAND_REPLY_TIMEOUT = 5
# Repair-issue translation key for that "live push is dead" state.
ISSUE_PUSH_FAILURE = "websocket_push_failure"
# Repair-issue translation key for "the gateway presents a certificate other
# than the pinned one" (see `_report_fingerprint_mismatch`). Fixable: the fix
# flow in repairs.py re-learns the fingerprint, but only once the user has
# confirmed the gateway was reset or replaced — never silently.
ISSUE_TLS_MISMATCH = "tls_certificate_changed"
# Repair-issue translation key for "two or more gateway labels resolve to the
# same device slug, so only one of them gets entities" (`duplicate_slugs`).
# Raised and withdrawn by the capability watcher in __init__.py, which already
# computes the collisions on every device-list adoption; not fixable here —
# only a rename in the JUNG HOME app resolves it.
ISSUE_DUPLICATE_LABELS = "duplicate_device_labels"

# Minimum spacing, in seconds, between two reads of the gateway's project
# export (`GET /project/junghome`) after the one at setup. The export is read
# again only when an adopted device list carries a function we hold no
# hardware identity for — a device the user just added in the app — and
# never more often than this: the document is the whole mesh project (every
# node, group and scene, hundreds of kilobytes on a large installation) and
# the app re-uploads it to the gateway a few seconds after each change, so a
# fresh read a few minutes later catches the addition without re-reading the
# project on every poll. A gateway whose firmware lacks the endpoint answers
# 404 at this cadence for as long as unidentified functions exist — one small
# request every ten minutes, logged at DEBUG.
NODE_IDENTITY_REFETCH_INTERVAL = 600

# How often, in seconds, the device *properties* the function list lacks are
# re-read from the deprecated verbose device endpoint (`GET /devices/{id}?
# verbose=true`, ~8 KB per device — probed 2026-09-16): a metering socket's
# cumulative energy counter, every device's firmware revision, reachability.
# The middleware itself re-polls a device's properties every five minutes
# (`profile.dirtyAfterSeconds` 300), so reading more often buys nothing. Only
# the devices with an energy counter are re-read each interval; the full list
# (~190 KB on 49 devices) is read once at setup and again only when a function
# appears that the last answer did not list (one the endpoint omits is not
# asked for again — it would be omitted again; only a missing endpoint or a
# failed read is retried, and that is one small request).
DEVICE_PROPERTIES_REFRESH_INTERVAL = 300

# The entry's persisted device-slug -> element map behind rename following
# (`JungHomeDataUpdateCoordinator.follow_renames`): one small document per
# entry, saved a few seconds after it changes — which is on membership change
# only. `function_anchors_store` builds the store; setup loads it before the
# first refresh and entry removal deletes it.
FUNCTION_ANCHORS_STORAGE_VERSION = 1
FUNCTION_ANCHORS_SAVE_DELAY = 5


def function_anchors_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the entry's store for `coordinator.function_anchors`."""
    return Store(
        hass, FUNCTION_ANCHORS_STORAGE_VERSION, f"{DOMAIN}.{entry_id}.functions"
    )


# Diagnostics: a bounded log of the most recent raw WebSocket frames so a
# downloadable report shows what the gateway actually sends (the connect-time
# handshake — version/message/functions/groups/scenes — plus live datapoint
# pushes) against how we parse it. Frames carry no secrets (the token is a
# connect header, never a frame body), but can be large, so each is truncated and
# only the most recent are kept.
WS_FRAME_LOG_SIZE = 60
WS_FRAME_MAX_CHARS = 2000
# The sixteen frame types the gateway's WebSocket server enumerates
# (`WebSocketMessageType`, api-server `websocket-server-service.js:36-53`;
# the table in docs/gateway-websocket.md). Three of them — `devices`,
# `config` and `state` — have their emitters commented out on current
# firmware and never arrive, but the set is the server's own vocabulary,
# kept whole so a firmware that re-enables one is still captured complete.
# The latest frame of each is kept IN FULL in `ws_last_frame_by_type`, so a
# report always carries the complete handshake (and the `groups-new` /
# `groups-deleted` deltas an app edit produces). The `type` field is the
# peer's to fill in, though, so a type outside this vocabulary is stored
# truncated, and only while the store holds fewer than WS_FRAME_TYPES_MAX
# distinct types — a peer minting a new type per frame could otherwise grow
# the store without bound, each entry a full frame.
WS_KNOWN_FRAME_TYPES = frozenset(
    {
        "message",
        "version",
        "functions",
        "groups",
        "groups-new",
        "groups-deleted",
        "scenes",
        "scenes-new",
        "scenes-deleted",
        "devices",
        "devices-new",
        "devices-deleted",
        "config",
        "datapoint",
        "scene",
        "state",
    }
)
WS_FRAME_TYPES_MAX = 32


def _truncate_frame(raw: str) -> str:
    """Cut a raw frame down to WS_FRAME_MAX_CHARS for the rolling frame log."""
    if len(raw) > WS_FRAME_MAX_CHARS:
        return raw[:WS_FRAME_MAX_CHARS] + "…[truncated]"
    return raw


# Sanity bounds for a gateway-advertised colour-temperature range. Anything
# outside this is not a plausible tunable-white range and is treated as an
# unrecognised payload rather than trusted (a bogus range would otherwise be
# declared to Home Assistant, which enforces it against the user).
MIN_PLAUSIBLE_KELVIN = 1000
MAX_PLAUSIBLE_KELVIN = 20000

# The gateway state DB's declared defaults for the `version` topic: the
# middleware ships these until the board controller has answered
# `MSG_SW_VERSION_IND`, so they mean "not known yet", not "version 0".
UNREAD_VERSION_RELEASE = "0.0.0"
UNREAD_VERSION_BUILD = "0"


def _clean_version_field(raw: Any) -> str | None:
    """Return a stripped, non-empty string version field, else ``None``."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# Config entry carrying the coordinator as runtime_data.
type JungHomeConfigEntry = ConfigEntry[JungHomeDataUpdateCoordinator]


def poll_interval_from_options(options: Mapping[str, Any]) -> int:
    """Return the configured poll interval in seconds, defaulted and clamped.

    The options form already bounds the value, but the stored option is still
    re-validated here: an entry written by an older version has no value (use
    the default), and one edited by hand or migrated oddly may hold anything.
    A malformed value falls back to the default rather than raising out of
    coordinator construction (which would fail the whole entry setup over a
    tuning knob); a numeric one is clamped into
    [MIN_POLL_INTERVAL_SECONDS, MAX_POLL_INTERVAL_SECONDS] so a hand-edited
    ``1`` cannot hammer the gateway and a huge value cannot effectively
    disable the poll backstop. ``bool`` is rejected explicitly (it is an
    ``int`` subclass, and ``True`` is not an interval); ``int()`` on a
    non-finite float raises ``ValueError``/``OverflowError``, both caught.
    """
    raw = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        seconds = int(float(raw))
    except (ValueError, OverflowError):
        return DEFAULT_POLL_INTERVAL_SECONDS
    return max(MIN_POLL_INTERVAL_SECONDS, min(MAX_POLL_INTERVAL_SECONDS, seconds))


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


def entry_derived_issue_ids(entry_id: str) -> list[str]:
    """Return the ids of the entry's repair issues re-derived from gateway state.

    The health issues (one per ``health.HEALTH_CONDITIONS``) and the
    colliding-label issue; ``stop()`` and entry removal withdraw them all.
    """
    return [*health_issue_ids(entry_id), f"{ISSUE_DUPLICATE_LABELS}_{entry_id}"]


# Registry lookups scoped to one config entry, on every supported core.
#
# HA 2026.9 made device identifiers and connections unique per config entry
# (not registry-wide), added ``async_get_device_by_identifier`` /
# ``async_get_device_by_connection`` for the scoped lookup, and deprecated the
# registry-wide ``async_get_device`` (removed in 2027.8; its ``report_usage``
# raises when no integration frame is on the stack). Older cores have only the
# registry-wide call. Every device this integration looks up is one of its own
# entry's, so a walk of that entry's devices asks the same question wherever
# the scoped lookup is missing — and the deprecated call is made on no core.
# Feature-detected (``getattr``), never version-compared; the floor is
# HA 2025.12.4.
def device_by_identifier(
    registry: dr.DeviceRegistry, entry_id: str, identifier: tuple[str, str]
) -> dr.DeviceEntry | None:
    """Return the config entry's device holding ``identifier``, if any."""
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return cast("dr.DeviceEntry | None", lookup(identifier, entry_id))
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if identifier in device.identifiers
        ),
        None,
    )


def device_by_connection(
    registry: dr.DeviceRegistry, entry_id: str, connection: tuple[str, str]
) -> dr.DeviceEntry | None:
    """Return the config entry's device holding ``connection``, if any."""
    lookup = getattr(registry, "async_get_device_by_connection", None)
    if lookup is not None:
        return cast("dr.DeviceEntry | None", lookup(connection, entry_id))
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if connection in device.connections
        ),
        None,
    )


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
        # The id of the device that owns that datapoint (same one-dispatch
        # lifetime). Entities compare it against their own device to skip the
        # state write for a push that cannot concern them — see
        # ``JungHomeEntity._skip_foreign_device_push`` for the full contract.
        # None when the owning device carries no id, which entities treat as
        # "don't skip" (fail open).
        self.pushed_device_id: str | None = None
        # Gateway firmware version, reported by the WebSocket "version" frame.
        # The gateway's own SOFTWARE version, e.g. "2.1.3 (2840)", fetched
        # over REST (`async_fetch_gateway_version`). This is what a device page
        # should show as `sw_version`.
        self.gateway_version: str | None = None
        # Device-registry id of the synthetic gateway (hub) device, set by
        # ``async_setup_entry`` right after it registers the hub and before any
        # platform loads. Entities link their device to the hub through it
        # (``via_device_id``) on cores that know that key — see
        # ``JungHomeEntity.device_info``.
        self.gateway_device_registry_id: str | None = None
        # The REST/WebSocket API version the gateway implements, e.g. "1.5.0",
        # announced in the WebSocket handshake's `version` frame. It is
        # `api-junghome`'s own package version — a protocol number, NOT the
        # firmware — so it is surfaced in diagnostics only. It was previously
        # stamped on every device as `sw_version`, which reported "1.5.0" for a
        # gateway running firmware 2.1.3.
        self.api_version: str | None = None
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
        # — the scheduled poll (`_handle_refresh_interval`), a manual
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
        # can change device *membership*. Listeners whose work depends only on
        # the adopted list (the stale-device pruner, the area assigner and the
        # capability watcher in __init__.py) compare this instead of counting
        # raw dispatches: pushes, scenes broadcasts and the WS-drop
        # notification all call async_update_listeners too, and counting those
        # shrank the pruner's 10-poll window during a WS flap or a
        # scene-editing session while a device was transiently missing from
        # one poll — and re-running the assigner/watcher's O(devices) walks on
        # every push was steady waste on a chatty gateway.
        self.data_generation = 0
        # Monotonic count of adopted `functions` broadcasts, bumped only by
        # `_handle_functions_broadcast`. A poll snapshots it when its fetch
        # starts; a change by the time the fetch returns means a fresher,
        # authoritative membership was adopted mid-flight and the poll's older
        # snapshot is discarded (see `_async_update_data`). Deliberately a
        # separate counter from `data_generation`: polls bump that too, so
        # comparing generations would make overlapping polls discard each
        # other's (equally fresh) snapshots.
        self._functions_broadcasts_seen = 0
        # Stable-slug -> volatile device id, to detect firmware-update id changes.
        self._device_ids: dict[str, str] = {}
        # Device slug -> the element that function was last seen on
        # (`models.FunctionAnchor`), so a function renamed in the app keeps
        # its Home Assistant device (`follow_renames`). Loaded from the entry's
        # store by `attach_function_anchors` before the first refresh; empty —
        # and rename following off — on a bare coordinator.
        self.function_anchors: dict[str, FunctionAnchor] = {}
        # The renames the latest ``follow_renames`` pass followed, new slug ->
        # old slug, for the listeners that keep their own slug-keyed state
        # (the capability watcher's baselines) to carry it across.
        self.followed_renames: dict[str, str] = {}
        self._anchor_store: Store[dict[str, Any]] | None = None
        # A delayed save of the map is scheduled and has not written yet;
        # ``stop()`` flushes it (see there).
        self._anchor_save_pending = False
        # Gateway function id -> the hardware identity of the mesh element
        # behind it (node UUID, Bluetooth address, unicast, location), parsed
        # from `GET /project/junghome` at setup (`async_fetch_node_identities`)
        # and re-read, debounced, when a device list carries an id with no
        # entry here. Read-only: it is replaced wholesale, never mutated, and
        # it never holds anything from the export beyond those fields — the
        # export also carries the mesh keys, which are dropped with the
        # document. Empty on firmware without the endpoint (< API 1.5.0).
        self.node_identities: Mapping[str, NodeIdentity] = MappingProxyType({})
        # `time.monotonic()` of the last export read (success or not); None
        # until the setup-time read has run, which also gates the debounced
        # re-reads below so an adoption during the first refresh cannot
        # schedule a second read alongside it.
        self._node_identity_fetched_at: float | None = None
        # The in-flight debounced re-read, so adoptions arriving while one is
        # running do not stack more.
        self._node_identity_task: asyncio.Task[None] | None = None
        # Function id -> what the verbose device endpoint adds to that function
        # (`models.DeviceProperties`): the energy counter of a metering socket,
        # the device's firmware revision, reachability. Read once at setup
        # (`async_fetch_device_properties`), the energy counters re-read every
        # DEVICE_PROPERTIES_REFRESH_INTERVAL (`_async_refresh_device_properties`,
        # armed by `start`). Replaced wholesale, never mutated. Empty on
        # firmware without the endpoint.
        self.device_properties: Mapping[str, DeviceProperties] = MappingProxyType({})
        # The live function ids at the last full-list read the endpoint
        # answered (None until it has): a function outside this set is new
        # since, and asks for the list again; one inside it that the map does
        # not cover was omitted by the gateway and is not asked for again.
        self._properties_listed_for: frozenset[str] | None = None
        self._properties_unsub: CALLBACK_TYPE | None = None
        self._properties_refresh_running = False
        # The gateway's health log (`GET /healthstatus/`, health.py) as last
        # read — diagnostics, and the source of the health repair issues
        # (`async_fetch_health_status`). Read once after the first refresh,
        # then every HEALTH_STATUS_REFRESH_INTERVAL (armed by `start`).
        self.health = HealthState(config_entry.entry_id)
        # Per-platform (entity-domain -> unique_ids) sets shared with each
        # platform's discovery. They are the add-once duplicate guard; the stale
        # device pruner clears a removed device's ids from them (see
        # ``forget_device_unique_ids``) so a device that reappears is re-added.
        self._known_unique_ids: dict[str, set[str]] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._closing = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        # Consecutive failed reconnects; reset once a session proves stable (see
        # ``_mark_session_stable``), not merely on a successful handshake. Shown
        # in the repair issue; the escalation itself is driven by the clock below.
        self._reconnect_failures = 0
        # ``time.monotonic()`` of the first failed reconnect of the current
        # outage, or None while no outage is in progress. Started by
        # ``_note_reconnect_failure`` and ended by ``_mark_session_stable`` only
        # — a flapping session leaves it running, so a reboot loop escalates
        # exactly like a dead gateway. Monotonic rather than wall-clock so an
        # NTP step during the outage cannot lengthen or shorten it.
        self._outage_started_at: float | None = None
        # Whether the current outage has already produced its one WARNING, so the
        # retry loop degrades to DEBUG instead of warning once a minute forever.
        self._unavailable_logged = False
        # Repair-issue id, scoped to this entry so two gateways each report
        # their own outage instead of overwriting one shared issue.
        self._push_failure_issue_id = f"{ISSUE_PUSH_FAILURE}_{config_entry.entry_id}"
        # TLS certificate pinning (tls.py). Every request and the WebSocket
        # upgrade pass ``ssl=`` from ``_async_ssl``: the fingerprint stored in
        # the entry, or — for an entry created before pinning existed — one
        # learned from the gateway on first contact and held here until the
        # first authenticated fetch on it succeeds, at which point
        # ``_persist_learned_fingerprint`` writes it into the entry (trust on
        # first use, against the entry's CURRENT host). Read from the entry
        # live rather than snapshotted at construction, so a fix flow that
        # re-pins reaches the next request even before its reload lands.
        self._learned_fingerprint: str | None = None
        # Per-entry repair issue for a mismatch (``_report_fingerprint_mismatch``),
        # and whether it is currently raised, so a poll that succeeds again
        # (a transient impostor, or the real gateway back on its address)
        # clears it without touching the registry on every healthy poll.
        self._tls_issue_id = f"{ISSUE_TLS_MISMATCH}_{config_entry.entry_id}"
        self._tls_mismatch_reported = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Jung Home",
            # Options-configurable (default 60 s, clamped — see
            # `poll_interval_from_options`). An options change reloads the
            # entry via the update listener in __init__.py, so a new interval
            # takes effect by rebuilding the coordinator; nothing re-reads the
            # option mid-flight.
            update_interval=timedelta(
                seconds=poll_interval_from_options(config_entry.options)
            ),
        )

    def _record_error(self, err: BaseException) -> None:
        """Remember the most recent REST/WebSocket failure for diagnostics."""
        self.last_error = str(err)
        self.last_error_at = dt_util.utcnow()

    async def _async_update_data(self) -> list[Device]:
        """Fetch data from the API."""
        _LOGGER.debug("Fetching new device data from Jung Home API")
        # Snapshot the broadcast counter at fetch start: if it moves while the
        # HTTP request is in flight, a `functions` broadcast adopted a fresher
        # authoritative device list mid-poll and this poll's older snapshot
        # must not overwrite its membership (checked after the fetch below).
        broadcasts_seen = self._functions_broadcasts_seen
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
        except aiohttp.ServerFingerprintMismatch as err:
            # The responder at the stored address is not the gateway this
            # entry pinned. aiohttp raised this at the TLS handshake, before
            # the request — so the token never left — and it must stay that
            # way: no fallback, no re-learn. Surface it as a repair issue
            # (fixable only by the user confirming the gateway was reset or
            # replaced) and fail the poll so every entity reads unavailable
            # rather than quietly polling a stranger every minute. Ordered
            # before the generic ClientError arm it would otherwise land in.
            self._report_fingerprint_mismatch(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="certificate_changed",
                translation_placeholders={"host": str(self.config["host"])},
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

        # The gateway answered an authenticated request on the pinned
        # connection: an entry that was still learning its fingerprint keeps
        # it from here on, and a mismatch reported earlier (a transient
        # impostor, or the gateway back on its address) is over.
        self._persist_learned_fingerprint()
        self._clear_fingerprint_mismatch()
        _LOGGER.debug("API Response: %s", response)
        if self._functions_broadcasts_seen != broadcasts_seen and self.data is not None:
            # A `functions` broadcast adopted a fresher, authoritative device
            # list while this fetch was in flight. The fetch started before
            # the broadcast, so its snapshot predates the membership change —
            # adopting it would resurrect removed devices and drop just-added
            # ones until the next poll healed it (the per-datapoint overlay
            # only covers *values*, not membership). Keep the broadcast's
            # list instead: every value change since the snapshot was pushed,
            # and the live merge path writes pushes into `self.data` (the
            # broadcast's dicts) directly, so discarding the snapshot loses
            # nothing. No `await` sits between the fetch completing and this
            # check, so the counter comparison exactly covers the fetch
            # window.
            #
            # Everything derived from the stale snapshot is skipped with it:
            # the overlay re-apply (its targets are already current in
            # `self.data`), the id-churn check (`_handle_functions_broadcast`
            # already ran it against the broadcast list — running it on the
            # OLD list here would false-flag the pre-broadcast ids as churn
            # and schedule a needless reload), and the `data_generation`
            # bump (listeners already counted this membership when the
            # broadcast adopted it; advancing again would double-count one
            # change as two polls in the pruner's miss debounce, and the
            # no-op dispatch that follows is cheap because the
            # generation-guarded listeners skip an unchanged generation).
            _LOGGER.debug(
                "A functions broadcast superseded this poll's snapshot; "
                "keeping the broadcast's device list"
            )
            return self.data
        if overlay:
            self._apply_push_overlay(response, overlay)
        self._reload_if_device_ids_changed(response)
        self.follow_renames(response)
        self._schedule_node_identity_refetch(response)
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

    def attach_function_anchors(
        self, store: Store[dict[str, Any]], document: dict[str, Any] | None
    ) -> None:
        """Adopt the entry's persisted slug -> element map (see ``follow_renames``).

        Called by setup before the first refresh, so a rename that happened
        while Home Assistant was down is followed on that refresh — before the
        platforms register anything under the new label.
        """
        self._anchor_store = store
        self.function_anchors = parse_function_anchors(document)

    @callback
    def follow_renames(self, devices: list[Device]) -> None:
        """Keep a function's HA device and entities when it is renamed in the app.

        Identity is label-derived (``device_slug``), so a rename used to be a
        new device: the old one pruned after ``STALE_DEVICE_PRUNE_MISSES``
        adoptions, its history, area and customisations left behind. The
        gateway's function id, volatile across re-provisioning, does NOT
        change on a rename (``md5(node UUID + element location)``), and the
        node's Bluetooth address plus element location survives even that. So
        a label with no registry device while a label this map knows has just
        vanished — both on the same element — is a rename: the old device's
        identifier and its entities' unique_ids are rewritten to the new slug
        in place (``_migrate_renamed_function``) BEFORE the platforms see the
        list, so nothing registers twice. Entity ids stay (Home Assistant
        never renames those on its own); the device name follows the label.

        Runs on every device-list adoption, on the list about to be adopted,
        and on setup's second pass once the identities are known. Not
        followed, by design: a label moved to a *different* element (the old
        label reused on another node, two labels swapped) stays label-keyed —
        the entities follow the name, as before; a rename combined with a
        re-provisioning is paired at setup only (live, the new function's
        identity is not known when its list arrives, so it is a new device).
        Colliding slugs are skipped like everywhere else (``duplicate_slugs``).
        """
        entry = self.config_entry
        if entry is None or self._anchor_store is None:
            return  # a bare coordinator: nothing persisted, nothing to follow
        colliding = duplicate_slugs(devices)
        live = {
            device_slug(d): d
            for d in devices
            if isinstance(d.get("id"), str) and device_slug(d) not in colliding
        }
        dev_reg = dr.async_get(self.hass)
        followed: dict[str, str] = {}
        for slug, device in live.items():
            old_slug = self._renamed_from(slug, device, live, dev_reg, entry.entry_id)
            if old_slug is not None and self._migrate_renamed_function(
                old_slug, slug, device, entry.entry_id
            ):
                followed[slug] = old_slug
        self.followed_renames = followed
        # Every live function, plus the vanished ones whose device still
        # exists (the pruner's window, or one partial list) so a rename that
        # lands an adoption later still pairs.
        fresh = {
            slug: self._anchor_for(device, self.function_anchors.get(slug))
            for slug, device in live.items()
        }
        for slug, anchor in self.function_anchors.items():
            if slug in fresh:
                continue
            if (
                device_by_identifier(dev_reg, entry.entry_id, (DOMAIN, slug))
                is not None
            ):
                fresh[slug] = anchor
        if fresh != self.function_anchors:
            self.function_anchors = fresh
            self._anchor_save_pending = True
            self._anchor_store.async_delay_save(
                self._anchor_document, FUNCTION_ANCHORS_SAVE_DELAY
            )

    def _renamed_from(
        self,
        slug: str,
        device: Device,
        live: Mapping[str, Device],
        dev_reg: dr.DeviceRegistry,
        entry_id: str,
    ) -> str | None:
        """Return the vanished slug whose element ``device`` now carries, if exactly one."""
        if device_by_identifier(dev_reg, entry_id, (DOMAIN, slug)) is not None:
            return None  # registered under this label already: nothing to follow
        identity = self.node_identity_for(device)
        candidates = [
            old
            for old, anchor in self.function_anchors.items()
            if old not in live and anchor.matches(str(device["id"]), identity)
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _migrate_renamed_function(
        self, old_slug: str, new_slug: str, device: Device, entry_id: str
    ) -> bool:
        """Rewrite ``old_slug``'s device and entities to ``new_slug``, in place.

        All-or-nothing: every entity is checked for a free target unique_id and
        the device's new identifier is claimed before any entity is touched,
        so the device can never end up half-renamed.
        Option values keyed by unique_id (the inverted covers) follow too.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        old_device = device_by_identifier(dev_reg, entry_id, (DOMAIN, old_slug))
        if old_device is None:
            return False
        label = str(device.get("label"))
        prefix = f"{old_slug}_"
        renames: list[tuple[er.RegistryEntry, str]] = []
        for entity in er.async_entries_for_device(
            ent_reg, old_device.id, include_disabled_entities=True
        ):
            if entity.platform != DOMAIN or not entity.unique_id.startswith(prefix):
                continue  # not ours, or not keyed by the slug: left alone
            new_uid = f"{new_slug}_{entity.unique_id[len(prefix) :]}"
            if ent_reg.async_get_entity_id(entity.domain, DOMAIN, new_uid) is not None:
                _LOGGER.warning(
                    "Jung Home: %s was renamed to %s in the app, but an entity "
                    "%s already exists; treating it as a new device",
                    old_device.name,
                    label,
                    new_uid,
                )
                return False
            renames.append((entity, new_uid))
        # The device first: before Home Assistant 2026.9 identifiers are unique
        # registry-wide, so another gateway's entry may already hold the new
        # slug. Rewriting the entities before finding that out would leave
        # them keyed by a device that never followed — and the next adoption
        # would try again and fail the poll.
        identifiers = {
            (DOMAIN, new_slug) if identifier == (DOMAIN, old_slug) else identifier
            for identifier in old_device.identifiers
        }
        try:
            dev_reg.async_update_device(
                old_device.id, new_identifiers=identifiers, name=label
            )
        except dr.DeviceIdentifierCollisionError as err:
            _LOGGER.warning(
                "Jung Home: %s was renamed to %s in the app, but %s; treating it "
                "as a new device",
                old_device.name,
                label,
                err,
            )
            return False
        renamed_uids: dict[str, str] = {}
        for entity, new_uid in renames:
            ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_uid)
            renamed_uids[entity.unique_id] = new_uid
            known = self._known_unique_ids.get(entity.domain)
            if known is not None and entity.unique_id in known:
                known.discard(entity.unique_id)
                known.add(new_uid)
        self._follow_rename_in_entry(old_slug, new_slug, renamed_uids)
        _LOGGER.info(
            "Jung Home: %s was renamed to %s in the app; its Home Assistant device "
            "and %d entities follow (entity ids unchanged)",
            old_device.name,
            label,
            len(renames),
        )
        return True

    def _follow_rename_in_entry(
        self, old_slug: str, new_slug: str, renamed_uids: Mapping[str, str]
    ) -> None:
        """Re-point what the entry keys by slug or unique_id.

        The area assigner's record of devices it already considered is keyed
        by slug; left behind, a renamed device whose area the user cleared on
        purpose would be placed again. The options carry the inverted covers.
        """
        entry = self.config_entry
        if entry is None:  # pragma: no cover - follow_renames returned already
            return
        considered = entry.data.get(DATA_AREA_ASSIGNED)
        if isinstance(considered, list) and old_slug in considered:
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    DATA_AREA_ASSIGNED: sorted({*considered, new_slug} - {old_slug}),
                },
            )
        self._follow_rename_in_options(renamed_uids)

    def _follow_rename_in_options(self, renamed_uids: Mapping[str, str]) -> None:
        """Re-point the options keyed by unique_id (the inverted covers).

        The snapshot moves with the options, so the update listener sees no
        options change and does not reload: a live cover keeps the flag it was
        built with, and the next setup reads it under the new unique_id. A
        stale snapshot would instead turn the next unrelated entry write into
        a reload (and, live, reload in the middle of the adoption).
        """
        entry = self.config_entry
        if entry is None:  # pragma: no cover - follow_renames returned already
            return
        flagged = entry.options.get(CONF_INVERTED_COVERS)
        if not isinstance(flagged, list) or not any(
            uid in renamed_uids for uid in flagged
        ):
            return
        options = {
            **entry.options,
            CONF_INVERTED_COVERS: [renamed_uids.get(uid, uid) for uid in flagged],
        }
        self.options_snapshot = dict(options)
        self.hass.config_entries.async_update_entry(entry, options=options)

    def _anchor_for(
        self, device: Device, previous: FunctionAnchor | None
    ) -> FunctionAnchor:
        identity = self.node_identity_for(device)
        if identity is None and previous is not None and previous.id == device["id"]:
            # No identity this time (the export read failed, or has not run
            # yet), but the same element as before: keep the address and
            # location it was anchored with. Dropping them would quietly turn
            # off pairing a later rename combined with re-provisioning.
            return previous
        return FunctionAnchor(
            id=str(device["id"]),
            mac=identity.mac if identity is not None else None,
            location=identity.location if identity is not None else None,
        )

    @callback
    def _anchor_document(self) -> dict[str, Any]:
        self._anchor_save_pending = False
        return {
            "functions": {
                slug: asdict(anchor) for slug, anchor in self.function_anchors.items()
            }
        }

    def _stored_fingerprint(self) -> str | None:
        """Return the fingerprint pinned in the entry, if it holds a valid one."""
        entry = self.config_entry
        if entry is None:  # pragma: no cover - an entry coordinator always has one
            return None
        return normalize_fingerprint(entry.data.get(CONF_TLS_FINGERPRINT))

    async def _async_learn_fingerprint(self, host: str) -> str:
        """Learn the certificate fingerprint ``host`` presents (see tls.py).

        A thin, patchable seam over the module helper: the test suite stubs it
        the way it stubs every other network read, so the setup fixtures never
        open a socket.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        return await async_learn_fingerprint(session, host)

    async def _async_ssl(self) -> aiohttp.Fingerprint:
        """Return the ``ssl=`` argument every request and WS upgrade must pass.

        The pinned certificate: the fingerprint stored in the entry, else the
        one learned earlier in this coordinator's life, else — trust on first
        use, for an entry created before pinning existed — the one the
        gateway at the entry's CURRENT host presents right now. The learn is
        a bare TLS handshake that aiohttp aborts before any request (tls.py),
        so even that first contact sends no token to an unverified peer: the
        request that follows is already pinned to what the learn saw. Held in
        ``_learned_fingerprint`` and written into the entry only once an
        authenticated fetch has succeeded on it
        (``_persist_learned_fingerprint``).
        """
        fingerprint = self._stored_fingerprint() or self._learned_fingerprint
        if fingerprint is None:
            fingerprint = await self._async_learn_fingerprint(self.config["host"])
            self._learned_fingerprint = fingerprint
            _LOGGER.info(
                "Learned the Jung Home gateway's TLS certificate fingerprint "
                "(%s); it is pinned from now on",
                format_fingerprint(fingerprint),
            )
        return fingerprint_ssl(fingerprint)

    @callback
    def _persist_learned_fingerprint(self) -> None:
        """Store the fingerprint learned on first use once it has proven itself.

        Called after every successful authenticated ``/functions`` fetch; a
        no-op unless this coordinator learned the fingerprint itself and the
        entry still lacks one. The write touches neither host, token nor
        options, so the entry's update listener does not reload it.
        """
        entry = self.config_entry
        if (
            self._learned_fingerprint is None
            or entry is None
            or self._stored_fingerprint() is not None
        ):
            return
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TLS_FINGERPRINT: self._learned_fingerprint},
        )

    @callback
    def _report_fingerprint_mismatch(
        self, err: aiohttp.ServerFingerprintMismatch
    ) -> None:
        """Raise the repair issue for a responder with the wrong certificate.

        Shared by the REST poll and the WebSocket loop: whichever hits the
        mismatch first reports it, the other re-reports the same issue id.
        The issue names both digests so a user comparing against the JUNG
        app or the gateway itself can tell a regenerated certificate from an
        impostor; its fix flow (repairs.py) re-pins only after the user
        confirms. ``last_error`` gets a readable line rather than aiohttp's
        tuple repr (the host in it is scrubbed by diagnostics as usual).
        """
        expected = err.expected.hex()
        observed = err.got.hex()
        self.last_error = (
            f"TLS certificate of {self.config['host']} changed: expected "
            f"{format_fingerprint(expected)}, got {format_fingerprint(observed)}"
        )
        self.last_error_at = dt_util.utcnow()
        # Once per outage, not once per coordinator: a mismatch on the first
        # refresh leaves the entry in SETUP_RETRY, and every retry (up to one
        # every ten minutes, for as long as the mismatch lasts) builds a fresh
        # coordinator with this flag cleared. The issue in the registry is
        # the memory that spans those rebuilds — it is only ever withdrawn
        # when the pinned gateway answers again or the entry goes away.
        if not self._tls_mismatch_reported and (
            ir.async_get(self.hass).async_get_issue(DOMAIN, self._tls_issue_id) is None
        ):
            _LOGGER.error(
                "The Jung Home gateway at %s presents a TLS certificate that "
                "does not match the pinned one (expected %s, got %s); refusing "
                "to send the access token until the change is confirmed in "
                "Settings > Repairs",
                self.config["host"],
                format_fingerprint(expected),
                format_fingerprint(observed),
            )
        self._tls_mismatch_reported = True
        entry_id = self.config_entry.entry_id if self.config_entry else ""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._tls_issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_TLS_MISMATCH,
            translation_placeholders={
                "host": str(self.config["host"]),
                "expected": format_fingerprint(expected),
                "observed": format_fingerprint(observed),
            },
            data={"entry_id": entry_id},
        )

    @callback
    def _clear_fingerprint_mismatch(self) -> None:
        """Withdraw the mismatch issue once the pinned gateway answers again.

        The registry, not this coordinator's flag, says whether there is one:
        a mismatch on the first refresh raised it from a coordinator that the
        SETUP_RETRY rebuild threw away, and the one that finally reaches the
        pinned gateway never raised anything (the report side keys on the
        registry for the same reason).
        """
        if (
            not self._tls_mismatch_reported
            and ir.async_get(self.hass).async_get_issue(DOMAIN, self._tls_issue_id)
            is None
        ):
            return
        self._tls_mismatch_reported = False
        _LOGGER.info(
            "The Jung Home gateway at %s presents the pinned TLS certificate again",
            self.config["host"],
        )
        ir.async_delete_issue(self.hass, DOMAIN, self._tls_issue_id)

    async def _fetch_devices_from_api(self, host: str, token: str) -> list[Device]:
        """Fetch devices from the Jung Home API."""
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop. The pin
        # (`ssl=`) is what actually authenticates the peer — see tls.py.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/functions"
        headers = {"token": f"{token}", "Content-Type": "application/json"}

        async with asyncio.timeout(30):
            async with session.get(url, headers=headers, ssl=ssl) as response:
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
        # trust boundary: untyped gateway JSON becomes the typed `Device` model
        # (`sanitize_devices` drops or repairs malformed objects — the same
        # boundary the `functions` broadcast passes through). Downstream code
        # keeps defensive `.get(...)` access for absent keys.
        return sanitize_devices(data)

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
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/groups"
        headers = {"token": f"{token}", "Content-Type": "application/json"}
        async with asyncio.timeout(30):
            async with session.get(url, headers=headers, ssl=ssl) as response:
                response.raise_for_status()
                data = await response.json()
        if not isinstance(data, list):
            return []
        return [g for g in data if isinstance(g, dict)]

    async def _fetch_version_from_api(self, host: str) -> dict[str, Any] | None:
        """Best-effort read of ``GET /version/`` — the gateway's version numbers.

        The one unauthenticated data endpoint: it returns ``api_version``
        (``api-junghome``'s package version, the API contract) next to
        ``version_release`` / ``version_build`` (the gateway's own software
        version, from the middleware's ``version`` topic —
        ``01_version-controller.js:32-42``). No token is sent; the request is
        still pinned to the gateway's certificate like every other one.
        Firmware before API 1.5.0 answers with ``api_version`` only.

        Returns the decoded object, or ``None`` for any transport failure,
        non-200 or non-object body — callers treat that as "unknown".
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/version/"
        try:
            ssl = await self._async_ssl()
            async with (
                asyncio.timeout(30),
                session.get(url, ssl=ssl) as response,
            ):
                if response.status != 200:
                    return None
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read the gateway version: %s", err)
            return None
        return data if isinstance(data, dict) else None

    async def async_fetch_gateway_version(self) -> None:
        """Populate ``gateway_version`` (and ``api_version``) from ``GET /version/``.

        The WebSocket handshake's ``version`` frame carries the *API* version
        (``api-junghome``'s package version, "1.5.0"), which was being stamped
        on the hub and on every device as ``sw_version`` — so a gateway running
        firmware 2.1.3 build 2840 reported "1.5.0" on every device page.

        The real value lives in the middleware's ``version`` topic, populated
        from the board controller's ``MSG_SW_VERSION_IND`` (which reports e.g.
        ``"2.1.3 Release (2840)"``; the middleware splits the parenthesised
        build into ``version_build`` and keeps the rest as
        ``version_release``). The unauthenticated ``/version/`` reply carries
        both, so this is one token-less request rather than two authenticated
        ``config/parameter`` reads.

        Best-effort, like the groups and scenes fetches: a version string is not
        worth failing setup over, and an older firmware without the fields
        just leaves the previous value in place. ``"0.0.0"`` is the state DB's
        declared default and means the middleware has not read the board yet —
        treated as unknown rather than published as a version.
        """
        data = await self._fetch_version_from_api(self.config["host"])
        if data is None:
            _LOGGER.debug("Gateway version not available")
            return
        api_version = data.get("api_version")
        if isinstance(api_version, str) and api_version.strip():
            # The same number the WebSocket handshake announces; REST makes
            # it known before the socket connects (or when it never does).
            self.api_version = api_version.strip()
        release = _clean_version_field(data.get("version_release"))
        if not release or release == UNREAD_VERSION_RELEASE:
            _LOGGER.debug("Gateway software version not available yet")
            return
        build = _clean_version_field(data.get("version_build"))
        version = (
            f"{release} ({build})"
            if build and build != UNREAD_VERSION_BUILD
            else release
        )
        if version == self.gateway_version:
            return
        self.gateway_version = version
        _LOGGER.info("Jung Home gateway software version: %s", self.gateway_version)
        self._apply_gateway_version()

    async def _fetch_scenes_from_api(self, host: str, token: str) -> list[Scene]:
        """Fetch the gateway's scenes from the REST API."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/scenes/"
        headers = {"token": f"{token}", "Content-Type": "application/json"}
        async with asyncio.timeout(30):
            async with session.get(url, headers=headers, ssl=ssl) as response:
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

    async def async_config_entry_first_refresh(self) -> None:
        """Run the first refresh, then read the hardware identities once.

        Extends the base method rather than adding a call in ``__init__``'s
        setup sequence so the read happens exactly where it belongs: after the
        first ``/functions`` fetch has proven the token (a rejected token
        raises out of the base call and never reaches the export) and before
        the platforms build their first ``device_info``, which is when a
        device's ``serial_number`` / ``connections`` are read. Best-effort like
        the groups/scenes fetches — see ``async_fetch_node_identities``.
        """
        await super().async_config_entry_first_refresh()
        await self.async_fetch_node_identities()
        # Second rename-following pass, now with the hardware identities: a
        # function renamed AND re-provisioned while Home Assistant was down
        # changed its id, so the first pass (inside the refresh above) could
        # not pair it; the node's address + element location can. Still
        # before the platforms register anything.
        self.follow_renames(self.data or [])
        await self.async_fetch_device_properties()
        await self.async_fetch_health_status()

    async def _fetch_project_export_from_api(self, host: str, token: str) -> Any:
        """Read the gateway's project export (``GET /project/junghome``).

        Returns the decoded JSON document, or ``None`` when the gateway has
        none to give: a 404 (firmware before API 1.5.0 has no ``project/*``
        routes), a 501, or any other non-200 — the export is an optional
        enrichment, so every such answer means "no identities", not an error.

        **The document carries the mesh NetKey, AppKeys and device keys.** It
        is never logged (not even at DEBUG — every other fetch here logs its
        response) and never stored; the caller parses the handful of identity
        fields out of it and drops it.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/project/junghome"
        headers = {"token": f"{token}"}
        async with (
            asyncio.timeout(30),
            session.get(url, headers=headers, ssl=ssl) as response,
        ):
            if response.status != 200:
                _LOGGER.debug(
                    "Gateway has no project export to read (HTTP %s)",
                    response.status,
                )
                return None
            return await response.json()

    async def async_fetch_node_identities(self) -> None:
        """Populate ``node_identities`` from the project export, best-effort.

        Best-effort for the same reason as the groups and scenes fetches: a
        hardware identity is an enrichment of the device page (serial number,
        Bluetooth address, a stable join key for tooling), not something worth
        failing setup over, and older firmware has no export at all. A
        gateway that is unreachable, slow, or answers with a body that is not
        JSON leaves the map as it was — an empty map at setup, the previous
        map on a re-read (a transient failure must not strip identities the
        registry already carries). Any other exception is a bug here rather
        than a gateway problem and is left to propagate.

        The parsed map replaces the old one only when it resolved something:
        an export the parser cannot make sense of (a shape this code does not
        know) reads as "nothing learned", and keeping the previous map is
        strictly better than emptying it. The parser is written never to
        raise, and its own guards cover every shape found so far — but the
        document is untrusted and the enrichment is optional, so should it
        raise anyway, that is logged by exception *type* (never the document,
        which carries the mesh keys) and setup carries on without identities
        rather than failing with ``SETUP_ERROR``.
        """
        self._node_identity_fetched_at = time.monotonic()
        try:
            document = await self._fetch_project_export_from_api(
                self.config["host"], self.config["token"]
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read the Jung Home project export: %s", err)
            return
        if document is None:
            return
        try:
            identities = parse_project_export(document)
        except Exception as err:
            _LOGGER.warning(
                "Could not parse the project export: %s", type(err).__name__
            )
            return
        finally:
            # Drop the document (and its keys) the moment the parse is over.
            del document
        if not identities:
            _LOGGER.debug("Project export carried no usable node identities")
            return
        if identities == dict(self.node_identities):
            return
        self.node_identities = MappingProxyType(identities)
        _LOGGER.debug(
            "Resolved hardware identity for %d gateway functions", len(identities)
        )
        self.apply_node_identities()

    async def _fetch_devices_verbose_from_api(
        self, host: str, token: str
    ) -> list[Any] | None:
        """Read ``GET /devices/?verbose=true``: the raw middleware device objects.

        Deprecated/experimental in the gateway's OpenAPI but live on 2.1.3;
        ``None`` for any non-200 (older firmware) or a non-list body. Large
        (~190 KB on 49 devices), so read sparingly — see
        DEVICE_PROPERTIES_REFRESH_INTERVAL. Labels are in it; never logged.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/devices/?verbose=true"
        headers = {"token": f"{token}"}
        async with (
            asyncio.timeout(30),
            session.get(url, headers=headers, ssl=ssl) as response,
        ):
            if response.status != 200:
                _LOGGER.debug(
                    "Gateway has no verbose device list (HTTP %s)", response.status
                )
                return None
            data = await response.json()
        return data if isinstance(data, list) else None

    async def _fetch_device_verbose_from_api(
        self, host: str, token: str, device_id: str
    ) -> dict[str, Any] | None:
        """Read one device's verbose object (``GET /devices/{id}?verbose=true``)."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        safe_id = quote(device_id, safe="")
        url = f"https://{host}/api/junghome/devices/{safe_id}?verbose=true"
        headers = {"token": f"{token}"}
        async with (
            asyncio.timeout(30),
            session.get(url, headers=headers, ssl=ssl) as response,
        ):
            if response.status != 200:
                return None
            data = await response.json()
        return data if isinstance(data, dict) else None

    async def async_fetch_device_properties(self) -> None:
        """Populate ``device_properties`` from the verbose device list, best-effort.

        Best-effort like the identities: firmware without the endpoint, a
        transport failure or an unusable body leaves the map as it was — an
        empty map at setup, the previous map on a re-read.
        """
        # The functions this answer can speak for, taken BEFORE the read: one
        # adopted while the (~190 KB) body is in flight is not in it, and
        # counting it as asked would mean it is never read.
        asked_for = frozenset(
            device_id
            for d in self.data or []
            if isinstance(device_id := d.get("id"), str)
        )
        try:
            raw = await self._fetch_devices_verbose_from_api(
                self.config["host"], self.config["token"]
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read the verbose device list: %s", err)
            return
        if raw is None:
            return
        # The endpoint answered: whatever live function it left out, it will
        # leave out next time too, so the periodic refresh stops asking for
        # the full list until the membership changes (`_properties_listed_for`).
        self._properties_listed_for = asked_for
        parsed = parse_devices_verbose(raw)
        if not parsed or parsed == dict(self.device_properties):
            return
        self.device_properties = MappingProxyType(parsed)
        _LOGGER.debug("Read properties for %d gateway devices", len(parsed))

    async def _async_refresh_device_properties(self, _now: datetime) -> None:
        """Periodic re-read of the properties that change: the energy counters.

        A function that appeared since the last full-list answer (added in the
        app), or no answer yet (firmware without the endpoint, a failed read),
        triggers a full-list read instead; otherwise each device holding an
        energy counter is re-read on its own, small endpoint. Listeners are
        notified only when a value changed. Runs are not stacked.
        """
        if self._properties_refresh_running:
            return
        self._properties_refresh_running = True
        try:
            await self._refresh_device_properties()
        finally:
            self._properties_refresh_running = False

    async def _refresh_device_properties(self) -> None:
        known = self.device_properties
        listed = self._properties_listed_for
        if listed is None or any(
            isinstance(device_id := d.get("id"), str) and device_id not in listed
            for d in self.data or []
        ):
            await self.async_fetch_device_properties()
            if self.device_properties is not known:
                self.async_update_listeners()
            return
        updated = dict(known)
        changed = False
        for device_id, props in known.items():
            if not props.has_energy:
                continue
            try:
                document = await self._fetch_device_verbose_from_api(
                    self.config["host"], self.config["token"], device_id
                )
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Could not re-read device %s: %s", device_id, err)
                continue
            fresh = parse_device_properties(document)
            if fresh is not None and fresh != props:
                updated[device_id] = fresh
                changed = True
        if changed:
            self.device_properties = MappingProxyType(updated)
            self.async_update_listeners()

    async def _fetch_health_status_from_api(self, host: str, token: str) -> Any:
        """Read ``GET /healthstatus/``; None for any non-200.

        The route takes the same token as ``/functions/`` (``auth()`` with no
        role — health.py); a token it rejects answers 401, which the REST poll
        turns into reauth on its own, so here it is only "not readable".
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        url = f"https://{host}/api/junghome/healthstatus/"
        headers = {"token": f"{token}"}
        async with (
            asyncio.timeout(30),
            session.get(url, headers=headers, ssl=ssl) as response,
        ):
            if response.status != 200:
                _LOGGER.debug(
                    "Gateway health status not readable (HTTP %s)", response.status
                )
                return None
            return await response.json()

    async def _fetch_config_parameter_from_api(
        self, host: str, token: str, parameter: str
    ) -> Any:
        """Read one ``GET /config/parameter/{parameter}`` value; None for a non-200."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        ssl = await self._async_ssl()
        safe = quote(parameter, safe="")
        url = f"https://{host}/api/junghome/config/parameter/{safe}"
        headers = {"token": f"{token}"}
        async with (
            asyncio.timeout(30),
            session.get(url, headers=headers, ssl=ssl) as response,
        ):
            if response.status != 200:
                return None
            return await response.json()

    async def async_fetch_health_status(self) -> None:
        """Read the gateway's health log and raise or withdraw its repair issues.

        One issue per condition in ``health.HEALTH_CONDITIONS`` the log shows;
        every other condition's issue is withdrawn. The log only ever grows
        until the gateway restarts, so a condition with no clearing message
        (a Bluetooth chip failure, out of sequence numbers) stays raised until
        then — which is also what the gateway's own text tells the user to do.
        The time-sync condition is the exception: it is withdrawn as soon as
        the gateway reports its clock synchronised again (``time_error``).

        Best-effort: a transport failure, a non-200 (401 included) or an
        unusable body changes nothing — issues from an earlier read stay until
        a read proves otherwise.
        """
        host, token = self.config["host"], self.config["token"]
        try:
            raw = await self._fetch_health_status_from_api(host, token)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read the gateway health status: %s", err)
            return
        entries = parse_health_status(raw)
        if entries is None:
            return
        active = active_health_conditions(entries)
        if ISSUE_TIME_SYNC in active and await self._time_sync_recovered(host, token):
            del active[ISSUE_TIME_SYNC]
        if self._closing:
            return  # unloaded while the read was in flight; stop() withdrew all
        self.health.entries = entries
        self.health.conditions = frozenset(active)
        self._apply_health_issues(active)

    async def _time_sync_recovered(self, host: str, token: str) -> bool:
        """Whether the gateway reports its clock synchronised again.

        Only a definite ``false`` counts: an unreadable parameter keeps the
        issue the log raised.
        """
        try:
            value = await self._fetch_config_parameter_from_api(
                host, token, TIME_ERROR_PARAMETER
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not read the gateway time status: %s", err)
            return False
        return value is False

    def _apply_health_issues(self, active: Mapping[str, HealthCondition]) -> None:
        """Create the issue of every active condition, delete every other one."""
        for condition in HEALTH_CONDITIONS:
            issue_id = self.health.issue_id(condition.key)
            if condition.key not in active:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            # Idempotent: an unchanged issue is neither saved nor re-announced.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=condition.severity,
                translation_key=condition.key,
                translation_placeholders={"host": str(self.config["host"])},
            )

    async def _async_refresh_health_status(self, _now: datetime) -> None:
        """Periodic health-log re-read; runs are not stacked."""
        if self.health.refresh_running:
            return
        self.health.refresh_running = True
        try:
            await self.async_fetch_health_status()
        finally:
            self.health.refresh_running = False

    def device_properties_for(self, device: Device) -> DeviceProperties | None:
        """Return the verbose endpoint's properties for a function, if read."""
        device_id = device.get("id")
        if not isinstance(device_id, str):
            return None
        return self.device_properties.get(device_id)

    def button_reports_each_tap_once(self, device: Device) -> bool:
        """Whether ``device`` is KNOWN to run firmware older than the doubling one.

        Device firmware 2.2.0.x publishes every button event twice; older
        firmware reports each tap once, so suppressing duplicates there only
        costs fast double-taps. Only a revision the verbose endpoint actually
        reported, and that is older, exempts a device — unknown stays
        suppressed, the safe default.
        """
        props = self.device_properties_for(device)
        return (
            props is not None
            and props.software_revision is not None
            and props.software_revision < DOUBLED_BUTTON_FIRMWARE
        )

    def node_identity_for(self, device: Device) -> NodeIdentity | None:
        """Return the hardware identity behind a gateway function, if known.

        Keyed by the function's *volatile* id on purpose: that id is
        ``function_id_for(node UUID, element location)``, i.e. it *is* the
        hardware identity in hashed form, so it is the one join key the two
        documents share. Nothing durable is derived from it — ``unique_id``s
        and device identifiers stay label-based (see ``device_slug``).
        """
        device_id = device.get("id")
        if not isinstance(device_id, str):
            return None
        return self.node_identities.get(device_id)

    @callback
    def _schedule_node_identity_refetch(self, devices: list[Device]) -> None:
        """Re-read the export, debounced, if a device list has unknown functions.

        Called on every device-list adoption (REST poll and ``functions``
        broadcast). A function id with no identity means a node was added or
        re-provisioned in the app since the last read — the app pushes the
        updated project to the gateway within seconds of any change — so a
        re-read resolves it. Never more often than
        ``NODE_IDENTITY_REFETCH_INTERVAL``, never while a read is in flight,
        and not before the setup-time read has run (an adoption during the
        first refresh would otherwise schedule a second read alongside it).
        The read runs as an entry background task so it neither delays the
        adoption nor outlives the entry.
        """
        fetched_at = self._node_identity_fetched_at
        if fetched_at is None:
            return
        task = self._node_identity_task
        if task is not None and not task.done():
            return
        if not any(
            isinstance(device_id := d.get("id"), str)
            and device_id not in self.node_identities
            for d in devices
        ):
            return
        if time.monotonic() - fetched_at < NODE_IDENTITY_REFETCH_INTERVAL:
            return
        entry = self.config_entry
        if entry is None:  # pragma: no cover - an entry coordinator always has one
            return
        _LOGGER.debug("Device list carries unidentified functions; re-reading export")
        self._node_identity_task = entry.async_create_background_task(
            self.hass, self.async_fetch_node_identities(), name="junghome_identity"
        )

    @callback
    def apply_node_identities(self) -> None:
        """Write the resolved identities onto every device of the entry.

        The registry's identity rows are written from here and from
        ``link_node_identity`` only — never from ``device_info``. Runs when
        the identity map is (re)resolved, so a device registered before its
        identity was known — every device on an install upgraded to this
        version, or one the gateway reported while the export still lacked
        its node — is filled in without a reload; and after a device of this
        entry is removed (the stale-device pruner, a manual delete), because
        the removed device may have held the node's Bluetooth connection that
        its relabelled successor is waiting for (see ``_write_node_identity``).

        Colliding slugs are skipped (``duplicate_slugs``): two functions
        sharing one registry device would otherwise take turns writing their
        own node's address over each other on every pass.
        """
        if not self.node_identities or self.config_entry is None:
            return
        devices = self.data or []
        colliding = duplicate_slugs(devices)
        by_slug = {
            device_slug(d): d for d in devices if device_slug(d) not in colliding
        }
        registry = dr.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        for device_entry in dr.async_entries_for_config_entry(registry, entry_id):
            identity = next(
                (
                    self.node_identity_for(by_slug[identifier])
                    for domain, identifier in device_entry.identifiers
                    if domain == DOMAIN and identifier in by_slug
                ),
                None,
            )
            if identity is not None:
                self._write_node_identity(registry, entry_id, device_entry, identity)

    @callback
    def link_node_identity(self, device_id: str, device: Device) -> None:
        """Write ``device``'s identity onto its just-registered registry row.

        Called from ``JungHomeEntity.async_added_to_hass`` — the first moment
        the device's registry row exists. ``device_info`` deliberately carries
        no connection (see its docstring), so this is how a device registered
        while its identity is already known gets one: every device of a fresh
        install, a device the gateway starts reporting later, a pruned device
        the gateway reports again. Same collision guard as
        ``apply_node_identities``; a device whose identity is still unknown is
        picked up by that pass once the export re-read resolves it.
        """
        if self.config_entry is None:
            return
        identity = self.node_identity_for(device)
        if identity is None or device_slug(device) in duplicate_slugs(self.data or []):
            return
        registry = dr.async_get(self.hass)
        device_entry = registry.async_get(device_id)
        # ``isinstance`` rather than a None check: from HA 2026.9 ``async_get``
        # may also return a child device, which no entity of ours ever has.
        if isinstance(device_entry, dr.DeviceEntry):
            self._write_node_identity(
                registry, self.config_entry.entry_id, device_entry, identity
            )

    def _write_node_identity(
        self,
        registry: dr.DeviceRegistry,
        entry_id: str,
        device_entry: dr.DeviceEntry,
        identity: NodeIdentity,
    ) -> None:
        """Write one function's identity onto one registry device.

        The node's Bluetooth address goes on as ``serial_number`` on every
        function of the node, and as a ``CONNECTION_BLUETOOTH`` connection on
        the function at the node's primary element only (a connection
        resolves devices in the registry, so it must be unique per device —
        see ``NodeIdentity``) — and only when no other live device of this
        entry holds it. That holder check is the point of writing the
        connection here rather than in ``device_info``: a relabelled function
        registers under a new slug while the old device is still live (the
        pruner keeps it for ``STALE_DEVICE_PRUNE_MISSES`` adoptions), and a
        connection in ``device_info`` made ``async_get_or_create`` resolve the
        new slug to the OLD device by connection and merge the two — the old
        entity then stayed registered and live forever, and the pruner never
        removed a device whose identifiers were all still current. Now the
        successor is a fresh device, the old one is pruned as documented, and
        the successor gains the connection on the pass that follows the prune.

        On cores before HA 2026.9 a connection is unique across ALL config
        entries, so a device another integration keeps for the same radio
        (the Bluetooth-direct sibling) blocks the link with a collision the
        per-entry lookup cannot see; the write is let raise and the address
        left to its holder. The serial number is written either way.
        """
        if identity.mac is None:
            return
        if device_entry.serial_number != identity.mac:
            registry.async_update_device(device_entry.id, serial_number=identity.mac)
        connection = (dr.CONNECTION_BLUETOOTH, identity.mac)
        if not identity.primary or connection in device_entry.connections:
            return
        holder = device_by_connection(registry, entry_id, connection)
        if holder is not None:
            _LOGGER.debug(
                "Not linking %s to Bluetooth address %s: held by device %s (%s)",
                device_entry.name,
                identity.mac,
                holder.id,
                holder.name,
            )
            return
        # Replace, never add to, the device's Bluetooth connection: a node
        # swapped under the same label (new radio, same name — measured once
        # across a real update) otherwise kept the old address next to the
        # new one for good.
        kept = {c for c in device_entry.connections if c[0] != dr.CONNECTION_BLUETOOTH}
        try:
            registry.async_update_device(
                device_entry.id, new_connections=kept | {connection}
            )
        except dr.DeviceConnectionCollisionError as err:
            _LOGGER.debug(
                "Not linking %s to Bluetooth address %s: %s",
                device_entry.name,
                identity.mac,
                err,
            )

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
            ssl = await self._async_ssl()
            async with asyncio.timeout(30):
                async with session.post(url, headers=headers, ssl=ssl) as response:
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
        except aiohttp.ServerFingerprintMismatch as err:
            # Same contract as the poll: the token was never sent, the user
            # gets the repair issue, and the service call fails with the
            # reason rather than a "reconnecting, try again" that never
            # comes true.
            self._report_fingerprint_mismatch(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="certificate_changed",
                translation_placeholders={"host": str(self.config["host"])},
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
            except aiohttp.ServerFingerprintMismatch as err:
                # The upgrade was refused at the TLS handshake (no token
                # sent). Report it like the poll does, then keep the ordinary
                # backoff: each retry is another aborted handshake, so a
                # transient impostor costs nothing and the real gateway back
                # on its address is picked up without a reload.
                self._report_fingerprint_mismatch(err)
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
        """Count a failed reconnect and, once the outage has lasted, tell the user.

        A dropped WebSocket degrades the integration to the REST poll: state
        still updates, so nothing looks broken, it just stops being live. For
        the first ``WEBSOCKET_OUTAGE_REPAIR_AFTER`` seconds of an outage that is
        an ordinary blip (a gateway reboot, a Wi-Fi hiccup) the backoff rides
        out silently. Past that, raise a repair issue so the degradation is
        visible rather than buried in a log warning. It is deliberately not
        fixable from the UI — only the gateway or the network coming back fixes
        it, and ``_mark_session_stable`` deletes the issue once a session holds.

        The clock starts at the first failure of the outage and is only ever
        stopped by ``_mark_session_stable``: an attempt count was the wrong
        measure because the backoff makes attempts cheap early on (five inside
        ~20 s), so the count said "sustained outage" about every reboot.
        """
        self._reconnect_failures += 1
        now = time.monotonic()
        if self._outage_started_at is None:
            self._outage_started_at = now
        if now - self._outage_started_at < WEBSOCKET_OUTAGE_REPAIR_AFTER:
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

    def _reject_pending_reply(self, message_id: str, reason: str) -> None:
        """Fail the command awaiting `message_id`: the gateway rejected it.

        Same no-op rules as ``_resolve_pending_reply``. ``reason`` is the
        gateway's own text from the ``error:`` frame; it is the only place the
        gateway ever says *why*, so it is carried into the
        ``command_rejected`` error the calling service raises (the caller's
        WARNING keeps it in the log as well).
        """
        future = self._pending_replies.get(message_id)
        if future is not None and not future.done():
            future.set_exception(
                HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_rejected",
                    translation_placeholders={"error": reason},
                )
            )

    def _dispatch_text_frame(self, raw: str) -> None:
        """Parse one TEXT frame and route it to the right handler.

        Split out of ``_run_websocket`` so that function stays about the session
        lifecycle. Never raises: a malformed frame must not tear down an
        otherwise healthy connection.
        """
        _LOGGER.debug("Received WebSocket message: %s", raw)
        # Parse exactly once; the diagnostics logger below reuses this parse's
        # frame type instead of decoding the frame a second time (this path
        # runs for every frame a chatty gateway pushes).
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as e:
            # RecursionError included: the C parser overflows the stack on
            # absurdly nested input (~1M brackets on CPython 3.14), and this
            # except is all that stands between the parse and the receive
            # loop — "a malformed frame must never tear down a healthy
            # session" has to hold for that frame too.
            # Still recorded for diagnostics — the rolling log should show
            # exactly what the gateway sent — just never keyed by type.
            self._log_ws_frame(raw, None)
            _LOGGER.error("Error decoding WebSocket message: %s", e)
            return
        frame_type = data.get("type") if isinstance(data, dict) else None
        self._log_ws_frame(raw, frame_type if isinstance(frame_type, str) else None)
        try:
            # Every frame is `{"type": ..., "data": ...}`; a bare list or JSON
            # scalar ("hello", 42) is not a frame. Rejecting it here keeps it
            # a clean log line instead of an AttributeError in the catch-all.
            if not isinstance(data, dict):
                _LOGGER.error("Received non-object WebSocket message: %s", data)
                return
            # Only a reply to one of OUR OWN datapoint sets/gets carries
            # message_id back (websocket-server-service.js never assigns it to
            # a broadcast), so a correlated frame can only ever concern
            # something `_send_datapoint_command` is awaiting. What it means
            # depends on the frame type, so the future is settled inside the
            # type switch below — never before it: a correlated frame is not a
            # confirmation just because it echoes the id.
            message_id = data.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                message_id = None
            if frame_type == "version":
                # `api-junghome`'s package version (the API contract), not the
                # gateway's software version — see `api_version` in __init__.
                self.api_version = data.get("data")
                _LOGGER.debug("Jung Home gateway API version: %s", self.api_version)
                return
            if frame_type == "message":
                text = data.get("data")
                if isinstance(text, str) and text.startswith("error:"):
                    # The gateway reports a rejected command (e.g. a bad set) as
                    # an `error:` message frame. Current firmware sends it
                    # without a message_id, so the WARNING is the only place
                    # the gateway's own reason ever surfaces; should a frame
                    # carry one, the awaiting command is failed right away
                    # with that reason (minus the `error:` tag, which the
                    # translated message already says) instead of sitting
                    # out COMMAND_REPLY_TIMEOUT.
                    if message_id is not None:
                        reason = text.removeprefix("error:").strip() or text
                        self._reject_pending_reply(message_id, reason)
                    _LOGGER.warning("Jung Home gateway reported an error: %s", text)
                else:
                    _LOGGER.debug("Received message frame: %s", data)
                return
            if frame_type == "datapoint" and message_id is not None:
                # The confirmation of a set: the re-read datapoint. Resolving it
                # here does not short-circuit the frame: it still falls through
                # to the normal dispatch below, which merges `data.get("data")`
                # into `self.data` exactly like a push would — the confirmed
                # value replaces the optimistic one HA already wrote.
                self._resolve_pending_reply(message_id, data.get("data"))
            self._handle_websocket_message(data)
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
        # connected, `heartbeat=30` is what detects a silently dead peer. The
        # upgrade carries the token, so it is pinned exactly like a REST
        # request (`ssl=` is honoured by ws_connect — see tls.py).
        async with asyncio.timeout(WS_CONNECT_TIMEOUT):
            ssl = await self._async_ssl()
            ws = await session.ws_connect(url, headers=headers, heartbeat=30, ssl=ssl)
        async with ws:
            self.websocket = ws
            # Connected: resync state we may have missed while disconnected.
            # Logged at INFO (paired with the WARNING on disconnect) so the
            # drop/recover story is visible without enabling debug logging
            # during a long soak.
            #
            # The backoff, the failure counter and the outage clock are
            # deliberately NOT reset here. A successful upgrade proves nothing
            # yet — a gateway stuck in a reboot loop accepts the handshake and
            # drops us straight away, and resetting on connect made that flap
            # immortal: the delay went back to 1 s before the doubling could
            # ever apply, and the escalation towards the repair issue restarted
            # from zero on every connect, so the issue never appeared.
            # `_mark_session_stable` below does the reset once the session has
            # actually lasted STABLE_SESSION_SECONDS.
            _LOGGER.info("Jung Home WebSocket connected")
            self.ws_connected = True
            self.ws_last_connected = dt_util.utcnow()
            cancel_stable = async_call_later(
                self.hass, STABLE_SESSION_SECONDS, self._mark_session_stable
            )
            try:
                # Inside the try: `stop()` cancels this task, and a cancel that
                # lands while the resync's REST fetch is still in flight must
                # run the same teardown as a drop — outside it, the session
                # ended with `ws_connected` stuck True, in-flight commands
                # left to sit out their timeout, and the stable-session timer
                # still armed to fire on a stopped coordinator.
                await self.async_request_refresh()
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
        escalating its backoff and its outage clock keeps running towards the
        repair issue, while a gateway that is actually back ends the outage and
        clears both (and the issue, a no-op when it was never raised).
        """
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        self._reconnect_failures = 0
        self._outage_started_at = None
        # A gateway software update reboots the gateway, so a session that has
        # just proven stable is exactly when the version may have changed. Done
        # here rather than on connect so a flapping socket cannot turn it into a
        # request per reconnect; `async_fetch_gateway_version` is a no-op when
        # the value is unchanged.
        if self.config_entry is not None:
            self.config_entry.async_create_background_task(
                self.hass,
                self.async_fetch_gateway_version(),
                name="junghome_gateway_version",
            )
        if self._unavailable_logged:
            # Pair the single WARNING above with a matching recovery line, so an
            # outage has a visible end without trawling debug logs.
            _LOGGER.info("Jung Home WebSocket reconnected")
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

    def _log_ws_frame(self, raw: str, frame_type: str | None) -> None:
        """Record a raw WebSocket frame for diagnostics.

        ``frame_type`` is the frame's ``type`` field from the caller's single
        ``json.loads`` (``_dispatch_text_frame``), or ``None`` for a frame that
        is unparseable or carries no usable type — decoding the frame a second
        time here doubled the JSON work on the hottest path the integration has.
        The per-type store keeps the latest frame of each KNOWN type IN FULL:
        the gateway's frame vocabulary (``WS_KNOWN_FRAME_TYPES``) is a dozen
        strings, so that part holds at most one frame per type, and keeping
        the connect-time handshake (functions/groups/scenes/version/message)
        complete makes it directly comparable to the raw wire format. A type
        outside that vocabulary is the peer's to invent — a hostile or buggy
        one could mint a fresh type per frame — so those are kept as truncated
        previews and only while the store holds fewer than
        ``WS_FRAME_TYPES_MAX`` types; past that they land in the rolling
        buffer alone. The rolling buffer, which fills with high-frequency
        datapoint pushes, is always truncated.
        """
        if frame_type is not None:
            store = self.ws_last_frame_by_type
            if frame_type in WS_KNOWN_FRAME_TYPES:
                store[frame_type] = raw
            elif frame_type in store or len(store) < WS_FRAME_TYPES_MAX:
                store[frame_type] = _truncate_frame(raw)
        self.ws_frame_log.append(_truncate_frame(raw))

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
            if msg_type == "datapoint":
                self._handle_datapoint_push(message, data)
                return
            # Any other object-carrying frame (`config` is the only one the
            # server defines, and current firmware never emits it) is not a
            # datapoint: treating it as one logged a spurious ERROR about a
            # missing datapoint_id for a frame that was never malformed.
            _LOGGER.debug("Received %s frame (ignored): %s", msg_type, message)
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

    def _handle_datapoint_push(
        self, message: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Merge one pushed datapoint into the stored data and notify listeners.

        Split out of ``_handle_websocket_message`` so that method stays a pure
        frame router; the behaviour is unchanged.
        """
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
        pushed_device_id: str | None = None
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
                    pushed_device_id = device.get("id")
                    updated = True
                    break
            if updated:
                break
        if updated:
            # Flag the pushed datapoint (and the device that owns it) for
            # the duration of this dispatch so event entities fire on the
            # push itself and unrelated entities can skip their write. The
            # dispatch below notifies listeners synchronously, so the flags
            # are valid for exactly this push and are cleared immediately
            # afterwards; REST polls never set them and therefore never
            # fire phantom events or suppress a full re-read.
            self.pushed_datapoint_id = datapoint_id
            self.pushed_device_id = pushed_device_id
            try:
                # Deliberately NOT `async_set_updated_data`: that helper
                # cancels the scheduled refresh and re-arms it a full
                # `update_interval` from now, so a gateway that pushes more
                # often than `update_interval` would defer the REST poll forever.
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
                self.pushed_device_id = None
        elif datapoint_id not in self._unmatched_push_ids:
            # An unmatched push usually means a device was just added in
            # the app and is pushing before the next poll has discovered
            # it. Request a (debounced) refresh on the FIRST sighting of an
            # unknown id — discovery then lands in seconds instead of up
            # to a poll interval — and warn once per id rather than per frame (a
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

    def _handle_functions_broadcast(self, data: list[Any]) -> None:
        """Adopt a pushed ``functions`` list as if a REST poll had returned it.

        The gateway broadcasts the full, authoritative device list on connect
        and whenever it changes (captured frames match ``GET /functions/``
        exactly). Treating it as a poll result makes device add/remove
        push-driven — discovery, pruning, area assignment and the capability
        watcher all run on it via their coordinator listeners — instead of
        waiting out the next poll interval.

        ``async_set_updated_data`` is correct HERE (and deliberately avoided in
        the per-datapoint push path above): this frame carries data as fresh
        and complete as a poll, so re-arming the poll timer a full interval out
        loses nothing — and the frame only arrives on membership change, so it
        cannot starve the poll the way per-value pushes did. No push marker is
        set, so event entities never read a broadcast as a button edge, and the
        unmatched-id memory resets because the authoritative list may have just
        added those devices.
        """
        # The same trust boundary as the REST poll (`_fetch_devices_from_api`):
        # a malformed device object is dropped or repaired here, not left to
        # raise out of a platform listener.
        devices = sanitize_devices(data)
        _LOGGER.debug("Adopting functions broadcast (%d devices)", len(devices))
        self._unmatched_push_ids.clear()
        self._reload_if_device_ids_changed(devices)
        self.follow_renames(devices)
        # Counted immediately before the adoption, and never before it: a poll
        # whose fetch was in flight across this point discards its own older
        # snapshot in favour of this list (see `_async_update_data`), so the
        # count must only rise once this list is actually adopted. Should
        # `_reload_if_device_ids_changed` above ever raise (it used to, on a
        # non-string label, before `sanitize_devices` enforced the shape),
        # `_dispatch_text_frame`'s catch-all swallows it — counting first would
        # let that frame suppress a racing poll that carried the fresher list,
        # leaving stale membership for a full poll interval. Nothing awaits
        # between here and the adoption, so no poll can observe the gap.
        self._functions_broadcasts_seen += 1
        self.async_set_updated_data(devices)
        self._schedule_node_identity_refetch(devices)

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
            # (a scene's id is `id` + hex(mesh scene number), a number the app
            # may reassign — see `models.Scene`), so a delta that assigned a
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
        # The energy counters move; everything else in the map is static.
        self._properties_unsub = async_track_time_interval(
            self.hass,
            self._async_refresh_device_properties,
            timedelta(seconds=DEVICE_PROPERTIES_REFRESH_INTERVAL),
        )
        self.health.unsub = async_track_time_interval(
            self.hass,
            self._async_refresh_health_status,
            timedelta(seconds=HEALTH_STATUS_REFRESH_INTERVAL),
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
        # Same for the certificate-mismatch issue: a reload (the fix flow's
        # own, or a reconfigure onto a confirmed new certificate) rebuilds the
        # coordinator, which re-raises it on the next poll if it still holds.
        ir.async_delete_issue(self.hass, DOMAIN, self._tls_issue_id)
        # The health and colliding-label issues: both are re-derived on the
        # next setup (its first health read, its watcher's seed pass), so an
        # entry that is disabled or removed takes them with it.
        for issue_id in entry_derived_issue_ids(self.health.entry_id):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self._properties_unsub is not None:
            self._properties_unsub()
            self._properties_unsub = None
        if self.health.unsub is not None:
            self.health.unsub()
            self.health.unsub = None
        if (task := self._node_identity_task) is not None and not task.done():
            # Entry unload cancels its background tasks itself; a full HA
            # shutdown reaches here without an unload, so cancel explicitly.
            task.cancel()
        self._node_identity_task = None
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close()
        self.websocket = None
        # Write a pending anchor save now rather than from a timer that
        # outlives the entry: removing the entry deletes the store right after
        # this unload, and the old timer (or the final-write listener) would
        # then write it back — a store file no entry ever deletes again.
        if self._anchor_save_pending and self._anchor_store is not None:
            await self._anchor_store.async_save(self._anchor_document())

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
        will ever resolve. A send can also fail *because* the session ended
        while ``send_str`` was suspended on the transport, in which case
        ``_fail_pending_replies`` has already set ``cannot_send`` on the
        future this method then never awaits; its exception is retrieved on
        that path so asyncio does not report it as never retrieved.
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
            try:
                await self.send_websocket_message(message)
            except BaseException:
                if future.done() and not future.cancelled():
                    future.exception()  # settled by the session's teardown
                raise
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
