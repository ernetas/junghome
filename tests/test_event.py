"""RockerSwitch event platform tests for Jung Home.

The gesture tests drive the fixture's rocker (Button A: ``idrock1-00c`` is
``up_request``, ``idrock1-00d`` is ``down_request``) with the edge sequences the
gateway actually emits (docs/gateway-websocket.md, rocker section) on a frozen
clock, and read the events back off the bus the device triggers listen to —
so every assertion covers the entity event, the bus event and the timing.
"""

import logging
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import (
    BUTTON_DUPLICATE_WINDOW,
    CONF_SUPPRESS_DUPLICATE_PRESSES,
    DOMAIN,
    EVENT_BUTTON_ACTION,
)
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.event import ButtonGestureTracker, JungHomeEventEntity
from tests.conftest import PRISTINE_DEVICES, _fake_run_websocket, bare_coordinator

UP = "idrock1-00c"
DOWN = "idrock1-00d"
_KEYS = {UP: "up_request", DOWN: "down_request"}

# The labelled capture's shape of one tap on current device firmware: the
# gateway's synthesised release ~0.4 s after the press, and the firmware's
# second copy of the whole pair 0.11-1.03 s after that release.
TAP_PULSE = 0.4
COPY_GAP = 0.5


def _push(coordinator: JungHomeDataUpdateCoordinator, dp_id: str, value: str) -> None:
    """Push one raw edge (``"1"`` press / ``"0"`` release) for a rocker side."""
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": dp_id, "values": [{"key": _KEYS[dp_id], "value": value}]},
        }
    )


class _Rocker:
    """Drive the fixture rocker on a frozen clock."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        coordinator: JungHomeDataUpdateCoordinator,
    ) -> None:
        self.hass, self.freezer, self.coordinator = hass, freezer, coordinator

    async def advance(self, seconds: float) -> None:
        """Move the frozen clock on and run the timers that came due."""
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()

    async def edge(self, dp_id: str, value: str, *, after: float = 0) -> None:
        """Advance the clock by ``after`` seconds, then push one edge."""
        if after:
            await self.advance(after)
        _push(self.coordinator, dp_id, value)
        await self.hass.async_block_till_done()

    async def edge_between_ticks(self, dp_id: str, value: str, *, after: float) -> None:
        """Move the clock ``after`` seconds and push one edge WITHOUT running timers.

        ``async_fire_time_changed`` fires a timer once the frozen clock has
        moved (since the freeze) by at least the timer's remaining time, so an
        intermediate tick past ~0.5 s runs the 1 s hold timer early. An edge
        that must land strictly before the threshold ticks the clock only;
        the next ``advance`` evaluates the timers.
        """
        self.freezer.tick(timedelta(seconds=after))
        _push(self.coordinator, dp_id, value)
        await self.hass.async_block_till_done()

    async def tap(self, first: str = UP, copy: str = UP) -> None:
        """One physical tap as current firmware reports it: two pairs.

        ``copy`` is the side the second pair lands on — the same side on a
        rocker half, the other side on a single-key element.
        """
        await self.edge(first, "1")
        await self.edge(first, "0", after=TAP_PULSE)
        await self.edge(copy, "1", after=COPY_GAP)
        await self.edge(copy, "0", after=TAP_PULSE)


@pytest.fixture
def rocker(
    hass: HomeAssistant, init_integration, freezer: FrozenDateTimeFactory
) -> _Rocker:
    """The fixture rocker of a freshly set-up integration, on a frozen clock."""
    return _Rocker(hass, freezer, init_integration.runtime_data)


@pytest.fixture
def bus_events(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Collect every button bus event as ``(side, event_type)``."""
    events: list[tuple[str, str]] = []

    # A plain function would be run in the executor, losing the order the
    # events were fired in; a callback listener runs inline, in order.
    @callback
    def _record(event: Event) -> None:
        events.append((event.data["type"], event.data["subtype"]))

    hass.bus.async_listen(EVENT_BUTTON_ACTION, _record)
    return events


