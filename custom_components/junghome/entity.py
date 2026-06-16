"""Shared base entity for Jung Home device platforms.

Every device-backed platform (light, switch, sensor, event, cover, climate)
repeated the same ``device_info``, ``available`` and coordinator-data lookups.
This base centralises them. The scene platform is intentionally *not* based on
it — scenes have no backing device.

Behaviour preserved exactly: ``device_info`` produces the same dict as before,
``available`` keys off the same ``ws_connected or last_update_success`` signal,
and the lookup helpers return the same objects the inline ``next(...)`` calls
did. Subclasses keep their own ``unique_id``/naming and their own
``_handle_coordinator_update`` write logic (which intentionally differs between
platforms).
"""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_slug
from .coordinator import JungHomeDataUpdateCoordinator
from .models import Datapoint, Device


class JungHomeEntity(CoordinatorEntity[JungHomeDataUpdateCoordinator]):
    """Base for entities backed by a Jung Home device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
    ) -> None:
        """Initialise with the coordinator and the device this entity belongs to."""
        super().__init__(coordinator)
        self._device = device

    @property
    def available(self) -> bool:
        """Return if the device is available.

        A live WebSocket link means the gateway is reachable, so it is the
        primary availability signal; fall back to the last REST poll result
        until the socket first connects (or while it is reconnecting). Keeping
        this on the base means every entity on a device goes (un)available
        together rather than diverging on a transient poll miss.
        """
        return self.coordinator.ws_connected or self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information, linking the entity to its Jung Home device."""
        return {
            "identifiers": {(DOMAIN, device_slug(self._device))},
            "name": self._device.get("label", "Jung Device"),
            "manufacturer": "Jung",
            "model": self._device.get("type", "Unknown Model"),
            "sw_version": self._device.get("sw_version")
            or self.coordinator.gateway_version
            or "Unknown Version",
        }

    def _current_device(self) -> Device | None:
        """Return this entity's device from the latest coordinator data."""
        return next(
            (
                d
                for d in self.coordinator.data or []
                if d.get("id") == self._device["id"]
            ),
            None,
        )

    def _find_datapoint(self, datapoint_id: str) -> Datapoint | None:
        """Return a datapoint by id from this entity's current device data."""
        device = self._current_device()
        if device is None:
            return None
        return next(
            (dp for dp in device.get("datapoints", []) if dp.get("id") == datapoint_id),
            None,
        )
