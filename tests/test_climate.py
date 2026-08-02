"""Climate (thermostat) platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.climate import JungHomeClimate
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from tests.conftest import bare_coordinator


def _climate(
    coordinator: JungHomeDataUpdateCoordinator,
    current_unit: str | None = "°C",
    target: str = "21.5",
    preset: str = "comfort",
    switch: str | None = None,
) -> JungHomeClimate:
    ctrl_dp = {
        "id": "t-1",
        "type": "temperature_ctrl",
        "values": [
            {"key": "temperature_ctrl", "value": target},
            {"key": "temperature_ctrl_preset", "value": preset},
        ],
    }
    dps = [ctrl_dp]
    if switch is not None:
        dps.insert(
            0,
            {
                "id": "t-0",
                "type": "switch",
                "values": [{"key": "switch", "value": switch}],
            },
        )
    if current_unit is not None:
        dps.append(
            {
                "id": "t-10",
                "type": "quantity",
                "values": [
                    {"key": "quantity", "value": "20.0"},
                    {"key": "quantity_unit", "value": current_unit},
                ],
            }
        )
    device = {"id": "t", "type": "Thermostat", "label": "T", "datapoints": dps}
    return JungHomeClimate(coordinator, device, ctrl_dp)


async def test_climate_created(hass: HomeAssistant, init_integration) -> None:
    state = hass.states.get("climate.living_room")
    assert state is not None
    assert state.attributes["temperature"] == 21.5
    assert state.attributes["preset_mode"] == "comfort"
    # Current temperature read from the sibling °C quantity datapoint.
    assert state.attributes["current_temperature"] == 20.0
    # A room regulator is heat-only: HEAT is the only mode, and the switch
    # datapoint (value "1") shows up as the momentary action instead.
    assert state.state == "heat"
    assert state.attributes["hvac_modes"] == ["heat"]
    assert state.attributes["hvac_action"] == "heating"


async def test_climate_hvac_off_is_rejected(
    hass: HomeAssistant, init_integration
) -> None:
    """The thermostat advertises no OFF mode, so HA rejects a request for one."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "off"},
            blocking=True,
        )
    assert hass.states.get("climate.living_room").state == "heat"


async def test_climate_set_hvac_mode_heat_sends_nothing(
    hass: HomeAssistant, init_integration
) -> None:
    """Re-asserting HEAT is accepted but never writes the switch datapoint."""
    coordinator = init_integration.runtime_data
    with (
        patch.object(coordinator, "turn_off_switch", AsyncMock()) as off,
        patch.object(coordinator, "turn_on_switch", AsyncMock()) as on,
    ):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "heat"},
            blocking=True,
        )
    off.assert_not_called()
    on.assert_not_called()
    assert hass.states.get("climate.living_room").state == "heat"


async def test_switch_echo_moves_hvac_action_not_state(
    hass: HomeAssistant, init_integration
) -> None:
    """Regression for #121: the regulator's own cycling must not change the state.

    The gateway's Thermostat `switch` datapoint tracks momentary heating activity
    (it flips several times an hour on its own), so it may only move
    ``hvac_action`` — mapping it to ``hvac_mode`` flooded the logbook and fired
    every ``climate.*`` state automation.
    """
    coordinator = init_integration.runtime_data
    state = hass.states.get("climate.living_room")
    assert (state.state, state.attributes["hvac_action"]) == ("heat", "heating")
    last_changed = state.last_changed

    for value, action in (("0", "idle"), ("1", "heating"), ("0", "idle")):
        coordinator._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {
                    "id": "idrtr1-000",
                    "values": [{"key": "switch", "value": value}],
                },
            }
        )
        await hass.async_block_till_done()
        state = hass.states.get("climate.living_room")
        assert state.state == "heat"
        assert state.attributes["hvac_action"] == action
        # An attribute-only change leaves last_changed alone, which is exactly
        # what keeps the logbook (and state triggers) quiet.
        assert state.last_changed == last_changed
        # Target temperature/preset unchanged by the switch echo.
        assert state.attributes["temperature"] == 21.5
        assert state.attributes["preset_mode"] == "comfort"


async def test_climate_commands(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    with (
        patch.object(coordinator, "set_temperature", AsyncMock()) as st,
        patch.object(coordinator, "set_temperature_preset", AsyncMock()) as sp,
    ):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.living_room", "temperature": 22.5},
            blocking=True,
        )
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": "climate.living_room", "preset_mode": "eco"},
            blocking=True,
        )
    assert st.call_args.args[1] == 22.5
    assert sp.call_args.args[1] == "eco"


async def test_climate_hvac_action_from_switch_value(hass: HomeAssistant) -> None:
    """Switch 1/0 maps to HEATING/IDLE; anything else leaves the action unknown."""
    coord = bare_coordinator(hass)
    heating = _climate(coord, switch="1")
    assert heating.hvac_action == HVACAction.HEATING
    assert heating.hvac_mode == HVACMode.HEAT
    assert heating.hvac_modes == [HVACMode.HEAT]
    assert _climate(coord, switch="0").hvac_action == HVACAction.IDLE
    # The gateway reports "NaN" for an offline device: unknown, not heating.
    assert _climate(coord, switch="NaN").hvac_action is None
    none = _climate(coord)  # no switch datapoint at all
    assert none._switch_datapoint_id is None
    assert none.hvac_action is None
    assert none.hvac_mode == HVACMode.HEAT
    # A switch datapoint with no `switch` key is unknown too.
    assert none._get_hvac_action_from_datapoint({"id": "x", "values": []}) is None