async def _setup_with_options(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    """Set the integration up like ``init_integration``, with entry options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        options=options,
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_event_pressed_and_depressed(
    hass: HomeAssistant, init_integration, bus_events
) -> None:
    """The raw edges still fire, and a short press completes as a click."""
    coordinator = init_integration.runtime_data
    _push(coordinator, UP, "1")
    await hass.async_block_till_done()
    assert hass.states.get("event.button_a_up").attributes["event_type"] == "pressed"
    _push(coordinator, UP, "0")
    await hass.async_block_till_done()
    # The release fires the edge first, then the gesture it completed — so
    # the entity's last event is the click, and the bus saw both in order.
    assert hass.states.get("event.button_a_up").attributes["event_type"] == "click"
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]


async def test_event_fires_on_each_push_not_on_rest_reread(
    hass: HomeAssistant, init_integration
) -> None:
    """Fire-on-push: every WS edge fires (even repeats); REST re-reads do not."""
    coordinator = init_integration.runtime_data
    press_frame = {
        "type": "datapoint",
        "data": {"id": "idrock1-00c", "values": [{"key": "up_request", "value": "1"}]},
    }
    with patch.object(JungHomeEventEntity, "_trigger_event") as mock_trigger:
        # Two identical-value pushes: a level diff would coalesce these into a
        # single (or zero) events; fire-on-push fires each genuine edge.
        coordinator._handle_websocket_message(press_frame)
        coordinator._handle_websocket_message(press_frame)
        await hass.async_block_till_done()
        assert mock_trigger.call_count == 2
        assert [c.args[0] for c in mock_trigger.call_args_list] == [
            "pressed",
            "pressed",
        ]

        # A REST poll re-reads the same datapoint values, but the coordinator's
        # pushed-datapoint marker is None for non-WS updates, so nothing fires.
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
        assert mock_trigger.call_count == 2


async def test_tap_reported_twice_fires_one_click(
    rocker: _Rocker, bus_events, caplog: pytest.LogCaptureFixture
) -> None:
    """One tap = two press/release pairs on the wire = exactly one click.

    The second pair is device firmware 2.2.0.x's copy of the first; it lands
    within the 1.2 s window after the click's release and is dropped whole —
    no second ``pressed``/``depressed`` either, so hand-written edge
    automations and device triggers see the tap once too.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.junghome.event")
    await rocker.tap()
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]
    assert "Dropping duplicate press on event.button_a_up" in caplog.text


async def test_two_taps_two_seconds_apart_fire_two_clicks(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A genuine second tap outside the window is a second click, copy dropped."""
    await rocker.tap()
    await rocker.advance(2.0)
    await rocker.tap()
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")] * 2


async def test_hold_fires_hold_start_by_timer_and_hold_end_at_release(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A press outlasting the threshold is a hold: no click, ever.

    ``hold_start`` comes from the timer at exactly 1.0 s — not earlier — and
    ``hold_end`` follows the raw release edge.
    """
    await rocker.edge(UP, "1")
    await rocker.advance(0.4)
    assert bus_events == [("up", "pressed")]
    await rocker.advance(0.6)
    assert bus_events == [("up", "pressed"), ("up", "hold_start")]
    assert hass.states.get("event.button_a_up").attributes["event_type"] == (
        "hold_start"
    )
    # A ~3 s hold, as captured; the release ends it.
    await rocker.edge(UP, "0", after=1.8)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]


async def test_key_element_copy_on_the_other_side_is_suppressed(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """On a single-key element the copy alternates sides — still one click.

    The gateway toggles the reported side on every reception for key
    elements, so the tap's two pairs land on ``up`` then ``down``. The window
    is per device, so the ``down`` pair is recognised as the copy; a
    per-entity window would have let it through as a second click.
    """
    await rocker.tap(first=UP, copy=DOWN)
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]


