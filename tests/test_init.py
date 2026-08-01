"""Integration setup / entity / lifecycle tests for Jung Home."""

import copy
from unittest.mock import AsyncMock, patch

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import CONF_HOST, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome import (
    STALE_DEVICE_PRUNE_MISSES,
    async_unload_entry,
)
from custom_components.junghome.const import (
    DATA_AREA_ASSIGNED,
    DOMAIN,
    device_slug,
)
from custom_components.junghome.coordinator import (
    JungHomeDataUpdateCoordinator,
    _parse_color_temp_range,
)
from custom_components.junghome.diagnostics import (
    _support_summary,
    async_get_config_entry_diagnostics,
)
from tests.conftest import DEVICES, _fake_run_websocket


def _bare_coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    return coordinator


async def test_all_entity_types_created(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get("light.hall_light") is not None
    assert hass.states.get("light.strip").state == "on"
    assert hass.states.get("switch.boiler").state == "on"
    assert hass.states.get("sensor.boiler_power").state == "5.0"
    # Unknown unit ("?") -> unitless MEASUREMENT sensor (no unit) -> value floated.
    assert hass.states.get("sensor.boiler_status").state == "42.0"
    assert hass.states.get("switch.button_a_status_led") is not None
    assert hass.states.get("event.button_a_up") is not None
    assert hass.states.get("event.button_a_down") is not None


async def test_state_update_via_websocket(
    hass: HomeAssistant, init_integration
) -> None:
    coordinator = init_integration.runtime_data
    coordinator._handle_websocket_message(
        {
            "type": "datapoint",
            "data": {"id": "idlight1-001", "values": [{"key": "switch", "value": "1"}]},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("light.hall_light").state == "on"
    # Unknown datapoint id, a groups/scenes list frame, and a non-dict data frame
    # are all handled gracefully and must not disturb existing state.
    coordinator._handle_websocket_message(
        {"type": "datapoint", "data": {"id": "nope", "values": []}}
    )
    coordinator._handle_websocket_message({"type": "groups", "data": [{"id": "g"}]})
    coordinator._handle_websocket_message({"type": "datapoint", "data": "weird"})
    await hass.async_block_till_done()
    # Prior state survives the no-op frames.
    assert hass.states.get("light.hall_light").state == "on"


async def test_diagnostics(hass: HomeAssistant, init_integration) -> None:
    coordinator = init_integration.runtime_data
    coordinator.scenes = [{"id": "s1", "label": "Movie"}]
    coordinator.groups = [{"id": "g1", "name": "Living room"}]
    coordinator.ws_frame_log.append('{"type":"version","data":"1.5.0"}')
    coordinator.ws_last_frame_by_type = {"functions": '{"type":"functions"}'}
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["device_count"] == len(DEVICES)
    assert diag["gateway_version"] == "1.5.0"
    # The live-link flag is surfaced so a dump explains stale-looking state.
    assert diag["ws_connected"] is True
    assert diag["entry"]["data"][CONF_TOKEN] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_HOST] == "**REDACTED**"
    # Scenes are a separate coordinator data category, surfaced for debugging.
    assert diag["scene_count"] == 1
    assert diag["scenes"] == [{"id": "s1", "label": "Movie"}]
    # Groups, raw WebSocket frames and the support summary are surfaced too.
    assert diag["group_count"] == 1
    assert diag["groups"] == [{"id": "g1", "name": "Living room"}]
    assert '{"type":"version"' in diag["recent_websocket_frames"][0]
    assert diag["latest_websocket_frame_by_type"]["functions"] == '{"type":"functions"}'
    # Every type the fixture uses is handled, so nothing is flagged unsupported.
    assert diag["support_summary"]["unhandled_function_types"] == []
    assert diag["support_summary"]["unhandled_datapoint_types"] == []
    assert diag["support_summary"]["function_types"]["ColorLight"] >= 1


def test_support_summary_flags_unhandled_types() -> None:
    """An unknown function/datapoint type is surfaced in the support summary."""
    devices = [
        {
            "id": "a",
            "type": "OnOff",
            "label": "A",
            "datapoints": [{"id": "a-1", "type": "switch", "values": []}],
        },
        {
            "id": "b",
            "type": "FutureGizmo",
            "label": "B",
            "datapoints": [{"id": "b-1", "type": "mystery", "values": []}],
        },
    ]
    summary = _support_summary(devices)
    assert summary["unhandled_function_types"] == ["FutureGizmo"]
    assert summary["unhandled_datapoint_types"] == ["mystery"]
    assert summary["function_types"]["OnOff"] == 1
    assert summary["datapoint_types"]["switch"] == 1


async def test_stale_device_pruned(hass: HomeAssistant) -> None:
    """A device absent for enough consecutive polls is removed, not on the first.

    Pruning is debounced so a single partial poll (e.g. right after a reload)
    doesn't destroy a live device's entities; the device must be missing for
    STALE_DEVICE_PRUNE_MISSES polls before it goes.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    stale = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ghost_device")}
    )
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

        coordinator = entry.runtime_data
        # Not pruned on the first pass — the debounce rides out a partial poll.
        assert dev_reg.async_get(stale.id) is not None
        # Absent across the threshold of further polls -> pruned.
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            coordinator.async_set_updated_data(coordinator.data)
            await hass.async_block_till_done()
        assert dev_reg.async_get(stale.id) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_transiently_missing_device_survives_and_resets(
    hass: HomeAssistant,
) -> None:
    """A device that reappears before the threshold is not pruned, and resets.

    Reproduces the reload race: a live device drops out of a single poll, then
    comes back. It must survive, and its miss counter must reset so a later
    single miss doesn't tip it over a stale count accumulated earlier.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
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

        coordinator = entry.runtime_data
        dev_reg = dr.async_get(hass)
        blind_slug = device_slug(next(d for d in DEVICES if d["id"] == "idblind1"))
        blind = dev_reg.async_get_device(identifiers={(DOMAIN, blind_slug)})
        assert blind is not None

        partial = [d for d in coordinator.data if d["id"] != "idblind1"]
        full = list(coordinator.data)

        # Miss it for one fewer poll than the threshold — must still be present.
        for _ in range(STALE_DEVICE_PRUNE_MISSES - 1):
            coordinator.async_set_updated_data(partial)
            await hass.async_block_till_done()
            assert dev_reg.async_get_device(identifiers={(DOMAIN, blind_slug)})
        # It reappears -> counter resets.
        coordinator.async_set_updated_data(full)
        await hass.async_block_till_done()
        # A single later miss must not prune it (the reset worked).
        coordinator.async_set_updated_data(partial)
        await hass.async_block_till_done()
        assert dev_reg.async_get_device(identifiers={(DOMAIN, blind_slug)})

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_pruned_device_is_readded_when_it_returns(hass: HomeAssistant) -> None:
    """A device pruned after going absent is re-created if it reappears.

    The per-platform discovery keeps a `known` set of unique_ids it has added, to
    avoid duplicate adds. When the pruner removes a device it must also drop that
    device's ids from those sets (via `forget_device_unique_ids`), or the id would
    stay `known` and permanently suppress the re-add — leaving the returning
    device with no entities until an entry reload.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
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

        coordinator = entry.runtime_data
        ent_reg = er.async_get(hass)
        light_uid = "hall_light_001"
        assert (
            ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, light_uid) is not None
        )

        partial = [d for d in coordinator.data if d["id"] != "idlight1"]
        full = list(coordinator.data)

        # Absent past the threshold -> the device (and its light entity) is pruned.
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            coordinator.async_set_updated_data(partial)
            await hass.async_block_till_done()
        assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, light_uid) is None

        # The gateway reports it again -> the entity is re-created, not suppressed.
        coordinator.async_set_updated_data(full)
        await hass.async_block_till_done()
        assert (
            ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, light_uid) is not None
        )

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_legacy_unique_id_migrated(hass: HomeAssistant) -> None:
    """An old id-based entity is re-pointed to the label-based stable id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    # Pre-create a light entity under the old volatile-id unique_id scheme.
    ent_reg.async_get_or_create(
        Platform.LIGHT,
        DOMAIN,
        "idlight1_idlight1-001",
        config_entry=entry,
    )
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

    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001")
        is not None
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_not_marked_done_on_failure(hass: HomeAssistant) -> None:
    """If migration raises at the top level, the entry isn't flagged migrated.

    Leaving ``stable_ids_migrated`` unset means setup retries the migration on the
    next load instead of silently skipping it forever.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
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
        patch(
            "custom_components.junghome.er.async_entries_for_config_entry",
            side_effect=RuntimeError("boom"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Setup still succeeds, but the migration flag must NOT be set (so it retries).
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_host_change_triggers_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """A stored host change reloads the entry (the coordinator caches the host)."""
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: "9.9.9.9"}
        )
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)


async def test_token_only_change_no_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """A token-only update (host unchanged) must not trigger a reload."""
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: "newtok"}
        )
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_datapoint_set_change_reloads_entry(hass: HomeAssistant) -> None:
    """A cover that gains a slat `angle` datapoint after creation reloads the entry.

    Regression guard for the reporter who lost tilt on 1.2.2: a cover's tilt
    support is frozen at construction and discovery is add-only, so when the
    gateway re-enumerates the device and its `angle` datapoint appears on a later
    poll (function type flips Position -> PositionAndAngle), the entry must reload
    to rebuild the entity with tilt instead of leaving it position-only forever.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    position_only = {
        "id": "idcov",
        "type": "Position",
        "label": "Rolladen",
        "datapoints": [
            {
                "id": "idcov-1",
                "type": "level",
                "values": [{"key": "level", "value": "30"}],
            },
        ],
    }
    with_angle = {
        "id": "idcov",
        "type": "PositionAndAngle",
        "label": "Rolladen",
        "datapoints": [
            {
                "id": "idcov-1",
                "type": "level",
                "values": [{"key": "level", "value": "30"}],
            },
            {
                "id": "idcov-2",
                "type": "angle",
                "values": [{"key": "angle", "value": "40"}],
            },
        ],
    }
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[position_only]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        # Built without a slat datapoint: position-only, no tilt exposed.
        state = hass.states.get("cover.rolladen")
        assert state is not None
        assert not (
            state.attributes["supported_features"]
            & CoverEntityFeature.SET_TILT_POSITION
        )

        # The gateway now re-enumerates the same cover WITH a slat angle datapoint;
        # the capability fingerprint changes, so a reload is scheduled to rebuild it.
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            coordinator.async_set_updated_data([with_angle])
            await hass.async_block_till_done()
        reload.assert_called_once_with(entry.entry_id)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_value_only_change_does_not_reload_entry(
    hass: HomeAssistant, init_integration
) -> None:
    """A normal value push (same datapoint *types*) must never reload the entry.

    The capability fingerprint keys on datapoint types, not values, so routine
    state updates don't trip the capability-change reload into a reload storm.
    """
    coordinator = init_integration.runtime_data
    devices = copy.deepcopy(coordinator.data)
    # Change only a value on the blind's level datapoint (types unchanged).
    for device in devices:
        if device["id"] == "idblind1":
            device["datapoints"][0]["values"][0]["value"] = "80"
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_set_updated_data(devices)
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_entity_availability_tracks_connection(
    hass: HomeAssistant, init_integration
) -> None:
    """available splits by control path: WS for controllables, REST otherwise.

    Read-only entities (sensor, event) follow ``last_update_success`` — the REST
    poll / WebSocket-push signal — and never key off ``ws_connected``, so a
    stale-True socket flag can't keep them "available" with frozen values after
    the gateway has gone unreachable (issue #120). Controllable entities (light,
    socket, LED switch) additionally require a live WebSocket, because commands
    only travel over it: with the socket down they read unavailable rather than
    accept commands that would silently fail.
    """
    coordinator = init_integration.runtime_data
    controllable = ("light.strip", "switch.boiler", "switch.button_a_status_led")
    read_only = ("sensor.boiler_power", "event.button_a_up")

    def states(entities: tuple[str, ...]) -> set[str]:
        return {
            "unavailable" if hass.states.get(e).state == "unavailable" else "available"
            for e in entities
        }

    # Both signals healthy -> everything available.
    coordinator.ws_connected = True
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(controllable) == {"available"}
    assert states(read_only) == {"available"}

    # WS down but REST still polling: controllables can't be commanded, so they
    # go unavailable; read-only entities keep reporting their polled state.
    coordinator.ws_connected = False
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(controllable) == {"unavailable"}
    assert states(read_only) == {"available"}

    # Gateway gone: REST poll failing -> everything unavailable, even if the
    # socket flag is still stale-True (a half-open WS must not mask it).
    coordinator.ws_connected = True
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(controllable) == {"unavailable"}
    assert states(read_only) == {"unavailable"}

    # Fully recovered -> everything available again.
    coordinator.ws_connected = True
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(controllable) == {"available"}
    assert states(read_only) == {"available"}


async def test_websocket_message_guard_without_data(hass: HomeAssistant) -> None:
    """A datapoint frame arriving before the first refresh must not raise.

    The ``for device in self.data or []`` guard tolerates ``data`` being None.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = None
    # Must not raise despite data being None.
    coordinator._handle_websocket_message(
        {"type": "datapoint", "data": {"id": "x", "values": []}}
    )


async def test_failed_platform_unload_still_stops_coordinator(
    hass: HomeAssistant,
) -> None:
    """A failed platform unload must still stop the coordinator's WS task."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
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

    coordinator = entry.runtime_data
    with (
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        ),
        patch.object(coordinator, "stop", AsyncMock(wraps=coordinator.stop)) as stop,
    ):
        # Call the unload handler directly so the failed-platform-unload path is
        # exercised without leaving the entry half-torn-down in HA's state machine.
        result = await async_unload_entry(hass, entry)
        await hass.async_block_till_done()
    stop.assert_awaited()
    assert coordinator._ws_task is None
    # Unload reports failure (platforms didn't unload) but cleanup still happened.
    assert result is False
    # Tear down cleanly now that the WS task is stopped.
    await coordinator.async_shutdown()


async def _setup_with_registry(hass: HomeAssistant, prepare) -> MockConfigEntry:
    """Create an entry, let `prepare(entry)` seed the registries, then set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    prepare(entry)
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
    return entry


async def test_migration_repoints_device_identifier(hass: HomeAssistant) -> None:
    """A device registered under a volatile gateway id is re-pointed to the slug."""
    dev_reg = dr.async_get(hass)
    holder: dict[str, str] = {}

    def prepare(entry: MockConfigEntry) -> None:
        # Pre-create a device keyed on the volatile gateway id "idlight1".
        dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "idlight1")}
        )
        holder["id"] = dev.id

    entry = await _setup_with_registry(hass, prepare)
    # The migration rewrote the identifier to device_slug("Hall Light").
    migrated = dev_reg.async_get(holder["id"])
    assert migrated is not None
    assert (DOMAIN, device_slug(DEVICES[0])) in migrated.identifiers
    assert (DOMAIN, "idlight1") not in migrated.identifiers
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_removes_colliding_stable_id(hass: HomeAssistant) -> None:
    """An old id-based entity is dropped when its stable id already exists."""
    ent_reg = er.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        # A leftover entity already under the stable id...
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "hall_light_001", config_entry=entry
        )
        # ...and the old volatile-id entity that should migrate onto it.
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "idlight1_idlight1-001", config_entry=entry
        )

    entry = await _setup_with_registry(hass, prepare)
    # The colliding old entity was removed rather than renamed onto the existing id.
    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "idlight1_idlight1-001")
        is None
    )
    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001")
        is not None
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_per_item_error_leaves_flag_unset(
    hass: HomeAssistant,
) -> None:
    """A per-entity migration failure is isolated but still blocks the done flag."""
    ent_reg = er.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "idlight1_idlight1-001", config_entry=entry
        )

    with patch(
        "homeassistant.helpers.entity_registry.EntityRegistry.async_update_entity",
        side_effect=RuntimeError("boom"),
    ):
        entry = await _setup_with_registry(hass, prepare)

    # Setup succeeds, but the per-item error means the migration isn't marked done.
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_malformed_cover_and_thermostat_skipped(hass: HomeAssistant) -> None:
    """A Position with no level / Thermostat with no temperature_ctrl is skipped."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="m", data={CONF_HOST: "h", CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    devices = [
        {"id": "badc", "type": "Position", "label": "Bad Cover", "datapoints": []},
        {"id": "badt", "type": "Thermostat", "label": "Bad Therm", "datapoints": []},
    ]
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=devices),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get("cover.bad_cover") is None
    assert hass.states.get("climate.bad_therm") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_device_repoint_error_isolated(hass: HomeAssistant) -> None:
    """A failure re-pointing a device identifier is isolated and blocks the done flag."""
    dev_reg = dr.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        # A device under the old volatile gateway id, so the migration tries to
        # re-point it (the only path that calls async_update_device with
        # new_identifiers).
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "idlight1")}
        )

    orig = dr.DeviceRegistry.async_update_device

    def boom(self, device_id, **kwargs):
        if "new_identifiers" in kwargs:
            raise RuntimeError("boom")  # only the migration re-point fails
        return orig(self, device_id, **kwargs)

    with patch.object(dr.DeviceRegistry, "async_update_device", boom):
        entry = await _setup_with_registry(hass, prepare)

    # Setup succeeds, but the per-device error means migration isn't marked done.
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_area_for_device_resolves_group_name(hass: HomeAssistant) -> None:
    """area_for_device maps parent_groups ids to the group's name (or label)."""
    coordinator = _bare_coordinator(hass)
    coordinator.groups = [{"id": "g1", "name": "Kitchen"}]
    assert (
        coordinator.area_for_device({"id": "d", "parent_groups": ["g1"]}) == "Kitchen"
    )
    # No parent groups, or an id that doesn't resolve -> no area.
    assert coordinator.area_for_device({"id": "d"}) is None
    assert coordinator.area_for_device({"id": "d", "parent_groups": ["gX"]}) is None
    # Falls back to `label` when the group has no `name`.
    coordinator.groups = [{"id": "g2", "label": "Bathroom"}]
    assert (
        coordinator.area_for_device({"id": "d", "parent_groups": ["g2"]}) == "Bathroom"
    )


