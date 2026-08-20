"""Integration setup / entity / lifecycle tests for Jung Home."""

import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_TOKEN,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.junghome import (
    STALE_DEVICE_PRUNE_MISSES,
    async_remove_config_entry_device,
    async_unload_entry,
)
from custom_components.junghome.const import (
    CONF_INVERTED_COVERS,
    CONF_POLL_INTERVAL,
    DATA_AREA_ASSIGNED,
    DOMAIN,
    device_slug,
    duplicate_slugs,
    gateway_device_id,
)
from custom_components.junghome.coordinator import (
    JungHomeDataUpdateCoordinator,
    _parse_color_temp_range,
)
from custom_components.junghome.diagnostics import (
    _scrub,
    _secrets,
    _support_summary,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.junghome.event import JungHomeEventEntity
from tests.conftest import (
    DEVICES,
    PRISTINE_DEVICES,
    _fake_run_websocket,
    bare_coordinator,
)


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
    # Options are dumped too: the poll interval changes observable timing by up
    # to 60x, so a report about "stale states" or "a device took hours to
    # disappear" is unreadable without it.
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_POLL_INTERVAL: 300, CONF_INVERTED_COVERS: ["awning_level"]},
    )
    await hass.async_block_till_done()
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
    assert diag["entry"]["options"] == {
        "poll_interval": 300,
        "inverted_covers": ["awning_level"],
    }
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


async def test_device_diagnostics(hass: HomeAssistant, init_integration) -> None:
    """A per-device dump narrows to one device and keeps gateway context."""
    coordinator = init_integration.runtime_data
    coordinator.gateway_version = "1.5.0"
    dev_reg = dr.async_get(hass)
    slug = device_slug(DEVICES[0])
    device = dev_reg.async_get_device(identifiers={(DOMAIN, slug)})
    assert device is not None

    diag = await async_get_device_diagnostics(hass, init_integration, device)

    assert diag["identifiers"] == [slug]
    assert diag["device"] is not None
    assert diag["device"]["label"] == DEVICES[0]["label"]
    # Gateway context travels with the device so the dump is interpretable.
    assert diag["gateway_version"] == "1.5.0"
    assert diag["ws_connected"] is True


async def test_device_diagnostics_redacts_credentials(
    hass: HomeAssistant, init_integration
) -> None:
    """Per-device dumps get the same redaction as the entry dump.

    They are pasted into public issues just as often, so a gateway payload that
    ever carries a token/host must not leak through the narrower report.
    """
    coordinator = init_integration.runtime_data
    devices = copy.deepcopy(DEVICES)
    devices[0]["token"] = "super-secret"
    devices[0]["host"] = "192.168.1.50"
    coordinator.async_set_updated_data(devices)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, device_slug(DEVICES[0]))})
    diag = await async_get_device_diagnostics(hass, init_integration, device)

    assert diag["device"]["token"] == "**REDACTED**"
    assert diag["device"]["host"] == "**REDACTED**"


async def test_device_diagnostics_when_gateway_no_longer_reports_it(
    hass: HomeAssistant, init_integration
) -> None:
    """A device the gateway has stopped reporting yields device=None.

    That is the useful signal: it is on its way to being pruned rather than
    simply absent from the dump for an unexplained reason.
    """
    dev_reg = dr.async_get(hass)
    ghost = dev_reg.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "ghost_device")},
    )

    diag = await async_get_device_diagnostics(hass, init_integration, ghost)

    assert diag["identifiers"] == ["ghost_device"]
    assert diag["device"] is None
    # Gateway context is still present, so the report is not empty.
    assert diag["ws_connected"] is True


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


