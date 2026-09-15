"""Tests for the project-export parser (``models.parse_project_export``).

The gateway's ``GET /project/junghome`` hands back the app's ``ExportDto``: the
Nordic mesh CDB (Base64 inside ``network``) plus the app's ``meta`` block. The
parser must turn that into function-id-keyed hardware identities, compute the
ids exactly the way the middleware does, and never carry a key out of it.

Every key below is synthetic (repeating hex patterns); the node UUIDs are
shaped like JUNG's (MAC in EUI-64 form) but belong to no real device.
"""

import base64
import hashlib
import json
from copy import deepcopy

import pytest

from custom_components.junghome.models import (
    NodeIdentity,
    format_mac,
    function_id_for,
    mac_from_uuid,
    parse_project_export,
)

# A 2-gang push button: primary element (load 1) at index 0, a second load,
# two rocker elements — one location shared by two elements, as the CDB lists
# a rocker's button and its LED model on separate elements.
NODE_A = "AABBCCFF-FE01-0203-0000-000000000000"
MAC_A = "AA:BB:CC:01:02:03"
# A socket, whole-node device: elements 0/1 at the same location.
NODE_B = "DDEEFFFF-FE0A-0B0C-0000-000000000000"
MAC_B = "DD:EE:FF:0A:0B:0C"
# A node whose UUID is not EUI-64 shaped (no ``FFFE`` infix), so the MAC can
# only come from the app's meta.
NODE_C = "12345678-1234-1234-1234-123456789ABC"
MAC_C_META = "10:20:30:40:50:60"

NET_KEY = "0123456789ABCDEF0123456789ABCDEF"
APP_KEY = "FEDCBA9876543210FEDCBA9876543210"
DEV_KEY_A = "A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1"
DEV_KEY_B = "B2B2B2B2B2B2B2B2B2B2B2B2B2B2B2B2"
DEV_KEY_C = "C3C3C3C3C3C3C3C3C3C3C3C3C3C3C3C3"
SECRETS = (NET_KEY, APP_KEY, DEV_KEY_A, DEV_KEY_B, DEV_KEY_C)


def _cdb() -> dict:
    """A Nordic-style CDB with three nodes and the keys a real one carries."""
    return {
        "meshUUID": "00000000-0000-0000-0000-000000000000",
        "netKeys": [{"index": 0, "key": NET_KEY}],
        "appKeys": [{"index": 0, "boundNetKey": 0, "key": APP_KEY}],
        "nodes": [
            {
                "UUID": NODE_A,
                "name": "Push-button",
                "deviceKey": DEV_KEY_A,
                "unicastAddress": "00CF",
                "cid": "0527",
                "pid": "0002",
                "elements": [
                    {"index": 0, "location": "0001", "models": []},
                    {"index": 1, "location": "0002", "models": []},
                    {"index": 2, "location": "0040", "models": []},
                    {"index": 3, "location": "0040", "models": []},
                    {"index": 4, "location": "0042", "models": []},
                ],
            },
            {
                "UUID": NODE_B,
                "name": "Socket",
                "deviceKey": DEV_KEY_B,
                "unicastAddress": "0148",
                "cid": "0527",
                "pid": "0003",
                "elements": [
                    {"index": 0, "location": "0001", "models": []},
                    {"index": 1, "location": "0001", "models": []},
                ],
            },
            {
                "UUID": NODE_C,
                "name": "Oddball",
                "deviceKey": DEV_KEY_C,
                "unicastAddress": "0200",
                "cid": "0527",
                "pid": "000A",
                "elements": [{"index": 0, "location": "0001", "models": []}],
            },
            # The provisioning phone: no elements the gateway would make a
            # function of; must simply be skipped.
            {"UUID": "0F0F0F0F-0F0F-0F0F-0F0F-0F0F0F0F0F0F", "unicastAddress": "0001"},
        ],
        "groups": [],
        "scenes": [],
    }