async def test_color_temp_range_for_device_reads_group_metadata(
    hass: HomeAssistant,
) -> None:
    """color_temp_range_for_device resolves the group's advertised Kelvin range."""
    coordinator = _bare_coordinator(hass)
    device = {"id": "d", "parent_groups": ["g1"]}
    # Both plausible encodings are accepted, and values may be strings.
    coordinator.groups = [
        {"id": "g1", "color_temperature_range": {"min": 2700, "max": 6500}}
    ]
    assert coordinator.color_temp_range_for_device(device) == (2700, 6500)
    coordinator.groups = [{"id": "g1", "color_temperature_range": ["2700", "6500"]}]
    assert coordinator.color_temp_range_for_device(device) == (2700, 6500)
    # No parent groups / an id that doesn't resolve / a group without a range.
    assert coordinator.color_temp_range_for_device({"id": "d"}) is None
    assert (
        coordinator.color_temp_range_for_device({"id": "d", "parent_groups": ["gX"]})
        is None
    )
    coordinator.groups = [{"id": "g1", "name": "Living room"}]
    assert coordinator.color_temp_range_for_device(device) is None
    # The first parent group advertising a usable range wins.
    coordinator.groups = [
        {"id": "g0", "name": "no range here"},
        {"id": "g1", "color_temperature_range": {"min": 2200, "max": 4000}},
    ]
    assert coordinator.color_temp_range_for_device(
        {"id": "d", "parent_groups": ["g0", "g1"]}
    ) == (2200, 4000)


