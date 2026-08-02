"""Tests for the firmware-stable identity helpers in const.py.

These guard the core invariant the whole integration relies on: identity is
derived from the device label + datapoint suffix, never the gateway's volatile
device id.
"""

from types import SimpleNamespace

from custom_components.junghome.const import (
    datapoint_suffix,
    device_slug,
    entry_anchor,
    entry_scope,
    gateway_device_id,
    is_presence_quantity,
    stable_unique_id,
)


def test_datapoint_suffix_returns_trailing_element_index():
    assert datapoint_suffix("id5f09764942a70ce-001") == "001"
    assert datapoint_suffix("idabc-00e") == "00e"


def test_datapoint_suffix_without_dash_returns_whole():
    assert datapoint_suffix("noindex") == "noindex"


def test_datapoint_suffix_trailing_dash_returns_empty():
    """A trailing dash leaves an empty suffix segment."""
    assert datapoint_suffix("trailing-") == ""


def test_device_slug_from_label():
    assert device_slug({"label": "Living Room R1 B"}) == "living_room_r1_b"


def test_device_slug_falls_back_to_id_then_default():
    assert device_slug({"id": "id123"}) == "id123"
    assert device_slug({}) == "jung"


def test_device_slug_unicode_label_is_transliterated():
    """Non-ASCII labels slugify deterministically (Küche -> kuche)."""
    assert device_slug({"label": "Küche"}) == "kuche"


def test_device_slug_whitespace_only_label_falls_through_to_id():
    """A whitespace-only label is unsluggable ("unknown"); fall through to the
    id, NOT to the literal "unknown"."""
    assert device_slug({"label": "   ", "id": "id123"}) == "id123"
    assert device_slug({"label": "   "}) == "jung"


def test_device_slug_symbol_only_label_falls_through_not_unknown():
    """A symbol-only label slugs to "unknown" in HA; the fallback must reach the
    id (or "jung"), never return "unknown"."""
    assert device_slug({"label": "❤", "id": "id123"}) == "id123"
    assert device_slug({"label": "❤"}) == "jung"


def test_device_slug_similar_labels_collide():
    """Documented limitation: labels that slug identically collide. The gateway
    exposes no hardware id, so this is accepted, not disambiguated."""
    assert device_slug({"label": "Lamp 1"}) == "lamp_1"
    assert device_slug({"label": "Lamp-1"}) == "lamp_1"
    assert device_slug({"label": "Lamp 1"}) == device_slug({"label": "Lamp-1"})


def test_stable_unique_id_duplicate_labels_collide():
    """Documented limitation: two devices with the same label produce the SAME
    stable_unique_id, so the second device's entity cannot register. Accepted
    gateway constraint, pinned here so a future change is a conscious one."""
    device_a = {"label": "Boiler", "id": "idAAAA-001"}
    device_b = {"label": "Boiler", "id": "idBBBB-001"}
    datapoint = {"id": "idXXXX-001"}
    assert stable_unique_id(device_a, datapoint) == stable_unique_id(
        device_b, datapoint
    )


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


def test_is_presence_quantity_matches_presence_labels():
    """Presence/occupancy/motion labels are boolean states (binary_sensor)."""
    # Trailing space mirrors the real gateway label "Presence Detected ".
    assert is_presence_quantity("Presence Detected ") is True
    assert is_presence_quantity("Occupancy") is True
    assert is_presence_quantity("Motion Detected") is True


def test_is_presence_quantity_rejects_measurement_labels():
    """ "Present Illuminance" must NOT match (substring "present", not "presence"),
    so it stays a numeric sensor; other measurements and empty/None too."""
    assert is_presence_quantity("Present Illuminance ") is False
    assert is_presence_quantity("Present Device Input Power ") is False
    assert is_presence_quantity("") is False
    assert is_presence_quantity(None) is False


def test_is_presence_quantity_non_empty_unit_vetoes_keyword():
    """A keyword in the label is not enough: a measured quantity carrying a real
    unit (e.g. a "Motion Light Level" reading in lux) stays a numeric sensor."""
    assert is_presence_quantity("Motion Light Level", "lux") is False
    assert is_presence_quantity("Occupancy Illuminance", "lx") is False


def test_is_presence_quantity_empty_or_missing_unit_keeps_match():
    """The real presence flag has an empty (or absent) unit, so an empty-string
    unit — or an omitted one — still matches on the keyword."""
    assert is_presence_quantity("Presence Detected ", "") is True
    assert is_presence_quantity("Presence Detected ", "  ") is True
    assert is_presence_quantity("Occupancy") is True  # unit omitted


def test_entry_anchor_prefers_frozen_anchor_then_unique_id_then_entry_id():
    """The identity anchor resolves frozen anchor > unique_id > entry_id.

    The frozen anchor is what keeps the hub device and scene ids stable when
    an entry's unique_id is migrated (legacy host/hostname -> serial); the
    fallbacks reproduce the historical anchor for entries created before it
    existed.
    """
    frozen = SimpleNamespace(
        data={"identity_anchor": "old-host.local"},
        unique_id="0000000084fb4b1b",
        entry_id="eid",
    )
    assert entry_anchor(frozen) == "old-host.local"
    assert gateway_device_id(frozen) == "gateway_old-host.local"

    legacy = SimpleNamespace(data={}, unique_id="1.2.3.4", entry_id="eid")
    assert entry_anchor(legacy) == "1.2.3.4"
    assert gateway_device_id(legacy) == "gateway_1.2.3.4"
    assert entry_scope(legacy) == "1_2_3_4"

    no_uid = SimpleNamespace(data={}, unique_id=None, entry_id="eid")
    assert entry_anchor(no_uid) == "eid"

    # An empty/garbage frozen anchor falls through rather than anchoring on "".
    blank = SimpleNamespace(
        data={"identity_anchor": ""}, unique_id="uid", entry_id="eid"
    )
    assert entry_anchor(blank) == "uid"
