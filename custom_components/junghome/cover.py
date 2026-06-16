"""Cover platform for Jung Home (blinds / shutters).

Two function types map here:

- ``Position`` — a ``level`` datapoint (position only).
- ``PositionAndAngle`` — a ``level`` datapoint plus an ``angle`` datapoint
  (slat tilt).

## Position convention (hardware-verifiable assumption)

The gateway's ``level`` datapoint is a 0-100 ``%`` value (Generic Level model).
The dump used to build this integration was factory-reset, so it carries no live
cover to confirm direction, and the gateway docs don't state it. This platform
follows the common JUNG/European blind convention where ``level`` is **percent
closed**, so it inverts to Home Assistant's "percent open" position:

    ha_position = 100 - device_level

If a real device turns out to report percent-open instead, flip ``_to_ha`` /
``_to_device`` below (they are the single inversion point) — nothing else needs
to change. The slat ``angle`` is mapped straight through (0-100%).
"""

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import datapoint_value, stable_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Commands are cheap async WebSocket sends; don't serialise them.
PARALLEL_UPDATES = 0

# Gateway level_move tri-state (see cdb_types_datapoints.json).
_MOVE_STOP = 0


def _to_ha(device_level: int) -> int:
    """Convert a device level (% closed) to a Home Assistant position (% open).

    Clamped to 0..100: the gateway level is untrusted JSON, and an out-of-range
    value would otherwise produce an invalid HA position and break ``is_closed``.
    """
    return max(0, min(100, 100 - device_level))


def _to_device(ha_position: int) -> int:
    """Convert a Home Assistant position (% open) to a device level (% closed)."""
    return 100 - ha_position


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home covers from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_covers() -> None:
        """Add entities for any covers not yet created (handles devices added later)."""
        new_entities: list[JungHomeCover] = []
        for device in coordinator.data or []:
            if device.get("type") not in ("Position", "PositionAndAngle"):
                continue
            level_dp = next(
                (
                    dp
                    for dp in device.get("datapoints", [])
                    if dp.get("type") == "level"
                ),
                None,
            )
            if level_dp is None:
                continue
            uid = stable_unique_id(device, level_dp)
            if uid in known:
                continue
            known.add(uid)
            new_entities.append(JungHomeCover(coordinator, device, level_dp))
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    _discover_covers()
    entry.async_on_unload(coordinator.async_add_listener(_discover_covers))


class JungHomeCover(JungHomeEntity, CoverEntity):
    """Representation of a Jung Home cover (blind / shutter)."""

    # The cover is the device's main feature, so it adopts the device name
    # (entity_id `cover.<device>`).
    _attr_name = None
    _attr_device_class = CoverDeviceClass.BLIND

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        level_datapoint: Datapoint,
    ) -> None:
        """Initialize the cover."""
        super().__init__(coordinator, device)
        self._datapoint = level_datapoint
        self._level_datapoint_id = level_datapoint["id"]
        self._angle_datapoint = next(
            (dp for dp in device.get("datapoints", []) if dp.get("type") == "angle"),
            None,
        )
        self._angle_datapoint_id = (
            self._angle_datapoint.get("id") if self._angle_datapoint else None
        )
        self._name = device.get("label", "Jung Cover")
        self._attr_unique_id = stable_unique_id(device, level_datapoint)

        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if self._angle_datapoint_id is not None:
            features |= (
                CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
            )
        self._attr_supported_features = features

        self._position = self._get_position_from_datapoint(level_datapoint)
        self._tilt = self._get_tilt_from_datapoint(self._angle_datapoint)

    @property
    def current_cover_position(self) -> int | None:
        """Return the cover position (0 closed .. 100 open)."""
        return self._position

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return the slat tilt position (0 .. 100), if supported."""
        return self._tilt

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""
        if self._position is None:
            return None
        return self._position == 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover fully."""
        await self.coordinator.set_level(self._level_datapoint_id, _to_device(100))
        self._position = 100
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover fully."""
        await self.coordinator.set_level(self._level_datapoint_id, _to_device(0))
        self._position = 0
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        ha_position = int(kwargs[ATTR_POSITION])
        await self.coordinator.set_level(
            self._level_datapoint_id, _to_device(ha_position)
        )
        self._position = ha_position
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self.coordinator.move_level(self._level_datapoint_id, _MOVE_STOP)
        # A stop ends travel at an unknown position, so the optimistic
        # open/close/set_position write is now stale. Re-read the real level
        # instead of waiting up to a minute for the next REST poll.
        await self.coordinator.async_request_refresh()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt the slats fully open."""
        await self._set_tilt(100)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt the slats fully closed."""
        await self._set_tilt(0)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Set the slat tilt to a specific position."""
        await self._set_tilt(int(kwargs[ATTR_TILT_POSITION]))

    async def _set_tilt(self, tilt: int) -> None:
        if self._angle_datapoint_id is None:
            return
        # Angle is mapped straight through (no inversion) — see module docstring.
        await self.coordinator.set_angle(self._angle_datapoint_id, tilt)
        self._tilt = tilt
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        level_dp = self._find_datapoint(self._level_datapoint_id)
        if level_dp:
            self._position = self._get_position_from_datapoint(level_dp)
        if self._angle_datapoint_id is not None:
            angle_dp = self._find_datapoint(self._angle_datapoint_id)
            if angle_dp:
                self._tilt = self._get_tilt_from_datapoint(angle_dp)
        self.async_write_ha_state()

    def _get_position_from_datapoint(self, datapoint: Datapoint | None) -> int | None:
        """Extract the HA position (% open) from a level datapoint."""
        value = datapoint_value(datapoint, "level")
        if value is None:
            return None
        try:
            return _to_ha(round(float(value)))
        except (TypeError, ValueError):
            return None

    def _get_tilt_from_datapoint(self, datapoint: Datapoint | None) -> int | None:
        """Extract the tilt position (0..100) from an angle datapoint."""
        value = datapoint_value(datapoint, "angle")
        if value is None:
            return None
        try:
            # Clamp to 0..100, mirroring the position guard in _to_ha: the
            # untrusted angle must satisfy HA's tilt-position contract.
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return None
