"""Constants and firmware-stable identity helpers for Jung Home."""

import ipaddress
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify
from yarl import URL

from .models import Datapoint, Device

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN = "junghome"

# Fired when the gateway reports a scene recall (including from a physical
# button), so users can automate on it. Shared with logbook.py so the
# registered description always matches the event actually fired.
EVENT_SCENE_RECALLED = f"{DOMAIN}_scene_recalled"

# Fired on every genuine rocker-button edge the gateway pushes. The event
# entities already expose those edges, but a device trigger has to attach to
# something on the *bus* (this is how HA's own button integrations do it),
# so the button platform re-emits each edge here and
# ``device_trigger`` matches on it.
EVENT_BUTTON_ACTION = f"{DOMAIN}_button_action"

# Device-trigger vocabulary.
#
# ``type`` is which side of the rocker fired and ``subtype`` is the event: the
# raw edge the gateway pushed (``pressed``/``depressed``) or a gesture the
# event platform derived from the edges' timing (``click``, ``hold_start``,
# ``hold_end`` — see ``event.py``). The gateway itself has no native
# single/double/hold, and single vs double click is unrecoverable over its API
# on current device firmware (docs/gateway-websocket.md) — hence no
# ``double_click`` here.
CONF_SUBTYPE = "subtype"

# Rocker datapoint type -> button side. Also drives the event entities'
# translation keys, so both surfaces name a given side identically.
BUTTON_DATAPOINT_TYPES = {
    "up_request": "up",
    "down_request": "down",
    "trigger_request": "press",
}
BUTTON_TRIGGER_TYPES = set(BUTTON_DATAPOINT_TYPES.values())
# Raw edges first, then the derived gestures. A tuple, not a set: this is the
# order the automation UI lists a button's triggers in, and the event
# entities' ``event_types``.
BUTTON_EVENT_TYPES = ("pressed", "depressed", "click", "hold_start", "hold_end")
BUTTON_TRIGGER_SUBTYPES = BUTTON_EVENT_TYPES

# Button gesture timing (seconds). Both rest on the labelled WebSocket capture
# of 2026-08-02 (one rocker, gateway 2.1.3, device firmware 2.2.0.x; 16 taps +
# 5 holds — tables in docs/gateway-websocket.md) and the mechanism the
# 2026-09-15 cross-repo audit established (docs/cross-repo-analysis.md §1.1).
#
# A press still down after this long is a *hold* (``hold_start`` fires at this
# moment, ``hold_end`` at the release); a press released sooner is a *click*.
# Tap pulses measured 0.40-0.53 s — that width is the gateway's own synthesised
# release (two 200 ms delays in its emitter loop), not the finger — and hold
# pulses 2.44-3.11 s (the finger): a five-fold empty band, so anywhere in
# ~1-2 s is safe. 1.0 s keeps ``hold_start`` responsive.
BUTTON_HOLD_THRESHOLD = 1.0
# Device firmware 2.2.0.x publishes every button event twice, ~1 s apart, and
# the gateway turns each copy of a click into its own press/release pair — one
# tap arrives as TWO pairs, the second press 0.11-1.03 s after the first
# release. A press on the same DEVICE (any side: on a single-key element the
# copy lands on the *other* datapoint) within this window after a click is
# that copy and is dropped. 1.2 s covers the 1.03 s worst case with margin;
# anything shorter lets some copies through. A hold's copy is value-and-mode
# unchanged and the gateway already suppresses it.
BUTTON_DUPLICATE_WINDOW = 1.2