async def test_suppression_disabled_lets_both_pairs_through(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, bus_events
) -> None:
    """With the option off every pair is its own click — the old behaviour.

    For older device firmware that reports each tap once, where a user
    double-taps faster than the 1.2 s window would allow.
    """
    entry = await _setup_with_options(hass, {CONF_SUPPRESS_DUPLICATE_PRESSES: False})
    await _Rocker(hass, freezer, entry.runtime_data).tap()
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")] * 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_hold_shortly_after_a_click_is_not_dropped(
    rocker: _Rocker, bus_events, caplog: pytest.LogCaptureFixture
) -> None:
    """A press inside the window that is still down at the threshold is a hold.

    Only a ~0.4 s copy is a duplicate. Click-then-hold within 1.2 s (turn on,
    then dim) must not lose the hold: its ``pressed`` edge is reinstated at
    the threshold, right before ``hold_start``, and the release ends it.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.junghome.event")
    await rocker.edge(UP, "1")
    await rocker.edge(UP, "0", after=TAP_PULSE)
    await rocker.edge(UP, "1", after=0.6)
    tap = [("up", "pressed"), ("up", "depressed"), ("up", "click")]
    assert bus_events == tap  # the press is on probation: nothing yet
    await rocker.advance(1.0)
    assert bus_events == [*tap, ("up", "pressed"), ("up", "hold_start")]
    assert "outlasted the duplicate window" in caplog.text
    await rocker.edge(UP, "0", after=1.5)
    assert bus_events == [
        *tap,
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]


async def test_press_while_down_restarts_and_closes_an_open_hold(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A press on a side that never released restarts the gesture.

    By the gateway code a single-key element's hold leaves one side down for
    good. The next press on it must not be measured from that stale press
    (which would turn a tap into a ``hold_end``), and the hold it interrupts
    gets the ``hold_end`` it is owed, so the pair stays balanced.
    """
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    assert bus_events == [("up", "pressed"), ("up", "hold_start")]
    # No release ever arrives; a fresh tap on the same side much later.
    await rocker.edge(UP, "1", after=60)
    await rocker.edge(UP, "0", after=TAP_PULSE)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "hold_end"),
        ("up", "pressed"),
        ("up", "depressed"),
        ("up", "click"),
    ]


async def test_release_without_a_press_is_an_edge_only(
    hass: HomeAssistant, init_integration, bus_events
) -> None:
    """A stray release (the gateway repeats a value on a mode change) is no click."""
    _push(init_integration.runtime_data, UP, "0")
    await hass.async_block_till_done()
    assert bus_events == [("up", "depressed")]


async def test_hold_timer_is_cancelled_on_unload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    bus_events,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unloading mid-press must cancel the hold timer, not fire on a dead entity."""
    entry = await _setup_with_options(hass, {})
    rocker = _Rocker(hass, freezer, entry.runtime_data)
    await rocker.edge(UP, "1")
    assert bus_events == [("up", "pressed")]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await rocker.advance(1.5)
    assert bus_events == [("up", "pressed")]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_unavailable_mid_press_abandons_the_gesture(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A press in flight when the socket drops completes no gesture.

    Deaf, the entity cannot see the release: the hold timer must not turn a
    tap whose release was lost into a ``hold_start``. Once the socket is back
    the next tap is a normal click.
    """
    coordinator = rocker.coordinator
    await rocker.edge(UP, "1")
    coordinator.ws_connected = False
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    assert hass.states.get("event.button_a_up").state == "unavailable"
    await rocker.advance(1.5)
    assert bus_events == [("up", "pressed")]

    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    await rocker.edge(UP, "1")
    await rocker.edge(UP, "0", after=TAP_PULSE)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "pressed"),
        ("up", "depressed"),
        ("up", "click"),
    ]


