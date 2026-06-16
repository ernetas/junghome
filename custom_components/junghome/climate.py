"""Climate platform for Jung Home (Thermostat / room temperature regulator).

A ``Thermostat`` function exposes a ``temperature_ctrl`` datapoint with two
keys (see ``cdb_types_datapoints.json``):

- ``temperature_ctrl`` — the target temperature in °C (range 5..30).
- ``temperature_ctrl_preset`` — ``none`` / ``frost`` / ``eco`` / ``comfort``.

The function carries no ambient reading, so ``current_temperature`` is only
populated if the device also exposes a temperature ``quantity`` datapoint;
otherwise it stays ``None`` (the measured value, if any, shows up as a separate
sensor entity).
"""

import logging
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
    # The RTR regulates heating; there is no gateway "off" for the function, so a
    # single HEAT mode is exposed (HA requires a non-empty hvac_modes list).
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
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
        self._unique_id = stable_unique_id(device, ctrl_datapoint)
        self._target_temperature = self._get_target_from_datapoint(ctrl_datapoint)
        self._preset_mode = self._get_preset_from_datapoint(ctrl_datapoint)
        self._current_temperature = self._get_current_temperature(device)

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the thermostat."""
        return self._unique_id

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
        """Accept the single supported HVAC mode (no-op)."""
        # Only HEAT is exposed; nothing to send to the gateway.

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        device = self._current_device()
        if device:
            ctrl_dp = self._find_datapoint(self._ctrl_datapoint_id)
            if ctrl_dp:
                self._target_temperature = self._get_target_from_datapoint(ctrl_dp)
                self._preset_mode = self._get_preset_from_datapoint(ctrl_dp)
            self._current_temperature = self._get_current_temperature(device)
        self.async_write_ha_state()

    def _get_target_from_datapoint(self, datapoint: Datapoint | None) -> float | None:
        value = datapoint_value(datapoint, "temperature_ctrl")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
                return float(value)
            except (TypeError, ValueError):
                return None
        return None