# Options-flow key: whether the event platform drops the firmware's duplicate
# copy of each tap (``BUTTON_DUPLICATE_WINDOW``). On by default — every install
# on device firmware 2.2.0.x needs it. The trade-off is inherent: any two
# presses on one device within 1.2 s count as one, so a user on *older* device
# firmware (one pair per tap) who double-taps faster than that turns it off.
CONF_SUPPRESS_DUPLICATE_PRESSES = "suppress_duplicate_presses"
DEFAULT_SUPPRESS_DUPLICATE_PRESSES = True
# The firmware's copy of a HOLD on a single-key element. The gateway toggles
# the reported side of such an element on every reception, so while a hold's
# first copy keeps one side down, the second copy lands as a press on the
# OTHER side, and the finger's release then lands on that other side too —
# the first side is never released (live capture 2026-09-16, 1-gang keys:
# press, other-side press +1.4 s, release on the copy's side at +2.55 s; three
# further holds on those keys carried no copy at all). A press on the other
# side of a device whose one side has been down for longer than any
# synthesised tap pulse (0.53 s measured — a tap's own copy only ever arrives
# after its release, which BUTTON_DUPLICATE_WINDOW handles) and less than this
# window is that copy: it is dropped, and its release completes the hold on
# the side that is actually down. On a rocker the gateway suppresses a hold's
# copy itself (same side, value unchanged), so this only ever misfires there
# if the other side is pressed with a second finger while the first is held.
BUTTON_HOLD_COPY_AFTER = 0.6
BUTTON_HOLD_COPY_WINDOW = 2.5

# Presentation of the synthetic gateway (hub) device. Kept as constants so the
# up-front registration in ``__init__`` and the connectivity sensor that lives on
# the device describe it identically (see ``gateway_device_info``).
GATEWAY_NAME = "JUNG HOME Gateway"
GATEWAY_MANUFACTURER = "Jung"
GATEWAY_MODEL = "Gateway"

# Options-flow key: the stable unique_ids of covers whose position the gateway
# reports inverted relative to Home Assistant's convention. The gateway's native
# `level` is percent-*closed* (firmware: closing drives the BT-Mesh Generic Level
# toward 100 %, opening toward 0 %), which is correct for roller shutters/blinds.
# Awnings (Markise) mount the motor the opposite way — "extended" is what the user
# calls open — so for them the mapping must be flipped. There is no awning hint in
# the gateway's function data, so the user marks them here. See cover.py.
CONF_INVERTED_COVERS = "inverted_covers"

# Options-flow key: how often the REST poll re-reads the gateway's device list,
# in seconds. The poll is the backstop behind the WebSocket push (device
# discovery, pruning, id-churn detection and the availability probe all ride on
# it), so it stays mandatory — this only tunes its cadence. Lengthening it
# mainly reduces gateway load; live value updates keep arriving over the
# WebSocket regardless. The bounds are enforced in the options form AND
# re-clamped when the coordinator reads the stored option (an option written by
# an older version, or edited by hand, must not produce a torrent of requests
# or an effectively-disabled backstop):
# - The floor is the fetch's own 30 s `asyncio.timeout`. Polls cannot overlap
#   (Home Assistant arms the next one only after the previous finishes), so a
#   shorter interval is not a correctness hazard — but asking for a re-read
#   more often than a single fetch can take turns the backstop into
#   near-continuous load on a slow gateway.
# - The ceiling (1 h) keeps the pruner's debounce meaningful: it counts
#   STALE_DEVICE_PRUNE_MISSES *device-list adoptions* — one per poll, plus one
#   per WS `functions` broadcast (connect, app edits) — so the stale-device
#   window scales linearly with this interval at most.
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL_SECONDS = 60
MIN_POLL_INTERVAL_SECONDS = 30
MAX_POLL_INTERVAL_SECONDS = 3600

# How long the WebSocket must have been continuously down, in seconds, before
# the coordinator raises the `websocket_push_failure` repair issue. Measured
# from the first failed (re)connect of the current outage; a session that
# stays up for STABLE_SESSION_SECONDS (coordinator.py) ends the outage.
#
# Elapsed time, not an attempt count: the reconnect backoff (1, 2, 4, 8 ... s)
# reaches five failed attempts in ~15-20 s when the port refuses, so a count
# threshold fired on every ordinary gateway reboot (~2 min on the Pi Zero —
# firmware updates reboot it too) and self-cleared half a minute after it came
# back, which is exactly the blip the issue is meant to ride out. Three minutes
# clears such a reboot with margin, while a gateway that is genuinely gone is
# still reported within minutes — the silent REST-only degradation the issue
# exists for is measured in hours, so the extra wait costs nothing. Note the
# backoff quantises when the issue can fire: with a refusing port the attempts
# land at ~0, 1, 3, 7, 15, 31, 63, 123, 183 ... s, so any threshold in
# (63, 123] would fire at ~123 s — inside a slow reboot — and the next slot is
# ~183 s.
WEBSOCKET_OUTAGE_REPAIR_AFTER = 180

