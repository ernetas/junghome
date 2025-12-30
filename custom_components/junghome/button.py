"""
Button event forwarding for the Jung Home integration.

This module listens for internal dispatcher events and forwards
stateless button presses to the Home Assistant event bus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_connect

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SIGNAL_JUNG_BUTTON_EVENT: str = "jung_home_button_event"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    _async_add_entities: Callable[..., None] | None = None,
) -> None:
    """Set up Jung Home button event forwarding (no entities)."""

    def handle_button_event(message: Mapping[str, Any]) -> None:
        """Handle a dispatcher message and fire a HA event on button press."""
        _LOGGER.debug("handle_button_event called with message: %s", message)
        data = message.get("data")
        if not data:
            return

        device_id = data.get("id")
        values = data.get("values", [])

        # Look for stateless press requests
        for value in values:
            if (
                value.get("key")
                in {"up_request", "down_request", "trigger_request"}
                and value.get("value") == "1"
            ):
                _LOGGER.debug(
                    "Stateless button event detected for device %s", device_id
                )
                hass.bus.fire(
                    "jung_home_button_press",
                    {
                        "device_id": device_id,
                        "datapoint_type": data.get("type"),
                    },
                )
                break

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_JUNG_BUTTON_EVENT, handle_button_event)
    )

# No entities to add; event forwarding only.
