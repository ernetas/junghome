"""Light platform for Jung Home (on/off, dimmable, tunable white)."""

import logging
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Commands are cheap async WebSocket sends; don't serialise them.
PARALLEL_UPDATES = 0

DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6500


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home lights from a config entry."""
    coordinator = config_entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_lights() -> None:
        """Add entities for any lights not yet created (handles devices added later)."""
        new_entities: list[JungHomeLight] = []
        for device in coordinator.data or []:
            if device.get("type") in ("OnOff", "DimmerLight", "ColorLight"):
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "switch":
                        uid = stable_unique_id(device, datapoint)
                        if uid in known:
                            continue
                        known.add(uid)
                        new_entities.append(
                            JungHomeLight(coordinator, device, datapoint)
                        )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_lights()
    config_entry.async_on_unload(coordinator.async_add_listener(_discover_lights))


class JungHomeLight(JungHomeEntity, LightEntity):
    """Representation of a Jung Home light."""

    # The light is the device's main feature, so it adopts the device name. With
    # has_entity_name the entity_id is `light.<device>` instead of the old
    # `light.<device>_<device>` (label was previously baked into the name too).
    _attr_name = None

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        # Find related datapoints (brightness / color_temperature) for ColorLight
        self._brightness_datapoint = next(
            (
                dp
                for dp in device.get("datapoints", [])
                if dp.get("type") == "brightness"
            ),
            None,
        )
        self._color_temp_datapoint = next(
            (
                dp
                for dp in device.get("datapoints", [])
                if dp.get("type") == "color_temperature"
            ),
            None,
        )
        self._brightness_datapoint_id = (
            self._brightness_datapoint.get("id") if self._brightness_datapoint else None
        )
        self._color_temp_datapoint_id = (
            self._color_temp_datapoint.get("id") if self._color_temp_datapoint else None
        )
        # Capabilities follow the datapoints the device actually exposes, not the
        # function type name: DimmerLight has brightness, ColorLight adds
        # color_temperature, OnOff has neither. (Before, brightness was gated on
        # type == "ColorLight", so DimmerLight produced no entity at all.)
        self._has_brightness = self._brightness_datapoint_id is not None
        self._has_color_temp = self._color_temp_datapoint_id is not None
        # Device brightness scale is 0-100 (device) — Home Assistant uses 0-255
        self._name = device.get("label", "Jung Light")
        # Firmware-stable id derived from the label, not the volatile device id.
        self._attr_unique_id = stable_unique_id(device, datapoint)
        self._is_on = self._get_state_from_datapoint(datapoint)

        # Brightness and color temperature are read independently: a device could
        # (unusually) expose color_temperature without a brightness datapoint, and
        # COLOR_TEMP still needs its kelvin range set, so this is not gated on
        # _has_brightness.
        self._brightness: int | None = (
            self._get_brightness_from_datapoint(self._brightness_datapoint)
            if self._has_brightness
            else None
        )
        self._color_temp: int | None = (
            self._get_color_temp_from_datapoint(self._color_temp_datapoint)
            if self._has_color_temp
            else None
        )
        if self._has_color_temp:
            self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
            self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN

        # Color mode is fixed by the device's capabilities (datapoints), not by
        # the current value. Home Assistant requires supported_color_modes to be
        # a stable set, so decide it once here. COLOR_TEMP already implies
        # brightness support.
        if self._has_color_temp:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif self._has_brightness:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    @property
    def is_on(self) -> bool | None:
        """Return the state of the light."""
        return self._is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the light."""
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin (device-native)."""
        return self._color_temp

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Handling coordinator update for light %s", self._name)
        # Refresh each attribute only from its own datapoint's push, so a switch=on
        # echo arriving before the brightness echo can't momentarily reset the
        # brightness slider to the stale snapshot value (UI flicker). On a REST
        # poll (no push marker) all three refresh together.
        if self._should_refresh(self._datapoint["id"]):
            switch_dp = self._find_datapoint(self._datapoint["id"])
            if switch_dp:
                self._is_on = self._get_state_from_datapoint(switch_dp)
        if self._brightness_datapoint_id and self._should_refresh(
            self._brightness_datapoint_id
        ):
            new_brightness = self._get_brightness_from_datapoint(
                self._find_datapoint(self._brightness_datapoint_id)
            )
            # The device reports brightness 0 when the light is off (on/off is the
            # separate switch datapoint). Ignore a 0 so an off->on transition keeps
            # the last level instead of briefly showing 0% until the device echoes
            # the restored brightness a frame later. HA delivers "set brightness 0"
            # as turn_off, so a genuine "on at 0%" never occurs.
            if new_brightness:
                self._brightness = new_brightness
        if self._color_temp_datapoint_id and self._should_refresh(
            self._color_temp_datapoint_id
        ):
            self._color_temp = self._get_color_temp_from_datapoint(
                self._find_datapoint(self._color_temp_datapoint_id)
            )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        _LOGGER.debug("Turning on light %s", self._name)
        # Turn on first, then apply brightness/color temperature to avoid
        # device-side overrides (some devices reset brightness on power-on).
        await self.coordinator.turn_on_light(self._datapoint["id"])
        self._is_on = True
        if self._has_brightness and "brightness" in kwargs:
            await self._set_brightness(kwargs["brightness"])
        if self._has_color_temp and "color_temp_kelvin" in kwargs:
            await self._set_color_temp(kwargs["color_temp_kelvin"])
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        _LOGGER.debug("Turning off light %s", self._name)
        await self.coordinator.turn_off_light(self._datapoint["id"])
        self._is_on = False
        self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool:
        """Extract the state of the light from its datapoint."""
        return datapoint_value(datapoint, "switch") == "1"

    def _get_brightness_from_datapoint(self, datapoint: Datapoint | None) -> int:
        """Extract the brightness of the light from its datapoint."""
        value = datapoint_value(datapoint, "brightness")
        if value is None:
            return 0
        try:
            raw = int(value)
        except (TypeError, ValueError):
            raw = 0
        # Device reports 0-100; convert linearly to HA 0-255, clamping the
        # untrusted gateway value into HA's documented 0-255 brightness range.
        return max(0, min(255, round(raw * 255 / 100)))

    def _ha_to_raw_brightness(self, ha_brightness: int) -> int:
        """Convert Home Assistant 0-255 brightness to device raw scale (0-100)."""
        raw = round(ha_brightness * 100 / 255)
        # A non-zero HA brightness (1-2) rounds to 0 on the 0-100 device scale,
        # which the device reads as off; floor it at 1 so "very dim" stays on.
        return max(1, raw) if ha_brightness > 0 else 0

    def _get_color_temp_from_datapoint(self, datapoint: Datapoint | None) -> int | None:
        """Extract the color temperature of the light from its datapoint."""
        value = datapoint_value(datapoint, "color_temperature")
        if value is None:
            return None
        try:
            kelvin = int(value)
        except (TypeError, ValueError):
            return None
        # Clamp to the advertised range so an out-of-range gateway value doesn't
        # violate the declared min/max color-temperature contract.
        return max(DEFAULT_MIN_KELVIN, min(DEFAULT_MAX_KELVIN, kelvin))

    async def _set_brightness(self, brightness: int) -> None:
        """Set the brightness of the light."""
        _LOGGER.debug("Setting brightness for light %s to %s", self._name, brightness)
        if not self._brightness_datapoint_id:
            _LOGGER.warning("No brightness datapoint id for light %s", self._name)
            return
        # Convert Home Assistant 0-255 brightness to device raw scale (0-100 or 0-255)
        ha_brightness = int(brightness)
        raw_value = self._ha_to_raw_brightness(ha_brightness)
        _LOGGER.debug(
            "Converted HA brightness %s -> raw %s for %s",
            ha_brightness,
            raw_value,
            self._name,
        )
        await self.coordinator.set_brightness(self._brightness_datapoint_id, raw_value)
        self._brightness = brightness
        self.async_write_ha_state()

    async def _set_color_temp(self, kelvin: int) -> None:
        """Set the color temperature of the light (Kelvin, device-native)."""
        if not self._color_temp_datapoint_id:
            _LOGGER.warning(
                "No color_temperature datapoint id for light %s", self._name
            )
            return
        kelvin = int(kelvin)
        _LOGGER.debug(
            "Setting color temperature for light %s to %sK", self._name, kelvin
        )
        await self.coordinator.set_color_temp(self._color_temp_datapoint_id, kelvin)
        self._color_temp = kelvin
        self.async_write_ha_state()
