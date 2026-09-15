"""Device-trigger tests for Jung Home rocker buttons."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_HOST,
    CONF_PLATFORM,
    CONF_TOKEN,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_get_device_automations,
)

from custom_components.junghome.const import (
    BUTTON_TRIGGER_SUBTYPES,
    CONF_SUBTYPE,
    DOMAIN,
)
from custom_components.junghome.device_trigger import async_validate_trigger_config


def _device_id(hass: HomeAssistant, slug: str) -> str:
    """Return the HA device id for a Jung Home device slug."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, slug)})
    assert device is not None
    return device.id


def _press(coordinator, value: str = "1") -> None:
    """Push an up_request edge for the fixture's rocker (Button A)."""
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idrock1-00c",
                "values": [{"key": "up_request", "value": value}],
            },
        }
    )


async def _automation_on(hass: HomeAssistant, device_id: str, side: str, subtype: str):
    """Set up one automation on a device trigger; return its fire log."""
    fired: list[str] = []
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: device_id,
                        CONF_TYPE: side,
                        CONF_SUBTYPE: subtype,
                    },
                    "action": {"event": "junghome_test_fired"},
                }
            ]
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_listen("junghome_test_fired", lambda e: fired.append("x"))
    return fired


async def test_get_triggers_lists_each_button_side_edge_and_gesture(
    hass: HomeAssistant, init_integration
) -> None:
    """A rocker offers both sides, each with the two edges and three gestures."""
    device_id = _device_id(hass, "button_a")
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    ours = [
        (t[CONF_TYPE], t[CONF_SUBTYPE])
        for t in triggers
        if t.get(CONF_DOMAIN) == DOMAIN
    ]
    # The fixture's Button A exposes up_request + down_request (and a status LED,
    # which is not a button), so: 2 sides x 5 subtypes — listed in the fixed
    # order of the constant (edges first), which is what the UI shows.
    assert ours == [
        (side, subtype)
        for side in ("up", "down")
        for subtype in ("pressed", "depressed", "click", "hold_start", "hold_end")
    ]
    assert BUTTON_TRIGGER_SUBTYPES == (
        "pressed",
        "depressed",
        "click",
        "hold_start",
        "hold_end",
    )


async def test_click_trigger_fires_once_per_doubled_tap(
    hass: HomeAssistant, init_integration, freezer: FrozenDateTimeFactory
) -> None:
    """A ``click`` device trigger sees one tap once, copy included.

    The event platform's duplicate suppression is what makes device triggers
    usable on current device firmware: the tap's second pair fires nothing.
    """
    fired = await _automation_on(hass, _device_id(hass, "button_a"), "up", "click")
    coordinator = init_integration.runtime_data
    for value, gap in (("1", 0), ("0", 0.4), ("1", 0.5), ("0", 0.4)):
        freezer.tick(timedelta(seconds=gap))
        async_fire_time_changed(hass)
        _press(coordinator, value)
        await hass.async_block_till_done()
    assert len(fired) == 1


async def test_hold_start_trigger_fires_from_the_timer(
    hass: HomeAssistant, init_integration, freezer: FrozenDateTimeFactory
) -> None:
    """A ``hold_start`` device trigger runs at the hold threshold, before release."""
    fired = await _automation_on(hass, _device_id(hass, "button_a"), "up", "hold_start")
    _press(init_integration.runtime_data, "1")
    await hass.async_block_till_done()
    assert fired == []
    freezer.tick(timedelta(seconds=1.0))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_non_button_device_offers_no_triggers(
    hass: HomeAssistant, init_integration
) -> None:
    """A device that is not a RockerSwitch contributes no button triggers."""
    device_id = _device_id(hass, "hall_light")
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    assert [t for t in triggers if t.get(CONF_DOMAIN) == DOMAIN] == []


async def test_trigger_fires_on_matching_press(
    hass: HomeAssistant, init_integration
) -> None:
    """An automation using the device trigger runs when that edge is pushed."""
    fired = await _automation_on(hass, _device_id(hass, "button_a"), "up", "pressed")
    coordinator = init_integration.runtime_data
    _press(coordinator, "1")
    await hass.async_block_till_done()
    assert len(fired) == 1

    # The opposite edge must NOT run this automation.
    _press(coordinator, "0")
    await hass.async_block_till_done()
    assert len(fired) == 1


async def test_invalid_trigger_is_rejected(
    hass: HomeAssistant, init_integration
) -> None:
    """A button side the device does not expose fails validation."""
    device_id = _device_id(hass, "button_a")
    # The fixture's rocker has no `trigger_request` datapoint, so "press" is not
    # a valid side for it.
    with pytest.raises(InvalidDeviceAutomationConfig):
        await async_validate_trigger_config(
            hass,
            {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_TYPE: "press",
                CONF_SUBTYPE: "pressed",
            },
        )


async def test_validate_accepts_config_for_unknown_device(
    hass: HomeAssistant, init_integration
) -> None:
    """An unresolvable device is accepted, so a restart can't break automations."""
    config = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: "does-not-exist",
        CONF_TYPE: "up",
        CONF_SUBTYPE: "pressed",
    }
    assert await async_validate_trigger_config(hass, config) == config


async def test_no_triggers_for_foreign_device(
    hass: HomeAssistant, init_integration
) -> None:
    """A registry device with no junghome identifier resolves to no triggers.

    ``_gateway_device`` must bail out before touching any config entry when
    the device carries only another integration's identifiers.
    """
    foreign_entry = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={("other_domain", "some-device")},
    )
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, foreign_entry.id
    )
    assert [t for t in triggers if t.get(CONF_DOMAIN) == DOMAIN] == []


async def test_no_triggers_when_entry_not_loaded(hass: HomeAssistant) -> None:
    """A junghome device whose entry is not loaded resolves to no triggers.

    ``runtime_data`` only exists while the entry is loaded; the lookup must
    skip such entries (and any non-junghome entries sharing the device)
    rather than raise, so the automation UI degrades to an empty list.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="9.9.9.9",
        data={CONF_HOST: "9.9.9.9", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)  # never set up: no runtime_data
    other = MockConfigEntry(domain="other_domain", unique_id="x")
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "button_b")},
    )
    registry.async_update_device(device.id, add_config_entry_id=other.entry_id)
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    assert [t for t in triggers if t.get(CONF_DOMAIN) == DOMAIN] == []


async def test_no_triggers_when_device_missing_from_poll(
    hass: HomeAssistant, init_integration
) -> None:
    """A registered device the gateway no longer reports offers no triggers.

    Covers the loop fall-through in ``_gateway_device``: the entry is loaded,
    but no device in the coordinator's data matches the slug (e.g. it was
    relabelled in the app and awaits pruning).
    """
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "vanished_rocker")},
    )
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    assert [t for t in triggers if t.get(CONF_DOMAIN) == DOMAIN] == []
