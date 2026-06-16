"""Switch platform for Jung Home (sockets and rocker status LEDs)."""

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Commands are cheap async WebSocket sends; don't serialise them.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home switches from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_switches() -> None:
        """Add entities for any switches not yet created (handles devices added later)."""
        new_entities: list[JungHomeSocket | JungHomeSwitch] = []
        for device in coordinator.data or []:
            if device.get("type") == "Socket":
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "switch":
                        uid = stable_unique_id(device, datapoint)
                        if uid not in known:
                            known.add(uid)
                            new_entities.append(
                                JungHomeSocket(coordinator, device, datapoint)
                            )
            elif device.get("type") == "RockerSwitch":
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") == "status_led":
                        uid = stable_unique_id(device, datapoint, "switch")
                        if uid not in known:
                            known.add(uid)
                            new_entities.append(
                                JungHomeSwitch(coordinator, device, datapoint)
                            )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_switches()
    entry.async_on_unload(coordinator.async_add_listener(_discover_switches))


class JungHomeSocket(JungHomeEntity, SwitchEntity):
    """Representation of a Jung Home socket."""

    # The socket is the device's main feature, so it adopts the device name
    # (entity_id `switch.<device>`, not the old `switch.<device>_<device>`).
    _attr_name = None
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
    ) -> None:
        """Initialize the socket."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        self._name = device.get("label", "Jung Socket")
        # Firmware-stable id derived from the label, not the volatile device id.
        self._unique_id = stable_unique_id(device, datapoint)
        self._is_on = self._get_state_from_datapoint(datapoint)

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the socket."""
        return self._unique_id

    @property
    def is_on(self) -> bool | None:
        """Return the state of the socket."""
        return self._is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug("Handling coordinator update for socket %s", self._name)
        datapoint = self._find_datapoint(self._datapoint["id"])
        if datapoint:
            self._is_on = self._get_state_from_datapoint(datapoint)
            _LOGGER.debug("Updated state for socket %s: %s", self._name, self._is_on)
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the socket on."""
        _LOGGER.debug("Turning on socket %s", self._name)
        await self.coordinator.turn_on_switch(self._datapoint["id"])
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the socket off."""
        _LOGGER.debug("Turning off socket %s", self._name)
        await self.coordinator.turn_off_switch(self._datapoint["id"])
        self._is_on = False
        self.async_write_ha_state()

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool:
        """Extract the state of the socket from its datapoint."""
        return datapoint_value(datapoint, "switch") == "1"


class JungHomeSwitch(JungHomeEntity, SwitchEntity):
    """Representation of a Jung Home status LED as a switch entity."""

    # Secondary entity on the rocker device; HA prepends the device name, so the
    # entity_id becomes `switch.<device>_status_led`. The name comes from the
    # entity.switch.status_led translation.
    _attr_translation_key = "status_led"

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
    ) -> None:
        """Initialize the status LED switch."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        self._attr_unique_id = stable_unique_id(device, datapoint, "switch")
        self._attr_is_on = self._get_state_from_datapoint(datapoint)

    def _get_state_from_datapoint(self, datapoint: Datapoint) -> bool:
        return datapoint_value(datapoint, "status_led") == "1"

    @property
    def is_on(self) -> bool | None:
        """Return whether the status LED is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the status LED on."""
        _LOGGER.debug("Turning on switch %s", self._attr_unique_id)
        await self.coordinator.set_status_led(self._datapoint["id"], True)
        # Optimistic update (only reached if the command was actually sent),
        # matching the socket/light behaviour.
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the status LED off."""
        _LOGGER.debug("Turning off switch %s", self._attr_unique_id)
        await self.coordinator.set_status_led(self._datapoint["id"], False)
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating switch for %s", self._attr_unique_id)
        datapoint = self._find_datapoint(self._datapoint["id"])
        if datapoint is not None:
            self._attr_is_on = self._get_state_from_datapoint(datapoint)
        # Write unconditionally (even when the datapoint is momentarily absent) so
        # the entity's availability tracks the gateway on every coordinator update.
        self.async_write_ha_state()
