"""Tests for the shipped button-gestures blueprint.

The blueprint is real, user-facing logic that HACS does not install and CI never
exercised. These tests load the actual YAML and render its templates through
Home Assistant's own engine, so a change to the gesture logic has to survive the
same cases a user's buttons produce.
"""

from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.template import Template

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
    """The inputs the README and docs tell users to fill in are all present."""
    assert blueprint["blueprint"]["domain"] == "automation"
    assert set(blueprint["blueprint"]["input"]) == {
        "button",
        "hold_time",
        "double_click_window",
        "single_action",
        "double_action",
        "hold_action",
    }
    # `mode: single` + silent max_exceeded is what stops a second press
    # re-entering the gesture state machine mid-run.
    assert blueprint["mode"] == "single"
    assert blueprint["max_exceeded"] == "silent"


def _render(hass: HomeAssistant, template: str, **variables: object) -> bool:
    return Template(template, hass).async_render(variables, parse_result=True)


def _event(event_type: str, stamp: str = "2026-08-01T12:00:00+00:00") -> State:
    """An event entity's state: a timestamp carrying the event_type attribute."""
    return State("event.button_a_up", stamp, {"event_type": event_type})


async def test_press_condition_fires_on_a_real_press(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """A genuine press (timestamp -> timestamp) starts the gesture."""
    condition = blueprint["conditions"][0]["value_template"]
    trigger = {
        "from_state": _event("depressed", "2026-08-01T11:59:59+00:00"),
        "to_state": _event("pressed"),
    }
    assert _render(hass, condition, trigger=trigger) is True


async def test_press_condition_ignores_recovery_from_unavailable(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """The `unavailable -> restored` transition must not fire a gesture.

    Event entities restore their last state, so a restart, an entry reload or a
    recovered poll re-presents the stored `event_type`. When that stored value is
    `pressed` — which is exactly what a socket drop between press and release
    leaves behind — every single recovery ran the user's action.
    """
    condition = blueprint["conditions"][0]["value_template"]
    for stale in ("unavailable", "unknown"):
        trigger = {
            "from_state": State("event.button_a_up", stale),
            "to_state": _event("pressed"),
        }
        assert _render(hass, condition, trigger=trigger) is False, stale


async def test_press_condition_ignores_a_release(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """Only `pressed` opens a gesture; the release is handled inside it."""
    condition = blueprint["conditions"][0]["value_template"]
    trigger = {
        "from_state": _event("pressed", "2026-08-01T11:59:59+00:00"),
        "to_state": _event("depressed"),
    }
    assert _render(hass, condition, trigger=trigger) is False


async def test_mid_gesture_wait_ignores_recovery(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """A recovery inside the hold window is not the second press of a double."""
    evt = blueprint["actions"][1]["variables"]["evt"]
    recovery = {
        "trigger": {
            "from_state": State("event.button_a_up", "unavailable"),
            "to_state": _event("pressed"),
        }
    }
    assert _render(hass, evt, wait=recovery) is None

    real_second_press = {
        "trigger": {
            "from_state": _event("depressed", "2026-08-01T11:59:59+00:00"),
            "to_state": _event("pressed"),
        }
    }
    assert _render(hass, evt, wait=real_second_press) == "pressed"


async def test_slow_double_wait_ignores_recovery(
    hass: HomeAssistant, blueprint: dict
) -> None:
    """The same guard applies to the second (slow double-click) wait."""
    default_branch = blueprint["actions"][2]["default"]
    condition = default_branch[1]["choose"][0]["conditions"][0]

    recovery = {
        "trigger": {
            "from_state": State("event.button_a_up", "unavailable"),
            "to_state": _event("pressed"),
        }
    }
    assert _render(hass, condition, wait=recovery) is False

    real = {
        "trigger": {
            "from_state": _event("depressed", "2026-08-01T11:59:59+00:00"),
            "to_state": _event("pressed"),
        }
    }
    assert _render(hass, condition, wait=real) is True

    # Nothing arrived within the window -> SINGLE, not DOUBLE.
    assert _render(hass, condition, wait={"trigger": None}) is False
