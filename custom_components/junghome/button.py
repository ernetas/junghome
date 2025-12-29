import logging
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SIGNAL_JUNG_BUTTON_EVENT = "jung_home_button_event"

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Jung Home button event forwarding (no entities)."""
    def handle_button_event(message):
        _LOGGER.debug("[JUNGHOME] handle_button_event called with message: %s", message)
        data = message.get("data")
        if not data:
            return
        device_id = data.get("id")
        values = data.get("values", [])
        for value in values:
            if value["key"] in {"up_request", "down_request", "trigger_request"} and value["value"] == "1":
                _LOGGER.debug("Stateless button event detected for device %s", device_id)
                hass.bus.fire(
                    "jung_home_button_press",
                    {
                        "device_id": device_id,
                        "datapoint_type": data.get("type"),
                    }
                )
                break
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_JUNG_BUTTON_EVENT, handle_button_event)
    )
    # No entities to add
    return

# Remove button.py as event.py now handles all button logic