async def test_non_poll_dispatches_do_not_consume_the_prune_debounce(
    hass: HomeAssistant,
) -> None:
    """Only real device-list adoptions count toward the prune threshold.

    Scenes broadcasts and the WS-drop notification also run every coordinator
    listener (``async_update_listeners`` with the SAME device list), and
    counting those as "polls" shrank the documented
    ``STALE_DEVICE_PRUNE_MISSES``-poll debounce — a WebSocket flapping while a
    device was transiently absent from one poll could burn the whole window in
    seconds. The miss counter must advance only when the coordinator adopts a
    fresh list (``data_generation``), and pruning at the threshold must still
    work.
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

        # Twice the threshold in bare re-dispatches — what a scene-editing
        # session or a WS drop/reconnect cycle produces. The ghost is absent
        # from every one of them, yet none may count as a missed poll.
        for _ in range(2 * STALE_DEVICE_PRUNE_MISSES):
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
        assert dev_reg.async_get(stale.id) is not None

        # Real adoptions still prune at the documented threshold.
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
    """An expected migration failure leaves the entry unflagged but set up.

    Leaving ``stable_ids_migrated`` unset means setup retries the migration on the
    next load instead of silently skipping it forever. ValueError stands in for
    Home Assistant rejecting a registry write (a unique_id already claimed) —
    one of the failures the migration is designed to absorb.
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
            side_effect=ValueError("boom"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Setup still succeeds, but the migration flag must NOT be set (so it retries).
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_migration_unexpected_error_fails_setup(hass: HomeAssistant) -> None:
    """An unanticipated migration error must not be absorbed as a skipped item.

    The migration rewrites the registry, so an unknown fault is a bug worth
    surfacing rather than folding into the "one bad item, carry on" path where
    it would be indistinguishable from malformed gateway data.
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

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.data.get("stable_ids_migrated") is not True


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


async def test_token_only_change_reloads_entry(
    hass: HomeAssistant, init_integration
) -> None:
    """A token-only update MUST reload — it is how reauth takes effect.

    The reauth flow stores the fresh token with ``async_update_and_abort`` and
    deliberately schedules no reload of its own (pairing a reloading flow helper
    with an update listener is deprecated in HA 2026.6 and raises from 2026.12).
    The coordinator caches its credentials at construction, so without this arm
    the new token would sit in ``entry.data`` while the coordinator kept using
    the rejected one — an endless reauth loop.
    """
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: "newtok"}
        )
        await hass.async_block_till_done()
    reload.assert_called_once()


async def test_unchanged_update_does_not_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """An update that changes nothing the coordinator cached must not reload."""
    entry = init_integration
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(entry, data={**entry.data})
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


async def test_capability_watch_runs_only_on_device_list_adoptions(
    hass: HomeAssistant, init_integration
) -> None:
    """A push-style dispatch (same device list) must not re-fingerprint devices.

    Datapoint types can only change when a fresh device list is adopted (a REST
    poll or a `functions` broadcast, both of which advance `data_generation`),
    so the watcher early-outs on every dispatch that re-presents the same list
    — per-datapoint pushes, scenes broadcasts, the WS-drop notification —
    instead of recomputing every device's signature on each of them. Proven by
    mutating the stored list in place: a plain listener dispatch must not see
    the change (the guard skipped the walk), while the next adoption of that
    same list must still schedule the capability reload.
    """
    coordinator = init_integration.runtime_data
    for device in coordinator.data:
        if device["id"] == "idblind1":
            device["datapoints"].append(
                {"id": "idblind1-x", "type": "color_temperature", "values": []}
            )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
    reload.assert_not_called()
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
    reload.assert_called_once_with(init_integration.entry_id)


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
    # The unmatched push schedules a (real) refresh; keep it off the network.
    with patch.object(
        coordinator, "_fetch_devices_from_api", AsyncMock(return_value=[])
    ):
        # Must not raise despite data being None.
        coordinator._handle_websocket_message(
            {"type": "datapoint", "data": {"id": "x", "values": []}}
        )
        await hass.async_block_till_done()
    # Cancel the refresh debouncer's cooldown timer the unmatched push armed.
    await coordinator.async_shutdown()


async def test_unmatched_push_warns_once_and_requests_one_refresh(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown datapoint id warns once and triggers one discovery refresh.

    A device added in the app pushes before the next poll lists it: the first
    sighting of its id requests a (debounced) refresh so discovery lands in
    seconds, and warns once — a 1 Hz push used to warn 60 times a minute.
    Later frames for the same unknown id are DEBUG-only and must not amplify
    polling.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, entry
    )
    coordinator.data = []
    push = {"type": "datapoint", "data": {"id": "idnew-001", "values": []}}
    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
        coordinator._handle_websocket_message(push)
        coordinator._handle_websocket_message(push)
        coordinator._handle_websocket_message(push)
        await hass.async_block_till_done()
    assert refresh.await_count == 1
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "idnew-001" in r.getMessage()
    ]
    assert len(warnings) == 1


async def test_functions_broadcast_discovers_a_new_device(
    hass: HomeAssistant, init_integration
) -> None:
    """A pushed ``functions`` list is adopted like a poll: new devices appear.

    The gateway broadcasts the authoritative device list on change; consuming
    it makes device add/remove push-driven instead of waiting up to 60 s for
    the next REST poll.
    """
    coordinator = init_integration.runtime_data
    assert hass.states.get("light.new_lamp") is None
    devices = [
        *copy.deepcopy(PRISTINE_DEVICES),
        {
            "id": "idnewlamp",
            "type": "OnOff",
            "label": "New Lamp",
            "datapoints": [
                {
                    "id": "idnewlamp-001",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "1"}],
                }
            ],
        },
    ]
    coordinator._handle_websocket_message({"type": "functions", "data": devices})
    await hass.async_block_till_done()
    state = hass.states.get("light.new_lamp")
    assert state is not None
    assert state.state == "on"


async def test_functions_broadcast_does_not_fire_button_events(
    hass: HomeAssistant, init_integration
) -> None:
    """Adopting a functions broadcast must never read as a button edge.

    The broadcast re-presents every datapoint value (including a rocker's
    ``up_request``) without the per-push marker, exactly like a REST re-read —
    an event fired here would be a phantom press.
    """
    coordinator = init_integration.runtime_data
    devices = copy.deepcopy(PRISTINE_DEVICES)
    for device in devices:
        if device["id"] == "idrock1":
            device["datapoints"][0]["values"] = [{"key": "up_request", "value": "1"}]
    with patch.object(JungHomeEventEntity, "_trigger_event") as trigger:
        coordinator._handle_websocket_message({"type": "functions", "data": devices})
        await hass.async_block_till_done()
    trigger.assert_not_called()


async def test_ha_shutdown_stops_the_coordinator(
    hass: HomeAssistant, init_integration
) -> None:
    """A full HA shutdown runs the orderly WebSocket teardown, not just unload.

    The `EVENT_HOMEASSISTANT_STOP` listener was registered but its body never
    ran in any test; this drives it and asserts the coordinator actually
    stopped (closing flag set, WS task cancelled, socket cleared).
    """
    coordinator = init_integration.runtime_data
    assert coordinator._closing is False
    assert coordinator._ws_task is not None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert coordinator._closing is True
    assert coordinator._ws_task is None
    assert coordinator.websocket is None


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
    """A per-entity migration failure is isolated but still blocks the done flag.

    The failure must be one of `_MIGRATION_ERRORS` — anything else is a defect
    and is deliberately left to propagate — so this raises the realistic registry
    error (`ValueError`, the unique_id already being taken) for exactly one
    entity and asserts the *rest* of the batch still migrated.
    """
    ent_reg = er.async_get(hass)

    def prepare(entry: MockConfigEntry) -> None:
        # Two entities under old volatile ids; only the first one's rename fails.
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "idlight1_idlight1-001", config_entry=entry
        )
        ent_reg.async_get_or_create(
            Platform.LIGHT, DOMAIN, "iddim1_iddim1-001", config_entry=entry
        )

    original = er.EntityRegistry.async_update_entity

    def boom(self, entity_id: str, **kwargs: object) -> object:
        if kwargs.get("new_unique_id") == "hall_light_001":
            raise ValueError("unique_id already registered")
        return original(self, entity_id, **kwargs)

    with patch.object(er.EntityRegistry, "async_update_entity", boom):
        entry = await _setup_with_registry(hass, prepare)

    # The error is isolated: setup still completes...
    assert entry.state is ConfigEntryState.LOADED
    # ...the failing entity keeps its old id...
    assert (
        ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "idlight1_idlight1-001")
        is not None
    )
    # ...the rest of the batch still migrated...
    assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "dimmer_001") is not None
    # ...and the one-shot flag stays unset so the next setup retries.
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
    """A failure re-pointing a device identifier is isolated and blocks the done flag.

    Raises `ValueError` — the realistic registry failure, and one of
    `_MIGRATION_ERRORS` — for a single device, and asserts the other device in
    the same batch was still re-pointed.
    """
    dev_reg = dr.async_get(hass)
    holder: dict[str, str] = {}

    def prepare(entry: MockConfigEntry) -> None:
        # Two devices under old volatile gateway ids, so the migration tries to
        # re-point both (the only path that calls async_update_device with
        # new_identifiers). Only the first one fails.
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "idlight1")}
        )
        holder["other"] = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "iddim1")}
        ).id

    orig = dr.DeviceRegistry.async_update_device

    def boom(self, device_id, **kwargs):
        if (DOMAIN, "hall_light") in kwargs.get("new_identifiers", ()):
            raise ValueError("identifier already claimed")
        return orig(self, device_id, **kwargs)

    with patch.object(dr.DeviceRegistry, "async_update_device", boom):
        entry = await _setup_with_registry(hass, prepare)

    # The error is isolated: setup still completes...
    assert entry.state is ConfigEntryState.LOADED
    # ...the other device in the same batch was still re-pointed to its slug...
    other = dev_reg.async_get(holder["other"])
    assert other is not None
    assert (DOMAIN, "dimmer") in other.identifiers
    # ...and the one-shot flag stays unset so the next setup retries.
    assert entry.data.get("stable_ids_migrated") is not True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_area_for_device_resolves_group_name(hass: HomeAssistant) -> None:
    """area_for_device maps parent_groups ids to the group's name (or label)."""
    coordinator = bare_coordinator(hass)
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


async def test_area_for_device_tolerates_malformed_gateway_json(
    hass: HomeAssistant,
) -> None:
    """Malformed groups/parents must not raise out of the _assign_areas listener.

    Same hardening contract as ``color_temp_range_for_device``: an unhashable
    group id or parent entry (a list, a dict) raised ``TypeError`` from dict
    construction/lookup inside a coordinator listener. HA contains a raising
    listener (each callback runs in its own try/except; the rest still
    dispatch), but that logs a full traceback for merely-malformed gateway
    data on every refresh, and area assignment silently stops for the device.
    """
    coordinator = bare_coordinator(hass)
    coordinator.groups = [
        "not-a-dict",
        {"id": ["unhashable"], "name": "Broken"},
        {"id": True, "name": "Bool"},
        {"id": "g1", "name": "Kitchen"},
        {"id": "g1", "name": "Duplicate"},  # first occurrence wins
        {"id": "g2"},  # no name/label -> unusable
    ]
    device = {"id": "d", "parent_groups": [["unhashable"], {"also": "bad"}, "g1"]}
    assert coordinator.area_for_device(device) == "Kitchen"
    # A non-list parent_groups is rejected wholesale, not iterated as chars.
    assert coordinator.area_for_device({"id": "d", "parent_groups": "g1"}) is None
    # A parent resolving to a nameless group keeps looking / returns None.
    assert coordinator.area_for_device({"id": "d", "parent_groups": ["g2"]}) is None


async def test_color_temp_range_for_device_reads_group_metadata(
    hass: HomeAssistant,
) -> None:
    """color_temp_range_for_device resolves the group's advertised Kelvin range."""
    coordinator = bare_coordinator(hass)
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


def test_parse_color_temp_range_survives_unrepresentable_numbers() -> None:
    """A huge JSON integer is rejected, not raised on.

    `json.loads` parses integer literals at arbitrary precision, so a frame can
    hand us an `int` that `float()` cannot represent — which raises
    `OverflowError`, not `ValueError`. This escaped the parser and propagated out
    of `JungHomeLight.__init__`, so a single malformed frame removed every light
    entity while the config entry still reported itself loaded.
    """
    huge = json.loads("9" * 400)  # an int, not a float
    assert isinstance(huge, int)
    with pytest.raises(OverflowError):
        float(huge)
    for raw in (
        {"min": 2700, "max": huge},
        {"min": huge, "max": 6500},
        [huge, 6500],
        [2700, huge],
        {"min": -huge, "max": huge},
    ):
        assert _parse_color_temp_range(raw) is None, raw
    # A huge *string* is representable (it becomes inf) and is rejected by the
    # finiteness guard instead.
    assert _parse_color_temp_range({"min": 2700, "max": "9" * 400}) is None


def test_color_temp_range_for_device_survives_malformed_groups(
    hass: HomeAssistant,
) -> None:
    """Non-scalar ids and a non-list parent_groups are rejected, not raised on.

    Both would otherwise raise `TypeError` out of a constructor: an unhashable
    id blows up the lookup dict, and a non-iterable `parent_groups` blows up the
    loop.
    """
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, MockConfigEntry(domain=DOMAIN)
    )
    good = {"id": "g1", "color_temperature_range": {"min": 2700, "max": 4000}}
    coordinator.groups = [good]
    for device in (
        {"id": "d", "parent_groups": 5},  # not iterable
        {"id": "d", "parent_groups": "g1"},  # a bare string, not a list
        {"id": "d", "parent_groups": [{"id": "g1"}]},  # unhashable member
        {"id": "d", "parent_groups": [["g1"]]},
        {"id": "d", "parent_groups": [None]},
    ):
        assert coordinator.color_temp_range_for_device(device) is None, device
    # Unhashable / malformed group entries are skipped rather than raising.
    coordinator.groups = [{"id": ["g1"]}, "not a dict", None, good]  # type: ignore[list-item]
    assert coordinator.color_temp_range_for_device(
        {"id": "d", "parent_groups": ["g1"]}
    ) == (2700, 4000)


