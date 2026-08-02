"""Describe Jung Home logbook events."""

from collections.abc import Callable

from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_SCENE_RECALLED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""

    @callback
    def async_describe_scene_recalled_event(event: Event) -> dict[str, str]:
        """Describe a junghome_scene_recalled logbook event."""
        label = event.data.get("label") or event.data.get("scene_id")
        entry = {
            LOGBOOK_ENTRY_NAME: str(label) if label is not None else "Scene",
            LOGBOOK_ENTRY_MESSAGE: "was recalled",
        }
        # Link the line to the scene entity when the coordinator resolved one,
        # so it is clickable/filterable in the logbook UI.
        entity_id = event.data.get("entity_id")
        if entity_id:
            entry[LOGBOOK_ENTRY_ENTITY_ID] = str(entity_id)
        return entry

    async_describe_event(
        DOMAIN, EVENT_SCENE_RECALLED, async_describe_scene_recalled_event
    )
