"""Diagnostics support for Jung Home."""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_TOKEN

from .const import DOMAIN, device_slug

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

    from .coordinator import JungHomeConfigEntry
    from .models import Device

# The gateway token is a bearer credential; never include it in a downloadable
# report. The host (gateway IP/hostname) is mild PII and diagnostics are often
# pasted into public issues, so redact it too. Device labels are intentionally
# retained in `devices` below: they're the stable identity anchor and are the
# main thing that makes a diagnostics dump useful for debugging.
TO_REDACT = {CONF_TOKEN, "token", CONF_HOST, "host"}

# `async_redact_data` only masks values it can reach by *key*. Several fields
# below are free-form text that can quote a secret inside a larger string, where
# no key exists to match:
#
# - `last_error` is `str(err)`, and an aiohttp connect failure reads
#   "Cannot connect to host <host>:443 ssl:True [...]" — re-leaking the host that
#   TO_REDACT deliberately removes from `entry.data`.
# - the raw WebSocket frames are gateway JSON kept verbatim for protocol
#   debugging. The token travels as a connect header rather than a frame body,
#   so it should never appear there, but a downloadable report that gets pasted
#   into public issues is the wrong place to rely on "should".
#
# So those go through `_scrub`, which does a literal (case-insensitive) sweep for
# the entry's own secrets.
#
# Only secrets of at least this length are swept: a one- or two-character host
# would otherwise match constantly and shred the very output being debugged.
_MIN_SCRUBBABLE = 4

# Function/datapoint types the integration turns into entities. These mirror the
# platform discovery (light/switch/sensor/event/cover/climate). The
# `support_summary` flags anything a gateway reports that is NOT in these sets, so
# an unsupported device or datapoint shows up in a downloadable report (the main
# thing needed to extend support beyond what we currently parse).
_HANDLED_FUNCTION_TYPES = {
    "OnOff",
    "DimmerLight",
    "ColorLight",
    "Socket",
    "Measurement",
    "Position",
    "PositionAndAngle",
    "Thermostat",
    "RockerSwitch",
}
_HANDLED_DATAPOINT_TYPES = {
    "switch",
    "brightness",
    "color_temperature",
    "quantity",
    "level",
    "angle",
    "temperature_ctrl",
    "up_request",
    "down_request",
    "trigger_request",
    "status_led",
}


def _secrets(entry: JungHomeConfigEntry) -> list[str]:
    """Return the entry's secrets, longest first so the token wins any overlap."""
    values = [str(entry.data.get(key) or "") for key in (CONF_TOKEN, CONF_HOST)]
    return sorted(
        (v for v in values if len(v) >= _MIN_SCRUBBABLE), key=len, reverse=True
    )


def _scrub(text: str | None, secrets: list[str]) -> str | None:
    """Mask any of ``secrets`` appearing inside free-form ``text``."""
    if not text:
        return text
    for secret in secrets:
        text = re.sub(re.escape(secret), "**REDACTED**", text, flags=re.IGNORECASE)
    return text


def _support_summary(devices: list[Device]) -> dict[str, Any]:
    """Count device/datapoint types and flag any the integration doesn't handle."""
    function_types: Counter[str] = Counter(d.get("type") or "Unknown" for d in devices)
    datapoint_types: Counter[str] = Counter(
        dp.get("type") or "unknown" for d in devices for dp in d.get("datapoints", [])
    )
    return {
        "function_types": dict(function_types),
        "unhandled_function_types": sorted(
            t for t in function_types if t not in _HANDLED_FUNCTION_TYPES
        ),
        "datapoint_types": dict(datapoint_types),
        "unhandled_datapoint_types": sorted(
            t for t in datapoint_types if t not in _HANDLED_DATAPOINT_TYPES
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    devices = coordinator.data or []
    secrets = _secrets(entry)
    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "title": entry.title,
        },
        "gateway_version": coordinator.gateway_version,
        # Whether the live push link is currently up (mirrors the gateway
        # connectivity binary_sensor); a dump taken while it is False explains
        # why state looks stale (it has fallen back to 1-minute REST polling).
        "ws_connected": coordinator.ws_connected,
        # When the WebSocket last completed a connect, and the most recent
        # REST/WebSocket failure (if any) — together they show how long a dump
        # taken mid-outage has been degraded and what's causing it.
        "ws_last_connected": coordinator.ws_last_connected,
        "last_error": _scrub(coordinator.last_error, secrets),
        "last_error_at": coordinator.last_error_at,
        # Quick map of what the gateway exposes vs what we implement — the first
        # thing to check when matching real hardware against our support.
        "support_summary": _support_summary(devices),
        "device_count": len(devices),
        # Same redaction rule as the per-device dump: today's device payloads
        # carry no host/token keys so this is a no-op, but the two endpoints
        # must not disagree about the rule.
        "devices": async_redact_data(devices, TO_REDACT),
        # Scenes and groups are separate coordinator data categories (not backed by
        # a device). Groups carry per-room capability metadata; both are kept so a
        # dump is complete for debugging discovery/recall and spotting capabilities
        # we don't yet implement.
        "scene_count": len(coordinator.scenes),
        "scenes": coordinator.scenes,
        "group_count": len(coordinator.groups),
        "groups": coordinator.groups,
        # The most recent raw WebSocket frames (live pushes), so the real wire
        # format can be matched against our parsing...
        "recent_websocket_frames": [
            _scrub(frame, secrets) for frame in coordinator.ws_frame_log
        ],
        # ...plus the latest *full* (untruncated) frame of each type, which always
        # retains the complete connect-time handshake (message / version /
        # functions / groups / scenes) even on a spammy gateway where the rolling
        # log above has churned past it.
        "latest_websocket_frame_by_type": {
            frame_type: _scrub(frame, secrets)
            for frame_type, frame in coordinator.ws_last_frame_by_type.items()
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single device.

    The entry-level dump above carries every device on the gateway, which is
    unwieldy on a large installation when the question is about one blind or one
    thermostat. This narrows it to the selected device while keeping the gateway
    context needed to interpret it — firmware version, whether live push is up,
    and the most recent error.

    ``matched`` is None when the HA device has no counterpart in the current
    poll, which is itself the useful signal: it means the gateway has stopped
    reporting it (renamed label, removed hardware) and it is on its way to being
    pruned.
    """
    coordinator = entry.runtime_data
    secrets = _secrets(entry)
    # HA devices are keyed by the firmware-stable slug, so resolve back through
    # the same function that produced the identifier.
    slugs = {
        identifier for domain, identifier in device.identifiers if domain == DOMAIN
    }
    matched = next(
        (d for d in coordinator.data or [] if device_slug(d) in slugs),
        None,
    )
    return {
        "gateway_version": coordinator.gateway_version,
        "ws_connected": coordinator.ws_connected,
        "last_error": _scrub(coordinator.last_error, secrets),
        "last_error_at": coordinator.last_error_at,
        "identifiers": sorted(slugs),
        # Redacted with the same rule as the entry dump: a per-device report is
        # pasted into public issues just as often as a full one.
        "device": async_redact_data(matched, TO_REDACT) if matched else None,
    }