def _export(cdb: dict | None = None, meta: dict | None = None) -> dict:
    """The app's ``ExportDto`` as the gateway serves it: Base64 CDB + meta."""
    network = base64.b64encode(json.dumps(cdb or _cdb()).encode()).decode()
    return {
        "version": "1.1",
        "appVersion": "2.2.0 (822956)",
        "platform": "Android",
        "meta": meta
        if meta is not None
        else {
            "devices": [
                {
                    "name": "Hall Light",
                    "macAddress": MAC_A,
                    "deviceId": {
                        "nodeId": NODE_A.lower(),
                        "locationIds": [1],
                        "productId": 2,
                        "actuatorFunctionId": 1,
                        "insertType": 2,
                    },
                },
                {
                    "name": "Hall Buttons",
                    "macAddress": MAC_A,
                    "deviceId": {"nodeId": NODE_A, "locationIds": [64, 66]},
                },
                # No meta entry for NODE_B at all.
                {
                    "name": "Oddball",
                    "macAddress": MAC_C_META,
                    "deviceId": {"nodeId": NODE_C, "locationIds": [1]},
                },
            ],
            "elementConnectionGroups": [],
            "scenes": [{"name": "Movie", "number": 1}],
        },
        "network": network,
    }


def test_function_id_for_matches_the_middleware_formula() -> None:
    """``"id"`` + md5(UUID upper-cased, dashes kept + 4-digit upper hex)[:15].

    Spelled out step by step as ``calculateDeviceId`` does it
    (``util/project_file_helper_methods.js:19-31``): the same expression
    reproduces the documented real-world id in ``docs/gateway-rest-api.md``.
    """
    digest = hashlib.md5((NODE_A.upper() + "0040").encode(), usedforsecurity=False)
    expected = "id" + digest.hexdigest()[:15]
    assert function_id_for(NODE_A, 0x40) == expected
    assert expected.startswith("id")
    assert len(expected) == 17
    # Case-insensitive on the UUID (the middleware upper-cases it) ...
    assert function_id_for(NODE_A.lower(), 0x40) == expected
    # ... and the location is four upper-case hex digits, zero-padded, so
    # decimal 64 and hex 0x40 are the same element and 1 is "0001".
    assert function_id_for(NODE_A, 64) == expected
    digest = hashlib.md5((NODE_A + "0001").encode(), usedforsecurity=False)
    assert function_id_for(NODE_A, 1) == "id" + digest.hexdigest()[:15]
    # Different location, different id: dashes and case are part of the hash
    # input, so a dash-less spelling would NOT match the gateway.
    assert function_id_for(NODE_A, 2) != expected
    assert function_id_for(NODE_A.replace("-", ""), 0x40) != expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aa:bb:cc:01:02:03", "AA:BB:CC:01:02:03"),
        ("AA-BB-CC-01-02-03", "AA:BB:CC:01:02:03"),
        ("aabbcc010203", "AA:BB:CC:01:02:03"),
        ("aabb.cc01.0203", "AA:BB:CC:01:02:03"),
        ("", None),
        ("aa:bb:cc:01:02", None),
        ("zz:bb:cc:01:02:03", None),
        (None, None),
        (123456789012, None),
        ({"rawValue": "base64"}, None),
    ],
)
def test_format_mac(raw: object, expected: str | None) -> None:
    assert format_mac(raw) == expected


@pytest.mark.parametrize(
    ("uuid", "expected"),
    [
        ("30FB10FF-FE12-3456-0000-000000000000", "30:FB:10:12:34:56"),
        ("30fb10ff-fe12-3456-0000-000000000000", "30:FB:10:12:34:56"),
        ("30FB10FFFE1234560000000000000000", "30:FB:10:12:34:56"),
        # Not EUI-64 shaped: refuse to guess.
        (NODE_C, None),
        ("30FB10FF-FE12-3456", None),
        ("", None),
        ("GGGGGGGG-GGGG-GGGG-GGGG-GGGGGGGGGGGG", None),
    ],
)
def test_mac_from_uuid(uuid: str, expected: str | None) -> None:
    assert mac_from_uuid(uuid) == expected