def test_parse_color_temp_range_rejects_bad_payloads() -> None:
    """The range parser only trusts a well-formed, plausible pair of numbers."""
    assert _parse_color_temp_range({"min": "2700", "max": 6500.4}) == (2700, 6500)
    assert _parse_color_temp_range([2700, 6500]) == (2700, 6500)
    for raw in (
        None,
        "2700-6500",
        42,
        {},  # no keys at all
        {"min": 2700},  # half a range
        {"min": "warm", "max": "cool"},  # non-numeric
        {"min": None, "max": 6500},
        {"min": {"nested": 1}, "max": 6500},  # not a scalar
        {"min": True, "max": 6500},  # bool is an int subclass, but not a Kelvin
        {"min": 6500, "max": 2700},  # reversed
        {"min": 4000, "max": 4000},  # zero-width
        {"min": 10, "max": 6500},  # implausibly low
        {"min": 2700, "max": 999999},  # implausibly high
        {"min": float("nan"), "max": float("nan")},  # json.loads accepts NaN
        {"min": 2700, "max": float("inf")},  # ...and Infinity
        [2700],  # wrong arity
        [2000, 4000, 6500],
    ):
        assert _parse_color_temp_range(raw) is None, raw


async def test_async_fetch_groups_is_best_effort(hass: HomeAssistant) -> None:
    """A groups fetch failure leaves groups empty and never raises."""
    coordinator = _bare_coordinator(hass)
    with patch.object(
        coordinator, "_fetch_groups_from_api", AsyncMock(side_effect=RuntimeError)
    ):
        await coordinator.async_fetch_groups()
    assert coordinator.groups == []
    with patch.object(
        coordinator,
        "_fetch_groups_from_api",
        AsyncMock(return_value=[{"id": "g", "name": "X"}]),
    ):
        await coordinator.async_fetch_groups()
    assert coordinator.groups == [{"id": "g", "name": "X"}]