def test_color_temp_range_for_device_first_group_wins(hass: HomeAssistant) -> None:
    """When two parent groups disagree, the first in `parent_groups` wins.

    Order-dependent by construction, which is only tolerable because nothing
    consumes the result yet. Pinned so a future caller finds the behaviour
    documented rather than discovering it.
    """
    coordinator = JungHomeDataUpdateCoordinator(
        hass, {"host": "h", "token": "t"}, MockConfigEntry(domain=DOMAIN)
    )
    coordinator.groups = [
        {"id": "g1", "color_temperature_range": {"min": 2700, "max": 4000}},
        {"id": "g2", "color_temperature_range": {"min": 2200, "max": 6500}},
    ]
    assert coordinator.color_temp_range_for_device(
        {"id": "d", "parent_groups": ["g1", "g2"]}
    ) == (2700, 4000)
    assert coordinator.color_temp_range_for_device(
        {"id": "d", "parent_groups": ["g2", "g1"]}
    ) == (2200, 6500)
    # Duplicate ids resolve to the first occurrence, matching the docstring.
    coordinator.groups = [
        {"id": "g1", "color_temperature_range": {"min": 2700, "max": 4000}},
        {"id": "g1", "color_temperature_range": {"min": 2200, "max": 6500}},
    ]
    assert coordinator.color_temp_range_for_device(
        {"id": "d", "parent_groups": ["g1"]}
    ) == (2700, 4000)