def test_parse_full_export_maps_every_element_and_drops_the_keys() -> None:
    document = _export()
    identities = parse_project_export(document)

    # Node A: four distinct locations -> four functions; the two elements at
    # 0x40 collapse into one (the gateway makes one device per location).
    assert identities[function_id_for(NODE_A, 1)] == NodeIdentity(
        uuid=NODE_A, location=1, mac=MAC_A, unicast=0xCF, product_id=2, primary=True
    )
    assert identities[function_id_for(NODE_A, 2)] == NodeIdentity(
        uuid=NODE_A, location=2, mac=MAC_A, unicast=0xD0, product_id=2
    )
    assert identities[function_id_for(NODE_A, 0x40)] == NodeIdentity(
        uuid=NODE_A, location=0x40, mac=MAC_A, unicast=0xD1, product_id=2
    )
    # The meta named location 66 (0x42) too; the CDB entry wins (has unicast).
    assert identities[function_id_for(NODE_A, 0x42)] == NodeIdentity(
        uuid=NODE_A, location=0x42, mac=MAC_A, unicast=0xD3, product_id=2
    )
    # Node B has no meta entry: the MAC is derived from the EUI-64 UUID, and
    # the whole-node socket's single function is primary.
    assert identities[function_id_for(NODE_B, 1)] == NodeIdentity(
        uuid=NODE_B, location=1, mac=MAC_B, unicast=0x148, product_id=3, primary=True
    )
    # Node C's UUID is not EUI-64, so only the app's recorded MAC can name it.
    assert identities[function_id_for(NODE_C, 1)] == NodeIdentity(
        uuid=NODE_C,
        location=1,
        mac=MAC_C_META,
        unicast=0x200,
        product_id=10,
        primary=True,
    )
    # The phone contributed nothing, and nothing else leaked in.
    assert len(identities) == 6
    assert all(isinstance(i, NodeIdentity) for i in identities.values())

    # The whole point: not one key survives the parse.
    dump = repr(identities)
    for secret in SECRETS:
        assert secret not in dump
    assert "meshUUID" not in dump
    # And the document itself was not mutated (the caller drops it, but a
    # parser that scrubbed it in place would hide that it read too much).
    assert document == _export()


def test_meta_mac_applies_to_every_function_of_the_node() -> None:
    """The app's ``macAddress`` is a node property, not a per-location one.

    Its entries name only some of a node's locations (the load, the rockers),
    but the address belongs to the radio, so a function at a location no meta
    entry lists (0x42 here) must still carry it — and it must beat the
    UUID-derived address when the two disagree.
    """
    meta = {
        "devices": [
            {
                "name": "Hall Light",
                "macAddress": "11:22:33:44:55:66",
                "deviceId": {"nodeId": NODE_A, "locationIds": [1]},
            }
        ]
    }
    identities = parse_project_export(_export(meta=meta))
    for location in (1, 2, 0x40, 0x42):
        assert identities[function_id_for(NODE_A, location)].mac == "11:22:33:44:55:66"
    # Nodes without a meta entry still fall back to the EUI-64 derivation.
    assert identities[function_id_for(NODE_B, 1)].mac == MAC_B


@pytest.mark.parametrize(
    "shape",
    ["mesh_network_wrapper", "network_object", "bare_cdb", "snake_case_meta"],
)
def test_parse_accepts_every_known_export_spelling(shape: str) -> None:
    """The gateway may serve the DTO as uploaded or its own re-serialisation."""
    cdb = _cdb()
    if shape == "mesh_network_wrapper":
        document = {"meshNetwork": cdb}
    elif shape == "network_object":
        document = {"version": "1.1", "network": {"meshNetwork": cdb}, "meta": {}}
    elif shape == "bare_cdb":
        document = cdb
    else:
        document = {
            "network": base64.b64encode(json.dumps(cdb).encode()).decode(),
            "meta": {
                "devices": [
                    {
                        "name": "Hall Light",
                        "mac_address": "11-22-33-44-55-66",
                        "device_id": {"node_id": NODE_A, "location_ids": ["1"]},
                    }
                ]
            },
        }
    identities = parse_project_export(document)
    primary = identities[function_id_for(NODE_A, 1)]
    assert primary.unicast == 0xCF
    assert primary.primary is True
    if shape == "snake_case_meta":
        assert primary.mac == "11:22:33:44:55:66"
    else:
        assert primary.mac == MAC_A
    assert function_id_for(NODE_B, 1) in identities


def test_meta_only_export_yields_identities_without_unicast_or_primary() -> None:
    """No CDB at all: the meta still names nodes and locations.

    Enough for a serial number, but the primary element is a CDB fact, so no
    function is marked primary (and hence none gets a registry connection).
    """
    document = {
        "meta": {
            "devices": [
                {
                    "name": "Hall Light",
                    "macAddress": MAC_A,
                    "deviceId": {"nodeId": NODE_A, "locationIds": [1, 64]},
                },
                # Flat spelling (no deviceId object) with a uuid key.
                {"name": "Socket", "uuid": NODE_B, "locationIds": [1]},
            ]
        }
    }
    identities = parse_project_export(document)
    assert identities == {
        function_id_for(NODE_A, 1): NodeIdentity(uuid=NODE_A, location=1, mac=MAC_A),
        function_id_for(NODE_A, 64): NodeIdentity(uuid=NODE_A, location=64, mac=MAC_A),
        function_id_for(NODE_B, 1): NodeIdentity(uuid=NODE_B, location=1, mac=MAC_B),
    }