# Entry-data key: the gateway's hardware serial (from the mDNS TXT record or
# the REST `config/parameter/system_serial` endpoint). Presence of this key
# means the entry's identity is verified: `unique_id` equals this serial, a
# rediscovered gateway updates the stored host by serial match, and
# reconfigure can refuse an address that answers with a *different* serial.
# Entries created before serial-keying (or against firmware that does not
# expose the serial) lack it and keep their legacy host/hostname `unique_id`
# until a discovery or reconfigure migrates them.
CONF_SERIAL = "serial"

# Entry-data key: the frozen identity anchor for ids derived from the entry
# itself (the synthetic hub device, scene unique_id scoping — see
# `entry_anchor`). `gateway_device_id` and `entry_scope` historically anchored
# on `entry.unique_id`, so re-keying an entry's unique_id (host → serial)
# would silently re-key the hub device and every scene entity. Freezing the
# anchor at creation/migration time decouples entry identity (unique_id, may
# change) from entity identity (anchor, never changes).
CONF_IDENTITY_ANCHOR = "identity_anchor"

# Entry-data key: the SHA-256 fingerprint (64 lower-case hex characters) of
# the TLS certificate this entry's gateway presents. The gateway's certificate
# is self-signed, so certificate-authority verification is impossible and the
# integration talks over Home Assistant's no-verify session — without a pin,
# ANY HTTPS responder at the stored address would be handed the API token.
# The JUNG app pins the certificate by the fingerprint it reads over the mesh
# (docs/gateway-rest-api.md, security notes); Home Assistant has no mesh
# path, so the fingerprint is learned on first contact (trust on first use)
# and enforced on every request and WebSocket upgrade from then on — see
# ``tls.py``. Learned at registration for new entries; an entry created
# before pinning existed learns it on its next successful connect to its
# CURRENT host (coordinator TOFU) and carries it from then on. A later
# mismatch never re-learns silently: it raises the ``tls_certificate_changed``
# repair issue, whose fix flow re-pins only after the user confirms. The
# fingerprint is not a secret (it is public on every TLS handshake), so
# diagnostics list it.
CONF_TLS_FINGERPRINT = "tls_fingerprint"

# Entry-data key: the device slugs whose Home Assistant area has already been
# considered for auto-placement from the gateway's group (room) data.
#
# Placement is a *one-time* decision per device, mirroring what HA's own
# (deprecated) `suggested_area` did: a device is placed only if it has no area
# at the moment we first see it, and once recorded here it is never touched
# again. Without this record, a device whose area the user deliberately cleared
# would be re-placed on the next refresh. See `_assign_areas` in __init__.py.
DATA_AREA_ASSIGNED = "auto_area_assigned"


# Quantity labels that denote a boolean *state* rather than a measured value.
# Presence/motion detectors (JUNG "BWM") report detection as a `quantity`
# datapoint with an empty `quantity_unit` and a 0/1 `quantity` value, so it is
# surfaced as an occupancy binary_sensor, not a numeric sensor. Matched as
# case-insensitive substrings of the (English) label the gateway reports, e.g.
# "Presence Detected". ("Present Illuminance" has unit "lux" and the substring
# "present", not "presence", so it stays a numeric illuminance sensor.)
_PRESENCE_LABEL_KEYWORDS = ("presence", "occupancy", "motion")


def is_presence_quantity(label: str | None, unit: str | None = None) -> bool:
    """Whether a quantity datapoint's label denotes presence/occupancy (boolean).

    The binary_sensor platform claims such datapoints and the numeric sensor
    platform skips them, so the two never double-expose the same datapoint (see
    ``binary_sensor.py`` / ``sensor.py``).

    ``unit`` is the datapoint's ``quantity_unit``. A boolean presence datapoint
    carries an **empty** unit (the detector's 0/1 detection flag); a *measured*
    quantity that merely happens to contain a keyword in its label (e.g. a
    "Motion Light Level" illuminance reading with unit ``lux``) carries a real
    unit and must stay a numeric sensor. So a non-empty unit vetoes the match:
    keyword alone is not enough. When ``unit`` is omitted, only the label
    heuristic applies (callers that already know the datapoint has no usable
    unit).
    """
    if not label:
        return False
    if unit is not None and unit.strip():
        return False
    text = label.strip().lower()
    return any(keyword in text for keyword in _PRESENCE_LABEL_KEYWORDS)


