"""Climate (thermostat) platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.climate import JungHomeClimate
from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


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
    # The switch datapoint (value "1") maps to HVAC HEAT, with OFF available.
    assert state.state == "heat"
    assert set(state.attributes["hvac_modes"]) == {"off", "heat"}


async def test_climate_hvac_on_off(hass: HomeAssistant, init_integration) -> None:
    """HVAC OFF / HEAT drives the thermostat's switch datapoint."""
    coordinator = init_integration.runtime_data
    assert hass.states.get("climate.living_room").state == "heat"
    with (
        patch.object(coordinator, "turn_off_switch", AsyncMock()) as off,
        patch.object(coordinator, "turn_on_switch", AsyncMock()) as on,
    ):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "off"},
            blocking=True,
        )
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "heat"},
            blocking=True,
        )
    off.assert_called_once_with("idrtr1-000")
    on.assert_called_once_with("idrtr1-000")


async def test_climate_hvac_mode_follows_switch_echo(
    hass: HomeAssistant, init_integration
) -> None:
    """A switch=0 echo flips the thermostat to OFF without touching target/preset."""
    coordinator = init_integration.runtime_data
    assert hass.states.get("climate.living_room").state == "heat"
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idrtr1-000", "values": [{"key": "switch", "value": "0"}]},
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get("climate.living_room")
    assert state.state == "off"
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


async def test_climate_hvac_mode_from_switch_value(hass: HomeAssistant) -> None:
    """Switch 1/0 maps to HEAT/OFF; a thermostat without a switch stays HEAT-only."""
    coord = _bare_coordinator(hass)
    on = _climate(coord, switch="1")
    assert on._attr_hvac_mode == HVACMode.HEAT
    assert set(on._attr_hvac_modes) == {HVACMode.OFF, HVACMode.HEAT}
    off = _climate(coord, switch="0")
    assert off._attr_hvac_mode == HVACMode.OFF
    none = _climate(coord)  # no switch datapoint
    assert none._switch_datapoint_id is None
    assert none._attr_hvac_modes == [HVACMode.HEAT]


async def test_climate_extractors_defensive(hass: HomeAssistant) -> None:
    """Climate target/preset extractors tolerate missing/garbage datapoints."""
    climate = _climate(_bare_coordinator(hass))
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
    coordinator = _bare_coordinator(hass)
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
    coordinator = _bare_coordinator(hass)
    climate = _climate(coordinator)
    with patch.object(coordinator, "set_temperature", AsyncMock()) as st:
        await climate.async_set_temperature()
    st.assert_not_called()


async def test_climate_unknown_preset_noops(hass: HomeAssistant) -> None:
    coordinator = _bare_coordinator(hass)
    climate = _climate(coordinator)
    with patch.object(coordinator, "set_temperature_preset", AsyncMock()) as sp:
        await climate.async_set_preset_mode("nonsense")
    sp.assert_not_called()


async def test_switchless_thermostat_hvac_mode_is_noop(hass: HomeAssistant) -> None:
    """A thermostat without a switch datapoint accepts set_hvac_mode but sends nothing."""
    coordinator = _bare_coordinator(hass)
    climate = _climate(coordinator)  # no switch datapoint
    with patch.object(coordinator, "send_websocket_message", AsyncMock()) as send:
        await climate.async_set_hvac_mode(HVACMode.HEAT)  # must not raise
    send.assert_not_called()


async def test_climate_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    climate = _climate(_bare_coordinator(hass))  # coordinator.data is []
    with patch.object(climate, "async_write_ha_state") as write_state:
        climate._handle_coordinator_update()
    write_state.assert_called_once()


async def test_climate_current_temp_skips_valueless_quantity(
    hass: HomeAssistant,
) -> None:
    """A °C quantity datapoint with no value is skipped (current_temperature None)."""
    coordinator = _bare_coordinator(hass)
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