async def test_async_fetch_groups_is_best_effort(hass: HomeAssistant) -> None:
    """A gateway-side groups failure leaves groups empty and never raises."""
    coordinator = bare_coordinator(hass)
    with patch.object(
        coordinator,
        "_fetch_groups_from_api",
        AsyncMock(side_effect=aiohttp.ClientError),
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


async def test_async_fetch_groups_lets_unexpected_errors_surface(
    hass: HomeAssistant,
) -> None:
    """Best-effort covers gateway failures, not bugs in our own code.

    A RuntimeError here is not the gateway being unreachable; it is a defect,
    and swallowing it would hide it behind a debug-level log line forever.
    """
    coordinator = bare_coordinator(hass)
    with (
        patch.object(
            coordinator, "_fetch_groups_from_api", AsyncMock(side_effect=RuntimeError)
        ),
        pytest.raises(RuntimeError),
    ):
        await coordinator.async_fetch_groups()


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


async def test_area_placement_runs_only_on_device_list_adoptions(
    hass: HomeAssistant,
) -> None:
    """A push-style dispatch (same device list) must not re-run area placement.

    Every per-datapoint push notifies coordinator listeners, and the placement
    walk is O(devices + registry) — so it early-outs unless a fresh device list
    was adopted (`data_generation` advanced). Rooms that arrive *between*
    adoptions (here: the WebSocket delivering groups the REST fetch missed)
    therefore take effect on the next adoption, not on the next value push.
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
            AsyncMock(return_value=[_grouped_lamp()]),
        ),
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

        coordinator = entry.runtime_data
        coordinator.groups = [{"id": "grp-living", "name": "Living Room"}]
        # A push-style dispatch re-presents the same device list: the guard
        # skips the walk, so the lamp is NOT placed yet.
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry.area_id is None

        # The next adoption (a poll or a `functions` broadcast) places it.
        coordinator.async_set_updated_data([_grouped_lamp()])
        await hass.async_block_till_done()
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry.area_id is not None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_runtime_added_device_is_placed_on_the_next_dispatch(
    hass: HomeAssistant,
) -> None:
    """A device arriving in an adoption is placed on the NEXT dispatch.

    No walk the adoption itself triggers can place the new device, because its
    registry entry does not exist yet during any of them: the adoption
    dispatch runs the assigner before the platforms' scheduled entity-add task,
    and the refresh HA core requests while adding the entity
    (``update_before_add`` → ``async_device_update`` →
    ``async_request_refresh``) runs BEFORE the device is registered. The
    assigner therefore leaves the generation unrecorded once, so the very next
    dispatch — typically the new device's own first value pushes, seconds
    away — retries the walk and places it, instead of waiting out the 60 s
    poll.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    devices_mock = AsyncMock(return_value=[])
    with (
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", devices_mock
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
        coordinator = entry.runtime_data

        # The gateway now reports a new device: a `functions` broadcast adopts
        # it, and the entity-add-triggered refresh (which polls) agrees. Both
        # of those adoptions walk before the device's registry entry exists —
        # only the armed retry, consumed by a later dispatch, can place it.
        devices_mock.return_value = [_grouped_lamp()]
        coordinator.async_set_updated_data([_grouped_lamp()])
        await hass.async_block_till_done()

        # A push-style dispatch (no new adoption) is all it takes.
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert device_entry is not None
        assert device_entry.area_id is not None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_area_placement_retry_is_bounded_for_unplaceable_devices(
    hass: HomeAssistant,
) -> None:
    """The follow-up walk happens exactly once per adoption.

    A grouped device no platform supports never gets a registry entry, so its
    placement can never succeed. The retry must not keep the per-push walk
    open for it: one follow-up dispatch re-walks, then the guard settles until
    the next adoption.
    """
    unsupported = {
        "id": "idmyst",
        "type": "Mystery",
        "label": "Mystery Box",
        "parent_groups": ["grp-living"],
        "datapoints": [],
    }
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
            AsyncMock(return_value=[]),
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
        coordinator = entry.runtime_data

        # No platform claims a "Mystery" device, so no entity-add refresh
        # follows this adoption and the retry stays armed for the next
        # dispatch.
        coordinator.async_set_updated_data([unsupported])
        await hass.async_block_till_done()

        # The walk calls `area_for_device` once per device, so counting those
        # calls counts the walks.
        with patch.object(
            coordinator, "area_for_device", wraps=coordinator.area_for_device
        ) as walked:
            coordinator.async_update_listeners()  # the one follow-up walk
            await hass.async_block_till_done()
            assert walked.call_count == 1
            coordinator.async_update_listeners()  # settled: no walk
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            assert walked.call_count == 1

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_area_placement_skips_colliding_slugs(
    hass: HomeAssistant,
) -> None:
    """A slug shared by two devices is never placed; unique slugs still are.

    Two labels that slug identically share ONE registry device, so which room
    the shared slug would map to depends on device-list order — an arbitrary
    choice between the two devices' rooms. The assigner skips colliding slugs
    (mirroring the capability watcher's `duplicate_slugs` guard) instead of
    placing the shared device in whichever room happened to come last.
    """
    lamp_a = _grouped_lamp()
    lamp_b = _grouped_lamp()
    lamp_b["id"] = "idlamp2"
    lamp_b["label"] = "Sofa-Lamp"  # slugify -> sofa_lamp, same as lamp_a
    lamp_b["parent_groups"] = ["grp-kitchen"]
    desk = _grouped_lamp()
    desk["id"] = "idlamp3"
    desk["label"] = "Desk Lamp"
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
            AsyncMock(return_value=[lamp_a, lamp_b, desk]),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_groups_from_api",
            AsyncMock(
                return_value=[
                    {"id": "grp-living", "name": "Living Room"},
                    {"id": "grp-kitchen", "name": "Kitchen"},
                ]
            ),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        shared = dev_reg.async_get_device(identifiers={(DOMAIN, "sofa_lamp")})
        assert shared is not None
        assert shared.area_id is None  # ambiguous room: never auto-placed
        placed = dev_reg.async_get_device(identifiers={(DOMAIN, "desk_lamp")})
        assert placed.area_id is not None  # unique slugs are unaffected

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
    coordinator = bare_coordinator(hass)
    with patch.object(coordinator, "async_update_listeners") as notify:
        # A genuine drop notifies listeners so the connectivity sensor flips off.
        coordinator._notify_websocket_closed()
        assert notify.call_count == 1
        # During stop()/unload the platforms are already going away, so the
        # guard suppresses the redundant notification.
        coordinator._closing = True
        coordinator._notify_websocket_closed()
        assert notify.call_count == 1


async def test_diagnostics_scrubs_host_from_free_form_text(
    hass: HomeAssistant, init_integration
) -> None:
    """The host must not survive inside `last_error` or a raw WebSocket frame.

    `async_redact_data` masks values by key; these are free-form strings where no
    key exists to match. An aiohttp connect failure reads "Cannot connect to host
    <host>:443 ...", which re-leaked exactly what TO_REDACT removes from
    entry.data — and diagnostics get pasted into public issues.
    """
    coordinator = init_integration.runtime_data
    host = init_integration.data[CONF_HOST]
    token = init_integration.data[CONF_TOKEN]
    coordinator.last_error = f"Cannot connect to host {host}:443 ssl:True [timeout]"
    coordinator.ws_frame_log.append(f'{{"type":"x","url":"wss://{host}/ws"}}')
    coordinator.ws_last_frame_by_type = {"version": f'{{"url":"wss://{host}/ws"}}'}

    diag = await async_get_config_entry_diagnostics(hass, init_integration)

    assert host not in diag["last_error"]
    assert "**REDACTED**" in diag["last_error"]
    # The surrounding context survives, so the dump is still debuggable.
    assert "Cannot connect to host" in diag["last_error"]
    assert host not in diag["recent_websocket_frames"][-1]
    assert host not in diag["latest_websocket_frame_by_type"]["version"]
    # The fixture's 3-character token is below the sweep threshold; see
    # test_scrub_masks_secrets_but_ignores_tiny_ones for a realistic one.
    assert token == "tok"


async def test_device_diagnostics_scrubs_host_from_last_error(
    hass: HomeAssistant, init_integration
) -> None:
    """The per-device dump gets the same treatment as the entry dump."""
    coordinator = init_integration.runtime_data
    host = init_integration.data[CONF_HOST]
    coordinator.last_error = f"Cannot connect to host {host}:443"

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, device_slug(DEVICES[0]))})
    diag = await async_get_device_diagnostics(hass, init_integration, device)

    assert host not in diag["last_error"]


