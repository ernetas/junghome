from homeassistant.util import slugify

DOMAIN = "junghome"


def datapoint_suffix(datapoint_id) -> str:
    """
    Return the stable element index of a datapoint id.

    Datapoint ids look like ``id5f09764942a70ce-001``. The ``id...`` prefix is
    the device id, which the gateway regenerates on firmware updates, but the
    suffix (``001``, ``010``, ``00e`` ...) is a stable element/property index.
    """
    return str(datapoint_id).rsplit("-", 1)[-1]


def device_slug(device: dict) -> str:
    """
    Return a firmware-stable slug for a device, based on its label.

    The gateway exposes no hardware identifier (serial/MAC/address); the user
    facing label is the only attribute that survives firmware updates, so it is
    used as the identity anchor. Falls back to the volatile id only if a device
    has no label.
    """
    return slugify(device.get("label") or device.get("id") or "jung")


def stable_unique_id(
    device: dict, datapoint: dict, qualifier: str | None = None
) -> str:
    """Build a firmware-stable unique id from a device label and datapoint suffix."""
    parts = [device_slug(device), datapoint_suffix(datapoint.get("id"))]
    if qualifier:
        parts.append(qualifier)
    return "_".join(parts)