async def test_climate_extractors_defensive(hass: HomeAssistant) -> None:
    """Climate target/preset extractors tolerate missing/garbage datapoints."""
    climate = _climate(bare_coordinator(hass))
    assert climate._get_target_from_datapoint(None) is None
    assert (
        climate._get_target_from_datapoint(
            {"id": "x", "values": [{"key": "temperature_ctrl", "value": "abc"}]}
        )
        is None
    )
    assert climate._get_preset_from_datapoint(None) is None
    # An unknown device preset maps to None.
    assert (
        climate._get_preset_from_datapoint(
            {"id": "x", "values": [{"key": "temperature_ctrl_preset", "value": "huh"}]}
        )
        is None
    )
    # Out-of-range target clamps to 5..30; non-finite values -> None.
    assert (
        climate._get_target_from_datapoint(
            {"id": "x", "values": [{"key": "temperature_ctrl", "value": "99"}]}
        )
        == 30.0
    )
    assert (
        climate._get_target_from_datapoint(
            {"id": "x", "values": [{"key": "temperature_ctrl", "value": "-5"}]}
        )
        == 5.0
    )
    for bad in ("inf", "-inf", "nan"):
        assert (
            climate._get_target_from_datapoint(
                {"id": "x", "values": [{"key": "temperature_ctrl", "value": bad}]}
            )
            is None
        )


async def test_climate_current_temperature_paths(hass: HomeAssistant) -> None:
    """current_temperature ignores non-°C siblings and unparseable values."""
    coordinator = bare_coordinator(hass)
    # A "%" sibling is not a temperature -> None.
    assert _climate(coordinator, current_unit="%").current_temperature is None
    # No sibling quantity at all -> None.
    assert _climate(coordinator, current_unit=None).current_temperature is None
    # A °C sibling with a garbage value -> None.
    climate = _climate(coordinator)
    assert (
        climate._get_current_temperature(
            {
                "datapoints": [
                    {
                        "type": "quantity",
                        "values": [
                            {"key": "quantity", "value": "abc"},
                            {"key": "quantity_unit", "value": "°C"},
                        ],
                    }
                ]
            }
        )
        is None
    )


async def test_climate_set_temperature_without_value_noops(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    climate = _climate(coordinator)
    with patch.object(coordinator, "set_temperature", AsyncMock()) as st:
        await climate.async_set_temperature()
    st.assert_not_called()


async def test_climate_unknown_preset_noops(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    climate = _climate(coordinator)
    with patch.object(coordinator, "set_temperature_preset", AsyncMock()) as sp:
        await climate.async_set_preset_mode("nonsense")
    sp.assert_not_called()


async def test_set_hvac_mode_never_writes_the_switch_datapoint(
    hass: HomeAssistant,
) -> None:
    """set_hvac_mode is inert: the regulator has no on/off to command."""
    coordinator = bare_coordinator(hass)
    climate = _climate(coordinator, switch="1")
    with patch.object(coordinator, "send_websocket_message", AsyncMock()) as send:
        await climate.async_set_hvac_mode(HVACMode.HEAT)  # must not raise
    send.assert_not_called()
    assert climate.hvac_action == HVACAction.HEATING


async def test_climate_poll_refreshes_hvac_action(hass: HomeAssistant) -> None:
    """A REST poll (no pushed datapoint) re-reads the action from the device data."""
    coordinator = bare_coordinator(hass)
    climate = _climate(coordinator, switch="1")
    coordinator.data = [climate._device]
    assert climate.hvac_action == HVACAction.HEATING
    switch_dp = next(
        dp for dp in climate._device["datapoints"] if dp["type"] == "switch"
    )
    switch_dp["values"] = [{"key": "switch", "value": "0"}]
    with patch.object(climate, "async_write_ha_state"):
        climate._handle_coordinator_update()
    assert climate.hvac_action == HVACAction.IDLE


async def test_climate_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    climate = _climate(bare_coordinator(hass))  # coordinator.data is []
    with patch.object(climate, "async_write_ha_state") as write_state:
        climate._handle_coordinator_update()
    write_state.assert_called_once()


async def test_climate_current_temp_skips_valueless_quantity(
    hass: HomeAssistant,
) -> None:
    """A °C quantity datapoint with no value is skipped (current_temperature None)."""
    coordinator = bare_coordinator(hass)
    device = {
        "id": "t",
        "type": "Thermostat",
        "label": "T",
        "datapoints": [
            {
                "id": "t-1",
                "type": "temperature_ctrl",
                "values": [{"key": "temperature_ctrl", "value": "21"}],
            },
            {
                "id": "t-10",
                "type": "quantity",
                "values": [{"key": "quantity_unit", "value": "°C"}],  # no value key
            },
        ],
    }
    climate = JungHomeClimate(coordinator, device, device["datapoints"][0])
    assert climate.current_temperature is None


async def test_all_climate_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every climate entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.CLIMATE)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
