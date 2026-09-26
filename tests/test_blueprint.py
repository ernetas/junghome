"""Tests for the shipped button-gestures blueprint.

The blueprint is real, user-facing logic that HACS does not install and CI never
exercised. These tests load the actual YAML and render its templates through
Home Assistant's own engine, so a change to the gesture logic has to survive the
same cases a user's buttons produce.
"""

import asyncio
import importlib.util
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.junghome.const import (
    BUTTON_DUPLICATE_WINDOW,
    BUTTON_HOLD_THRESHOLD,
    CONF_SUPPRESS_DUPLICATE_PRESSES,
    DOMAIN,
)
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from tests.conftest import PRISTINE_DEVICES, _fake_run_websocket

_REPO = Path(__file__).parent.parent
_BLUEPRINT = _REPO / "blueprints" / "automation" / "junghome" / "button_gestures.yaml"
_CAPTURE_TOOL = _REPO / "tools" / "ws-capture" / "capture_ws.py"


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Home Assistant's `!input` tag."""


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: f"!input {loader.construct_scalar(node)}"
)


@pytest.fixture(name="blueprint")
def blueprint_fixture() -> dict:
    """The parsed blueprint."""
    # S506 is a false positive here: _BlueprintLoader subclasses SafeLoader and
    # only adds a constructor for `!input`, so it cannot instantiate arbitrary
    # objects.
    source = _BLUEPRINT.read_text(encoding="utf-8")
    return yaml.load(source, Loader=_BlueprintLoader)  # noqa: S506


def test_blueprint_declares_the_expected_inputs(blueprint: dict) -> None:
    """The inputs the README and docs tell users to fill in are all present.

    Every input has a default (the entity list aside), so automations created
    from the previous revision — which had a `hold_time` and no
    `legacy_double_click` — keep loading: Home Assistant only rejects a
    *missing* input, never a stale one.
    """
    assert blueprint["blueprint"]["domain"] == "automation"
    inputs = blueprint["blueprint"]["input"]
    assert set(inputs) == {
        "button",
        "single_action",
        "hold_action",
        "legacy_double_click",
        "double_click_window",
        "double_action",
    }
    assert all("default" in spec for name, spec in inputs.items() if name != "button")
    # Double-click detection is the old-firmware path and must stay opt-in.
    assert inputs["legacy_double_click"]["default"] is False
    # `mode: single` + silent max_exceeded is what lets the legacy path's
    # wait_for_trigger catch the second click instead of a second run.
    assert blueprint["mode"] == "single"
    assert blueprint["max_exceeded"] == "silent"


