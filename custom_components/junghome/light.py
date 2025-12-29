import json
import logging
import time
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from .const import DOMAIN
from .coordinator import JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500

def kelvin_to_mired(kelvin: int) -> int:
    try:
        return round(1000000 / kelvin)
    except Exception:
        return round(1000000 / DEFAULT_MIN_KELVIN)

def mired_to_kelvin(mired: int) -> int:
    try:
        return round(1000000 / mired)
    except Exception:
        return DEFAULT_MIN_KELVIN

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up Jung Home lights from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    # Fetch devices from the coordinator
    await coordinator.async_refresh()
    devices = coordinator.data

    # Create entities for each light device
    entities = []
    for device in devices:
        _LOGGER.debug("Processing device: %s", device)
        if device['type'] == 'OnOff' or device['type'] == 'ColorLight':  # Add devices with type "OnOff" or "ColorLight"
            for datapoint in device.get('datapoints', []):
                if datapoint['type'] == 'switch':
                    entities.append(JungHomeLight(coordinator, device, datapoint))

    if entities:
        async_add_entities(entities, update_before_add=True)

class JungHomeLight(CoordinatorEntity, LightEntity):
    """Representation of a Jung Home light."""

    def __init__(self, coordinator, device, datapoint):
        """Initialize the light."""
        super().__init__(coordinator)
        self._device = device
        self._datapoint = datapoint
        # Find related datapoints (brightness / color_temperature) for ColorLight
        self._brightness_datapoint = next(
            (dp for dp in device.get('datapoints', []) if dp.get('type') == 'brightness'),
            None,
        )
        self._color_temp_datapoint = next(
            (dp for dp in device.get('datapoints', []) if dp.get('type') == 'color_temperature'),
            None,
        )
        self._brightness_datapoint_id = (
            self._brightness_datapoint.get('id') if self._brightness_datapoint else None
        )
        self._color_temp_datapoint_id = (
            self._color_temp_datapoint.get('id') if self._color_temp_datapoint else None
        )
        # Device brightness scale is 0-100 (device) — Home Assistant uses 0-255
        # Track last local write to debounce weird rapid WS echoes
        self._last_written_brightness_raw = None
        self._last_written_brightness_ts = 0.0
        # Track last local write for color temperature (Kelvin)
        self._last_written_color_temp_raw = None
        self._last_written_color_temp_ts = 0.0
        self._name = device.get("label", "Jung Light")
        self._unique_id = f"{device.get('id')}_{datapoint.get('id')}"  # Use device ID and datapoint ID
        self._is_on = self._get_state_from_datapoint(datapoint)
        self.entity_id = f"light.{self._unique_id}"  # Set the entity ID

        if device['type'] == 'ColorLight':
            # Read brightness and color_temp from their specific datapoints (if present)
            self._brightness = self._get_brightness_from_datapoint(self._brightness_datapoint)
            self._color_temp = self._get_color_temp_from_datapoint(self._color_temp_datapoint)
            self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
            self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN
        else:
            self._brightness = None
            self._color_temp = None

    @property
    def name(self):
        """Return the name of the light."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the light."""
        return self._unique_id

    @property
    def is_on(self):
        """Return the state of the light."""
        return self._is_on

    @property
    def brightness(self):
        """Return the brightness of the light."""
        return self._brightness

    @property
    def color_temp(self):
        """Return the color temperature of the light."""
        # Home Assistant expects color_temp in mireds; device reports Kelvin
        if self._color_temp is None:
            return None
        return kelvin_to_mired(self._color_temp)

    @property
    def min_mireds(self) -> int:
        """Return minimum mireds supported."""
        return kelvin_to_mired(DEFAULT_MAX_KELVIN)

    @property
    def max_mireds(self) -> int:
        """Return maximum mireds supported."""
        return kelvin_to_mired(DEFAULT_MIN_KELVIN)

    @property
    def supported_color_modes(self):
        """Return the supported color modes (only the current mode, per HA 2025.3+)."""
        return {self.color_mode}

    @property
    def color_mode(self):
        """Return the currently active color mode."""
        if self._device['type'] == 'ColorLight':
            # If color_temp is set, prefer COLOR_TEMP, else BRIGHTNESS
            if self._color_temp is not None:
                return ColorMode.COLOR_TEMP
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

    @property
    def device_info(self):
        """Return device information about this light."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},  # Link to the device
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version", "Unknown Version"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Handling coordinator update for light %s", self._name)
        device = next(
            (d for d in self.coordinator.data if d["id"] == self._device["id"]), None
        )
        if device:
            datapoint = next(
                (dp for dp in device.get('datapoints', []) if dp["id"] == self._datapoint["id"]), None
            )
            if datapoint:
                self._is_on = self._get_state_from_datapoint(datapoint)
                if self._device['type'] == 'ColorLight':
                    # Update brightness/color_temp from their respective datapoints (if available)
                    if self._brightness_datapoint_id:
                        brightness_dp = next(
                            (dp for dp in device.get('datapoints', []) if dp.get('id') == self._brightness_datapoint_id),
                            None,
                        )
                        # Read raw brightness value first
                        raw_brightness = None
                        if brightness_dp:
                            for v in brightness_dp.get('values', []):
                                if v.get('key') == 'brightness':
                                    try:
                                        raw_brightness = int(v.get('value'))
                                    except (TypeError, ValueError):
                                        raw_brightness = None
                                    break
                        # If we recently wrote a brightness, debounce transient device
                        # echoes for a short window unless the echo matches our write.
                        now_ts = time.monotonic()
                        debounce_window = 3.0
                        if (
                            raw_brightness is not None
                            and self._last_written_brightness_raw is not None
                            and (now_ts - self._last_written_brightness_ts) < debounce_window
                        ):
                            if raw_brightness != self._last_written_brightness_raw:
                                _LOGGER.debug(
                                    "Ignoring transient brightness echo %s for %s (recent write %s)",
                                    raw_brightness,
                                    self._name,
                                    self._last_written_brightness_raw,
                                )
                                # keep local self._brightness until confirmed
                            else:
                                # device echoed the same value we wrote — accept and clear tracking
                                self._brightness = self._get_brightness_from_datapoint(brightness_dp)
                                self._last_written_brightness_raw = None
                                self._last_written_brightness_ts = 0.0
                        else:
                            self._brightness = self._get_brightness_from_datapoint(brightness_dp)
                    if self._color_temp_datapoint_id:
                        color_dp = next(
                            (dp for dp in device.get('datapoints', []) if dp.get('id') == self._color_temp_datapoint_id),
                            None,
                        )
                        # read raw Kelvin value
                        raw_kelvin = None
                        if color_dp:
                            for v in color_dp.get('values', []):
                                if v.get('key') == 'color_temperature':
                                    try:
                                        raw_kelvin = int(v.get('value'))
                                    except (TypeError, ValueError):
                                        raw_kelvin = None
                                    break
                        # debounce transient color temp echoes similar to brightness
                        now_ts = time.monotonic()
                        debounce_window = 3.0
                        if (
                            raw_kelvin is not None
                            and self._last_written_color_temp_raw is not None
                            and (now_ts - self._last_written_color_temp_ts) < debounce_window
                            and raw_kelvin != self._last_written_color_temp_raw
                        ):
                            _LOGGER.debug(
                                "Ignoring transient color_temp echo %sK for %s (recent write %sK)",
                                raw_kelvin,
                                self._name,
                                self._last_written_color_temp_raw,
                            )
                            # keep local value until confirmed
                        else:
                            self._color_temp = self._get_color_temp_from_datapoint(color_dp)
                _LOGGER.debug("Updated state for light %s: %s", self._name, self._is_on)
                self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        _LOGGER.debug("Turning on light %s", self._name)
        # Turn on first, then apply brightness/color temperature to avoid
        # device-side overrides (some devices reset brightness on power-on).
        await self.coordinator.turn_on_light(self._datapoint["id"])
        self._is_on = True
        if self._device['type'] == 'ColorLight':
            if "brightness" in kwargs:
                brightness = kwargs["brightness"]
                await self._set_brightness(brightness)
            if "color_temp" in kwargs:
                color_temp = kwargs["color_temp"]
                await self._set_color_temp(color_temp)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        _LOGGER.debug("Turning off light %s", self._name)
        await self.coordinator.turn_off_light(self._datapoint["id"])
        self._is_on = False
        self.async_write_ha_state()

    @property
    def should_poll(self):
        """No polling needed for this entity."""
        return False

    @property
    def available(self):
        """Return if the device is available."""
        return self.coordinator.last_update_success

    def _get_state_from_datapoint(self, datapoint):
        """Extract the state of the light from its datapoint."""
        for value in datapoint.get('values', []):
            if value['key'] == 'switch':
                return value['value'] == '1'
        return False

    def _get_brightness_from_datapoint(self, datapoint):
        """Extract the brightness of the light from its datapoint."""
        if not datapoint:
            return 0
        for value in datapoint.get('values', []):
            if value['key'] == 'brightness':
                try:
                    raw = int(value['value'])
                except (TypeError, ValueError):
                    raw = 0
                # Device reports 0-100; convert linearly to HA 0-255
                return round(raw * 255 / 100)
        return 0

    def _raw_to_ha_brightness(self, raw: int) -> int:
        """Convert device raw brightness (0-100) to Home Assistant 0-255 scale."""
        try:
            return round(raw * 255 / 100)
        except Exception:
            return 0

    def _ha_to_raw_brightness(self, ha_brightness: int) -> int:
        """Convert Home Assistant 0-255 brightness to device raw scale (0-100)."""
        try:
            return round(int(ha_brightness) * 100 / 255)
        except Exception:
            return round(int(ha_brightness) * 100 / 255)

    def _get_color_temp_from_datapoint(self, datapoint):
        """Extract the color temperature of the light from its datapoint."""
        if not datapoint:
            return 3000
        for value in datapoint.get('values', []):
            if value['key'] == 'color_temperature':
                try:
                    # Device reports Kelvin; store Kelvin
                    return int(value['value'])
                except (TypeError, ValueError):
                    return 3000
        return 3000

    async def _set_brightness(self, brightness):
        """Set the brightness of the light."""
        _LOGGER.debug("Setting brightness for light %s to %s", self._name, brightness)
        if not self._brightness_datapoint_id:
            _LOGGER.warning("No brightness datapoint id for light %s", self._name)
            return
        # Convert Home Assistant 0-255 brightness to device raw scale (0-100 or 0-255)
        ha_brightness = int(brightness)
        raw_value = self._ha_to_raw_brightness(ha_brightness)
        _LOGGER.debug("Converted HA brightness %s -> raw %s for %s", ha_brightness, raw_value, self._name)
        await self.coordinator.set_brightness(self._brightness_datapoint_id, raw_value)
        # Record last write to debounce device echoes
        try:
            self._last_written_brightness_raw = int(raw_value)
        except Exception:
            self._last_written_brightness_raw = raw_value
        self._last_written_brightness_ts = time.monotonic()
        self._brightness = brightness
        self.async_write_ha_state()

    async def _set_color_temp(self, color_temp):
        """Set the color temperature of the light."""
        _LOGGER.debug("Setting color temperature for light %s to %s", self._name, color_temp)
        if not self._color_temp_datapoint_id:
            _LOGGER.warning("No color_temperature datapoint id for light %s", self._name)
            return
        # Home Assistant supplies mireds; convert to Kelvin for device
        kelvin = mired_to_kelvin(int(color_temp))
        _LOGGER.debug("Converted HA color_temp %s mired -> %s K for %s", color_temp, kelvin, self._name)
        await self.coordinator.set_color_temp(self._color_temp_datapoint_id, kelvin)
        # store Kelvin locally
        self._color_temp = kelvin
        self.async_write_ha_state()