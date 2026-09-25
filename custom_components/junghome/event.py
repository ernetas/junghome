"""Event platform for Jung Home rocker buttons.

The gateway pushes raw ``pressed``/``depressed`` edges only. Every edge is
re-fired verbatim (existing automations and device triggers rely on them),
and on top of them each entity derives the gestures the gateway knew but
discarded (docs/gateway-websocket.md, rocker section):

- ``click`` — at the release of a press that lasted less than
  ``BUTTON_HOLD_THRESHOLD``;
- ``hold_start`` — by timer, once a press has lasted the threshold without a
  release;
- ``hold_end`` — at the release of a hold. Always paired with a
  ``hold_start``: if the release was never reported (socket down, or a
  single-key element's hold, which by the gateway code leaves one side down),
  it fires on the next edge seen on that side instead.

Tap vs hold is classified on pulse width alone (taps at most 0.53 s, holds at least 2.44 s
in the labelled capture — a clean band). There is deliberately no double-click:
on current device firmware a single and a double click are indistinguishable
on the wire.

**Duplicate suppression.** Device firmware 2.2.0.x reports every tap twice,
so one tap arrives as two press/release pairs. After a click, the next press
on the same *device* (either side — on a single-key element the copy lands on
the other datapoint) within ``BUTTON_DUPLICATE_WINDOW`` is that copy: it and
its release are dropped, edges included. A dropped press that is still down
at the hold threshold was not a copy after all (copies are ~0.4 s pulses) but
a real hold following a quick tap, so it is reinstated then: its ``pressed``
edge fires late, followed by ``hold_start``. The option
``CONF_SUPPRESS_DUPLICATE_PRESSES`` (default on) switches suppression off for
older device firmware that reports each tap once.

A **hold** on a single-key element can be copied too, and the copy lands on the
*other* side while the first is still down (the gateway toggles the side on
every reception); the finger's release then arrives on the copy's side and
the first side is never released. A press on the other side of a device whose
one side has been down for longer than a tap pulse is that copy: it is
dropped, and its release completes the hold on the side that is down
(``BUTTON_HOLD_COPY_AFTER`` / ``BUTTON_HOLD_COPY_WINDOW``).
"""

import logging
from datetime import datetime

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import CONF_DEVICE_ID, CONF_TYPE, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    BUTTON_DATAPOINT_TYPES,
    BUTTON_DUPLICATE_WINDOW,
    BUTTON_EVENT_TYPES,
    BUTTON_HOLD_COPY_AFTER,
    BUTTON_HOLD_COPY_WINDOW,
    BUTTON_HOLD_THRESHOLD,
    CONF_SUBTYPE,
    CONF_SUPPRESS_DUPLICATE_PRESSES,
    DEFAULT_SUPPRESS_DUPLICATE_PRESSES,
    EVENT_BUTTON_ACTION,
    datapoint_bool,
    stable_unique_id,
)
from .coordinator import JungHomeConfigEntry, JungHomeDataUpdateCoordinator
from .entity import JungHomeEntity, claim_new_entity, entry_unloading
from .models import Datapoint, Device

_LOGGER = logging.getLogger(__name__)

# Read-only platform; no update serialisation needed.
PARALLEL_UPDATES = 0

# Translation keys per rocker datapoint type. With `_attr_has_entity_name`, HA
# prepends the device name; the entity name itself comes from the
# `entity.event.*` translations (strings.json), so it's localisable rather than
# hardcoded. Shared with `device_trigger` (see BUTTON_DATAPOINT_TYPES) so a
# button side is named the same in both surfaces.
_EVENT_TRANSLATION_KEYS = BUTTON_DATAPOINT_TYPES


