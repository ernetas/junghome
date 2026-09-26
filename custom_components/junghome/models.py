"""Typed models for the JUNG HOME gateway REST/WebSocket payloads.

These ``TypedDict``s describe the shape of the device data the gateway returns
from ``GET /functions`` (and re-broadcasts over the WebSocket). They are a
*static* contract: the data still arrives as untrusted JSON, so call sites keep
their defensive ``.get(...)`` access for malformed payloads — but typing the
stored device list as ``list[Device]`` makes key typos and wrong-key access
``mypy`` errors instead of silent ``Any``.

``sanitize_devices`` is that boundary: both device-list adoption points (the
REST poll and the WebSocket ``functions`` broadcast) pass the raw list through
it, so one malformed device object from the gateway is dropped or repaired
there instead of taking every platform down with a ``TypeError`` downstream.

The second half holds the hardware-identity model: ``function_id_for`` (the
gateway's own id derivation) and ``parse_project_export``, the trust boundary
that turns the app's project export (``GET /project/junghome``) into key-free
``NodeIdentity`` values.
"""

import base64
import binascii
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict, cast

_LOGGER = logging.getLogger(__name__)


class DatapointValue(TypedDict):
    """A single ``key``/``value`` pair inside a datapoint (values are strings)."""

    key: str
    value: str


class Datapoint(TypedDict):
    """A device datapoint (e.g. ``switch``, ``brightness``, ``up_request``)."""

    id: str
    type: str
    values: list[DatapointValue]


class Device(TypedDict):
    """A gateway device (``OnOff``, ``ColorLight``, ``Socket``, ``RockerSwitch``)."""

    id: str
    type: str
    label: str
    datapoints: list[Datapoint]
    sw_version: NotRequired[str]
    # Ids of the gateway groups (rooms) this device belongs to. Used to suggest a
    # Home Assistant area for the device; resolved against the groups list.
    parent_groups: NotRequired[list[str]]


class Scene(TypedDict):
    """A gateway scene (``GET /scenes``).

    ``id`` is derived, not opaque — ``"id"`` + hex(mesh scene number), so
    ``id0001`` ↔ ``value`` ``"0001"`` — and the number is the app's to
    reassign. The scene platform anchors identity on the ``label`` (the scene
    as the user sees it, and what existing installs' ``unique_id``s are keyed
    on) and re-resolves ``id`` from the coordinator's scene list at activation
    time.
    """

    id: str
    label: str
    related_functions: NotRequired[list[str]]
    value: NotRequired[str]


# --- Device-list sanitising (``GET /functions`` / WS ``functions``) ----------
#
# The platforms and the registry helpers key on these fields without further
# guards: ``slugify(label)`` raises on a non-string, ``_capability_signature``
# hashes datapoint types, ``stable_unique_id`` slices ``datapoint["id"]``,
# ``.strip()`` runs on quantity labels/units, and a non-string ``sw_version``
# reaches the device registry as a deprecation report. Each of those turned
# one malformed device object from the gateway into every entity of the entry
# failing (a poll that raises fails every poll; a platform that raises in
# setup loses all of its entities), so the shape is enforced once, here.

# Device / datapoint keys that must be strings when present. A wrong-typed
# one is dropped rather than coerced: ``str(["1"])`` would be a fake value.
_STRING_DEVICE_KEYS = ("label", "type", "sw_version")


def _sanitize_values(datapoint: dict[str, Any]) -> int:
    """Enforce ``values: [{"key": str, "value": str}, ...]``; return repairs."""
    repairs = 0
    values = datapoint.get("values")
    if not isinstance(values, list):
        datapoint["values"] = []
        return 1
    kept: list[Any] = []
    for entry in values:
        if not isinstance(entry, dict):
            repairs += 1
            continue
        value = entry.get("value")
        if "value" in entry and not isinstance(value, str):
            # A number is the one plausible mis-encoding of a value (the
            # gateway itself stringifies every state); anything else is not a
            # value at all and the entry is dropped.
            repairs += 1
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            entry["value"] = str(value)
        kept.append(entry)
    if len(kept) != len(values):
        datapoint["values"] = kept
    return repairs


