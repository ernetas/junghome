"""Tests for the firmware-stable identity helpers in const.py.

These guard the core invariant the whole integration relies on: identity is
derived from the device label + datapoint suffix, never the gateway's volatile
device id.
"""

from custom_components.junghome.const import (
    datapoint_suffix,
    device_slug,
    stable_unique_id,
)


def test_datapoint_suffix_returns_trailing_element_index():
    assert datapoint_suffix("id5f09764942a70ce-001") == "001"
    assert datapoint_suffix("idabc-00e") == "00e"


def test_datapoint_suffix_without_dash_returns_whole():
    assert datapoint_suffix("noindex") == "noindex"


def test_device_slug_from_label():
    assert device_slug({"label": "Living Room R1 B"}) == "living_room_r1_b"


def test_device_slug_falls_back_to_id_then_default():
    assert device_slug({"id": "id123"}) == "id123"
    assert device_slug({}) == "jung"


def test_stable_unique_id_combines_slug_suffix_and_qualifier():
    device = {"label": "Living Room R1 B"}
    datapoint = {"id": "idXYZ-001"}
    assert stable_unique_id(device, datapoint) == "living_room_r1_b_001"
    assert stable_unique_id(device, datapoint, "event") == "living_room_r1_b_001_event"


def test_stable_unique_id_is_independent_of_the_gateway_device_id():
    """Same label + datapoint suffix must yield the same id even after the
    gateway regenerates the device id on a firmware update."""
    device = {"label": "Kitchen Light"}
    before = {"id": "idAAAA1111-010"}
    after = {"id": "idBBBB2222-010"}
    assert stable_unique_id(device, before) == stable_unique_id(device, after)