def _grouped_lamp() -> dict:
    return {
        "id": "idlamp",
        "type": "OnOff",
        "label": "Sofa Lamp",
        "parent_groups": ["grp-living"],
        "datapoints": [
            {
                "id": "idlamp-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            }
        ],
    }


async def test_device_area_assigned_from_group(hass: HomeAssistant) -> None:
    """A device in a gateway group is placed in the matching HA area."""
    groups = [{"id": "grp-living", "name": "Living Room"}]
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_grouped_lamp()]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(return_value=groups),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="1.2.3.4",
            data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        area_reg = ar.async_get(hass)
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry is not None
        assert device_entry.area_id is not None
        assert area_reg.async_get_area(device_entry.area_id).name == "Living Room"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_device_area_does_not_override_user_choice(hass: HomeAssistant) -> None:
    """A device the user already placed in an area keeps it."""
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    office = area_reg.async_get_or_create("Office")

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    # Pre-create the device already assigned to "Office", as if the user moved it.
    existing = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "sofa_lamp")}
    )
    dev_reg.async_update_device(existing.id, area_id=office.id)

    groups = [{"id": "grp-living", "name": "Living Room"}]
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_grouped_lamp()]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(return_value=groups),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
    # Still in Office — the group suggestion must not move a user-placed device.
    assert area_reg.async_get_area(device_entry.area_id).name == "Office"