def _sanitize_device(device: dict[str, Any]) -> int:
    """Repair one device dict in place; return how many items were touched."""
    repairs = 0
    for key in _STRING_DEVICE_KEYS:
        if key in device and not isinstance(device[key], str):
            del device[key]
            repairs += 1
    datapoints = device.get("datapoints")
    if not isinstance(datapoints, list):
        device["datapoints"] = []
        return repairs + 1
    kept: list[Any] = []
    for datapoint in datapoints:
        if not isinstance(datapoint, dict) or not isinstance(datapoint.get("id"), str):
            repairs += 1
            continue
        if "type" in datapoint and not isinstance(datapoint["type"], str):
            del datapoint["type"]
            repairs += 1
        repairs += _sanitize_values(datapoint)
        kept.append(datapoint)
    if len(kept) != len(datapoints):
        device["datapoints"] = kept
    return repairs


def sanitize_devices(raw: list[Any]) -> list[Device]:
    """Return the well-formed ``Device`` dicts of an untrusted device list.

    The trust boundary for ``GET /functions`` and the WebSocket ``functions``
    broadcast. Enforced, per device: a string ``id`` (a device without one
    cannot be addressed by any push and would raise from every entity's
    device lookup, so it is dropped); ``label`` / ``type`` / ``sw_version``
    strings when present (a wrong-typed one is removed, and the platforms'
    ``.get(..., default)`` fallbacks take over); ``datapoints`` a list of
    dicts each with a string ``id`` (others dropped) and a string ``type``
    when present; ``values`` a list of dicts whose ``value`` is a string (a
    number is stringified, the gateway's own encoding; anything else drops the
    entry). Repairs happen in place — the coordinator merges pushes into the
    very dicts it adopts, so the objects must be the ones handed in — and the
    good devices are returned in their original order.

    One WARNING per call names how many items were dropped or repaired and
    never what they contained: the payload is the gateway's whole device
    list, and a malformed field is exactly the thing not to log verbatim.
    """
    devices: list[Device] = []
    repairs = 0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            repairs += 1
            continue
        repairs += _sanitize_device(item)
        devices.append(cast("Device", item))
    if repairs:
        _LOGGER.warning(
            "Dropped or repaired %d malformed item(s) in the gateway's device "
            "list; the affected devices or datapoints may be missing until the "
            "gateway reports them correctly",
            repairs,
        )
    return devices


# --- Hardware identity (``GET /project/junghome``) -------------------------
#
# The gateway's function ids are not opaque: the middleware derives each one
# from the mesh node's UUID and the element's GATT location descriptor
# (``calculateDeviceId``, ``util/project_file_helper_methods.js:19-31`` in
# firmware v2.1.3): the UUID upper-cased as the CDB spells it (dashes kept),
# then the location as four upper-case hex digits with no prefix
# (``formatHex(..., 4, "")``), md5 over the concatenation, the first 15 hex
# characters of the digest behind an ``id`` prefix. ``location`` is
# ``parseInt(elements[0].location, 16)`` of the (split) device
# (``services/devices_service.js:210,225``). So the same document that
# carries the hardware identity also lets us compute which function each
# element became — no join key from the gateway needed. The document is the
# app's ``ExportDto``: the Nordic mesh CDB (``network``, Base64 JSON, or an
# inline ``meshNetwork``) plus a ``meta`` block with the app's devices. The
# CDB carries the NetKey, AppKeys and every device key; ``parse_project_export``
# reads only the fields named below and the caller drops the document.

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def function_id_for(uuid: str, location: int) -> str:
    """Return the gateway's function id for a mesh element.

    ``uuid`` is the node UUID exactly as the CDB spells it (upper-cased here,
    dashes kept — the middleware hashes the string verbatim), ``location`` the
    element's location descriptor. Mirrors ``calculateDeviceId`` (see the
    module comment above).
    """
    digest = hashlib.md5(
        f"{uuid.upper()}{location:04X}".encode(), usedforsecurity=False
    ).hexdigest()
    return f"id{digest[:15]}"


