"""Device triggers for Jung Home rocker buttons.

Rocker buttons are already exposed as ``event`` entities, but those only show up
in the *entity* automation picker. Device triggers put "Button A — up pressed"
directly in the device's automation UI, which is where users look first for a
wall switch.

A device trigger can only attach to something on the Home Assistant bus, so the
event platform re-emits every genuine edge as ``EVENT_BUTTON_ACTION`` and the
triggers here are thin wrappers that match it (the same shape HA's own button
integrations use). The gateway reports only press/release — there is no native
single/double/hold — so those two edges are all that is offered here; gestures
are still derived in an automation via the shipped blueprint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.helpers import device_registry as dr

from .const import (
    BUTTON_DATAPOINT_TYPES,
    BUTTON_TRIGGER_SUBTYPES,
    BUTTON_TRIGGER_TYPES,
    CONF_SUBTYPE,
    DOMAIN,
    EVENT_BUTTON_ACTION,
    device_slug,
)

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

    from .coordinator import JungHomeDataUpdateCoordinator
    from .models import Device

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(BUTTON_TRIGGER_TYPES),
        vol.Required(CONF_SUBTYPE): vol.In(BUTTON_TRIGGER_SUBTYPES),
    }
)


def _gateway_device(hass: HomeAssistant, device_id: str) -> Device | None:
    """Return the gateway device backing a Home Assistant device id.

    Device identity is label-derived (see ``device_slug``), so the registry entry
    is matched back to the coordinator's device list by slug rather than by the
    gateway's volatile id.
    """
    device_entry = dr.async_get(hass).async_get(device_id)
    if device_entry is None:
        return None
    slugs = {
        identifier
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
    }
    if not slugs:
        return None
    for entry_id in device_entry.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        # `runtime_data` is only set while the entry is loaded.
        coordinator: JungHomeDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is None:
            continue
        devices: list[Device] = coordinator.data or []
        for device in devices:
            if device_slug(device) in slugs:
                return device
    return None


def _button_types(device: Device) -> list[str]:
    """Return the button sides a device exposes, in a stable order."""
    present = {
        side
        for datapoint in device.get("datapoints", [])
        if (side := BUTTON_DATAPOINT_TYPES.get(datapoint.get("type", ""))) is not None
    }
    # Order by the canonical map rather than set iteration, so the automation UI
    # lists a device's buttons the same way every time.
    return [side for side in BUTTON_DATAPOINT_TYPES.values() if side in present]


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers a Jung Home device offers."""
    device = _gateway_device(hass, device_id)
    if device is None or device.get("type") != "RockerSwitch":
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: button_type,
            CONF_SUBTYPE: subtype,
        }
        for button_type in _button_types(device)
        # Sorted so the two edges are always offered in the same order.
        for subtype in sorted(BUTTON_TRIGGER_SUBTYPES)
    ]


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate a trigger against what the device actually exposes."""
    config = TRIGGER_SCHEMA(config)
    device = _gateway_device(hass, config[CONF_DEVICE_ID])
    # An unavailable device (gateway offline, entry not loaded) can't be checked;
    # accept the config rather than break an existing automation on a restart.
    if device is None:
        return config
    if config[CONF_TYPE] not in _button_types(device):
        raise InvalidDeviceAutomationConfig(
            translation_domain=DOMAIN,
            translation_key="invalid_trigger",
            translation_placeholders={
                "trigger": f"{config[CONF_TYPE]} {config[CONF_SUBTYPE]}"
            },
        )
    return config


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger by listening for the matching button event."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_BUTTON_ACTION,
            CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                CONF_TYPE: config[CONF_TYPE],
                CONF_SUBTYPE: config[CONF_SUBTYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