@pytest.mark.parametrize(
    "inputs",
    [
        {"single_action": [{"action": "light.toggle"}]},
        {"hold_action": [{"action": "light.toggle"}]},
        {"legacy_double_click": True, "double_action": [{"action": "light.toggle"}]},
    ],
    ids=["click", "hold", "legacy_double"],
)
async def test_blueprint_substitutes_into_a_valid_automation(
    hass: HomeAssistant, inputs: dict
) -> None:
    """The blueprint, with inputs filled in, is an automation HA accepts.

    Loaded through Home Assistant's own blueprint machinery and validated
    with the automation config validator (which raises on an invalid
    trigger, condition or action) — the same path the UI's "use blueprint"
    flow takes, so a typo in a `!input` reference or a malformed `choose`
    cannot ship.
    """
    blueprint = Blueprint(
        load_yaml_dict(str(_BLUEPRINT)),
        expected_domain="automation",
        path="junghome/button_gestures.yaml",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    filled = BlueprintInputs(
        blueprint,
        {
            "alias": "probe",
            "use_blueprint": {
                "path": "junghome/button_gestures.yaml",
                "input": {"button": ["event.button_a_up"], **inputs},
            },
        },
    )
    filled.validate()
    config = await async_validate_config_item(hass, "probe", filled.async_substitute())
    assert config is not None


def _render(hass: HomeAssistant, template: str, **variables: object) -> bool:
    return Template(template, hass).async_render(variables, parse_result=True)


def _event(event_type: str, stamp: str = "2026-08-01T12:00:00+00:00") -> State:
    """An event entity's state: a timestamp carrying the event_type attribute."""
    return State("event.button_a_up", stamp, {"event_type": event_type})


@pytest.mark.parametrize("gesture", ["click", "hold_start"])
async def test_gesture_condition_fires_on_a_real_gesture(
    hass: HomeAssistant, blueprint: dict, gesture: str
) -> None:
    """A genuine click or hold_start (timestamp -> timestamp) starts a run."""
    condition = blueprint["conditions"][0]["value_template"]
    trigger = {
        "from_state": _event("pressed", "2026-08-01T11:59:59+00:00"),
        "to_state": _event(gesture),
    }
    assert _render(hass, condition, trigger=trigger) is True


@pytest.mark.parametrize("gesture", ["click", "hold_start"])
async def test_gesture_condition_ignores_recovery_from_unavailable(
    hass: HomeAssistant, blueprint: dict, gesture: str
) -> None:
    """The `unavailable -> restored` transition must not fire a gesture.

    Event entities restore their last state, so a restart, an entry reload or a
    recovered poll re-presents the stored `event_type`. When that stored value
    is a gesture, every recovery would otherwise run the user's action.
    """
    condition = blueprint["conditions"][0]["value_template"]
    for stale in ("unavailable", "unknown"):
        trigger = {
            "from_state": State("event.button_a_up", stale),
            "to_state": _event(gesture),
        }
        assert _render(hass, condition, trigger=trigger) is False, stale


@pytest.mark.parametrize("edge", ["pressed", "depressed", "hold_end"])
async def test_gesture_condition_ignores_raw_edges_and_hold_end(
    hass: HomeAssistant, blueprint: dict, edge: str
) -> None:
    """Only the two gestures the blueprint maps to actions start a run.

    The raw edges still fire on the entity for other automations; `hold_end`
    has no action here. Reacting to them would double up every gesture.
    """
    condition = blueprint["conditions"][0]["value_template"]
    trigger = {
        "from_state": _event("click", "2026-08-01T11:59:59+00:00"),
        "to_state": _event(edge),
    }
    assert _render(hass, condition, trigger=trigger) is False


async def test_hold_start_runs_the_hold_action_and_click_runs_the_click_action(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """The gesture branches map straight onto the integration's events."""
    gesture_var = blueprint["actions"][0]["variables"]["gesture"]
    assert (
        _render(hass, gesture_var, trigger={"to_state": _event("hold_start")})
        == "hold_start"
    )
    choose = blueprint["actions"][1]
    hold_branch, legacy_branch = choose["choose"]
    assert _render(hass, hold_branch["conditions"][0], gesture="hold_start") is True
    assert _render(hass, hold_branch["conditions"][0], gesture="click") is False
    assert hold_branch["sequence"][0]["default"] == "!input hold_action"
    # A click with the legacy path off falls through to the click action
    # immediately — no window, no wait.
    assert (
        _render(
            hass,
            legacy_branch["conditions"][0],
            gesture="click",
            legacy_double_click=False,
        )
        is False
    )
    assert choose["default"][0]["default"] == "!input single_action"


async def test_legacy_double_click_path_is_opt_in_and_guarded(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """Old-firmware double-click detection waits for a second real click.

    Entered only for a click with the option on; the second-click check
    applies the same real-previous-state guard as the trigger condition, so a
    recovery inside the window cannot fake a double.
    """
    legacy_branch = blueprint["actions"][1]["choose"][1]
    assert (
        _render(
            hass,
            legacy_branch["conditions"][0],
            gesture="click",
            legacy_double_click=True,
        )
        is True
    )
    assert (
        _render(
            hass,
            legacy_branch["conditions"][0],
            gesture="hold_start",
            legacy_double_click=True,
        )
        is False
    )
    wait, decide = legacy_branch["sequence"]
    # The wait must complete on the second CLICK, not on the next state
    # change: the second tap's `pressed` edge is the very next change, and a
    # bare state trigger completed on it — the double action never ran.
    assert wait["wait_for_trigger"] == [
        {
            "trigger": "state",
            "entity_id": "!input button",
            "attribute": "event_type",
            "to": "click",
        }
    ]
    assert wait["timeout"] == {"milliseconds": "!input double_click_window"}
    assert wait["continue_on_timeout"] is True

    condition = decide["choose"][0]["conditions"][0]
    assert decide["choose"][0]["sequence"][0]["default"] == "!input double_action"
    assert decide["default"][0]["default"] == "!input single_action"

    recovery = {
        "trigger": {
            "from_state": State("event.button_a_up", "unavailable"),
            "to_state": _event("click"),
        }
    }
    assert _render(hass, condition, wait=recovery) is False
    # The transition a real second click produces: the release edge wrote
    # `depressed` an instant before the click (each event is its own write).
    real = {
        "trigger": {
            "from_state": _event("depressed", "2026-08-01T11:59:59+00:00"),
            "to_state": _event("click"),
        }
    }
    assert _render(hass, condition, wait=real) is True
    # Nothing arrived within the window -> CLICK, not DOUBLE.
    assert _render(hass, condition, wait={"trigger": None}) is False


def _install_blueprint(hass: HomeAssistant) -> None:
    """Copy the shipped blueprint where the automation integration looks."""
    dest = Path(hass.config.path("blueprints/automation/junghome"))
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(_BLUEPRINT, dest / "button_gestures.yaml")


class _LiveRocker:
    """Drive the fixture rocker's `up` side with real edges on a frozen clock.

    Settles with a few loop turns rather than ``async_block_till_done``: an
    automation run parked in its double-click wait is a pending task, and
    blocking on it would wait out a window that only advances by ``tick``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        coordinator: JungHomeDataUpdateCoordinator,
    ) -> None:
        self.hass, self.freezer, self.coordinator = hass, freezer, coordinator

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed_exact(self.hass)
        await self.settle()

    async def edge(self, value: str, *, after: float = 0) -> None:
        if after:
            await self.advance(after)
        self.coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {
                    "id": _UP_DATAPOINT,
                    "values": [{"key": "up_request", "value": value}],
                },
            }
        )
        await self.settle()

    async def tap(self, *, after: float = 0) -> None:
        """One press/release pair, as old (single-reporting) firmware sends it."""
        await self.edge("1", after=after)
        await self.edge("0", after=_TAP_PULSE)


# The fixture rocker's `up_request` datapoint, and the gateway's synthesised
# release ~0.4 s after a press (see tests/test_event.py).
_UP_DATAPOINT = "idrock1-00c"
_TAP_PULSE = 0.4


@asynccontextmanager
async def _live_rocker(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, inputs: dict
) -> AsyncIterator[tuple[_LiveRocker, list[str]]]:
    """The integration + an automation from the blueprint, edges recorded.

    Old-firmware conditions: duplicate suppression off (one pair per tap is
    what such firmware sends, and the legacy double-click path requires it).
    Every action fires a `probe_action` bus event naming the branch. The
    gateway mocks stay in place for the whole test: the test drives the clock
    forward, and any poll that releases must still hit a mock.
    """
    _install_blueprint(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        options={CONF_SUPPRESS_DUPLICATE_PRESSES: False},
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
        fired: list[str] = []

        # A @callback runs in the event loop as the event fires. A plain
        # function is run in the executor, so whether it has recorded the
        # action within `settle()`'s loop turns was down to thread scheduling —
        # a slow CI runner lost that race.
        @callback
        def _record(event: Event) -> None:
            fired.append(event.data["kind"])

        hass.bus.async_listen("probe_action", _record)

        def _action(kind: str) -> list[dict]:
            return [{"event": "probe_action", "event_data": {"kind": kind}}]

        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": [
                    {
                        "alias": "probe",
                        "use_blueprint": {
                            "path": "junghome/button_gestures.yaml",
                            "input": {
                                "button": ["event.button_a_up"],
                                "single_action": _action("single"),
                                "hold_action": _action("hold"),
                                "double_action": _action("double"),
                                **inputs,
                            },
                        },
                    }
                ]
            },
        )
        await hass.async_block_till_done()
        assert hass.states.get("automation.probe") is not None
        rocker = _LiveRocker(hass, freezer, entry.runtime_data)
        # Warm the entity up with a real previous state (the blueprint's guard
        # ignores the first transition out of `unknown`), then let it go quiet.
        await rocker.tap()
        await rocker.advance(3.0)
        fired.clear()
        yield rocker, fired
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_blueprint_end_to_end_click_and_hold(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Real edges through the real integration: a click acts at once, a hold once."""
    async with _live_rocker(hass, freezer, {}) as (rocker, fired):
        await rocker.tap()
        assert fired == ["single"]

        fired.clear()
        await rocker.edge("1", after=3.0)
        await rocker.advance(1.0)  # BUTTON_HOLD_THRESHOLD: hold_start fires
        assert fired == ["hold"]
        await rocker.edge("0", after=2.0)  # hold_end is not an action
        assert fired == ["hold"]


async def test_blueprint_end_to_end_legacy_double_click(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Old firmware, legacy double-click on: two taps in the window -> DOUBLE.

    The shipped wait was a bare state trigger, and the second tap's `pressed`
    edge — the very next state change — completed it, so this ran the single
    action twice and the double action never once. A lone tap still runs the
    single action, after the window has elapsed. Runs with the blueprint's
    default window: a click is reported at the gateway's synthesised release
    (~0.4 s after the press), so the second click of a double-click lands
    0.5-1 s after the first, which the old 400 ms default could never cover.
    """
    inputs = {"legacy_double_click": True}
    async with _live_rocker(hass, freezer, inputs) as (rocker, fired):
        # A double-click: two single pairs 0.3 s apart (release to next press),
        # so the second click lands 0.7 s after the first.
        await rocker.tap()
        assert fired == []  # the window is open, nothing decided yet
        await rocker.tap(after=0.3)
        assert fired == ["double"]
        await rocker.advance(2.0)
        assert fired == ["double"]

        fired.clear()
        await rocker.tap(after=3.0)
        assert fired == []  # waiting out the window
        await rocker.advance(0.5)
        assert fired == []
        await rocker.advance(0.6)
        assert fired == ["single"]


def test_capture_tool_mirrors_the_gesture_constants(blueprint: dict) -> None:
    """The WS capture tool's `analyze` verdicts use the shipped values.

    The tool cannot import the integration (it runs without Home Assistant),
    so it carries copies of the hold threshold, the duplicate window and the
    blueprint's double-click default; it once judged captures against a 2 s
    hold and a 0.4 s window that nothing shipped any more.
    """
    spec = importlib.util.spec_from_file_location("capture_ws", _CAPTURE_TOOL)
    assert spec is not None
    assert spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.HOLD_THRESHOLD_S == BUTTON_HOLD_THRESHOLD
    assert tool.DUPLICATE_WINDOW_S == BUTTON_DUPLICATE_WINDOW
    default_ms = blueprint["blueprint"]["input"]["double_click_window"]["default"]
    assert default_ms == tool.LEGACY_DOUBLE_CLICK_WINDOW_S * 1000
