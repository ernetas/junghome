"""Tests for ``models.sanitize_devices``, the device-list trust boundary.

Every shape here was produced by a fuzz run against the shipped code, where
each one took the whole integration down through a different platform:
``slugify`` on a non-string label failed every poll, a non-list
``datapoints`` raised out of ``async_setup_entry``, a datapoint without an
``id`` killed the light platform, a list-valued thermostat value was
unhashable, a list-valued quantity label had no ``.strip()``, and a
list-valued ``sw_version`` reached the device registry as a deprecation
report. The sanitiser must turn each into one warning and one missing item.
"""

import logging
from copy import deepcopy

import pytest

from custom_components.junghome.models import sanitize_devices


def _device(**over: object) -> dict:
    base: dict = {
        "id": "idfuzz",
        "type": "OnOff",
        "label": "Fuzz",
        "datapoints": [
            {
                "id": "idfuzz-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            }
        ],
    }
    base.update(over)
    return base


GOOD = _device()


def test_a_well_formed_list_passes_through_untouched_and_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The common case: the very same dicts, in order, and no warning.

    Identity matters — the coordinator merges WebSocket pushes into the dicts
    it adopted, so the sanitiser must hand back the objects it was given.
    """
    caplog.set_level(logging.WARNING)
    first, second = _device(), _device(id="idother", label="Other")
    result = sanitize_devices([first, second])
    assert result[0] is first
    assert result[1] is second
    assert result == [_device(), _device(id="idother", label="Other")]
    assert not caplog.records


@pytest.mark.parametrize(
    "item",
    ["not a dict", None, 42, [], _device(id=5), _device(id=None), {"label": "No id"}],
    ids=["str", "none", "int", "list", "id_int", "id_none", "id_missing"],
)
def test_a_device_without_a_string_id_is_dropped(item: object) -> None:
    """No id means nothing can address it; a non-dict is not a device at all."""
    assert sanitize_devices([GOOD, item, deepcopy(GOOD)]) == [GOOD, GOOD]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("label", 123),
        ("label", ["a"]),
        ("label", True),
        ("label", None),
        ("type", ["OnOff"]),
        ("type", 7),
        ("sw_version", ["1"]),
        ("sw_version", 1.5),
    ],
)
def test_wrong_typed_string_fields_are_removed(key: str, value: object) -> None:
    """A wrong-typed label/type/sw_version is dropped, not stringified.

    The platforms read these with ``.get(key, default)``, so a missing key is
    the shape they already handle; ``str(["1"])`` would be a fabricated value
    on the device page.
    """
    device = _device(**{key: value})
    (result,) = sanitize_devices([device])
    assert result is device
    assert key not in result
    expected = _device()
    expected.pop(key, None)
    assert result == expected


@pytest.mark.parametrize(
    "datapoints", [None, {"id": "x"}, "abc", 3], ids=["none", "dict", "str", "int"]
)
def test_non_list_datapoints_become_an_empty_list(datapoints: object) -> None:
    (result,) = sanitize_devices([_device(datapoints=datapoints)])
    assert result["datapoints"] == []


def test_datapoints_without_a_string_id_or_not_dicts_are_dropped() -> None:
    """The stable unique id slices ``datapoint["id"]``: no id, no entity."""
    good = {"id": "idfuzz-002", "type": "brightness", "values": []}
    device = _device(
        datapoints=[
            {"type": "switch", "values": []},
            {"id": 5, "type": "switch", "values": []},
            {"id": ["a"], "type": "switch", "values": []},
            "x",
            1,
            None,
            good,
        ]
    )
    (result,) = sanitize_devices([device])
    assert result["datapoints"] == [good]
    assert result["datapoints"][0] is good


def test_a_non_string_datapoint_type_is_removed() -> None:
    """The capability watcher hashes datapoint types; a list is unhashable."""
    (result,) = sanitize_devices(
        [_device(datapoints=[{"id": "idfuzz-001", "type": ["switch"], "values": []}])]
    )
    assert result["datapoints"] == [{"id": "idfuzz-001", "values": []}]


@pytest.mark.parametrize("values", [None, "ab", {"key": "switch"}, 0])
def test_non_list_values_become_an_empty_list(values: object) -> None:
    (result,) = sanitize_devices(
        [_device(datapoints=[{"id": "idfuzz-001", "type": "switch", "values": values}])]
    )
    assert result["datapoints"][0]["values"] == []


def test_value_entries_are_dicts_with_string_values() -> None:
    """Numbers are stringified (the gateway's own encoding); the rest go.

    A list-valued thermostat preset was unhashable in the climate platform, a
    list-valued quantity label had no ``.strip()`` in the sensor platform;
    both now simply read as absent.
    """
    datapoint = {
        "id": "idfuzz-001",
        "type": "quantity",
        "values": [
            {"key": "quantity", "value": 21},
            {"key": "quantity_scaled", "value": 21.5},
            {"key": "quantity_label", "value": ["Power"]},
            {"key": "quantity_unit", "value": True},
            {"key": "quantity_note", "value": None},
            {"key": "quantity_extra"},
            1,
            "x",
            {"key": "kept", "value": "1"},
        ],
    }
    (result,) = sanitize_devices([_device(datapoints=[datapoint])])
    assert result["datapoints"][0]["values"] == [
        {"key": "quantity", "value": "21"},
        {"key": "quantity_scaled", "value": "21.5"},
        {"key": "quantity_extra"},
        {"key": "kept", "value": "1"},
    ]


def test_one_warning_per_call_names_the_count_and_never_the_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three malformed things in one list -> one WARNING saying "3"."""
    caplog.set_level(logging.WARNING)
    secret_label = "TOP-SECRET-LABEL"
    devices = [
        _device(label=[secret_label]),
        "junk",
        _device(id="idb", datapoints=[{"id": "idb-1", "values": "nope"}]),
    ]
    result = sanitize_devices(devices)
    assert [d["id"] for d in result] == ["idfuzz", "idb"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "3 malformed item(s)" in warnings[0].getMessage()
    assert secret_label not in caplog.text
    assert "junk" not in caplog.text