class ButtonGestureTracker:
    """Duplicate-press state shared by every event entity of one button element.

    One instance per gateway device (a ``RockerSwitch`` function = one mesh
    button element), handed to both of its ``up``/``down`` entities. The
    duplicate copy of a tap lands on the same datapoint on a rocker half but on
    the *other* datapoint on a single-key element, so the window has to be
    per device, not per entity — which is the only reason this is not entity
    state.
    """

    def __init__(self, *, suppress_duplicates: bool) -> None:
        """Initialise with the entry's suppression option."""
        self.suppress_duplicates = suppress_duplicates
        # Loop time of the release that completed the last click, until the
        # next press on any side has consumed it.
        self._last_click_release: float | None = None
        # The side currently down — its entity and the loop time of its press
        # — so a press on the OTHER side can be recognised as the firmware's
        # copy of a hold (``hold_copy_target``).
        self._down: tuple[JungHomeEventEntity, float] | None = None

    def note_click(self, now: float) -> None:
        """Record the release that completed a click."""
        self._last_click_release = now

    def is_duplicate_press(self, now: float) -> bool:
        """Whether a press arriving now is the firmware's copy of the last click.

        Consumes the click either way: only the *next* press after a click is
        a candidate, so a genuine second tap after the copy is never dropped.
        """
        if not self.suppress_duplicates or self._last_click_release is None:
            return False
        since_release = now - self._last_click_release
        self._last_click_release = None
        return since_release <= BUTTON_DUPLICATE_WINDOW

    def note_press(self, entity: "JungHomeEventEntity", now: float) -> None:
        """Record that ``entity``'s side went down at ``now``."""
        self._down = (entity, now)

    def note_up(self, entity: "JungHomeEventEntity") -> None:
        """Forget ``entity``'s press, if it is the one recorded."""
        if self._down is not None and self._down[0] is entity:
            self._down = None

    def hold_copy_target(
        self, entity: "JungHomeEventEntity", now: float
    ) -> "JungHomeEventEntity | None":
        """Return the side whose hold a press on ``entity`` copies, or None.

        The gateway toggles the reported side of a single-key element on every
        reception, so the firmware's second copy of a HOLD lands on the other
        datapoint while the first side is still down, and the finger's release
        follows on the copy's side (captured 2026-09-16: press, other-side press
        +1.4 s, release there at +2.55 s, the first side never released). A
        press on the other side is that copy when this device's down side has
        been down longer than any synthesised tap pulse — a tap's own copy only
        arrives after its release, which the click window handles — and less
        than the copy window. Nothing is consumed here: the copy's release is
        what ends the hold. Off with suppression off (older firmware sends no
        copies).
        """
        if not self.suppress_duplicates or self._down is None:
            return None
        other, since = self._down
        if other is entity:
            return None
        if BUTTON_HOLD_COPY_AFTER <= now - since <= BUTTON_HOLD_COPY_WINDOW:
            return other
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JungHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Jung Home event entities from a config entry."""
    coordinator = entry.runtime_data
    known = coordinator.known_unique_ids(Platform.EVENT)
    # Read once here — an options change reloads the entry (update listener in
    # __init__), the same way cover.py reads its inverted-covers flags.
    suppress_duplicates = bool(
        entry.options.get(
            CONF_SUPPRESS_DUPLICATE_PRESSES, DEFAULT_SUPPRESS_DUPLICATE_PRESSES
        )
    )
    # Keyed by the gateway's device id — runtime-only state, never identity
    # (see the stable-identity rules in CLAUDE.md), and the two datapoints of
    # one device may be discovered on different passes.
    trackers: dict[str, ButtonGestureTracker] = {}

    @callback
    def _discover_events() -> None:
        """Add entities for any events not yet created (handles devices added later)."""
        if entry_unloading(entry):
            return
        new_entities = []
        for device in coordinator.data or []:
            if device.get("type") == "RockerSwitch":
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("type") in {
                        "down_request",
                        "up_request",
                        "trigger_request",
                    }:
                        uid = stable_unique_id(device, datapoint, "event")
                        if not claim_new_entity(known, uid):
                            continue
                        # A button the gateway reports as running firmware
                        # older than the doubling one (verbose device
                        # endpoint) reports each tap once: no copies to drop,
                        # only fast double-taps to lose. Unknown stays on.
                        tracker = trackers.setdefault(
                            str(device.get("id")),
                            ButtonGestureTracker(
                                suppress_duplicates=suppress_duplicates
                                and not coordinator.button_reports_each_tap_once(device)
                            ),
                        )
                        new_entities.append(
                            JungHomeEventEntity(coordinator, device, datapoint, tracker)
                        )
        if new_entities:
            async_add_entities(new_entities)

    _discover_events()
    entry.async_on_unload(coordinator.async_add_listener(_discover_events))


# ------------------------------------------
# 🔹 EVENT ENTITY (For UI Integration)
# ------------------------------------------
class JungHomeEventEntity(JungHomeEntity, EventEntity):
    """Event entity for Jung Home button presses."""

    _attr_event_types = list(BUTTON_EVENT_TYPES)
    _attr_device_class = EventDeviceClass.BUTTON

    # Edges only ever arrive as WebSocket pushes (``_handle_coordinator_update``
    # fires on the per-push marker; a REST poll re-reads the same values and
    # fires nothing), so with the socket down this entity is deaf — a press is
    # lost, not delayed. Reading unavailable says so, and is what lets an
    # automation abort a gesture mid-way (the shipped blueprint's
    # abort-on-unavailable guard) instead of waiting on a release that will
    # never be reported. Without it the entity looked live on the REST poll
    # alone and that guard could never engage.
    _needs_websocket = True

    def __init__(
        self,
        coordinator: JungHomeDataUpdateCoordinator,
        device: Device,
        datapoint: Datapoint,
        tracker: ButtonGestureTracker,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, device)
        self._datapoint = datapoint
        dp_type = datapoint.get("type", "Unknown")
        translation_key = _EVENT_TRANSLATION_KEYS.get(dp_type)
        if translation_key:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = dp_type
        self._attr_unique_id = stable_unique_id(device, datapoint, "event")
        # Icon comes from icons.json (icon-translations).
        self._tracker = tracker
        # Gesture state for the press currently down on this datapoint: whether
        # one is pending at all, the pending hold timer's cancel handle, and
        # whether it was dropped as a duplicate copy (its edges withheld).
        # ``_holding`` is "a ``hold_start`` fired and its ``hold_end`` is still
        # owed" — it outlives the press on purpose, so a hold whose release
        # was lost is closed by the next edge on this side.
        self._press_pending = False
        self._cancel_hold_timer: CALLBACK_TYPE | None = None
        self._suppressed = False
        self._holding = False
        # Set while this side's press was taken as the firmware's copy of a
        # hold on the other side: its release then completes that hold.
        self._copy_of: JungHomeEventEntity | None = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending hold timer so it cannot fire on a removed entity."""
        self._stop_hold_timer()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire events when this datapoint is pushed over the WebSocket.

        Press detection keys off the coordinator's per-push marker rather than
        diffing snapshots. The gateway broadcasts a ``datapoint`` frame on every
        genuine press/release edge, whereas REST polls (and the full-list resync
        frames) re-read the same values without setting the marker. So every real
        edge fires exactly once — including rapid same-value taps that a level
        diff would coalesce — and a re-read never fires a phantom press.
        """
        # Another device's push cannot be an edge on this button, and the write
        # below would only re-publish the identical state (skipped while the
        # entity is already shown available — see the base helper). Pushes for
        # THIS device (its own edges, or its status LED) fall through.
        if self._skip_foreign_device_push():
            return
        emitted = False
        if not self.available:
            # Deaf from here on (socket down, or the poll failed): the release
            # of a press in flight will never be seen, so no gesture can be
            # completed for it — and a hold timer left running would fire
            # ``hold_start`` for a tap whose release was simply lost.
            self._abandon_press()
        # Fire only on a genuine WebSocket push for THIS datapoint. REST re-reads
        # (marker is None) and pushes for sibling datapoints skip the fire but
        # still write state below, so availability tracks the gateway connection
        # without ever emitting a phantom press.
        elif self.coordinator.pushed_datapoint_id == self._datapoint["id"]:
            datapoint = self._find_datapoint(self._datapoint["id"])
            pressed = self._get_state_from_datapoint(datapoint)
            if pressed is not None:
                emitted = self._on_press() if pressed else self._on_release()
        # Every emitted event already wrote its own state (see ``_emit``).
        if not emitted:
            self.async_write_ha_state()

    @callback
    def _on_press(self) -> bool:
        """Handle a ``pressed`` edge; return whether anything was emitted.

        Starts the measurement, unless the press is the firmware's duplicate
        copy of the click just completed. A press while this side is already
        down restarts the measurement — the earlier press's release was never
        reported (a single-key element's hold, by the gateway code, leaves one
        side down for good) — after closing an open hold, so that
        ``hold_start``/``hold_end`` stay paired.
        """
        now = self.hass.loop.time()
        emitted = self._end_open_hold()
        if (target := self._tracker.hold_copy_target(self, now)) is not None:
            # The other side is mid-hold: this press is the firmware's copy
            # of it. Withheld, edges included; its release ends that hold.
            _LOGGER.debug(
                "Dropping press on %s: the firmware's copy of the hold on %s",
                self.entity_id,
                target.entity_id,
            )
            self._copy_of = target
            return emitted
        if self._press_pending:
            _LOGGER.debug(
                "%s pressed while already down; restarting the gesture",
                self.entity_id,
            )
            self._stop_hold_timer()
        # A genuine press supersedes a copy marker left over from a hold the
        # other side finished itself (its release can land there after all:
        # the gateway's side toggle is shared by every button, so an unrelated
        # key pressed mid-hold flips it back). Stale, it would route THIS
        # press's release to the other side and lose the click.
        self._copy_of = None
        self._press_pending = True
        self._tracker.note_press(self, now)
        self._suppressed = self._tracker.is_duplicate_press(now)
        if self._suppressed:
            _LOGGER.debug(
                "Dropping duplicate press on %s (firmware copy of the last click)",
                self.entity_id,
            )
        else:
            self._emit("pressed")
            emitted = True
        # Armed for a dropped press too: if it is still down at the threshold
        # it was a real hold after a quick tap, not a ~0.4 s copy.
        self._cancel_hold_timer = async_call_later(
            self.hass, BUTTON_HOLD_THRESHOLD, self._on_hold_threshold
        )
        return emitted

    @callback
    def _on_release(self) -> bool:
        """Handle a ``depressed`` edge; return whether anything was emitted.

        Completes the pending press as a click or a hold. Without a pending
        press the edge is still re-fired (the gateway re-sends a value on a
        mode-only change), and it closes a hold whose press was abandoned
        while the entity was unavailable. A release on a side whose press was
        the copy of the other side's hold is the finger's release of THAT
        hold, and completes it there; nothing fires on this side.
        """
        now = self.hass.loop.time()
        if (target := self._copy_of) is not None:
            self._copy_of = None
            _LOGGER.debug(
                "Release on %s ends the hold on %s (the copy's release is the "
                "finger's)",
                self.entity_id,
                target.entity_id,
            )
            return target.complete_copied_hold(now)
        return self._complete_release(now)

    @callback
    def complete_copied_hold(self, now: float) -> bool:
        """Finish this side's press with its copy's release; nothing if already over.

        The held side may have been released on its own first (see
        ``_on_press``); the copy's release is then the copy's alone and is
        dropped whole, like its press — re-firing a ``depressed`` here would
        report an edge the gateway never sent for this side.
        """
        if not self._press_pending:
            return False
        return self._complete_release(now)

    @callback
    def _complete_release(self, now: float) -> bool:
        """Finish this side's press at ``now``; return whether anything was emitted."""
        self._stop_hold_timer()
        suppressed, pending = self._suppressed, self._press_pending
        self._press_pending = self._suppressed = False
        self._tracker.note_up(self)
        if suppressed:
            _LOGGER.debug(
                "Dropping the duplicate press's release on %s", self.entity_id
            )
            return False
        self._emit("depressed")
        if not self._end_open_hold() and pending:
            self._emit("click")
            self._tracker.note_click(now)
        return True

    @callback
    def _on_hold_threshold(self, _now: datetime) -> None:
        """Classify the press still down at the hold threshold as a hold."""
        self._cancel_hold_timer = None
        if not self.available:
            # The dispatch that flips availability abandons the press already;
            # this only covers the timer landing first on the same loop turn.
            self._abandon_press()
            return
        if self._suppressed:
            # Reinstated: the copy of a click releases within ~0.5 s, so a
            # press still down now is a genuine hold that followed a quick tap.
            _LOGGER.debug(
                "Press on %s outlasted the duplicate window; treating as a hold",
                self.entity_id,
            )
            self._suppressed = False
            self._emit("pressed")
        self._holding = True
        self._emit("hold_start")

    @callback
    def _end_open_hold(self) -> bool:
        """Fire the ``hold_end`` owed for an open hold; return whether one was."""
        if not self._holding:
            return False
        self._holding = False
        self._emit("hold_end")
        return True

    @callback
    def _abandon_press(self) -> None:
        """Forget a press in flight without emitting anything for it."""
        self._copy_of = None
        if not self._press_pending:
            return
        _LOGGER.debug(
            "%s unavailable mid-press; abandoning the gesture", self.entity_id
        )
        self._stop_hold_timer()
        self._press_pending = self._suppressed = False
        self._tracker.note_up(self)

    @callback
    def _stop_hold_timer(self) -> None:
        """Cancel the pending hold timer, if any."""
        if self._cancel_hold_timer is not None:
            self._cancel_hold_timer()
            self._cancel_hold_timer = None

    @callback
    def _emit(self, event_type: str) -> None:
        """Fire one event on the entity and on the bus, and publish the state.

        Each event gets its own state write: an event entity's state is the
        last event, so two events from one edge (``depressed`` + ``click``)
        written together would hide the first from state-change triggers.
        """
        _LOGGER.debug("Triggering %s event for %s", event_type, self.entity_id)
        self._trigger_event(event_type)
        self._fire_bus_event(event_type)
        self.async_write_ha_state()

    @callback
    def _fire_bus_event(self, event_type: str) -> None:
        """Re-emit this event on the Home Assistant bus for device triggers.

        Device triggers can only attach to a bus event, not to an entity, so the
        event is published a second time here (this mirrors how HA's own button
        integrations do it). Skipped for a datapoint type with no button side, and
        when the entity is not yet in the device registry — a device trigger is
        keyed on the device id, so an event without one would match nothing.
        """
        button_type = _EVENT_TRANSLATION_KEYS.get(self._datapoint.get("type", ""))
        device_entry = self.device_entry
        if button_type is None or device_entry is None:
            return
        self.hass.bus.async_fire(
            EVENT_BUTTON_ACTION,
            {
                CONF_DEVICE_ID: device_entry.id,
                CONF_TYPE: button_type,
                CONF_SUBTYPE: event_type,
                "entity_id": self.entity_id,
                "device_name": device_entry.name_by_user or device_entry.name,
            },
        )

    def _get_state_from_datapoint(self, datapoint: Datapoint | None) -> bool | None:
        """Extract the edge from datapoint values. True if pressed.

        Scoped to this datapoint's own type so bundled request keys don't merge.
        ``None`` when the datapoint is missing or the gateway reports ``"NaN"``
        — it gave up reading the button after three failed requests, which is
        not an edge. Firing ``depressed`` for it would be a phantom release,
        and these states are ``POLL_ONCE``: a button that never answers is
        re-polled every cycle, so it would repeat indefinitely.
        """
        return datapoint_bool(datapoint, self._datapoint.get("type", ""))
