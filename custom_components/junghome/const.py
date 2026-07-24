"""Constants and firmware-stable identity helpers for Jung Home."""

from typing import TYPE_CHECKING

from homeassistant.util import slugify

from .models import Datapoint, Device

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN = "junghome"

# Options-flow key: the stable unique_ids of covers whose position the gateway
# reports inverted relative to Home Assistant's convention. The gateway's native
# `level` is percent-*closed* (firmware: closing drives the BT-Mesh Generic Level
# toward 100 %, opening toward 0 %), which is correct for roller shutters/blinds.
# Awnings (Markise) mount the motor the opposite way — "extended" is what the user
# calls open — so for them the mapping must be flipped. There is no awning hint in
# the gateway's function data, so the user marks them here. See cover.py.
CONF_INVERTED_COVERS = "inverted_covers"

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


def is_presence_quantity(label: str | None) -> bool:
    """Whether a quantity datapoint's label denotes presence/occupancy (boolean).

    The binary_sensor platform claims such datapoints and the numeric sensor
    platform skips them, so the two never double-expose the same datapoint (see
    ``binary_sensor.py`` / ``sensor.py``).
    """
    if not label:
        return False
    text = label.strip().lower()
    return any(keyword in text for keyword in _PRESENCE_LABEL_KEYWORDS)


def gateway_device_id(entry: "ConfigEntry") -> str:
    """Return the stable identifier for the synthetic gateway (hub) device.

    The gateway itself is not one of the gateway's *functions*, so it has no
    device slug from the device list. Give it a fixed, per-entry identifier so
    gateway-level entities (e.g. the connectivity sensor) can share one hub
    device. Anchored on the entry's ``unique_id`` (the host/mDNS hostname, which
    survives reconfigure) and falling back to the entry id.

    The ``gateway_`` prefix is what ``__init__._prune_stale_devices`` keys off to
    avoid pruning this device (it never appears in the gateway's device list).
    """
    return f"gateway_{entry.unique_id or entry.entry_id}"


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


def datapoint_suffix(datapoint_id: str) -> str:
    """Return the stable element index of a datapoint id.

    Datapoint ids look like ``id5f09764942a70ce-001``. The ``id...`` prefix is
    the device id, which the gateway regenerates on firmware updates, but the
    suffix (``001``, ``010``, ``00e`` ...) is a stable element/property index.
    """
    return str(datapoint_id).rsplit("-", 1)[-1]


def device_slug(device: Device) -> str:
    """Return a firmware-stable slug for a device, based on its label.

    The gateway exposes no hardware identifier (serial/MAC/address); the user
    facing label is the only attribute that survives firmware updates, so it is
    used as the identity anchor. Falls back to the volatile id only if the label
    is missing or unsluggable.

    The fallback inspects the slug *result*, not the raw candidate: HA's
    ``slugify`` maps symbol/whitespace-only strings (e.g. ``"❤"`` or ``"   "``)
    to the literal string ``"unknown"`` rather than an empty string. A naive
    ``label or id`` check never reaches the id fallback for such labels (the
    truthy ``"unknown"`` short-circuits it) and lets two unsluggable labels
    collide on ``"unknown"``. So each candidate is slugified in turn and the
    first non-empty, non-``"unknown"`` slug wins.

    Known limitation (accepted gateway constraint, not disambiguated here):
    two devices with identical — or identically-slugging — labels (e.g.
    ``"Lamp 1"`` vs ``"Lamp-1"``, both ``"lamp_1"``) produce the same slug and
    therefore the same ``stable_unique_id``. Because the gateway exposes no
    hardware id, the second device silently loses (its entity can't register).
    Per-poll disambiguation is deliberately *not* done — it would make
    unique_ids depend on poll order/membership, breaking the stable-identity
    invariant.

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


def stable_unique_id(
    device: Device, datapoint: Datapoint, qualifier: str | None = None
) -> str:
    """Build a firmware-stable unique id from a device label and datapoint suffix."""
    parts = [device_slug(device), datapoint_suffix(datapoint["id"])]
    if qualifier:
        parts.append(qualifier)
    return "_".join(parts)
