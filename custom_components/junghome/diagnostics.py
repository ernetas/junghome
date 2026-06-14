"""Diagnostics support for Jung Home."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# The gateway token is a bearer credential; never include it in a downloadable
# report. Datapoint values/labels are not secret and are kept for debugging.
TO_REDACT = {CONF_TOKEN, "token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "title": entry.title,
        },
        "device_count": len(coordinator.data or []),
        "devices": coordinator.data or [],
    }