async def test_hold_timer_landing_while_unavailable_fires_nothing(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """The timer itself checks availability, for the drop-then-timer ordering."""
    coordinator = rocker.coordinator
    await rocker.edge(UP, "1")
    # The socket flag flips before its listener dispatch has run.
    coordinator.ws_connected = False
    await rocker.advance(1.0)
    assert bus_events == [("up", "pressed")]
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await rocker.edge(UP, "0", after=1.0)
    # Abandoned: the release is an edge, not the end of anything.
    assert bus_events == [("up", "pressed"), ("up", "depressed")]


async def test_press_abandoned_in_an_outage_is_no_longer_down(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """An abandoned press is forgotten by the device's tracker too.

    Kept as "down", a tap on the other side 0.6-2.5 s after it — once the
    socket is back — read as the firmware's copy of a hold and was dropped.
    """
    coordinator = rocker.coordinator
    await rocker.edge(UP, "1")
    coordinator.ws_connected = False
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    await rocker.edge(DOWN, "1", after=1.5)
    await rocker.edge(DOWN, "0", after=TAP_PULSE)
    assert bus_events == [
        ("up", "pressed"),
        ("down", "pressed"),
        ("down", "depressed"),
        ("down", "click"),
    ]


async def test_outage_forgets_a_hold_copy_marker(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A copy taken before a drop does not route a release seen after it.

    Both sides abandon their gesture: the release that follows on the copy's
    side is an edge on that side (as after any abandoned press), not a
    silent completion of the other side's abandoned hold.
    """
    coordinator = rocker.coordinator
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    await rocker.edge(DOWN, "1", after=0.4)  # the copy of the hold
    coordinator.ws_connected = False
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    await rocker.edge(DOWN, "0", after=0.5)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("down", "depressed"),
    ]


async def test_hold_end_is_owed_across_an_outage(
    hass: HomeAssistant, rocker: _Rocker, bus_events
) -> None:
    """A hold interrupted by a drop still gets its ``hold_end`` at the release."""
    coordinator = rocker.coordinator
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    coordinator.ws_connected = False
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    # Still holding when the socket came back; the release now arrives.
    await rocker.edge(UP, "0", after=1.0)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]


async def test_event_unavailable_while_websocket_down(
    hass: HomeAssistant, init_integration
) -> None:
    """A button event entity is unavailable while the WebSocket is down.

    Edges only ever arrive as WebSocket pushes — a REST poll re-reads the same
    values and fires nothing — so with the socket down the entity is deaf, not
    merely late. It used to stay "available" on the REST signal alone, which
    hid every lost press and meant the shipped blueprint's abort-on-unavailable
    guard (a gesture cut short by a drop) could never engage. It must read
    unavailable on the drop and come back on reconnect, still firing edges.
    """
    coordinator = init_integration.runtime_data
    press = {
        "type": "datapoint",
        "data": {"id": "idrock1-00c", "values": [{"key": "up_request", "value": "1"}]},
    }
    coordinator._handle_websocket_message(press)
    await hass.async_block_till_done()
    live = hass.states.get("event.button_a_up")
    assert live.attributes["event_type"] == "pressed"

    # The socket drops: the coordinator's own drop notification (what the
    # `_run_websocket` finally block runs) must flip the entity unavailable
    # even though the REST poll is still succeeding.
    coordinator.ws_connected = False
    coordinator._notify_websocket_closed()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is True
    assert hass.states.get("event.button_a_up").state == "unavailable"
    # A pure state reader on the same REST signal is unaffected.
    assert (
        hass.states.get("sensor.boiler_present_device_input_power").state
        != "unavailable"
    )

    # Reconnect (the real connect path refreshes, which re-dispatches).
    coordinator.ws_connected = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    restored = hass.states.get("event.button_a_up")
    assert restored.state == live.state
    assert restored.attributes["event_type"] == "pressed"

    # And it is genuinely live again: the next edge fires.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00c",
                "values": [{"key": "up_request", "value": "0"}],
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.button_a_up").attributes["event_type"] == "depressed"


def _bare_entity(hass: HomeAssistant, dp_type: str) -> JungHomeEventEntity:
    coordinator = bare_coordinator(hass)
    device = {"id": "d", "type": "RockerSwitch", "label": "Btn", "datapoints": []}
    datapoint = {"id": "d-x", "type": dp_type, "values": []}
    tracker = ButtonGestureTracker(suppress_duplicates=True)
    return JungHomeEventEntity(coordinator, device, datapoint, tracker)


async def test_event_unknown_datapoint_type_uses_name(hass: HomeAssistant) -> None:
    """A datapoint type with no translation key falls back to a plain name."""
    entity = _bare_entity(hass, "weird_request")
    # No matching translation key -> _attr_name is set to the raw dp type.
    assert entity._attr_name == "weird_request"


async def test_event_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    """_handle_coordinator_update returns early when the device is gone."""
    entity = _bare_entity(hass, "up_request")
    # coordinator.data is [] so the device lookup yields None -> early return.
    with patch.object(entity, "async_write_ha_state") as write_state:
        entity._handle_coordinator_update()  # must not raise
    write_state.assert_called_once()


async def test_fire_bus_event_skipped_without_device_entry(
    hass: HomeAssistant,
) -> None:
    """No bus event is emitted for an entity not yet in the device registry.

    A device trigger is keyed on the registry device id, so an event without
    one would match nothing; the early return must not raise either.
    """
    entity = _bare_entity(hass, "up_request")
    entity.hass = hass
    fired: list[object] = []
    hass.bus.async_listen("junghome_button_action", fired.append)

    entity._fire_bus_event("pressed")  # device_entry is None: must no-op
    await hass.async_block_till_done()
    assert fired == []


async def test_all_event_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every event entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.EVENT)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_nan_button_state_is_not_an_edge(
    hass: HomeAssistant, init_integration
) -> None:
    """A `"NaN"` button value must fire nothing at all.

    The gateway sends it once it has given up reading the node. Treated as a
    falsy value it became a `depressed` edge — a phantom release, on the entity
    AND on the bus event that device triggers listen to. These states are
    `POLL_ONCE`, and an empty value keeps them dirty, so a button that never
    answers is re-polled every cycle: the phantom would repeat indefinitely.
    """
    coordinator = init_integration.runtime_data
    events: list = []
    hass.bus.async_listen("junghome_button_action", events.append)

    before = hass.states.get("event.button_a_up").state
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00c",
                "values": [{"key": "up_request", "value": "NaN"}],
            },
        }
    )
    await hass.async_block_till_done()

    assert events == []
    assert hass.states.get("event.button_a_up").state == before


async def test_genuine_press_after_the_copy_but_within_the_window_is_kept(
    rocker: _Rocker, bus_events
) -> None:
    """Only the FIRST press after a click is a duplicate candidate.

    ``is_duplicate_press`` consumes the click whether or not it drops the
    press, so a genuine second tap that lands after the copy but still inside
    1.2 s of the first click's release (tap, copy, tap in quick succession)
    must fire — a tracker that kept the click armed dropped it. Timeline: tap
    released at 0.4 s, copy 0.9-1.3 s, real press at 1.5 s (1.1 s after the
    click's release).
    """
    await rocker.tap()
    tap = [("up", "pressed"), ("up", "depressed"), ("up", "click")]
    assert bus_events == tap
    await rocker.edge(UP, "1", after=0.2)
    assert bus_events == [*tap, ("up", "pressed")]
    await rocker.edge(UP, "0", after=TAP_PULSE)
    assert bus_events == tap * 2


async def test_duplicate_window_boundary_is_inclusive() -> None:
    """A copy landing exactly ``BUTTON_DUPLICATE_WINDOW`` after the click is dropped.

    The window covers the measured 1.03 s worst case with margin; the bound
    itself is inclusive (``<=``), so a press exactly on it is still the copy,
    and the first instant past it is a genuine press. The click is noted at
    0.0 so the difference is exactly the window: ``(10.0 + 1.2) - 10.0`` is
    ``1.1999999999999993`` in floating point, which a strict ``<`` passed too.
    """
    tracker = ButtonGestureTracker(suppress_duplicates=True)
    tracker.note_click(0.0)
    assert tracker.is_duplicate_press(BUTTON_DUPLICATE_WINDOW)
    tracker.note_click(0.0)
    assert not tracker.is_duplicate_press(BUTTON_DUPLICATE_WINDOW + 0.001)


# --- The firmware's copy of a HOLD on a single-key element (2026-09-16) ------
#
# Live capture on the 1-gang's keys: a hold arrived as ``up`` press, then a
# ``down`` press 1.4 s later (the copy, on the other side — the gateway toggles
# the side on every reception), then the finger's release on ``down`` at
# 2.55 s; ``up`` was never released. Three further holds on those keys carried
# no copy and were a single clean pulse, which the ordinary path handles.


async def test_key_element_hold_copy_completes_the_hold_on_the_first_side(
    rocker: _Rocker, bus_events
) -> None:
    """The captured shape: one ``hold_start``/``hold_end`` pair, on the held side."""
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    assert bus_events == [("up", "pressed"), ("up", "hold_start")]
    await rocker.edge(DOWN, "1", after=0.4)  # +1.4 s: the copy, other side
    assert bus_events == [("up", "pressed"), ("up", "hold_start")]
    await rocker.edge(DOWN, "0", after=1.15)  # +2.55 s: the finger, copy's side
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]
    # Nothing is stuck: the next tap on ``up`` is an ordinary tap (no stale
    # ``hold_end`` first), and ``down`` owes nothing either.
    bus_events.clear()
    await rocker.tap(first=UP, copy=UP)
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]


async def test_hold_copy_marker_does_not_outlive_the_hold(
    rocker: _Rocker, bus_events
) -> None:
    """The held side's own release ends the hold; the copy side owes nothing after.

    The gateway's side toggle is one field shared by every button, so an
    unrelated key pressed during the hold flips it back and the finger's
    release lands on the held side after all. The copy side then never gets
    a release, and its next tap must be a tap — not a press whose release is
    routed to the other side (which lost the click and re-fired a stale
    ``depressed`` there).
    """
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    await rocker.edge(DOWN, "1", after=0.4)  # +1.4 s: the copy
    await rocker.edge(UP, "0", after=0.6)  # +2.0 s: the finger, held side
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]
    bus_events.clear()
    await rocker.tap(first=DOWN, copy=DOWN)
    assert bus_events == [("down", "pressed"), ("down", "depressed"), ("down", "click")]


async def test_hold_copy_marker_is_spent_by_its_release(
    rocker: _Rocker, bus_events
) -> None:
    """The copy's release ends the hold once; a later stray release is an edge.

    The gateway re-sends a value on a mode-only change, so a release with no
    press can follow on the copy's side; it re-fires as that side's edge like
    any other (``test_release_without_a_press_is_an_edge_only``) instead of
    being routed to the finished hold again and swallowed.
    """
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    await rocker.edge(DOWN, "1", after=0.4)  # the copy
    await rocker.edge(DOWN, "0", after=1.15)  # the finger, copy's side
    bus_events.clear()
    await rocker.edge(DOWN, "0", after=0.5)
    assert bus_events == [("down", "depressed")]


async def test_hold_copy_release_after_the_hold_ended_is_nothing(
    rocker: _Rocker, bus_events
) -> None:
    """A copy's release arriving once the held side has released is dropped whole."""
    await rocker.edge(UP, "1")
    await rocker.advance(1.0)
    await rocker.edge(DOWN, "1", after=0.4)
    await rocker.edge(UP, "0", after=0.6)
    bus_events.clear()
    await rocker.edge(DOWN, "0", after=0.3)
    assert bus_events == []


async def test_hold_copy_before_the_threshold_still_yields_one_hold(
    rocker: _Rocker, bus_events
) -> None:
    """A copy inside the first second is dropped; the timer still classifies."""
    await rocker.edge(UP, "1")
    await rocker.edge_between_ticks(DOWN, "1", after=0.8)
    assert bus_events == [("up", "pressed")]
    await rocker.advance(0.2)  # the hold threshold on ``up``
    assert bus_events == [("up", "pressed"), ("up", "hold_start")]
    await rocker.edge(DOWN, "0", after=1.0)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "hold_start"),
        ("up", "depressed"),
        ("up", "hold_end"),
    ]


async def test_hold_copy_released_before_the_threshold_is_a_click(
    rocker: _Rocker, bus_events
) -> None:
    """The copy's release before the threshold completes the first side as a click."""
    await rocker.edge(UP, "1")
    await rocker.edge_between_ticks(DOWN, "1", after=0.7)
    await rocker.edge_between_ticks(DOWN, "0", after=0.25)
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]
    # The cancelled hold timer never fires.
    await rocker.advance(1.0)
    assert bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")]


async def test_other_side_pressed_during_a_tap_is_a_genuine_press(
    rocker: _Rocker, bus_events
) -> None:
    """The captured "A then immediately B" on a rocker: two clicks.

    The other side's press lands 0.445 s after the first press, i.e. inside a
    tap pulse — not a hold copy — and the gateway interleaves the pairs. Only
    the trailing copy of the second tap is dropped (by the click window).
    """
    await rocker.edge(UP, "1")
    await rocker.edge(DOWN, "1", after=0.445)
    await rocker.edge(UP, "0", after=0.045)
    await rocker.edge(DOWN, "0", after=0.392)
    await rocker.edge(DOWN, "1", after=0.527)
    await rocker.edge(DOWN, "0", after=0.325)
    assert bus_events == [
        ("up", "pressed"),
        ("down", "pressed"),
        ("up", "depressed"),
        ("up", "click"),
        ("down", "depressed"),
        ("down", "click"),
    ]


async def test_other_side_tap_after_a_completed_tap_is_genuine(
    rocker: _Rocker, bus_events
) -> None:
    """A released side is forgotten: a later other-side tap is not a hold copy.

    The release that completes a click clears the device's "side down" record
    (``note_up``). Kept, a tap on the other side landing 0.6-2.5 s after the
    first PRESS — here 2.05 s, past the 1.2 s click window — read as the
    firmware's copy of a hold on the released side and was dropped whole.
    """
    await rocker.edge(UP, "1")
    await rocker.edge(UP, "0", after=0.45)
    await rocker.edge(DOWN, "1", after=1.6)
    await rocker.edge(DOWN, "0", after=TAP_PULSE)
    assert bus_events == [
        ("up", "pressed"),
        ("up", "depressed"),
        ("up", "click"),
        ("down", "pressed"),
        ("down", "depressed"),
        ("down", "click"),
    ]


async def test_hold_copy_is_not_looked_for_with_suppression_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, bus_events
) -> None:
    """Older firmware sends no copies: a second-side press during a hold is genuine."""
    entry = await _setup_with_options(hass, {CONF_SUPPRESS_DUPLICATE_PRESSES: False})
    rocker = _Rocker(hass, freezer, entry.runtime_data)
    await rocker.edge(UP, "1")
    await rocker.edge(DOWN, "1", after=1.4)
    assert bus_events == [("up", "pressed"), ("up", "hold_start"), ("down", "pressed")]
    await hass.config_entries.async_unload(entry.entry_id)