def format_mac(raw: object) -> str | None:
    """Normalise a MAC to ``AA:BB:CC:DD:EE:FF``, or ``None`` if it isn't one.

    Upper-case with colons, the spelling Home Assistant's own Bluetooth
    integrations register under ``CONNECTION_BLUETOOTH`` (bleak reports
    addresses that way; ``dr.format_mac`` lower-cases and is the *network* MAC
    convention). Accepts colon/dash/dot separators or a bare 12-hex string.
    """
    if not isinstance(raw, str):
        return None
    digits = "".join(ch for ch in raw if ch not in ":-. ")
    if len(digits) != 12 or not set(digits) <= _HEX_DIGITS:
        return None
    digits = digits.upper()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def mac_from_uuid(uuid: str) -> str | None:
    """Derive a JUNG node's Bluetooth address from its UUID, or ``None``.

    JUNG devices form their node UUID from the radio MAC in EUI-64 form —
    ``30FB10FF-FE12-3456-0000-000000000000`` is ``30:FB:10:12:34:56`` — so the
    MAC is recoverable when the app's ``meta`` carries none (iOS exports: Core
    Bluetooth hides addresses). Anything without the ``FFFE`` infix is not
    such a UUID and yields ``None`` rather than a guess.
    """
    digits = uuid.replace("-", "").upper()
    if len(digits) != 32 or digits[6:10] != "FFFE" or not set(digits) <= _HEX_DIGITS:
        return None
    return format_mac(digits[:6] + digits[10:16])


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """Hardware identity of the mesh element behind one gateway function.

    Immutable and key-free by construction: it is built from the project
    export's node/element/meta fields only, so it can be stored on the
    coordinator and dumped into diagnostics.
    """

    # The node UUID as the CDB spells it (upper-case, dashed).
    uuid: str
    # The element's GATT location descriptor the function id was derived from.
    location: int
    # Bluetooth address ``AA:BB:CC:DD:EE:FF`` (the app's ``macAddress``, else
    # derived from the UUID), or None when neither source yields one.
    mac: str | None = None
    # The element's unicast address (the node's for its primary element), or
    # None when the export carries no CDB node for it.
    unicast: int | None = None
    # The node's product id (``pid``), or None when unknown.
    product_id: int | None = None
    # Whether this is the function at the node's primary element (``elements[0]``
    # — the one answering at the node's unicast address). A node backs several
    # functions (a 2-gang push button is rockers *and* loads on one radio); the
    # node's Bluetooth address is registered as a device *connection* on this
    # one only, because Home Assistant resolves devices by connection and would
    # merge every function of the node into one device otherwise.
    primary: bool = False


@dataclass(frozen=True, slots=True)
class FunctionAnchor:
    """What ties a function's *label* to the element it was last seen on.

    Kept per device slug (``coordinator.function_anchors``, persisted in the
    entry's store) so a label that disappears and reappears under another
    name on the SAME element is recognised as a rename in the app and the
    Home Assistant device follows it (``coordinator.follow_renames``) instead
    of being replaced. The function id is ``md5(node UUID + location)``, so it
    identifies the element until the node is re-provisioned; the Bluetooth
    address plus element location identifies it across that too, when the
    project export was readable.
    """

    id: str
    mac: str | None = None
    location: int | None = None

    def matches(self, function_id: str, identity: NodeIdentity | None) -> bool:
        """Whether a live function with ``function_id``/``identity`` is this element."""
        if self.id == function_id:
            return True
        return (
            identity is not None
            and identity.mac is not None
            and self.mac == identity.mac
            and self.location == identity.location
        )


def parse_function_anchors(raw: Any) -> dict[str, FunctionAnchor]:
    """Rebuild the slug -> anchor map from the store's document (tolerant)."""
    if not isinstance(raw, dict):
        return {}
    functions = raw.get("functions")
    if not isinstance(functions, dict):
        return {}
    anchors: dict[str, FunctionAnchor] = {}
    for slug, item in functions.items():
        if not isinstance(slug, str) or not isinstance(item, dict):
            continue
        function_id = item.get("id")
        if not isinstance(function_id, str) or not function_id:
            continue
        mac = item.get("mac")
        location = item.get("location")
        anchors[slug] = FunctionAnchor(
            id=function_id,
            mac=mac if isinstance(mac, str) and mac else None,
            location=location
            if isinstance(location, int) and not isinstance(location, bool)
            else None,
        )
    return anchors


# Longest numeric string the parsers accept. Every CDB/meta number here is a
# 16-bit mesh quantity (unicast, location, pid) or a small element index, so
# ten characters is generous — and it keeps ``int()`` off a multi-kilobyte
# string (CPython refuses decimal strings over 4300 digits with ValueError).
_MAX_NUMERIC_CHARS = 10


def _hex_int(raw: object) -> int | None:
    """Parse a CDB number (``"00DC"``, ``"0x40"``, or an int), or ``None``."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().removeprefix("0x").removeprefix("0X")
    if not text or len(text) > _MAX_NUMERIC_CHARS or not set(text) <= _HEX_DIGITS:
        return None
    return int(text, 16)


def _decimal_int(raw: object) -> int | None:
    """Parse a ``meta`` location id (an int, or a decimal string), or ``None``.

    ASCII digits only: ``str.isdigit`` is also true of superscripts and other
    Unicode digits (``"²"``), which ``int()`` then rejects with ``ValueError``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdigit() or len(text) > _MAX_NUMERIC_CHARS:
        return None
    return int(text)


