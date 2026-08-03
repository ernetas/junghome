"""Cover (position / tilt) platform tests for Jung Home."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.cover import CoverDeviceClass, CoverEntityFeature
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome.const import CONF_INVERTED_COVERS, DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator
from custom_components.junghome.cover import JungHomeCover
from tests.conftest import DEVICES, _fake_run_websocket, bare_coordinator


def _cover(
    coordinator: JungHomeDataUpdateCoordinator, with_angle: bool = True
) -> JungHomeCover:
    dps = [{"id": "b-1", "type": "level", "values": [{"key": "level", "value": "30"}]}]
    if with_angle:
        dps.append(
            {"id": "b-2", "type": "angle", "values": [{"key": "angle", "value": "40"}]}
        )
    device = {
        "id": "b",
        "type": "PositionAndAngle" if with_angle else "Position",
        "label": "B",
        "datapoints": dps,
    }
    return JungHomeCover(coordinator, device, dps[0])


async def test_cover_created_and_position(
    hass: HomeAssistant, init_integration
) -> None:
    """Position is inverted: device level 30 (closed%) -> HA position 70 (open%)."""
    state = hass.states.get("cover.bedroom_blind")
    assert state is not None
    assert state.attributes["current_position"] == 70
    assert state.attributes["current_tilt_position"] == 40
    assert state.state == "open"


async def test_cover_commands(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    with (
        patch.object(coordinator, "set_level", AsyncMock()) as sl,
        patch.object(coordinator, "set_angle", AsyncMock()) as sa,
        patch.object(coordinator, "move_level", AsyncMock()) as ml,
    ):
        await hass.services.async_call(
            "cover", "open_cover", {"entity_id": "cover.bedroom_blind"}, blocking=True
        )
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": "cover.bedroom_blind"}, blocking=True
        )
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.bedroom_blind", "position": 25},
            blocking=True,
        )
        await hass.services.async_call(
            "cover", "stop_cover", {"entity_id": "cover.bedroom_blind"}, blocking=True
        )
        await hass.services.async_call(
            "cover",
            "set_cover_tilt_position",
            {"entity_id": "cover.bedroom_blind", "tilt_position": 60},
            blocking=True,
        )
    # open -> device level 0, close -> device level 100, position 25 -> device 75
    assert [c.args[1] for c in sl.call_args_list] == [0, 100, 75]
    assert sa.call_args.args[1] == 60
    assert ml.call_args.args[1] == 0  # stop


async def test_cover_inverted_awning_position(hass: HomeAssistant) -> None:
    """A flagged (awning) cover maps the gateway level straight through.

    The reporter's awning sends level 0 when physically retracted ("closed") and
    100 when extended ("open"). Inverted, HA reads those as 0/closed and 100/open
    instead of the shutter convention's 100/open and 0/closed.
    """
    coordinator = bare_coordinator(hass)

    def _awning(level: str) -> JungHomeCover:
        dps = [
            {"id": "a-1", "type": "level", "values": [{"key": "level", "value": level}]}
        ]
        device = {"id": "a", "type": "Position", "label": "Awning", "datapoints": dps}
        return JungHomeCover(coordinator, device, dps[0], inverted=True)

    retracted = _awning("0")
    assert retracted.current_cover_position == 0
    assert retracted.is_closed is True
    # An inverted cover is treated as an awning, not a blind (see cover.py).
    assert retracted.device_class == CoverDeviceClass.AWNING

    extended = _awning("100")
    assert extended.current_cover_position == 100
    assert extended.is_closed is False


async def test_cover_position_only_device_class_is_shutter(
    hass: HomeAssistant,
) -> None:
    """A position-only cover is a roller shutter, not a blind.

    No ``angle`` datapoint means no slats, so HA must not render it with the
    slat-oriented blind icon/controls.
    """
    coordinator = bare_coordinator(hass)
    dps = [{"id": "b-1", "type": "level", "values": [{"key": "level", "value": "0"}]}]
    device = {"id": "b", "type": "Position", "label": "Shutter", "datapoints": dps}
    cover = JungHomeCover(coordinator, device, dps[0])
    assert cover.device_class == CoverDeviceClass.SHUTTER


async def test_cover_with_tilt_device_class_is_blind(hass: HomeAssistant) -> None:
    """A cover with an ``angle`` datapoint drives slats, so it stays a blind."""
    coordinator = bare_coordinator(hass)
    dps = [
        {"id": "b-1", "type": "level", "values": [{"key": "level", "value": "0"}]},
        {"id": "b-2", "type": "angle", "values": [{"key": "angle", "value": "0"}]},
    ]
    device = {
        "id": "b",
        "type": "PositionAndAngle",
        "label": "Venetian",
        "datapoints": dps,
    }
    cover = JungHomeCover(coordinator, device, dps[0])
    assert cover.device_class == CoverDeviceClass.BLIND


async def test_cover_inverted_with_tilt_is_still_awning(hass: HomeAssistant) -> None:
    """The inverted (awning) flag wins over the tilt-derived blind class."""
    coordinator = bare_coordinator(hass)
    dps = [
        {"id": "b-1", "type": "level", "values": [{"key": "level", "value": "0"}]},
        {"id": "b-2", "type": "angle", "values": [{"key": "angle", "value": "0"}]},
    ]
    device = {
        "id": "b",
        "type": "PositionAndAngle",
        "label": "Awning",
        "datapoints": dps,
    }
    cover = JungHomeCover(coordinator, device, dps[0], inverted=True)
    assert cover.device_class == CoverDeviceClass.AWNING


async def test_cover_inverted_commands_pass_through(hass: HomeAssistant) -> None:
    """Inverted covers send the HA position to the gateway unchanged (no 100-x)."""
    coordinator = bare_coordinator(hass)
    dps = [{"id": "a-1", "type": "level", "values": [{"key": "level", "value": "0"}]}]
    device = {"id": "a", "type": "Position", "label": "Awning", "datapoints": dps}
    cover = JungHomeCover(coordinator, device, dps[0], inverted=True)
    with (
        patch.object(coordinator, "set_level", AsyncMock()) as sl,
        patch.object(cover, "async_write_ha_state"),
    ):
        await cover.async_open_cover()  # HA open -> device level 100
        await cover.async_close_cover()  # HA close -> device level 0
        await cover.async_set_cover_position(position=25)  # -> device level 25
    assert [c.args[1] for c in sl.call_args_list] == [100, 0, 25]


async def test_cover_inverted_via_options(hass: HomeAssistant) -> None:
    """A cover listed in entry options uses the awning mapping end to end."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        options={CONF_INVERTED_COVERS: ["bedroom_blind_001"]},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=DEVICES),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Flagged inverted: device level 30 -> HA position 30 (not the 70 a shutter
        # would show); the unflagged kitchen shade keeps the inverting convention.
        bedroom = hass.states.get("cover.bedroom_blind")
        assert bedroom.attributes["current_position"] == 30
        kitchen = hass.states.get("cover.kitchen_shade")
        assert kitchen.attributes["current_position"] == 100  # device level 0 -> open
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_options_flow_inverts_cover_after_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """Saving the inverted-covers option reloads the entry and flips the cover."""
    # Default (shutter) convention: device level 30 -> HA position 70.
    assert hass.states.get("cover.bedroom_blind").attributes["current_position"] == 70

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INVERTED_COVERS: ["bedroom_blind_001"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # The options change reloaded the entry; the blind now uses the awning mapping.
    assert hass.states.get("cover.bedroom_blind").attributes["current_position"] == 30
    assert init_integration.options[CONF_INVERTED_COVERS] == ["bedroom_blind_001"]


async def test_cover_position_only_has_no_tilt(hass: HomeAssistant, init_integration):
    """A Position (level-only) device exposes no tilt and creates a cover."""
    state = hass.states.get("cover.kitchen_shade")
    assert state is not None
    assert state.attributes.get("current_tilt_position") is None
    assert not (
        state.attributes["supported_features"] & CoverEntityFeature.SET_TILT_POSITION
    )


async def test_cover_extractors_defensive(hass: HomeAssistant) -> None:
    """Cover value extractors tolerate missing/garbage datapoints."""
    cover = _cover(bare_coordinator(hass))
    assert cover._get_position_from_datapoint(None) is None
    assert cover._get_tilt_from_datapoint(None) is None
    assert (
        cover._get_position_from_datapoint(
            {"id": "x", "values": [{"key": "level", "value": "NaN"}]}
        )
        is None
    )
    assert (
        cover._get_tilt_from_datapoint(
            {"id": "x", "values": [{"key": "angle", "value": "NaN"}]}
        )
        is None
    )
    assert cover._get_position_from_datapoint({"id": "x", "values": []}) is None
    # An inf-parsing value raises OverflowError from round(), not ValueError —
    # it must read as unknown, not escape into the listener dispatch.
    for huge in ("Infinity", "1e999", "-1e999"):
        assert (
            cover._get_position_from_datapoint(
                {"id": "x", "values": [{"key": "level", "value": huge}]}
            )
            is None
        )
        assert (
            cover._get_tilt_from_datapoint(
                {"id": "x", "values": [{"key": "angle", "value": huge}]}
            )
            is None
        )
    # Out-of-range tilt is clamped to 0..100 (mirrors the position clamp).
    assert (
        cover._get_tilt_from_datapoint(
            {"id": "x", "values": [{"key": "angle", "value": "150"}]}
        )
        == 100
    )
    assert (
        cover._get_tilt_from_datapoint(
            {"id": "x", "values": [{"key": "angle", "value": "-10"}]}
        )
        == 0
    )


async def test_cover_is_closed_none_when_position_unknown(hass: HomeAssistant) -> None:
    """is_closed is None when the level can't be read."""
    cover = _cover(bare_coordinator(hass))
    cover._position = None
    assert cover.is_closed is None


async def test_cover_tilt_commands(hass: HomeAssistant) -> None:
    """open/close tilt drive the angle command to 100/0."""
    coordinator = bare_coordinator(hass)
    cover = _cover(coordinator)
    with (
        patch.object(coordinator, "set_angle", AsyncMock()) as sa,
        patch.object(cover, "async_write_ha_state"),
    ):
        await cover.async_open_cover_tilt()
        await cover.async_close_cover_tilt()
    assert [c.args[1] for c in sa.call_args_list] == [100, 0]


async def test_cover_stop_requests_refresh(hass: HomeAssistant) -> None:
    """Stop sends level_move 0 and re-reads the real position."""
    coordinator = bare_coordinator(hass)
    cover = _cover(coordinator)
    with (
        patch.object(coordinator, "move_level", AsyncMock()) as ml,
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as rr,
    ):
        await cover.async_stop_cover()
    ml.assert_called_once()
    rr.assert_called_once()


async def test_cover_handle_update_missing_device_noops(hass: HomeAssistant) -> None:
    """Cover update writes state even when the device is gone."""
    cover = _cover(bare_coordinator(hass))  # coordinator.data is []
    with patch.object(cover, "async_write_ha_state") as write_state:
        cover._handle_coordinator_update()
    write_state.assert_called_once()


async def test_cover_set_tilt_without_angle_noops(hass: HomeAssistant) -> None:
    """_set_tilt is a no-op on a position-only cover (no angle datapoint)."""
    coordinator = bare_coordinator(hass)
    cover = _cover(coordinator, with_angle=False)
    assert cover._angle_datapoint_id is None
    with patch.object(coordinator, "set_angle", AsyncMock()) as set_angle:
        await cover._set_tilt(50)
    set_angle.assert_not_called()


async def test_cover_set_position_is_optimistic(
    hass: HomeAssistant, init_integration
) -> None:
    """set_cover_position writes the optimistic HA position immediately."""
    coordinator = init_integration.runtime_data
    with patch.object(coordinator, "set_level", AsyncMock()):
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.bedroom_blind", "position": 25},
            blocking=True,
        )
    assert hass.states.get("cover.bedroom_blind").attributes["current_position"] == 25


async def test_all_cover_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_platform,
) -> None:
    """Snapshot every cover entity: its registry entry (unique_id) and state.

    Identity here is label-derived (``stable_unique_id``), so a change to the
    slugging would silently re-key every entity. The committed ``.ambr`` pins
    the unique_ids alongside the state and attributes each platform publishes,
    turning that into a visible diff.
    """
    entry = await init_platform(Platform.COVER)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