async def _setup_grouped_lamp(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up with one grouped lamp in the "Living Room" group."""
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_grouped_lamp()]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(return_value=[{"id": "grp-living", "name": "Living Room"}]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_device_area_not_reassigned_after_user_clears_it(
    hass: HomeAssistant,
) -> None:
    """Clearing a device's area on purpose sticks; it is not re-placed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    await _setup_grouped_lamp(hass, entry)

    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
    assert device_entry.area_id is not None  # placed on first setup
    # The device is recorded as already considered, so it is never re-placed.
    assert "sofa_lamp" in entry.data[DATA_AREA_ASSIGNED]

    # The user deliberately removes the device from every area...
    dev_reg.async_update_device(device_entry.id, area_id=None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # ...and it stays cleared across a full reload.
    await _setup_grouped_lamp(hass, entry)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
    assert device_entry.area_id is None


async def test_device_area_reuses_existing_area_by_name(hass: HomeAssistant) -> None:
    """A group matching an existing area links to it instead of duplicating it."""
    area_reg = ar.async_get(hass)
    existing = area_reg.async_get_or_create("Living Room")
    before = len(area_reg.areas)

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    await _setup_grouped_lamp(hass, entry)

    device_entry = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "sofa_lamp")}
    )
    assert device_entry.area_id == existing.id
    assert len(area_reg.areas) == before  # no duplicate area was created


