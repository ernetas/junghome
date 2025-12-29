import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from .coordinator import JungHomeDataUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Jung Home integration."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jung Home from a config entry."""
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        return False

    host = entry.data.get("host")
    token = entry.data.get("token")

    # Initialize the coordinator with the host and token
    coordinator = JungHomeDataUpdateCoordinator(hass, {"host": host, "token": token})

    # Store the coordinator in hass data for later use
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator
    }

    # Start the coordinator (fetch initial data and connect to WebSocket)
    await coordinator.start()

    # Forward the setup to the appropriate platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["light", "switch", "sensor", "event"])

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        del hass.data[DOMAIN][entry.entry_id]

    # Unload platforms
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "light")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "switch")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "event")

    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload a config entry."""
    await async_unload_entry(hass, entry)
    return await async_setup_entry(hass, entry)