def entry_anchor(entry: "ConfigEntry") -> str:
    """Return the frozen identity anchor for entry-derived ids.

    ``gateway_device_id`` and ``entry_scope`` derive the hub-device identifier
    and the scene unique_id scope from this. It must NEVER change for an
    existing entry — changing it re-keys the hub device and every scene
    entity — which is why it is frozen into ``entry.data`` at creation, and
    why migrating an entry's ``unique_id`` (legacy host/hostname → gateway
    serial) freezes the *old* unique_id here first. Entries created before the
    anchor existed fall back to ``unique_id``/``entry_id``, which reproduces
    their historical anchor exactly.
    """
    anchor = entry.data.get(CONF_IDENTITY_ANCHOR)
    if isinstance(anchor, str) and anchor:
        return anchor
    return entry.unique_id or entry.entry_id


def gateway_device_id(entry: "ConfigEntry") -> str:
    """Return the stable identifier for the synthetic gateway (hub) device.

    The gateway itself is not one of the gateway's *functions*, so it has no
    device slug from the device list. Give it a fixed, per-entry identifier so
    gateway-level entities (e.g. the connectivity sensor) can share one hub
    device. Anchored on ``entry_anchor`` (frozen at entry creation; survives
    reconfigure and unique_id migration).

    ``__init__._prune_stale_devices`` adds this exact identifier to its live
    set so the hub is never pruned (it never appears in the gateway's device
    list). A host-based anchor keeps its dots here (unlike a device slug), so
    a device label cannot normally collide with it.
    """
    return f"gateway_{entry_anchor(entry)}"


def entry_scope(entry: "ConfigEntry") -> str:
    """Return a per-gateway prefix for ids that aren't tied to a device.

    Scenes have no device, so their id was the scene label alone — and Home
    Assistant requires a unique_id to be unique across *all* config entries
    of an integration, so two gateways each holding a "Movie night" scene
    collided and the second entity was rejected. Device-backed ids are NOT
    scoped this way: they are the device slug plus a datapoint suffix
    (``stable_unique_id``), so two gateways each reporting a function with
    the same label produce the same unique_id and the second entity is
    rejected exactly as the scenes were. That is a known limitation of the
    label-keyed scheme (multi-gateway installs must keep labels distinct
    across gateways), accepted rather than fixed: prefixing device ids would
    re-key every existing install's entities.

    Anchored on ``entry_anchor`` (frozen at entry creation; survives
    reconfigure and unique_id migration). Same anchor as
    ``gateway_device_id``.
    """
    return slugify(entry_anchor(entry))


def scene_slug(label: str) -> str:
    """Return a firmware-stable slug for a scene label."""
    slug = slugify(label or "")
    if slug and slug != "unknown":
        return slug
    return "scene"


def scene_unique_id(entry: "ConfigEntry", label: str) -> str:
    """Return the firmware-stable, per-gateway unique_id for a scene.

    Scoped by ``entry_scope`` because a scene has no backing device to make it
    unique: the id used to be the scene label alone, so two gateways each with
    a "Movie night" scene produced the same unique_id and Home Assistant
    rejected the second entity. ``_migrate_scene_unique_ids`` in ``__init__``
    re-keys entities created under the old unscoped scheme.

    Lives here (not in scene.py) so the coordinator can resolve a recalled
    scene's entity at event-fire time without importing the platform module.
    """
    return f"{entry_scope(entry)}_{scene_slug(label)}_scene"


