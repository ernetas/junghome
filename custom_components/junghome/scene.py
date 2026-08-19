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
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

# scene_slug/scene_unique_id live in const.py so the coordinator can resolve a
# recalled scene's entity without importing this platform module.
from .const import DOMAIN, scene_unique_id
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Recalls are cheap async REST posts; don't serialise them.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home scenes from a config entry."""
    coordinator = entry.runtime_data
    entities: dict[str, JungHomeScene] = {}
    # unique_ids with an in-flight async_remove(). Kept out of `entities` only
    # once removal completes (see _remove_scene), so a same-tick re-add can't
    # construct a duplicate entity with the same unique_id while the old one is
    # still deregistering.
    removing: set[str] = set()
    # Colliding uids already warned about. _discover_scenes runs on EVERY
    # coordinator notification (each push and each poll), so without this a
    # persisting label collision warned several times a minute, indefinitely
    # (same once-guard as the capability watcher's `warned_collisions`).
    warned_collisions: set[str] = set()

    async def _remove_scene(uid: str) -> None:
        try:
            await entities[uid].async_remove()
        finally:
            entities.pop(uid, None)
            removing.discard(uid)
        # The scene may have reappeared during removal; re-run discovery so it is
        # re-added rather than lost.
        _discover_scenes()

    @callback
    def _discover_scenes() -> None:
        """Add scenes that appeared and remove scenes the gateway deleted.

        Unlike the device platforms (which prune whole devices in __init__),
        scenes have no backing device, so they are added and removed here as the
        gateway's ``scenes`` / ``scenes-deleted`` broadcasts change the list.
        """
        current: dict[str, str] = {}  # unique_id -> label
        for scene in coordinator.scenes or []:
            label = scene.get("label")
            if label:
                uid = scene_unique_id(entry, label)
                # Two scenes whose labels slug identically collide on one uid, so
                # only one entity can exist (same accepted limitation as devices —
                # see const.device_slug). Surface the dropped one rather than
                # letting it vanish silently — once, not on every refresh.
                if (
                    uid in current
                    and current[uid] != label
                    and uid not in warned_collisions
                ):
                    warned_collisions.add(uid)
                    _LOGGER.warning(
                        "Jung Home scenes %r and %r map to the same id %r; "
                        "only one scene entity will be created",
                        current[uid],
                        label,
                        uid,
                    )
                current[uid] = label

        new_entities: list[JungHomeScene] = []
        for uid, label in current.items():
            # Skip uids still deregistering — re-adding now would collide on the
            # unique_id; _remove_scene re-runs discovery once removal completes.
            if uid not in entities and uid not in removing:
                entity = JungHomeScene(coordinator, label, uid)
                entities[uid] = entity
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

        # Drop entities whose scene no longer exists, so a deleted scene doesn't
        # linger as an always-failing entity. The removal is an entry-scoped
        # background task (cancelled on unload, exceptions tracked).
        for uid in list(entities):
            if uid not in current and uid not in removing:
                removing.add(uid)
                entry.async_create_background_task(
                    hass, _remove_scene(uid), name=f"junghome_scene_remove_{uid}"
                )

    _discover_scenes()
    entry.async_on_unload(coordinator.async_add_listener(_discover_scenes))


class JungHomeScene(CoordinatorEntity[JungHomeDataUpdateCoordinator], SceneEntity):
    """Representation of a Jung Home scene."""

    # Scenes have no backing JUNG device, so (like Home Assistant's own scene
    # platform) the scene's own label is the entity name rather than a
    # device-prefixed one. This is why the scene entity does not use
    # JungHomeEntity and sets has_entity_name = False with an explicit name.
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
        """Return if the gateway is reachable.

        Keyed off ``last_update_success`` only (the REST poll / push signal), not
        ``ws_connected`` — a stale-True socket flag must not keep a scene
        "available" after the gateway has gone unreachable (see issue #120 and
        the note on ``JungHomeEntity.available``).
        """
        return self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state, skipping the pushes that cannot possibly change it.

        A scene's state is its last activation timestamp and its availability
        is ``last_update_success``; neither is a function of any device's
        datapoints, so a per-datapoint push can only re-publish the identical
        state. Scenes are not ``JungHomeEntity`` (no backing device), so they
        do not inherit ``_skip_foreign_device_push`` — and the cost scales with
        the user's scene count: on a socket pushing once a second, every scene
        took a write per frame.

        Same guard as the base helper: never skipped while the entity is shown
        unavailable (or not yet written), because a push proves the gateway
        alive and must be what flips a post-failed-poll scene back.
        """
        if self.coordinator.pushed_datapoint_id is not None:
            state = self.hass.states.get(self.entity_id)
            if state is not None and state.state != STATE_UNAVAILABLE:
                return
        super()._handle_coordinator_update()

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
