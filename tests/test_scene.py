"""Scene platform tests for Jung Home."""

from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.scene import JungHomeScene, _scene_slug
from tests.conftest import _fake_run_websocket


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


async def test_all_scene_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every scene entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``_scene_slug``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes the platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.SCENE)
    # Scenes have no backing device, so unlike every other platform they don't
    # come from the polled device list: they only exist once the gateway has
    # broadcast its scene list. Push one before snapshotting.
    entry.runtime_data._handle_websocket_message(
        {
            "type": "scenes",
            "data": [
                {"id": "idscene1", "label": "Movie Night"},
                {"id": "idscene2", "label": "Good Morning"},
            ],
        }
    )
    await hass.async_block_till_done()
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.real_scenes_fetch
async def test_scenes_exist_without_the_websocket(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Scenes come up on the REST fetch alone.

    They previously arrived only in the WebSocket handshake, which connects
    *after* the platforms are set up — so scene entities did not exist at the end
    of setup, and never appeared at all on a gateway whose WebSocket could not
    connect, even though every other platform keeps working on the REST poll.
    """
    aioclient_mock.get(
        "https://1.2.3.4/api/junghome/scenes/",
        json=[{"id": "id0001", "label": "Movie night"}],
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)

    async def _never_connects(self) -> None:
        raise aiohttp.ClientError("no websocket here")

    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(JungHomeDataUpdateCoordinator, "_run_websocket", _never_connects),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("scene.movie_night") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.real_scenes_fetch
async def test_scene_fetch_failure_is_not_fatal(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A scene list is not worth failing setup over."""
    aioclient_mock.get("https://1.2.3.4/api/junghome/scenes/", status=500)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.scenes == []
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_revoked_token_on_recall_starts_reauth(hass: HomeAssistant) -> None:
    """A 401 on scene recall is permanent, so it must drive reauth.

    It used to be reported as `cannot_send` — "the gateway is reconnecting, try
    again in a moment" — leaving the user retrying a scene forever with nothing
    prompting them to re-authenticate.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="h", data={CONF_HOST: "h", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )

    class _Denied:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def raise_for_status(self):
            raise aiohttp.ClientResponseError(Mock(), (), status=401)

    session = Mock()
    session.post = Mock(return_value=_Denied())
    with (
        patch(
            "custom_components.junghome.coordinator.async_get_clientsession",
            return_value=session,
        ),
        patch.object(entry, "async_start_reauth") as start_reauth,
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.activate_scene("id0001")
    start_reauth.assert_called_once()
