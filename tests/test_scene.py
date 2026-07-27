"""Scene platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.scene import JungHomeScene, _scene_slug


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


def test_scene_slug_fallback() -> None:
    """Unsluggable labels fall back to a stable 'scene' slug."""
    assert _scene_slug("") == "scene"  # empty -> falsy
    assert _scene_slug("❤") == "scene"  # slugify maps to "unknown" -> fallback
    assert _scene_slug("   ") == "scene"  # whitespace -> "unknown" -> fallback
    assert _scene_slug("Movie Night") == "movie_night"


async def test_scene_created_and_activated(
    hass: HomeAssistant, init_integration
) -> None:
    """Scenes arrive over the WebSocket and recall via REST with a re-resolved id."""
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "scenes",
            "data": [{"id": "idscene1", "label": "Movie Night"}],
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("scene.movie_night") is not None

    with patch.object(coordinator, "activate_scene", AsyncMock()) as act:
        await hass.services.async_call(
            "scene", "turn_on", {"entity_id": "scene.movie_night"}, blocking=True
        )
    assert act.call_args.args[0] == "idscene1"


async def test_scene_removed_when_deleted(
    hass: HomeAssistant, init_integration
) -> None:
    """A scene the gateway deletes has its entity removed."""
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {"type": "scenes", "data": [{"id": "s1", "label": "Movie Night"}]}
    )
    await hass.async_block_till_done()
    assert hass.states.get("scene.movie_night") is not None
    # Gateway removes it (scene list now empty) -> the live entity is removed,
    # leaving only a restored/unavailable placeholder that can't be activated.
    coordinator._handle_websocket_message({"type": "scenes", "data": []})
    await hass.async_block_till_done()
    state = hass.states.get("scene.movie_night")
    assert state is None or state.state == "unavailable"


async def test_scene_activate_raises_when_missing(hass: HomeAssistant) -> None:
    """Activating a scene absent from the coordinator raises a translated error."""
    coordinator = _bare_coordinator(hass)
    coordinator.scenes = []
    scene = JungHomeScene(coordinator, "Ghost", "ghost_scene")
    with pytest.raises(HomeAssistantError):
        await scene.async_activate()


async def test_scene_reresolves_id_after_firmware_change(
    hass: HomeAssistant, init_integration
) -> None:
    """Activation re-resolves the volatile scene id from the stable label."""
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {"type": "scenes", "data": [{"id": "old", "label": "Movie Night"}]}
    )
    await hass.async_block_till_done()
    # Firmware update regenerates the scene id under the same label.
    coordinator._handle_websocket_message(
        {"type": "scenes", "data": [{"id": "new", "label": "Movie Night"}]}
    )
    await hass.async_block_till_done()
    with patch.object(coordinator, "activate_scene", AsyncMock()) as act:
        await hass.services.async_call(
            "scene", "turn_on", {"entity_id": "scene.movie_night"}, blocking=True
        )
    assert act.call_args.args[0] == "new"
