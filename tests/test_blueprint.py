"""Tests for the shipped button-gestures blueprint.

The blueprint is real, user-facing logic that HACS does not install and CI never
exercised. These tests load the actual YAML and render its templates through
Home Assistant's own engine, so a change to the gesture logic has to survive the
same cases a user's buttons produce.
"""

from pathlib import Path

import pytest
import yaml
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml_dict

_BLUEPRINT = (
    Path(__file__).parent.parent
    / "blueprints"
    / "automation"
    / "junghome"
    / "button_gestures.yaml"
)


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
    assert wait["wait_for_trigger"][0]["entity_id"] == "!input button"
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
    real = {
        "trigger": {
            "from_state": _event("click", "2026-08-01T11:59:59+00:00"),
            "to_state": _event("click"),
        }
    }
    assert _render(hass, condition, wait=real) is True
    # A release edge or a hold inside the window is not a second click.
    for other in ("depressed", "hold_start"):
        not_a_click = {
            "trigger": {
                "from_state": _event("click", "2026-08-01T11:59:59+00:00"),
                "to_state": _event(other),
            }
        }
        assert _render(hass, condition, wait=not_a_click) is False, other
    # Nothing arrived within the window -> CLICK, not DOUBLE.
    assert _render(hass, condition, wait={"trigger": None}) is False