def test_a_function_the_export_does_not_cover_has_no_identity() -> None:
    """A device id with no (node, location) behind it simply isn't in the map."""
    identities = parse_project_export(_export())
    assert "idunknown" not in identities
    assert function_id_for(NODE_A, 0x41) not in identities
    assert function_id_for("00000000-0000-0000-0000-000000000000", 1) not in identities


@pytest.mark.parametrize(
    "document",
    [
        None,
        "not a dict",
        [],
        42,
        {},
        {"network": "%%% not base64 %%%"},
        {"network": base64.b64encode(b"not json").decode()},
        {"network": base64.b64encode(b'"a json string"').decode()},
        {"network": base64.b64encode(b'{"meshNetwork": []}').decode()},
        {"meshNetwork": {"nodes": "not a list"}},
        {"meshNetwork": {"nodes": [None, 1, "x", []]}},
        {"nodes": [{"UUID": "", "elements": []}]},
        {"nodes": [{"UUID": None, "elements": [{"location": "0001"}]}]},
        {"nodes": [{"UUID": NODE_A, "elements": "0001"}]},
        {"nodes": [{"UUID": NODE_A, "elements": [{"location": "zz"}, 7, None]}]},
        {"nodes": [{"UUID": NODE_A, "elements": [{"location": True}]}]},
        {"nodes": [{"UUID": NODE_A, "elements": [{"location": -1}]}]},
        {"meta": "not a dict"},
        {"meta": {"devices": "not a list"}},
        {"meta": {"devices": [None, 1, {"deviceId": "x"}, {"deviceId": {}}]}},
        {"meta": {"devices": [{"deviceId": {"nodeId": NODE_A, "locationIds": "1"}}]}},
        {
            "meta": {
                "devices": [
                    {
                        "deviceId": {
                            "nodeId": NODE_A,
                            "locationIds": [True, "x", -2, None],
                        }
                    }
                ]
            }
        },
    ],
)
def test_parse_tolerates_malformed_documents(document: object) -> None:
    """Untrusted gateway JSON: garbage yields nothing, never an exception."""
    assert parse_project_export(document) == {}


def test_parse_keeps_the_good_parts_of_a_partly_malformed_export() -> None:
    cdb = _cdb()
    # A node with a bad unicast and a non-numeric pid, plus one bad element.
    cdb["nodes"][0]["unicastAddress"] = "not hex"
    cdb["nodes"][0]["pid"] = None
    cdb["nodes"][0]["elements"].append({"location": "nope"})
    # An element without an index: the primary stays the indexed 0 one.
    cdb["nodes"][1]["elements"] = [
        {"location": "0002"},
        {"index": 0, "location": "0001"},
    ]
    identities = parse_project_export(_export(cdb=cdb, meta={}))
    node_a = identities[function_id_for(NODE_A, 1)]
    assert node_a.unicast is None
    assert node_a.product_id is None
    assert node_a.primary is True
    assert node_a.mac == MAC_A  # derived: the meta is empty here
    assert identities[function_id_for(NODE_B, 1)].primary is True
    assert identities[function_id_for(NODE_B, 2)].primary is False
    assert identities[function_id_for(NODE_B, 2)].unicast == 0x148


def test_unindexed_elements_take_the_first_listed_as_primary() -> None:
    """Without ``index`` the middleware still uses ``elements[0]``."""
    cdb = _cdb()
    cdb["nodes"][0]["elements"] = [{"location": "0040"}, {"location": "0001"}]
    identities = parse_project_export({"meshNetwork": cdb})
    assert identities[function_id_for(NODE_A, 0x40)].primary is True
    assert identities[function_id_for(NODE_A, 1)].primary is False
    # No index: the element's unicast falls back to the node's.
    assert identities[function_id_for(NODE_A, 1)].unicast == 0xCF


def test_node_identity_is_immutable() -> None:
    identity = parse_project_export(_export())[function_id_for(NODE_A, 1)]
    with pytest.raises(AttributeError):
        identity.mac = "00:00:00:00:00:00"  # type: ignore[misc]
    assert deepcopy(identity) == identity