def test_scrub_masks_secrets_but_ignores_tiny_ones() -> None:
    """`_scrub` masks realistic secrets and leaves absurdly short ones alone.

    A real gateway token is long; a one- or two-character value would match
    everywhere and shred the very output being debugged, so short values are
    deliberately not swept.
    """
    token = "eyJhbGciOiJIUzI1NiJ9.secret-token-value"
    host = "junghome-0022d1059602.local"
    secrets = _secrets(
        SimpleNamespace(data={CONF_TOKEN: token, CONF_HOST: host})  # type: ignore[arg-type]
    )

    text = f"GET https://{host}/api with token={token} failed"
    scrubbed = _scrub(text, secrets)
    assert token not in scrubbed
    assert host not in scrubbed
    assert scrubbed.startswith("GET https://**REDACTED**/api")

    # Case-insensitive: an error may echo a differently-cased hostname.
    assert host not in _scrub(f"Cannot connect to host {host.upper()}:443", secrets)

    # Below the threshold -> not swept, so a dump isn't shredded by a stub value.
    assert _secrets(SimpleNamespace(data={CONF_TOKEN: "t", CONF_HOST: "h"})) == []  # type: ignore[arg-type]
    assert _scrub("the host is h", []) == "the host is h"

    # Nothing to do on empty input.
    assert _scrub(None, secrets) is None