def test_hold_copy_target_window() -> None:
    """Other side, down for 0.6 to 2.5 s: a copy; the same side, or outside: not."""
    tracker = ButtonGestureTracker(suppress_duplicates=True)
    up, down = object(), object()
    assert tracker.hold_copy_target(down, 0.0) is None  # nothing down
    tracker.note_press(up, 0.0)
    assert tracker.hold_copy_target(up, 1.0) is None  # same side
    assert tracker.hold_copy_target(down, 0.5) is None  # inside a tap pulse
    assert tracker.hold_copy_target(down, 0.6) is up
    assert tracker.hold_copy_target(down, 2.5) is up
    assert tracker.hold_copy_target(down, 2.6) is None  # too late to be the copy
    tracker.note_up(down)  # not the recorded side: ignored
    assert tracker.hold_copy_target(down, 1.0) is up
    tracker.note_up(up)
    assert tracker.hold_copy_target(down, 1.0) is None


# --- Firmware-aware suppression default (verbose device endpoint) ------------


@pytest.mark.parametrize(
    ("revision", "clicks"),
    [
        ([2, 1, 4, 0], 2),  # pre-2.2.0: exempt, two pairs are two clicks
        ([2, 2, 0, 2], 1),  # current button firmware (where read): copy dropped
    ],
)
async def test_suppression_follows_the_button_firmware(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    bus_events,
    revision: list[int],
    clicks: int,
) -> None:
    """A button the gateway reports at firmware < 2.2.0 reports each tap once.

    Suppression stays on for the entry, but that device is exempt: two pairs
    from it are two clicks (the old-firmware double-tap). A revision at or
    above 2.2.0, or none at all, keeps the copy dropped.
    """
    firmware = [
        {
            "device_id": "idrock1",
            "device_type": "PushButton",
            "states": {},
            "property": {
                "software_revision": {
                    "state_type": "software_revision",
                    "value": revision,
                }
            },
        }
    ]
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_verbose_from_api",
        AsyncMock(return_value=firmware),
    ):
        entry = await _setup_with_options(hass, {})
    await _Rocker(hass, freezer, entry.runtime_data).tap()
    assert (
        bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")] * clicks
    )
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("revision", "clicks"),
    [([2, 1, 4, 0], 2), ([2, 2, 0, 2], 1)],
)
async def test_suppression_follows_the_firmware_of_the_buttons_node(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    bus_events,
    revision: list[int],
    clicks: int,
) -> None:
    """A button whose own revision is null takes its node's.

    The gateway fills the node-wide revision only on the function at the
    node's main element (18 of 20 buttons read ``null`` in the 2026-09-16
    probe); every function carries the revision state at that element's
    address, which is how the button finds its node's value.
    """

    def _function(device_id: str, value: list[int] | None) -> dict:
        return {
            "device_id": device_id,
            "device_type": "PushButton",
            "states": {},
            "property": {
                "software_revision": {
                    "state_type": "software_revision",
                    "value": value,
                    "model": {"address": 562, "category": "property"},
                }
            },
        }

    firmware = [_function("idlight1", revision), _function("idrock1", None)]
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_verbose_from_api",
        AsyncMock(return_value=firmware),
    ):
        entry = await _setup_with_options(hass, {})
    await _Rocker(hass, freezer, entry.runtime_data).tap()
    assert (
        bus_events == [("up", "pressed"), ("up", "depressed"), ("up", "click")] * clicks
    )
    await hass.config_entries.async_unload(entry.entry_id)
