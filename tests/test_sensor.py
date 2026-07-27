"""Numeric sensor platform tests for Jung Home."""

from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.sensor import JungHomeQuantity


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


async def test_sensor_native_value_non_numeric_returns_none(
    hass: HomeAssistant, init_integration
) -> None:
    """A non-numeric value on a unitless MEASUREMENT sensor yields native_value None."""
    coordinator = init_integration.runtime_data
    # sensor.boiler_status is the unknown-unit ("?") MEASUREMENT sensor.
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {
                "id": "idsock1-099",
                "values": [{"key": "quantity", "value": "not-a-number"}],
            },
        }
    )
    await hass.async_block_till_done()
    # float("not-a-number") -> ValueError -> native_value None -> "unknown".
    assert hass.states.get("sensor.boiler_status").state == "unknown"


async def test_sensor_value_extractor_defensive(hass: HomeAssistant) -> None:
    """Sensor helpers return None for a missing value / None state."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "s", "type": "Socket", "label": "S", "datapoints": []}
    dp = {
        "id": "s-1",
        "values": [
            {"key": "quantity", "value": "5"},
            {"key": "quantity_label", "value": "P"},
            {"key": "quantity_unit", "value": "W"},
        ],
    }
    q = JungHomeQuantity(coordinator, device, dp, "P", "W")
    # No "quantity" key -> None.
    assert q._get_value_from_datapoint({"id": "x", "values": []}) is None
    # native_value is None when the stored value is None.
    q._value = None
    assert q.native_value is None
    # NaN/inf parse through float() but must not reach a numeric sensor's state.
    for bad in ("nan", "inf", "-inf"):
        q._value = bad
        assert q.native_value is None


async def test_measurement_sensor_created(
    hass: HomeAssistant, init_integration
) -> None:
    """A Measurement function's quantity surfaces as a sensor (lux -> illuminance)."""
    state = hass.states.get("sensor.hallway_sensor_illuminance")
    assert state is not None
    assert state.state == "120.0"
    assert state.attributes["unit_of_measurement"] == "lx"
    assert state.attributes["device_class"] == "illuminance"


async def test_sensor_native_value_rejects_nan(hass: HomeAssistant) -> None:
    """A NaN reading on a numeric sensor yields None (never pollutes statistics)."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "s", "type": "Socket", "label": "S", "datapoints": []}
    dp = {"id": "s-1", "values": [{"key": "quantity", "value": "nan"}]}
    # An unknown unit makes a numeric MEASUREMENT sensor; NaN must read as None.
    quantity = JungHomeQuantity(coordinator, device, dp, "Status", "?")
    assert quantity.native_value is None
