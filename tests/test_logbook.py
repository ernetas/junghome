"""Logbook description tests for Jung Home."""

from homeassistant.core import Event, HomeAssistant

from custom_components.junghome.const import DOMAIN, EVENT_SCENE_RECALLED
from custom_components.junghome.logbook import async_describe_events


def test_scene_recalled_describes_with_label(hass: HomeAssistant) -> None:
    """The registered event/describer pair matches junghome_scene_recalled."""
    registered: list[tuple[str, str]] = []
    describe = None

    def _capture(domain: str, event_type: str, describe_event) -> None:
        nonlocal describe
        registered.append((domain, event_type))
        describe = describe_event

    async_describe_events(hass, _capture)
    assert registered == [(DOMAIN, EVENT_SCENE_RECALLED)]

    event = Event(EVENT_SCENE_RECALLED, {"scene_id": "s1", "label": "Movie Night"})
    assert describe is not None
    entry = describe(event)
    assert entry["name"] == "Movie Night"
    assert entry["message"] == "was recalled"


def test_scene_recalled_falls_back_to_scene_id(hass: HomeAssistant) -> None:
    """Without a label, the entry falls back to the scene id."""
    describe = None

    def _capture(domain: str, event_type: str, describe_event) -> None:
        nonlocal describe
        describe = describe_event

    async_describe_events(hass, _capture)
    event = Event(EVENT_SCENE_RECALLED, {"scene_id": "s1", "label": None})
    assert describe is not None
    assert describe(event)["name"] == "s1"


def test_scene_recalled_falls_back_without_any_identity(hass: HomeAssistant) -> None:
    """With neither label nor scene id, the entry still has a valid name."""
    describe = None

    def _capture(domain: str, event_type: str, describe_event) -> None:
        nonlocal describe
        describe = describe_event

    async_describe_events(hass, _capture)
    event = Event(EVENT_SCENE_RECALLED, {})
    assert describe is not None
    assert describe(event)["name"] == "Scene"
