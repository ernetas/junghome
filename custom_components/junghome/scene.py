"""Scene platform for Jung Home.

Scenes are recalled via REST (``POST /scenes/{id}``) — the WebSocket ``scene``
command is not implemented on the gateway. The scene ``id`` is volatile (it is
regenerated on firmware updates, like a device id), so identity is anchored on
the stable ``label`` and the current ``id`` is re-resolved from the
coordinator's scene list at activation time.
"""

import logging
from typing import Any

from homeassistant.components.scene import Scene as SceneEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Recalls are cheap async REST posts; don't serialise them.
PARALLEL_UPDATES = 0


def _scene_slug(label: str) -> str:
    """Return a firmware-stable slug for a scene label."""
    slug = slugify(label or "")
    if slug and slug != "unknown":
        return slug
    return "scene"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home scenes from a config entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover_scenes() -> None:
        """Add entities for any scenes not yet created."""
        new_entities: list[JungHomeScene] = []
        for scene in coordinator.scenes or []:
            label = scene.get("label")
            if not label:
                continue
            uid = f"{_scene_slug(label)}_scene"
            if uid in known:
                continue
            known.add(uid)
            new_entities.append(JungHomeScene(coordinator, label, uid))
        if new_entities:
            async_add_entities(new_entities)

    _discover_scenes()
    entry.async_on_unload(coordinator.async_add_listener(_discover_scenes))


class JungHomeScene(CoordinatorEntity[JungHomeDataUpdateCoordinator], SceneEntity):
    """Representation of a Jung Home scene."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        label: str,
        unique_id: str,
    ) -> None:
        """Initialize the scene."""
        super().__init__(coordinator)
        self._label = label
        self._attr_unique_id = unique_id
        self._attr_name = label

    @property
    def available(self) -> bool:
        """Return if the gateway is reachable."""
        return self.coordinator.ws_connected or self.coordinator.last_update_success

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the scene.

        Re-resolve the volatile scene id from the label each time, so a firmware
        update that regenerated ids doesn't leave us posting to a dead id.
        """
        scene_id = next(
            (
                s.get("id")
                for s in self.coordinator.scenes or []
                if s.get("label") == self._label and s.get("id")
            ),
            None,
        )
        if scene_id is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scene_not_found",
                translation_placeholders={"label": self._label},
            )
        await self.coordinator.activate_scene(scene_id)