def _ungrouped_lamp() -> dict:
    """A lamp the gateway reports in no room (empty ``parent_groups``)."""
    return {
        "id": "idlamp2",
        "type": "OnOff",
        "label": "Hall Lamp",
        "parent_groups": [],
        "datapoints": [
            {
                "id": "idlamp2-001",
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            }
        ],
    }


async def test_ungrouped_device_is_left_unplaced_and_reconsidered(
    hass: HomeAssistant,
) -> None:
    """A device with no resolvable room is not placed, and stays reconsiderable.

    It must not be recorded as considered: a room could still arrive later (over
    the WebSocket), and only then should the device be placed.
    """
    groups = [{"id": "grp-living", "name": "Living Room"}]
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_grouped_lamp(), _ungrouped_lamp()]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(return_value=groups),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="1.2.3.4",
            data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        grouped = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        ungrouped = dev_reg.async_get_device(identifiers={(DOMAIN, "hall_lamp")})
        assert grouped.area_id is not None  # placed in its room
        assert ungrouped.area_id is None  # no room -> not placed

        # The grouped device is settled; the ungrouped one is NOT recorded, so a
        # room arriving later still gets a chance to place it.
        assigned = entry.data[DATA_AREA_ASSIGNED]
        assert "sofa_lamp" in assigned
        assert "hall_lamp" not in assigned

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_device_area_self_heals_when_groups_arrive_later(
    hass: HomeAssistant,
) -> None:
    """If the REST groups fetch yields nothing, a later WebSocket delivery of the
    groups still places the device on the next refresh."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=[_grouped_lamp()]),
        ),
        # The pre-setup REST groups fetch comes back empty (e.g. it failed).
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry.area_id is None  # no rooms known yet -> not placed
        assert "sofa_lamp" not in entry.data.get(DATA_AREA_ASSIGNED, [])

        # The WebSocket handshake later delivers the groups; the next coordinator
        # update runs the placement again and now resolves the room.
        coordinator = entry.runtime_data
        coordinator.groups = [{"id": "grp-living", "name": "Living Room"}]
        coordinator.async_set_updated_data([_grouped_lamp()])
        await hass.async_block_till_done()

        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry.area_id is not None
        area_reg = ar.async_get(hass)
        assert area_reg.async_get_area(device_entry.area_id).name == "Living Room"
        assert "sofa_lamp" in entry.data[DATA_AREA_ASSIGNED]

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_gateway_connectivity_sensor(
    hass: HomeAssistant, init_integration
) -> None:
    """The gateway connectivity sensor is on while the WebSocket is connected."""
    coordinator = init_integration.runtime_data
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "gateway_1.2.3.4_connectivity"
    )
    assert entity_id is not None
    # The real _run_websocket refreshes the coordinator right after connecting
    # (the test's fake socket only parks), so drive one update to mirror that.
    assert coordinator.ws_connected is True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["device_class"] == "connectivity"


async def test_gateway_connectivity_reflects_disconnect(
    hass: HomeAssistant, init_integration
) -> None:
    """On a WebSocket drop the sensor reads off but stays available."""
    coordinator = init_integration.runtime_data
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "gateway_1.2.3.4_connectivity"
    )
    coordinator.ws_connected = False
    # REST poll still succeeds, so the entity must not go unavailable — it must
    # report the disconnect as "off".
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"


async def test_gateway_device_not_pruned(hass: HomeAssistant, init_integration) -> None:
    """The synthetic gateway device survives the stale-device prune."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, "gateway_1.2.3.4")})
    assert device is not None
    assert device.name == "JUNG HOME Gateway"


async def test_devices_linked_to_gateway_hub(
    hass: HomeAssistant, init_integration
) -> None:
    """Every function device hangs off the synthetic gateway (hub) via via_device."""
    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_device(identifiers={(DOMAIN, "gateway_1.2.3.4")})
    assert hub is not None
    light = dev_reg.async_get_device(identifiers={(DOMAIN, "hall_light")})
    assert light is not None
    assert light.via_device_id == hub.id


async def test_notify_websocket_closed_skips_during_teardown(
    hass: HomeAssistant,
) -> None:
    """The disconnect notify fires on a live drop but is muted while stopping."""
    coordinator = _bare_coordinator(hass)
    with patch.object(coordinator, "async_update_listeners") as notify:
        # A genuine drop notifies listeners so the connectivity sensor flips off.
        coordinator._notify_websocket_closed()
        assert notify.call_count == 1
        # During stop()/unload the platforms are already going away, so the
        # guard suppresses the redundant notification.
        coordinator._closing = True
        coordinator._notify_websocket_closed()
        assert notify.call_count == 1
