"""Typed models for the JUNG HOME gateway REST/WebSocket payloads.

These ``TypedDict``s describe the shape of the device data the gateway returns
from ``GET /functions`` (and re-broadcasts over the WebSocket). They are a
*static* contract: the data still arrives as untrusted JSON, so call sites keep
their defensive ``.get(...)`` access for malformed payloads — but typing the
stored device list as ``list[Device]`` makes key typos and wrong-key access
``mypy`` errors instead of silent ``Any``.

The second half holds the hardware-identity model: ``function_id_for`` (the
gateway's own id derivation) and ``parse_project_export``, the trust boundary
that turns the app's project export (``GET /project/junghome``) into key-free
``NodeIdentity`` values.
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict


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


def _hex_int(raw: object) -> int | None:
    """Parse a CDB number (``"00DC"``, ``"0x40"``, or an int), or ``None``."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().removeprefix("0x").removeprefix("0X")
    if not text or not set(text) <= _HEX_DIGITS:
        return None
    return int(text, 16)


def _decimal_int(raw: object) -> int | None:
    """Parse a ``meta`` location id (an int, or a decimal string), or ``None``."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


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
        except (ValueError, binascii.Error):
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
