"""Constants and firmware-stable identity helpers for Jung Home."""

from homeassistant.util import slugify

from .models import Datapoint, Device

DOMAIN = "junghome"

# Options-flow key: the stable unique_ids of covers whose position the gateway
# reports inverted relative to Home Assistant's convention. The gateway's native
# `level` is percent-*closed* (firmware: closing drives the BT-Mesh Generic Level
# toward 100 %, opening toward 0 %), which is correct for roller shutters/blinds.
# Awnings (Markise) mount the motor the opposite way — "extended" is what the user
# calls open — so for them the mapping must be flipped. There is no awning hint in
# the gateway's function data, so the user marks them here. See cover.py.
CONF_INVERTED_COVERS = "inverted_covers"


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
