"""Diagnostics support for Jung Home."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_TOKEN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import JungHomeConfigEntry

# The gateway token is a bearer credential; never include it in a downloadable
# report. The host (gateway IP/hostname) is mild PII and diagnostics are often
# pasted into public issues, so redact it too. Device labels are intentionally
# retained in `devices` below: they're the stable identity anchor and are the
# main thing that makes a diagnostics dump useful for debugging.
TO_REDACT = {CONF_TOKEN, "token", CONF_HOST, "host"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JungHomeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "title": entry.title,
        },
        "gateway_version": coordinator.gateway_version,
        "device_count": len(coordinator.data or []),
        "devices": coordinator.data or [],
    }