def gateway_device_info(entry: "ConfigEntry", sw_version: str | None) -> DeviceInfo:
    """Return the ``DeviceInfo`` for the synthetic gateway (hub) device.

    Shared by the up-front registration in ``__init__`` (which creates the hub
    before the platforms create the per-function devices that reference it via
    ``via_device``) and by the connectivity sensor that lives on it, so both
    describe the device identically.

    No ``None`` values, for the same reason ``JungHomeEntity.device_info``
    has none: ``async_get_or_create`` applies an explicit ``None`` as a
    change, so a setup whose version read failed (the state DB's ``"0.0.0"``
    right after a gateway reboot, or a timeout) would blank the version the
    registry already held from an earlier run until ``_mark_session_stable``
    re-read it. An omitted key leaves the registry row as it is.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, gateway_device_id(entry))},
        name=GATEWAY_NAME,
        manufacturer=GATEWAY_MANUFACTURER,
        model=GATEWAY_MODEL,
    )
    if sw_version:
        info["sw_version"] = sw_version
    # The hardware serial the entry already learned (mDNS TXT, or the
    # gateway's own `config/parameter/system_serial`). Shown on the device
    # page so a user with two gateways can tell them apart; it is not an
    # identity field, so adding it never re-keys the device. Absent on a
    # legacy entry that predates serial discovery.
    if serial := entry.data.get(CONF_SERIAL):
        info["serial_number"] = str(serial)
    # The gateway's own web page ("Visit" on the device page). Built from the
    # entry's host on every setup, so a reconfigure or a discovery that moves
    # the host — both reload the entry — re-points it.
    if url := gateway_configuration_url(entry.data.get(CONF_HOST)):
        info["configuration_url"] = url
    return info


def gateway_configuration_url(host: object) -> str | None:
    """Return ``https://<host>/``, the gateway's web page, or None.

    nginx proxies every path but ``/ws`` to the api-server
    (``etc/nginx/generate_nginx_sites.sh``, ``location /``), which serves its
    webview at ``/`` (``api.route_webviews`` in ``const/config.json``, mounted
    unconditionally in ``server.js``): the "JUNG HOME Gateway" landing page
    with the software version. The host is used exactly as the integration
    reaches the gateway — nginx answers only its own IP and mDNS name, which
    are what an entry stores. A bare IPv6 address is bracketed; a host that
    still does not make a URL with a host part yields None, because the
    device registry raises on an invalid ``configuration_url`` and a cosmetic
    link must not fail setup.
    """
    if not isinstance(host, str) or not (host := host.strip()):
        return None
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # a hostname, or an address with a port: used as is
    url = f"https://{host}/"
    try:
        valid = bool(URL(url).host)
    except ValueError:
        return None
    return url if valid else None


def datapoint_value(datapoint: Datapoint | None, key: str) -> str | None:
    """Return the value for ``key`` in a datapoint's ``values``, or ``None``.

    Centralises the "scan the ``[{key, value}, ...]`` list for a key" loop that
    every platform otherwise repeats. Callers convert/interpret the raw string
    value themselves (``== "1"``, ``float(...)``, scaling, ...).
    """
    if not datapoint:
        return None
    for value in datapoint.get("values", []):
        if value.get("key") == key:
            return value.get("value")
    return None


def datapoint_bool(datapoint: Datapoint | None, key: str) -> bool | None:
    """Return a boolean datapoint's value, or ``None`` when the gateway has none.

    ``"1"`` -> True, ``"0"`` -> False, **anything else -> None (unknown)**.

    The "anything else" that matters is ``"NaN"``. After
    ``btmesh.state_acceptable_request_fails`` (3) failed reads of a BT-Mesh node,
    the middleware's ``onResponseFail`` sets ``state.value = NaN`` and pushes it;
    ``composeDatapointByState`` stringifies every value, so the literal
    ``"NaN"`` lands on the wire, and ``/functions`` keeps serving it (the
    firmware's function assembly has no reachability filter). It appears on a
    mesh outage and, briefly, on every gateway reboot before each node has been
    read for the first time.

    A plain ``== "1"`` collapses that to False, which for a light or a socket is
    indistinguishable from someone having switched it off — history, logbook and
    every ``to: "off"`` automation see an off that never happened. The numeric
    readers in every other platform already degrade to unknown on ``"NaN"``
    (``cover``, ``sensor``, ``binary_sensor``, the brightness/colour-temperature
    parsers, and ``climate``'s map of this very ``switch`` key); this is the same
    contract for the boolean ones.
    """
    value = datapoint_value(datapoint, key)
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def datapoint_suffix(datapoint_id: str) -> str:
    """Return the stable element index of a datapoint id.

    Datapoint ids look like ``id5f09764942a70ce-001``. The ``id...`` prefix is
    the device id — derived from the node UUID and element location (see
    ``device_slug``), so it changes whenever the app re-provisions or
    re-enumerates a node — but the suffix (``001``, ``010``, ``00e`` ...) is a
    stable state index the firmware assigns per device type.
    """
    return str(datapoint_id).rsplit("-", 1)[-1]


def device_slug(device: Device) -> str:
    """Return a firmware-stable slug for a device, based on its label.

    The device ``id`` is not random — it is ``"id"`` + the first 15 hex digits
    of ``md5(node UUID + element location)`` (``models.function_id_for``,
    verified against the firmware) — but it changes whenever the app
    re-provisions a node or re-enumerates its elements, when a label is moved
    to another element, or when the hardware is swapped (the one measured
    app-driven device-firmware update, 2.1.0 → 2.2.0, changed no id; the ids
    that moved around it were labels moved or hardware swapped in the app).
    The user-facing label survives
    all of that, so it is the identity anchor; it also reads well in entity
    ids, which a hash never would. Falls back to the volatile id only if the
    label is missing or unsluggable.

    The hardware identity the ``functions`` payload lacks *is* available on
    API 1.5.0+ (``GET /project/junghome``: node UUID / Bluetooth address /
    unicast / element location — ``models.parse_project_export``); it is
    attached to the registry device as ``serial_number`` (``entity.py``) and,
    on the node's primary function, a Bluetooth ``connection`` written by the
    coordinator after registration (never through ``device_info`` — the
    registry also matches on connections, and a relabelled function would
    merge into its old device), never used as an identifier — existing
    registrations must keep merging on the slug.

    The fallback inspects the slug *result*, not the raw candidate: HA's
    ``slugify`` maps symbol/whitespace-only strings (e.g. ``"❤"`` or ``"   "``)
    to the literal string ``"unknown"`` rather than an empty string. A naive
    ``label or id`` check never reaches the id fallback for such labels (the
    truthy ``"unknown"`` short-circuits it) and lets two unsluggable labels
    collide on ``"unknown"``. So each candidate is slugified in turn and the
    first non-empty, non-``"unknown"`` slug wins.

    Known limitation (accepted, not disambiguated here): two devices with
    identical — or identically-slugging — labels (e.g. ``"Lamp 1"`` vs
    ``"Lamp-1"``, both ``"lamp_1"``) produce the same slug and therefore the
    same ``stable_unique_id``, and the second device silently loses (its
    entity can't register). Per-poll disambiguation is deliberately *not*
    done — it would make unique_ids depend on poll order/membership, breaking
    the stable-identity invariant — and the hardware identity is not folded
    in either: it is only known once the export has been read, and an id
    that depends on whether a fetch succeeded is not stable.

    ## migration note
    This change alters ``device_slug`` (and thus ``unique_id``s) only for
    devices whose label was previously symbol/whitespace-only and mapped to
    ``"unknown"`` — already-broken edge cases. Well-labelled devices are
    unaffected.
    """
    for candidate in (device.get("label"), device.get("id"), "jung"):
        slug = slugify(candidate or "")
        if slug and slug != "unknown":
            return slug
    return "jung"  # pragma: no cover - "jung" always slugs to itself; unreachable


def duplicate_slugs(devices: list[Device]) -> dict[str, list[str]]:
    """Map each colliding device slug to the labels that produced it.

    ``device_slug`` deliberately does not disambiguate two devices whose labels
    slug identically (see its docstring: per-poll disambiguation would make
    unique_ids depend on poll order, and the hardware identity is only known
    after a successful export read). The second such device simply loses — its
    entities can't register.

    That is survivable for identity, but **any caller keeping per-device state
    keyed by slug must skip a colliding slug**, because two devices would
    otherwise overwrite each other's entry within a single pass and look like a
    device that changes on every refresh. Returns only the slugs with more than
    one device, so callers can skip them and report them.
    """
    by_slug: dict[str, list[str]] = {}
    for device in devices:
        label = device.get("label") or device.get("id") or ""
        by_slug.setdefault(device_slug(device), []).append(str(label))
    return {slug: labels for slug, labels in by_slug.items() if len(labels) > 1}


def stable_unique_id(
    device: Device, datapoint: Datapoint, qualifier: str | None = None
) -> str:
    """Build a firmware-stable unique id from a device label and datapoint suffix."""
    parts = [device_slug(device), datapoint_suffix(datapoint["id"])]
    if qualifier:
        parts.append(qualifier)
    return "_".join(parts)
