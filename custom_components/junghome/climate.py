"""Climate platform for Jung Home (Thermostat / room temperature regulator).

A ``Thermostat`` function exposes three datapoints (see
``cdb_types_datapoints.json`` / ``cdb_types_functions.json``):

- ``switch`` — on/off (``0`` / ``1``), mapped to HVAC mode OFF / HEAT.
- ``temperature_ctrl`` — the target temperature in °C (range 5..30) plus a
  ``temperature_ctrl_preset`` key (``none`` / ``frost`` / ``eco`` / ``comfort``).
- ``quantity`` — the room temperature reading, surfaced here as
  ``current_temperature``. (Sensor discovery does not turn a Thermostat's
  quantity into a standalone sensor entity; it is only the ambient reading.)
"""

import logging
import math
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Commands are cheap async WebSocket sends; don't serialise them.
PARALLEL_UPDATES = 0

DEFAULT_MIN_TEMP = 5.0
DEFAULT_MAX_TEMP = 30.0

# "frost" (frost protection) has no standard HA preset constant; expose it under
# its gateway name. The others map onto HA's well-known presets.
PRESET_FROST = "frost"
# HA preset <-> gateway preset value (they coincide, but keep the map explicit
# so a future HA constant rename doesn't silently desync the wire value).
_HA_TO_DEVICE_PRESET = {
    PRESET_NONE: "none",
    PRESET_FROST: "frost",
    PRESET_ECO: "eco",
    PRESET_COMFORT: "comfort",
}
_DEVICE_TO_HA_PRESET = {v: k for k, v in _HA_TO_DEVICE_PRESET.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home thermostats from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_climates() -> None:
        """Add entities for any thermostats not yet created."""
        new_entities: list[JungHomeClimate] = []
        for device in coordinator.data or []:
            if device.get("type") != "Thermostat":
                continue
            ctrl_dp = next(
                (
                    dp
                    for dp in device.get("datapoints", [])
                    if dp.get("type") == "temperature_ctrl"
                ),
                None,
            )
            if ctrl_dp is None:
                continue
            uid = stable_unique_id(device, ctrl_dp)
            if uid in known:
                continue
            known.add(uid)
            new_entities.append(JungHomeClimate(coordinator, device, ctrl_dp))
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_climates()
    entry.async_on_unload(coordinator.async_add_listener(_discover_climates))


class JungHomeClimate(JungHomeEntity, ClimateEntity):
    """Representation of a Jung Home thermostat."""

    _attr_name = None
    # Drives attribute translations (the custom `frost` preset has no HA core
    # string). With _attr_name = None the entity still adopts the device name.
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = DEFAULT_MIN_TEMP
    _attr_max_temp = DEFAULT_MAX_TEMP
    _attr_preset_modes = [PRESET_NONE, PRESET_FROST, PRESET_ECO, PRESET_COMFORT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        ctrl_datapoint: Datapoint,
    ) -> None:
        """Initialize the thermostat."""
        super().__init__(coordinator, device)
        self._datapoint = ctrl_datapoint
        self._ctrl_datapoint_id = ctrl_datapoint["id"]
        self._name = device.get("label", "Jung Thermostat")
        self._attr_unique_id = stable_unique_id(device, ctrl_datapoint)
        # The Thermostat function carries a `switch` datapoint for on/off; map it
        # to HVAC OFF/HEAT. A thermostat without one (defensive) stays HEAT-only,
        # and HA requires a non-empty hvac_modes list either way.
        switch_dp = next(
            (dp for dp in device.get("datapoints", []) if dp.get("type") == "switch"),
            None,
        )
        self._switch_datapoint_id = switch_dp.get("id") if switch_dp else None
        self._attr_hvac_modes = (
            [HVACMode.OFF, HVACMode.HEAT]
            if self._switch_datapoint_id is not None
            else [HVACMode.HEAT]
        )
        self._attr_hvac_mode = self._get_hvac_mode_from_datapoint(switch_dp)
        self._target_temperature = self._get_target_from_datapoint(ctrl_datapoint)
        self._preset_mode = self._get_preset_from_datapoint(ctrl_datapoint)
        self._current_temperature = self._get_current_temperature(device)

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        return self._target_temperature

    @property
    def current_temperature(self) -> float | None:
        """Return the current ambient temperature, if the device reports one."""
        return self._current_temperature

    @property
    def preset_mode(self) -> str | None:
        """Return the active preset."""
        return self._preset_mode

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.set_temperature(self._ctrl_datapoint_id, temperature)
        self._target_temperature = temperature
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set a new preset."""
        device_preset = _HA_TO_DEVICE_PRESET.get(preset_mode)
        if device_preset is None:
            _LOGGER.warning("Unknown thermostat preset %r", preset_mode)
            return
        await self.coordinator.set_temperature_preset(
            self._ctrl_datapoint_id, device_preset
        )
        self._preset_mode = preset_mode
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn the thermostat on (HEAT) or off via its switch datapoint."""
        if self._switch_datapoint_id is None:
            return  # no on/off control on this thermostat
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.turn_off_switch(self._switch_datapoint_id)
        else:
            await self.coordinator.turn_on_switch(self._switch_datapoint_id)
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        device = self._current_device()
        if device is None:
            self.async_write_ha_state()
            return
        # Refresh each attribute only from its own datapoint's push (see
        # JungHomeEntity._should_refresh) so a switch echo can't clobber an
        # optimistic target/preset write, and vice versa.
        if self._should_refresh(self._ctrl_datapoint_id):
            ctrl_dp = self._find_datapoint(self._ctrl_datapoint_id)
            if ctrl_dp:
                self._target_temperature = self._get_target_from_datapoint(ctrl_dp)
                self._preset_mode = self._get_preset_from_datapoint(ctrl_dp)
        if self._switch_datapoint_id is not None and self._should_refresh(
            self._switch_datapoint_id
        ):
            self._attr_hvac_mode = self._get_hvac_mode_from_datapoint(
                self._find_datapoint(self._switch_datapoint_id)
            )
        # The ambient reading is read-only (never set optimistically), so always
        # refresh it from the latest data.
        self._current_temperature = self._get_current_temperature(device)
        self.async_write_ha_state()

    def _get_hvac_mode_from_datapoint(self, datapoint: Datapoint | None) -> HVACMode:
        """Map the switch datapoint to an HVAC mode (OFF only when explicitly off)."""
        if datapoint is not None and datapoint_value(datapoint, "switch") == "0":
            return HVACMode.OFF
        return HVACMode.HEAT

    def _get_target_from_datapoint(self, datapoint: Datapoint | None) -> float | None:
        value = datapoint_value(datapoint, "temperature_ctrl")
        if value is None:
            return None
        try:
            target = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(target):
            return None
        # Clamp to the advertised range so an out-of-range gateway value doesn't
        # surface a target outside the thermostat's declared min/max.
        return max(DEFAULT_MIN_TEMP, min(DEFAULT_MAX_TEMP, target))

    def _get_preset_from_datapoint(self, datapoint: Datapoint | None) -> str | None:
        value = datapoint_value(datapoint, "temperature_ctrl_preset")
        if value is None:
            return None
        return _DEVICE_TO_HA_PRESET.get(value)

    def _get_current_temperature(self, device: Device) -> float | None:
        """Read an ambient temperature from a sibling °C quantity datapoint, if any."""
        for dp in device.get("datapoints", []):
            if dp.get("type") != "quantity":
                continue
            unit = (datapoint_value(dp, "quantity_unit") or "").strip().lower()
            if unit not in ("°c", "c"):
                continue
            value = datapoint_value(dp, "quantity")
            if value is None:
                continue
            try:
                ambient = float(value)
            except (TypeError, ValueError):
                # A malformed reading shouldn't abort the search for a usable
                # sibling; keep looking rather than returning None outright.
                continue
            if math.isfinite(ambient):
                return ambient
        return None
