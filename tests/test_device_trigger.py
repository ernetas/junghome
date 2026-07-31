"""Device-trigger tests for Jung Home rocker buttons."""

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_get_device_automations

from custom_components.junghome.const import CONF_SUBTYPE, DOMAIN
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


async def test_get_triggers_lists_each_button_side_and_edge(
    hass: HomeAssistant, init_integration
) -> None:
    """A rocker offers both its sides, each with a pressed/released edge."""
    device_id = _device_id(hass, "button_a")
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    ours = [t for t in triggers if t.get(CONF_DOMAIN) == DOMAIN]
    # The fixture's Button A exposes up_request + down_request (and a status LED,
    # which is not a button), so: 2 sides x 2 edges.
    assert {(t[CONF_TYPE], t[CONF_SUBTYPE]) for t in ours} == {
        ("up", "pressed"),
        ("up", "depressed"),
        ("down", "pressed"),
        ("down", "depressed"),
    }


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
    device_id = _device_id(hass, "button_a")
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
                        CONF_TYPE: "up",
                        CONF_SUBTYPE: "pressed",
                    },
                    "action": {
                        "event": "junghome_test_fired",
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()

    hass.bus.async_listen("junghome_test_fired", lambda e: fired.append("x"))

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