def _mesh_network(document: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the Nordic CDB inside an export, decoding ``network`` if Base64.

    Accepts the app's ``ExportDto`` (``network`` = Base64 of the CDB JSON), the
    same with ``network`` already an object, a bare ``{"meshNetwork": ...}``
    wrapper, and a bare CDB (``nodes`` at the top level). Anything else, and
    any decoding failure, is ``None`` — the caller then has no node data.
    """
    if isinstance(mesh := document.get("meshNetwork"), dict):
        return mesh
    network: Any = document.get("network")
    if isinstance(network, str):
        try:
            network = json.loads(base64.b64decode(network, validate=True))
        except (ValueError, binascii.Error, RecursionError):
            # RecursionError: the C parser overflows the stack on absurdly
            # nested input, the same hazard ``_dispatch_text_frame`` contains
            # for frames; ``UnicodeDecodeError`` (non-UTF-8 bytes) is a
            # ``ValueError``.
            return None
    if isinstance(network, dict):
        inner = network.get("meshNetwork", network)
        return inner if isinstance(inner, dict) else None
    if isinstance(document.get("nodes"), list):
        return document
    return None


def _meta_devices(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the app's ``meta.devices`` list (``[]`` when absent/malformed)."""
    meta = document.get("meta")
    if not isinstance(meta, dict):
        return []
    devices = meta.get("devices")
    if not isinstance(devices, list):
        return []
    return [d for d in devices if isinstance(d, dict)]


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first present key's value.

    The export is camelCase from the app and the gateway's own stored copy is
    snake_case, so both spellings are tried.
    """
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def parse_project_export(document: Any) -> dict[str, NodeIdentity]:
    """Map gateway function ids to the hardware identity behind each.

    Reads, per CDB node, ``UUID`` / ``unicastAddress`` / ``pid`` and each
    element's ``location`` (+ ``index``); per ``meta`` device, its
    ``deviceId.nodeId`` / ``locationIds`` / ``productId`` and ``macAddress``
    (nested or flat, camelCase or snake_case). Everything else — the keys in
    particular — is never touched. Total: malformed input yields a partial or
    empty map, never an exception, because the document is untrusted gateway
    JSON and a bad export must not fail setup.

    Every (node, element location) pair becomes one entry keyed by
    ``function_id_for``; the gateway derives its ids from exactly those pairs,
    so a live function's ``id`` looks up its identity directly. Whole-node
    devices (sockets, thermostats) use their first element's location, which
    is one of the pairs, so they match too. The app's recorded ``macAddress``
    is a node property and beats the UUID-derived one for every function of
    that node; a ``meta`` device without a CDB node still yields an identity
    (no unicast, never primary — only the CDB knows the primary element).
    """
    if not isinstance(document, dict):
        return {}
    identities: dict[str, NodeIdentity] = {}

    # Pass 1: the app's meta — per node, its Bluetooth address and the
    # locations it names (with the product id it records).
    meta_macs: dict[str, str] = {}
    meta_locations: list[tuple[str, int, int | None]] = []
    for device in _meta_devices(document):
        device_id = _first(device, "deviceId", "device_id")
        source = device_id if isinstance(device_id, dict) else device
        uuid = _first(source, "nodeId", "node_id", "uuid", "UUID")
        if not isinstance(uuid, str) or not uuid.strip():
            continue
        uuid = uuid.strip().upper()
        mac = format_mac(_first(device, "macAddress", "mac_address", "mac"))
        if mac is not None:
            meta_macs.setdefault(uuid, mac)
        product_id = _hex_int(_first(source, "productId", "product_id"))
        locations = _first(source, "locationIds", "location_ids")
        for raw in locations if isinstance(locations, list) else []:
            location = _decimal_int(raw)
            if location is not None:
                meta_locations.append((uuid, location, product_id))

    # Pass 2: the CDB nodes — the authoritative element list, unicast and pid.
    mesh = _mesh_network(document)
    nodes = mesh.get("nodes") if mesh is not None else None
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        uuid = _first(node, "UUID", "uuid")
        if not isinstance(uuid, str) or not uuid.strip():
            continue
        uuid = uuid.strip().upper()
        node_unicast = _hex_int(_first(node, "unicastAddress", "unicast_address"))
        product_id = _hex_int(node.get("pid"))
        elements = node.get("elements")
        parsed: list[tuple[int, int | None]] = []  # (location, index)
        for element in elements if isinstance(elements, list) else []:
            if not isinstance(element, dict):
                continue
            location = _hex_int(element.get("location"))
            if location is None:
                continue
            parsed.append((location, _decimal_int(element.get("index"))))
        if not parsed:
            continue
        # The primary element is index 0 when indices are present, else the
        # first listed — the middleware takes ``elements[0]`` either way.
        primary_location = min(
            parsed, key=lambda item: (item[1] is None, item[1] or 0)
        )[0]
        mac = meta_macs.get(uuid) or mac_from_uuid(uuid)
        for location, index in parsed:
            function_id = function_id_for(uuid, location)
            if function_id in identities:
                continue  # a location repeats per element; first wins
            unicast = (
                node_unicast + index
                if node_unicast is not None and index is not None
                else node_unicast
            )
            identities[function_id] = NodeIdentity(
                uuid=uuid,
                location=location,
                mac=mac,
                unicast=unicast,
                product_id=product_id,
                primary=location == primary_location,
            )

    # Pass 3: meta locations the CDB did not cover.
    for uuid, location, product_id in meta_locations:
        function_id = function_id_for(uuid, location)
        if function_id not in identities:
            identities[function_id] = NodeIdentity(
                uuid=uuid,
                location=location,
                mac=meta_macs.get(uuid) or mac_from_uuid(uuid),
                product_id=product_id,
            )

    return identities


# --- Device properties (``GET /devices/?verbose=true``) -----------------------
#
# The deprecated/experimental verbose device endpoint returns the middleware's
# raw ``JungHomeDevice`` objects — ``device_id`` is the function id — and with
# them the device *properties* the function list never carries (probed live
# 2026-09-16, docs/gateway-rest-api.md): a metering socket's cumulative energy
# counter ``total_device_energy_use`` (Wh), every device's ``software_revision``
# (``[2, 2, 0, 2]``), per-state ``statistics.reachable``, and a tunable-white
# light's colour-temperature range. Only those four are read; the rest of the
# document is dropped.

# The device firmware that started publishing every button event twice
# (docs/cross-repo-analysis.md §1.1). A button whose revision is known to be
# older reports each tap once, so duplicate suppression would only cost it
# fast double-taps.
DOUBLED_BUTTON_FIRMWARE = (2, 2, 0)


@dataclass(frozen=True, slots=True)
class DeviceProperties:
    """The verbose endpoint's per-device facts the integration uses."""

    # The energy counter exists on this device (a metering socket); its value
    # is None until the middleware has polled it.
    has_energy: bool = False
    energy_wh: float | None = None
    # ``software_revision`` as a version tuple, e.g. ``(2, 2, 0, 2)``.
    software_revision: tuple[int, ...] | None = None
    # The middleware's ``isDeviceOnline``: any state reachable.
    reachable: bool | None = None
    # The (min, max) Kelvin window the gateway clamps this light's
    # colour-temperature writes to — see ``color_temp_range``.
    color_temp_range: tuple[int, int] | None = None


def _entries(collection: Any) -> list[dict[str, Any]]:
    """Return the dict-valued entries of a ``states``/``property`` map (dict or list)."""
    if isinstance(collection, dict):
        collection = list(collection.values())
    if not isinstance(collection, list):
        return []
    return [item for item in collection if isinstance(item, dict)]


def _revision(raw: object) -> tuple[int, ...] | None:
    """``[2, 2, 0, 2]`` or ``"2.2.0.2"`` -> ``(2, 2, 0, 2)``; anything else None."""
    parts: list[Any]
    if isinstance(raw, str):
        parts = raw.strip().split(".")
    elif isinstance(raw, list):
        parts = raw
    else:
        return None
    numbers: list[int] = []
    for part in parts:
        value = _decimal_int(part) if isinstance(part, str) else part
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        numbers.append(value)
    return tuple(numbers) if numbers else None


def _energy_wh(prop: dict[str, Any]) -> float | None:
    """Return the counter's value in Wh (a kWh-labelled value is scaled), or None."""
    value = prop.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    profile = prop.get("profile")
    unit = profile.get("unit") if isinstance(profile, dict) else None
    scale = 1000.0 if isinstance(unit, str) and unit.strip().lower() == "kwh" else 1.0
    result = float(value) * scale
    return result if result >= 0 else None


# The Light CTL Temperature Range a mesh node can report (Mesh Model spec,
# 0x0320-0x4E20 K; the gateway's own range state declares exactly this as its
# model range — `models/device_states/ColorTemperatureStateRange.js:83`). A
# range outside it is not a tunable-white range — the spec's 0xFFFF "unknown"
# lands here — and is treated as unknown rather than declared to Home
# Assistant, which would then offer it to the user.
MIN_PLAUSIBLE_KELVIN = 800
MAX_PLAUSIBLE_KELVIN = 20000


def _as_kelvin(raw: Any) -> int | None:
    """Coerce one end of a range to Kelvin, or None if it isn't a number.

    ``"2700"`` and ``2700`` are both accepted; ``bool`` is rejected explicitly
    (an ``int`` subclass, and ``True`` is not a temperature). Every conversion
    can raise on untrusted JSON, and not only ``ValueError``: ``float()`` on a
    huge ``int`` (``json.loads`` parses integer literals at arbitrary
    precision) raises ``OverflowError``, and ``round()`` rejects the bare
    ``NaN``/``Infinity`` literals ``json.loads`` also accepts — screened out
    by ``math.isfinite`` first.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        kelvin = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(kelvin):
        return None
    return round(kelvin)


def parse_kelvin_range(raw: Any) -> tuple[int, int] | None:
    """Parse a colour-temperature range, or None if unusable.

    Accepts the middleware's ``{"min": 2000, "max": 6000, ...}`` profile range
    and the range state's ``[2000, 6000]`` value. Rejects non-numeric,
    reversed, zero-width and implausible ranges — the light then keeps its
    defaults.
    """
    if isinstance(raw, dict):
        low, high = raw.get("min"), raw.get("max")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        low, high = raw[0], raw[1]
    else:
        return None
    low_k, high_k = _as_kelvin(low), _as_kelvin(high)
    if low_k is None or high_k is None or low_k >= high_k:
        return None
    if low_k < MIN_PLAUSIBLE_KELVIN or high_k > MAX_PLAUSIBLE_KELVIN:
        return None
    return low_k, high_k


def color_temp_range(states: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Return the Kelvin window the gateway clamps a light's writes to.

    That window is the ``color_temperature`` state's ``profile.range``: the
    state's ``publishValue`` clamps every write to it
    (`models/device_states/ColorTemperatureState.js:94-103`). The constructor
    sets 2000-6000 K (`:60`), and once the middleware has read the node's
    Light CTL Temperature Range (the ``color_temperature_range`` state,
    `ColorTemperatureStateRange.js:101-120`) the state binding copies it in
    (`services/device_state_service.js:664-687` ->
    `fromState_ColorTemperatureRange`, `ColorTemperatureState.js:190-197`). So
    the profile range is the gateway's effective limit either way — the
    range state's own value is not read, it is already folded in. Neither
    reaches ``/functions/`` (``getDatapointTypeByState`` maps the range state
    to no datapoint, `util/datapoint_helper_methods.js:97`), which is why only
    this endpoint carries it.
    """
    for state in states:
        if state.get("state_type") != "color_temperature":
            continue
        profile = state.get("profile")
        if isinstance(profile, dict):
            return parse_kelvin_range(profile.get("range"))
    return None


def parse_device_properties(document: Any) -> DeviceProperties | None:
    """Parse one verbose device object; None if it is not one."""
    if not isinstance(document, dict) or not isinstance(document.get("device_id"), str):
        return None
    has_energy = False
    energy_wh: float | None = None
    revision: tuple[int, ...] | None = None
    for prop in _entries(document.get("property")):
        kind = prop.get("state_type")
        if kind == "total_device_energy_use":
            has_energy = True
            energy_wh = _energy_wh(prop)
        elif kind == "software_revision":
            revision = _revision(prop.get("value"))
    reachable: bool | None = None
    states = _entries(document.get("states"))
    if states:
        reachable = any(
            isinstance(stats := state.get("statistics"), dict)
            and stats.get("reachable") is True
            for state in states
        )
    return DeviceProperties(
        has_energy=has_energy,
        energy_wh=energy_wh,
        software_revision=revision,
        reachable=reachable,
        color_temp_range=color_temp_range(states),
    )


def parse_devices_verbose(raw: Any) -> dict[str, DeviceProperties]:
    """Map function ids to properties from the verbose device list (best-effort)."""
    if not isinstance(raw, list):
        return {}
    result: dict[str, DeviceProperties] = {}
    for item in raw:
        parsed = parse_device_properties(item)
        if parsed is not None:
            result[item["device_id"]] = parsed
    return result