def _duplicate_label_devices() -> list[dict]:
    """Two devices whose labels slug to the same value."""
    devices = copy.deepcopy(DEVICES)[:2]
    devices[0]["label"] = "Hall Light"
    devices[1]["label"] = "Hall-Light"  # slugify -> hall_light as well
    return devices


async def test_duplicate_labels_do_not_cause_a_reload_loop(
    hass: HomeAssistant,
) -> None:
    """Two devices sharing a slug must not schedule any capability reload.

    The capability watcher keys its fingerprints by slug, so the second device
    used to overwrite the first's entry inside a single pass; the comparison then
    saw a change on every refresh and scheduled a reload, and each reload rebuilt
    the watcher with an empty map — an endless loop from two devices called
    "Lamp".
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        # Skip the migration path so this isolates the capability watcher.
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok", "stable_ids_migrated": True},
    )
    entry.add_to_hass(hass)
    reloads = 0

    def _count(entry_id: str) -> None:
        nonlocal reloads
        reloads += 1

    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=_duplicate_label_devices()),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
        patch.object(hass.config_entries, "async_schedule_reload", _count),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # A second refresh must stay quiet too.
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert reloads == 0, f"duplicate labels scheduled {reloads} reload(s)"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_duplicate_labels_still_complete_the_migration(
    hass: HomeAssistant,
) -> None:
    """A colliding device identifier is skipped, not left retrying forever.

    `DeviceIdentifierCollisionError` is a HomeAssistantError, so it was caught as
    a per-item failure and withheld the one-shot flag — meaning the migration
    re-ran and re-logged a full traceback on every setup, indefinitely.
    """
    devices = _duplicate_label_devices()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    for device in devices:
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, device["id"])}
        )

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

    assert entry.state is ConfigEntryState.LOADED
    # The collision no longer blocks completion, so the migration stops re-running.
    assert entry.data.get("stable_ids_migrated") is True
    # Exactly one device holds the shared slug; the other kept its old identifier.
    holders = [
        d
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if (DOMAIN, "hall_light") in d.identifiers
    ]
    assert len(holders) == 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_duplicate_slugs_reports_only_collisions() -> None:
    """`duplicate_slugs` returns the colliding slugs with their labels."""
    assert duplicate_slugs(_duplicate_label_devices()) == {
        "hall_light": ["Hall Light", "Hall-Light"]
    }
    # Distinct labels collide with nothing.
    assert duplicate_slugs(copy.deepcopy(DEVICES)) == {}
    assert duplicate_slugs([]) == {}


async def test_user_can_delete_a_device_the_gateway_dropped(
    hass: HomeAssistant, init_integration
) -> None:
    """An orphaned device can be removed from the UI.

    The automatic pruner is deliberately slow and conservative, so without a
    manual path a device the gateway has genuinely stopped reporting sits in the
    registry with no way to clear it.
    """
    dev_reg = dr.async_get(hass)
    ghost = dev_reg.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "ghost_device")},
    )

    assert await async_remove_config_entry_device(hass, init_integration, ghost) is True


async def test_user_cannot_delete_a_live_device(
    hass: HomeAssistant, init_integration
) -> None:
    """Deleting a device the gateway still reports is refused.

    The next poll would re-create it immediately, so allowing it would just look
    broken to the user.
    """
    dev_reg = dr.async_get(hass)
    live = dev_reg.async_get_device(identifiers={(DOMAIN, device_slug(DEVICES[0]))})
    assert live is not None

    assert await async_remove_config_entry_device(hass, init_integration, live) is False


async def test_the_gateway_hub_cannot_be_deleted(
    hass: HomeAssistant, init_integration
) -> None:
    """The synthetic hub is never in the device list but is not stale either."""
    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_device(
        identifiers={(DOMAIN, gateway_device_id(init_integration))}
    )
    assert hub is not None

    assert await async_remove_config_entry_device(hass, init_integration, hub) is False


async def test_pruning_a_device_is_logged(
    hass: HomeAssistant, init_integration, caplog: pytest.LogCaptureFixture
) -> None:
    """Removal must not be silent — it deletes every entity on the device."""
    coordinator = init_integration.runtime_data
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "ghost_device")},
    )
    with caplog.at_level(logging.WARNING):
        # Real device-list adoptions: bare async_update_listeners() dispatches
        # deliberately no longer count toward the miss threshold (see
        # test_non_poll_dispatches_do_not_consume_the_prune_debounce).
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            coordinator.async_set_updated_data(coordinator.data)
            await hass.async_block_till_done()

    assert "removing device" in caplog.text
    assert "ghost_device" in caplog.text


async def test_device_registry_entries(
    hass: HomeAssistant, init_integration, snapshot: SnapshotAssertion
) -> None:
    """Pin the device-registry rows.

    `snapshot_platform` in the per-platform tests is entity-only, so every
    device-level attribute was unpinned: the slug in `identifiers`, the
    `via_device` link to the synthetic hub, and manufacturer/model/sw_version/
    area. A change to `device_slug`, to `device_info`, or to the hub wiring was
    invisible to the suite unless it happened to alter an entity `unique_id`.

    Registry ids are random per run, so `via_device_id` is resolved back to the
    parent's identifiers and the rows are sorted by identifier.
    """
    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, init_integration.entry_id)
    assert devices, "no devices registered"

    by_id = {device.id: sorted(device.identifiers) for device in devices}
    rows = sorted(
        (
            {
                "identifiers": sorted(device.identifiers),
                "name": device.name,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "sw_version": device.sw_version,
                "area_id": device.area_id,
                "entry_type": device.entry_type,
                # Resolved, because the raw id is random per run.
                "via_device": by_id.get(device.via_device_id),
            }
            for device in devices
        ),
        key=lambda row: row["identifiers"],
    )
    assert rows == snapshot


async def test_every_device_links_to_the_gateway_hub(
    hass: HomeAssistant, init_integration
) -> None:
    """Each per-function device hangs off the synthetic hub via `via_device`.

    Asserted separately from the snapshot so the *invariant* is stated, not just
    recorded — a regenerated snapshot could otherwise quietly accept a broken
    topology.
    """
    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_device(
        identifiers={(DOMAIN, gateway_device_id(init_integration))}
    )
    assert hub is not None
    assert hub.via_device_id is None, "the hub must not be hung off anything"

    others = [
        device
        for device in dr.async_entries_for_config_entry(
            dev_reg, init_integration.entry_id
        )
        if device.id != hub.id
    ]
    assert others
    for device in others:
        assert device.via_device_id == hub.id, f"{device.name} is not linked to the hub"


async def test_gateway_software_version_reaches_every_device_page(
    hass: HomeAssistant,
) -> None:
    """The version read over REST must land on the hub and on every device.

    End-to-end counterpart to the coordinator's unit tests: `device_info` is
    only read when an entity is first added, so the value has to be known
    before the platforms run (setup fetches it) and `_apply_gateway_version`
    has to carry it into rows already written.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)

    async def _parameter(_self, _host: str, _token: str, parameter: str) -> str | None:
        return {"version_release": "2.1.3", "version_build": "2840"}.get(parameter)

    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_config_parameter", _parameter
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        assert devices
        assert {device.sw_version for device in devices} == {"2.1.3 (2840)"}

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
