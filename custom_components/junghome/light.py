"""Light platform for Jung Home (on/off, dimmable, tunable white)."""

import logging
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_bool, datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity, claim_new_entity, entry_unloading
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Commands are cheap async WebSocket sends; don't serialise them.
PARALLEL_UPDATES = 0

# Colour-temperature range. The gateway clamps every tunable-white write to
# the `color_temperature` state's `profile.range`
# (`middleware/dist/models/device_states/ColorTemperatureState.js:94-103`, current
# firmware sdb2), which starts at these 2000-6000 K (`:60`) and becomes the
# node's own Light CTL Temperature Range once the middleware has read it
# (`services/device_state_service.js:664-687`). The light reads that
# effective window from the verbose device endpoint (`DeviceProperties.
# color_temp_range`, `models.color_temp_range`) and uses these defaults only
# until — or unless — it is known: firmware without the endpoint, a failed
# read, a range that is not plausible. The defaults are the gateway's own
# starting range — what it clamps to until it has read the node's — and the
# range of every tunable-white light in the 2026-09-16 probe. (The
# `/types/datapoints` catalog advertises 2000-10000; it is a descriptor, not
# the enforcement.) HA does not validate a requested `color_temp_kelvin`
# against the entity's range, so writes are clamped here too (`_clamp_kelvin`)
# — the gateway would clamp them silently and confirm the clamped value.
DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6000


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home lights from a config entry."""
    coordinator = config_entry.runtime_data
    known = coordinator.known_unique_ids(Platform.LIGHT)

    @callback
    def _discover_lights() -> None:
        """Add entities for any lights not yet created (handles devices added later)."""
        if entry_unloading(config_entry):
            return
        new_entities: list[JungHomeLight] = []
        for device in coordinator.data or []:
            if device.get("type") in ("OnOff", "DimmerLight", "ColorLight"):
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "switch":
                        uid = stable_unique_id(device, datapoint)
                        if not claim_new_entity(known, uid):
                            continue
                        new_entities.append(
                            JungHomeLight(coordinator, device, datapoint)
                        )
        if new_entities:
            async_add_entities(new_entities)

    _discover_lights()
    config_entry.async_on_unload(coordinator.async_add_listener(_discover_lights))


class JungHomeLight(JungHomeEntity, LightEntity):
    """Representation of a Jung Home light."""

    # Commanded over the WebSocket, so it is unavailable when the socket is down.
    _needs_websocket = True

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
        # The Kelvin window is NOT frozen here: it is read live from the
        # coordinator's device properties (`_kelvin_range`), because the verbose
        # read that carries it can land after this entity exists (a light added
        # at runtime, a setup-time read that failed and is retried). The
        # properties refresh dispatches the listeners, and the write that
        # follows publishes the new min/max (HA updates the registry's
        # capabilities with it).
        self._color_temp: int | None = (
            self._get_color_temp_from_datapoint(self._color_temp_datapoint)
            if self._has_color_temp
            else None
        )

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

    @property
    def min_color_temp_kelvin(self) -> int:
        """Return the warmest colour temperature the gateway will set."""
        return self._kelvin_range()[0]

    @property
    def max_color_temp_kelvin(self) -> int:
        """Return the coldest colour temperature the gateway will set."""
        return self._kelvin_range()[1]

    def _kelvin_range(self) -> tuple[int, int]:
        """Return the device's own Kelvin window if known, else the default."""
        props = self.coordinator.device_properties_for(self._device)
        if props is not None and props.color_temp_range is not None:
            return props.color_temp_range
        return DEFAULT_MIN_KELVIN, DEFAULT_MAX_KELVIN

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._skip_foreign_device_push():
            return  # another device's push; nothing about this light changed
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
        was_on = self._is_on
        # Turn on first, then apply brightness/color temperature to avoid
        # device-side overrides (some devices reset brightness on power-on).
        await self.coordinator.turn_on_light(self._datapoint["id"])
        self._is_on = True
        if self._has_brightness and "brightness" in kwargs:
            await self._set_brightness(kwargs["brightness"])
        elif self._has_brightness and not was_on:
            # No explicit brightness on a genuine off->on: the device restores
            # its own level on power-on. Don't guess — showing a kept/stale
            # value looked like the light jumping to 100%. Clear it and let the
            # device's own brightness report populate the real current value
            # (its WS push, or the periodic poll as a fallback).
            #
            # Gated on `was_on` because a turn_on against an ALREADY-on dimmer
            # changes nothing on the device, so no push follows — clearing here
            # left brightness unknown until the next poll for no reason. The
            # value being shown is the device's current level; keep it.
            self._brightness = None
        if self._has_color_temp and "color_temp_kelvin" in kwargs:
            await self._set_color_temp(kwargs["color_temp_kelvin"])
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        _LOGGER.debug("Turning off light %s", self._name)
        await self.coordinator.turn_off_light(self._datapoint["id"])
        self._is_on = False
        self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool | None:
        """Extract the state of the light from its datapoint.

        ``None`` when the gateway reports ``"NaN"`` (it gave up reading the
        node) — the light reads *unknown*, not off. See ``datapoint_bool``.
        """
        return datapoint_bool(datapoint, "switch")

    def _get_brightness_from_datapoint(self, datapoint: Datapoint | None) -> int:
        """Extract the brightness of the light from its datapoint."""
        value = datapoint_value(datapoint, "brightness")
        if value is None:
            return 0
        # Parse as a float, not an int: gateway numerics arrive as strings, and
        # `int("50.0")` raises — which silently read as brightness 0, i.e. a lamp
        # stuck at 0 % with nothing logged. Every other platform (cover, climate,
        # sensor) already goes through float(). ValueError also covers NaN and
        # OverflowError covers inf, both of which `round` rejects.
        try:
            raw = round(float(value))
        except (TypeError, ValueError, OverflowError):
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
        # Same as brightness above: a decimal-formatted Kelvin value must not
        # blank the colour temperature.
        try:
            kelvin = round(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
        return self._clamp_kelvin(kelvin)

    def _clamp_kelvin(self, kelvin: int) -> int:
        """Clamp a Kelvin value into this light's declared min/max range.

        One clamp for both directions: an out-of-range gateway *read* must not
        violate the declared min/max contract, and an out-of-range *write* must
        not be sent as-is — the gateway silently clamps it to the device's
        window (see `DEFAULT_MAX_KELVIN`) and confirms the clamped value, so
        sending the raw request only meant the entity briefly showed a value
        outside its own `min/max_color_temp_kelvin`.
        """
        low, high = self._kelvin_range()
        return max(low, min(high, kelvin))

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
        # The awaited reply already merged the gateway's confirmed value into
        # the coordinator (and dispatched it through _handle_coordinator_update),
        # so read that back rather than store the request: the device only has
        # 0-100 steps, so HA's 0-255 request round-trips to a neighbouring value
        # (200 -> raw 78 -> 199), and storing the request made the entity
        # disagree with the next push/poll. The request stands in only when the
        # datapoint yields nothing usable (0 is "off"/unparseable — see
        # _handle_coordinator_update — not a level the device confirmed).
        self._brightness = (
            self._get_brightness_from_datapoint(
                self._find_datapoint(self._brightness_datapoint_id)
            )
            or ha_brightness
        )
        self.async_write_ha_state()

    async def _set_color_temp(self, kelvin: int) -> None:
        """Set the color temperature of the light (Kelvin, device-native)."""
        if not self._color_temp_datapoint_id:
            _LOGGER.warning(
                "No color_temperature datapoint id for light %s", self._name
            )
            return
        # Home Assistant's service schema does not check `color_temp_kelvin`
        # against the entity's declared range, and the gateway clamps the write
        # anyway, so clamp here first: what is sent is what can be confirmed.
        kelvin = self._clamp_kelvin(int(kelvin))
        _LOGGER.debug(
            "Setting color temperature for light %s to %sK", self._name, kelvin
        )
        await self.coordinator.set_color_temp(self._color_temp_datapoint_id, kelvin)
        # Same as brightness: the confirmed value the reply merged wins over the
        # request; the request stands in only if the datapoint yields nothing.
        confirmed = self._get_color_temp_from_datapoint(
            self._find_datapoint(self._color_temp_datapoint_id)
        )
        self._color_temp = kelvin if confirmed is None else confirmed
        self.async_write_ha_state()
