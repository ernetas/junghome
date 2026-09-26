"""Integration setup / entity / lifecycle tests for Jung Home."""

import asyncio
import base64
import contextlib
import copy
import json
import logging
import time
from types import MappingProxyType, SimpleNamespace
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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    flush_store,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
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
    ISSUE_PUSH_FAILURE,
    ISSUE_TLS_MISMATCH,
    NODE_IDENTITY_REFETCH_INTERVAL,
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
from custom_components.junghome.entity import JungHomeEntity
from custom_components.junghome.event import JungHomeEventEntity
from custom_components.junghome.light import JungHomeLight
from custom_components.junghome.models import (
    FunctionAnchor,
    NodeIdentity,
    function_id_for,
)
from tests.conftest import (
    DEVICES,
    PRISTINE_DEVICES,
    _fake_run_websocket,
    bare_coordinator,
    find_device,
)
from tests.test_coordinator import _MALFORMED_DEVICES, malformed_device


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
    slug = device_slug(DEVICES[0])
    device = find_device(hass, slug)
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

    device = find_device(hass, device_slug(DEVICES[0]))
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
    """A device absent from enough consecutive device lists is removed, not on the first.

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
        hub = find_device(hass, gateway_device_id(entry))
        assert hub is not None
        # Not pruned on the first pass — the debounce rides out a partial poll.
        assert dev_reg.async_get(stale.id) is not None
        # Absent across the threshold of further polls -> pruned.
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            coordinator.async_set_updated_data(coordinator.data)
            await hass.async_block_till_done()
        assert dev_reg.async_get(stale.id) is None
        # The synthetic hub device was just as absent from every one of those
        # lists (the gateway never reports itself in `functions`), so the very
        # pass that pruned the ghost must have singled the hub out to keep.
        assert dev_reg.async_get(hub.id) is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_empty_or_failed_polls_prune_nothing(hass: HomeAssistant) -> None:
    """An empty device list and a failed fetch never count as a missed poll.

    Every device is absent from an empty list, so without the guard a gateway
    answering ``[]`` for STALE_DEVICE_PRUNE_MISSES polls (a reboot, a half-up
    middleware) would delete every device the user has — and a failed fetch
    leaves the previous list in place, so it must count as nothing at all.
    Pinned by driving the real poll path, not by injecting the list directly.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ghost = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ghost_device")}
    )
    fetch = AsyncMock(return_value=DEVICES)
    with (
        patch.object(JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", fetch),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data

        def registered() -> int:
            return len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id))

        before = registered()
        assert before > 1  # the hub plus the fixture's devices (and the ghost)

        # The gateway answers with an empty list, over the whole threshold.
        fetch.return_value = []
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert coordinator.data == []
        assert registered() == before
        assert dev_reg.async_get(ghost.id) is not None

        # The fetch fails outright, over the whole threshold, with a real list
        # adopted just before so there is something the failures could wrongly
        # be measured against.
        fetch.return_value = DEVICES
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        fetch.side_effect = aiohttp.ClientError("gateway unreachable")
        for _ in range(STALE_DEVICE_PRUNE_MISSES):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert coordinator.last_update_success is False
        assert registered() == before
        assert dev_reg.async_get(ghost.id) is not None

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
        blind_slug = device_slug(next(d for d in DEVICES if d["id"] == "idblind1"))
        blind = find_device(hass, blind_slug)
        assert blind is not None

        partial = [d for d in coordinator.data if d["id"] != "idblind1"]
        full = list(coordinator.data)

        # Miss it for one fewer poll than the threshold — must still be present.
        for _ in range(STALE_DEVICE_PRUNE_MISSES - 1):
            coordinator.async_set_updated_data(partial)
            await hass.async_block_till_done()
            assert find_device(hass, blind_slug)
        # It reappears -> counter resets.
        coordinator.async_set_updated_data(full)
        await hass.async_block_till_done()
        # A single later miss must not prune it (the reset worked).
        coordinator.async_set_updated_data(partial)
        await hass.async_block_till_done()
        assert find_device(hass, blind_slug)

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


async def test_hub_configuration_url_follows_the_host(
    hass: HomeAssistant, init_integration
) -> None:
    """The hub links the gateway's web page, and a moved host re-points it.

    A reconfigure or an adopted discovery rewrites the stored host, which
    reloads the entry; setup registers the hub again from the new host.
    """
    entry = init_integration
    hub = find_device(hass, gateway_device_id(entry))
    assert hub is not None
    assert hub.configuration_url == "https://1.2.3.4/"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: "9.9.9.9"}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    hub = find_device(hass, gateway_device_id(entry))
    assert hub is not None
    assert hub.configuration_url == "https://9.9.9.9/"


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
        # the capability fingerprint changes. One sighting is not trusted (the
        # datapoints of a re-enumerating device arrive across several polls),
        # so the reload waits for the next adoption to confirm the change.
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            coordinator.async_set_updated_data([with_angle])
            await hass.async_block_till_done()
            reload.assert_not_called()
            coordinator.async_set_updated_data([with_angle])
            await hass.async_block_till_done()
        reload.assert_called_once_with(entry.entry_id)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_capability_change_seen_once_does_not_reload(
    hass: HomeAssistant, init_integration
) -> None:
    """A datapoint set that flaps for one adoption (A -> B -> A) never reloads.

    A partial poll can momentarily drop a cover's ``angle`` datapoint, and a
    re-enumerating device's datapoints arrive across several polls. Reloading
    on the first sighting rebuilt the entry from that transient set and — the
    rebuilt watcher having seeded its baseline from it — reloaded again when
    the next poll restored the full set: a reload per adoption for as long as
    the flap lasted. The watcher must let the change go unconfirmed instead.
    """
    coordinator = init_integration.runtime_data
    full = copy.deepcopy(coordinator.data)
    without_angle = copy.deepcopy(full)
    blind = next(d for d in without_angle if d["id"] == "idblind1")
    blind["datapoints"] = [dp for dp in blind["datapoints"] if dp["type"] != "angle"]
    assert [dp["type"] for dp in blind["datapoints"]] == ["level"]  # fixture sanity

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_set_updated_data(without_angle)  # B: seen once
        await hass.async_block_till_done()
        coordinator.async_set_updated_data(full)  # back to A: candidate dropped
        await hass.async_block_till_done()
        # A fresh B later is a fresh first sighting, not a second one.
        coordinator.async_set_updated_data(without_angle)
        await hass.async_block_till_done()
        coordinator.async_set_updated_data(full)
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_capability_change_confirmed_by_second_adoption_reloads_once(
    hass: HomeAssistant, init_integration
) -> None:
    """A datapoint set that persists across two adoptions (A -> B -> B) reloads once.

    The second sighting confirms the change; the reload is scheduled exactly
    once, and a third identical adoption must not schedule another (the
    closure is dead until the reload rebuilds it).
    """
    coordinator = init_integration.runtime_data
    without_angle = copy.deepcopy(coordinator.data)
    blind = next(d for d in without_angle if d["id"] == "idblind1")
    blind["datapoints"] = [dp for dp in blind["datapoints"] if dp["type"] != "angle"]

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_set_updated_data(without_angle)  # B: seen once
        await hass.async_block_till_done()
        reload.assert_not_called()
        coordinator.async_set_updated_data(without_angle)  # B again: confirmed
        await hass.async_block_till_done()
        reload.assert_called_once_with(init_integration.entry_id)
        coordinator.async_set_updated_data(without_angle)
        await hass.async_block_till_done()
    reload.assert_called_once_with(init_integration.entry_id)


async def test_empty_adoption_confirms_no_capability_change(
    hass: HomeAssistant, init_integration
) -> None:
    """A -> B -> [] -> B does not reload: an empty list confirms nothing.

    An empty poll adopts nothing to fingerprint, so it must also drop the
    candidate awaiting confirmation — otherwise the B after it would read as
    the second *consecutive* sighting and reload, though the two sightings
    were not consecutive at all. The B after the empty list is a fresh first
    sighting; only the B after THAT confirms.
    """
    coordinator = init_integration.runtime_data
    without_angle = copy.deepcopy(coordinator.data)
    blind = next(d for d in without_angle if d["id"] == "idblind1")
    blind["datapoints"] = [dp for dp in blind["datapoints"] if dp["type"] != "angle"]

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator.async_set_updated_data(without_angle)  # B: seen once
        await hass.async_block_till_done()
        coordinator.async_set_updated_data([])  # nothing to compare against
        await hass.async_block_till_done()
        coordinator.async_set_updated_data(without_angle)  # B: first sighting again
        await hass.async_block_till_done()
        reload.assert_not_called()
        coordinator.async_set_updated_data(without_angle)  # B: now confirmed
        await hass.async_block_till_done()
    reload.assert_called_once_with(init_integration.entry_id)


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
    the change (the guard skipped the walk) — neither as the first sighting nor
    as the confirming second one — while two adoptions of that same list must
    still schedule the capability reload.
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
        coordinator.async_set_updated_data(coordinator.data)  # first sighting
        await hass.async_block_till_done()
        coordinator.async_update_listeners()  # not an adoption: cannot confirm
        await hass.async_block_till_done()
        reload.assert_not_called()
        coordinator.async_set_updated_data(coordinator.data)  # confirmed
        await hass.async_block_till_done()
    reload.assert_called_once_with(init_integration.entry_id)


async def test_entity_availability_tracks_connection(
    hass: HomeAssistant, init_integration
) -> None:
    """available splits by what the entity needs the WebSocket for.

    Pure state readers (sensor) follow ``last_update_success`` — the REST
    poll / WebSocket-push signal — and never key off ``ws_connected``, so a
    stale-True socket flag can't keep them "available" with frozen values after
    the gateway has gone unreachable (issue #120). Entities whose function needs
    the socket additionally require it live: controllables (light, socket, LED
    switch) because commands only travel over it — with the socket down they
    read unavailable rather than accept commands that would silently fail — and
    button events because edges only *arrive* over it (the REST poll re-reads
    values and fires nothing), so a deaf button must not look live.
    """
    coordinator = init_integration.runtime_data
    needs_websocket = (
        "light.strip",
        "switch.boiler",
        "switch.button_a_status_led",
        "event.button_a_up",
    )
    read_only = ("sensor.boiler_power",)

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
    assert states(needs_websocket) == {"available"}
    assert states(read_only) == {"available"}

    # WS down but REST still polling: controllables can't be commanded and
    # buttons can't be heard, so they go unavailable; read-only entities keep
    # reporting their polled state.
    coordinator.ws_connected = False
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(needs_websocket) == {"unavailable"}
    assert states(read_only) == {"available"}

    # Gateway gone: REST poll failing -> everything unavailable, even if the
    # socket flag is still stale-True (a half-open WS must not mask it).
    coordinator.ws_connected = True
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(needs_websocket) == {"unavailable"}
    assert states(read_only) == {"unavailable"}

    # Fully recovered -> everything available again.
    coordinator.ws_connected = True
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert states(needs_websocket) == {"available"}
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

        area_reg = ar.async_get(hass)
        device_entry = find_device(hass, "sofa_lamp")
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

    device_entry = find_device(hass, "sofa_lamp")
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
    device_entry = find_device(hass, "sofa_lamp")
    assert device_entry.area_id is not None  # placed on first setup
    # The device is recorded as already considered, so it is never re-placed.
    assert "sofa_lamp" in entry.data[DATA_AREA_ASSIGNED]

    # The user deliberately removes the device from every area...
    dev_reg.async_update_device(device_entry.id, area_id=None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # ...and it stays cleared across a full reload.
    await _setup_grouped_lamp(hass, entry)
    device_entry = find_device(hass, "sofa_lamp")
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

    device_entry = find_device(hass, "sofa_lamp")
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

        grouped = find_device(hass, "sofa_lamp")
        ungrouped = find_device(hass, "hall_lamp")
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

        device_entry = find_device(hass, "sofa_lamp")
        assert device_entry.area_id is None  # no rooms known yet -> not placed
        assert "sofa_lamp" not in entry.data.get(DATA_AREA_ASSIGNED, [])

        # The WebSocket handshake later delivers the groups; the next coordinator
        # update runs the placement again and now resolves the room.
        coordinator = entry.runtime_data
        coordinator.groups = [{"id": "grp-living", "name": "Living Room"}]
        coordinator.async_set_updated_data([_grouped_lamp()])
        await hass.async_block_till_done()

        device_entry = find_device(hass, "sofa_lamp")
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
        device_entry = find_device(hass, "sofa_lamp")
        assert device_entry.area_id is None

        # The next adoption (a poll or a `functions` broadcast) places it.
        coordinator.async_set_updated_data([_grouped_lamp()])
        await hass.async_block_till_done()
        device_entry = find_device(hass, "sofa_lamp")
        assert device_entry.area_id is not None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_runtime_added_device_is_placed_on_the_next_dispatch(
    hass: HomeAssistant,
) -> None:
    """A device arriving in an adoption is placed on the NEXT dispatch.

    No walk the adoption itself triggers can place the new device, because its
    registry entry does not exist yet during any of them: the adoption
    dispatch runs the assigner before the platforms' scheduled entity-add
    task registers the device. The assigner therefore leaves the generation
    unrecorded once, so the very next dispatch — typically the new device's
    own first value pushes, seconds away — retries the walk and places it,
    instead of waiting out the 60 s poll.
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
        device_entry = find_device(hass, "sofa_lamp")
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

        shared = find_device(hass, "sofa_lamp")
        assert shared is not None
        assert shared.area_id is None  # ambiguous room: never auto-placed
        placed = find_device(hass, "desk_lamp")
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
    """The synthetic gateway device survives the stale-device prune.

    The gateway never lists itself in ``functions``, so from the pruner's point
    of view the hub is a device missing from every single adoption. Without the
    explicit hub protection it would be removed — with the connectivity sensor
    and every ``via_device`` link — as soon as the debounce ran out.
    """
    coordinator = init_integration.runtime_data
    dev_reg = dr.async_get(hass)
    device = find_device(hass, "gateway_1.2.3.4")
    assert device is not None
    assert device.name == "JUNG HOME Gateway"

    # Absent from the whole debounce window's worth of adoptions (and one more,
    # in case an off-by-one ever lands on the threshold itself).
    for _ in range(STALE_DEVICE_PRUNE_MISSES + 1):
        coordinator.async_set_updated_data(coordinator.data)
        await hass.async_block_till_done()
    assert dev_reg.async_get(device.id) is not None
    assert dev_reg.async_get(device.id).config_entries == {init_integration.entry_id}


async def test_devices_linked_to_gateway_hub(
    hass: HomeAssistant, init_integration
) -> None:
    """Every function device hangs off the synthetic gateway (hub) via via_device."""
    hub = find_device(hass, "gateway_1.2.3.4")
    assert hub is not None
    light = find_device(hass, "hall_light")
    assert light is not None
    assert light.via_device_id == hub.id
    # setup handed the hub's registry id to the coordinator for the
    # ``via_device_id`` form of that link
    assert init_integration.runtime_data.gateway_device_registry_id == hub.id


async def test_device_info_links_the_hub_by_registry_id_when_the_core_can(
    hass: HomeAssistant, init_integration
) -> None:
    """``device_info`` prefers ``via_device_id`` and falls back to the tuple.

    HA 2026.9's deprecation of the ``via_device`` tuple raised (not warned)
    under ``update_before_add=True``, dropping one entity per startup (issue
    #207). The registry-id form exists from 2026.8; older cores reject it, so
    the choice is made from what this core's ``DeviceInfo`` knows. The pinned
    test core predates the key, hence the flag is driven explicitly here.
    """
    hub = find_device(hass, "gateway_1.2.3.4")
    assert hub is not None
    light = hass.data[DATA_INSTANCES]["light"].get_entity("light.hall_light")
    assert isinstance(light, JungHomeEntity)

    with patch("custom_components.junghome.entity.VIA_DEVICE_ID_SUPPORTED", True):
        info = light.device_info
        assert info is not None
        assert info.get("via_device_id") == hub.id
        assert "via_device" not in info

        # no registry id (a coordinator that never went through setup) keeps
        # the tuple so the link is not silently lost
        light.coordinator.gateway_device_registry_id = None
        info = light.device_info
        assert info is not None
        assert "via_device_id" not in info
        assert info.get("via_device") == (DOMAIN, "gateway_1.2.3.4")

    with patch("custom_components.junghome.entity.VIA_DEVICE_ID_SUPPORTED", False):
        light.coordinator.gateway_device_registry_id = hub.id
        info = light.device_info
        assert info is not None
        assert "via_device_id" not in info
        assert info.get("via_device") == (DOMAIN, "gateway_1.2.3.4")


def test_device_info_leaves_unknown_fields_out(hass: HomeAssistant) -> None:
    """No made-up "Unknown Model"/"Unknown Version"/"Jung Device" rows.

    Unknown fields are left OUT rather than set to ``None``: a key that is
    absent leaves the registry's existing value alone (the gateway version an
    earlier run wrote), whereas ``None`` would clear it. The connection is
    never part of ``device_info`` — the coordinator writes it after the row
    exists (see ``JungHomeEntity.device_info``).
    """
    coordinator = bare_coordinator(hass)
    full = _identified_devices()[0]
    full["sw_version"] = "2.2.0"
    info = JungHomeLight(coordinator, full, full["datapoints"][0]).device_info
    assert info is not None
    assert info["name"] == "Hall Light"
    assert info["model"] == "OnOff"
    assert info["sw_version"] == "2.2.0"
    assert "connections" not in info

    bare = {"id": "idbare", "datapoints": full["datapoints"]}
    info = JungHomeLight(coordinator, bare, bare["datapoints"][0]).device_info  # type: ignore[arg-type]
    assert info is not None
    assert info["identifiers"] == {(DOMAIN, "idbare")}
    assert info["manufacturer"] == "Jung"
    for key in ("name", "model", "sw_version", "serial_number", "connections"):
        assert key not in info, key

    # The gateway's own version stands in for a function that reports none.
    coordinator.gateway_version = "2.1.3 (2840)"
    info = JungHomeLight(coordinator, bare, bare["datapoints"][0]).device_info  # type: ignore[arg-type]
    assert info is not None
    assert info["sw_version"] == "2.1.3 (2840)"


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

    device = find_device(hass, device_slug(DEVICES[0]))
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
    # Removed here and now, the way the pruner does it (core tolerates that).
    assert dev_reg.async_get(ghost.id) is None


async def test_user_cannot_delete_a_live_device(
    hass: HomeAssistant, init_integration
) -> None:
    """Deleting a device the gateway still reports is refused.

    The next poll would re-create it immediately, so allowing it would just look
    broken to the user.
    """
    live = find_device(hass, device_slug(DEVICES[0]))
    assert live is not None

    assert await async_remove_config_entry_device(hass, init_integration, live) is False


async def test_the_gateway_hub_cannot_be_deleted(
    hass: HomeAssistant, init_integration
) -> None:
    """The synthetic hub is never in the device list but is not stale either."""
    hub = find_device(hass, gateway_device_id(init_integration))
    assert hub is not None

    assert await async_remove_config_entry_device(hass, init_integration, hub) is False


async def test_user_can_delete_a_device_of_an_unloaded_entry(
    hass: HomeAssistant,
) -> None:
    """Without a coordinator (entry not loaded) nothing is live: core removes it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="9.9.9.9",
        data={CONF_HOST: "9.9.9.9", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)  # never set up: no runtime_data
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "ghost_device")}
    )
    assert await async_remove_config_entry_device(hass, entry, device) is True
    # Left to core here (no coordinator to relink for), unlike the loaded case.
    assert dev_reg.async_get(device.id) is not None


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
                "configuration_url": device.configuration_url,
                "area_id": device.area_id,
                "entry_type": device.entry_type,
                # Hardware identity from the project export; the fixture setup
                # serves none, so these pin the "older firmware" baseline.
                "connections": sorted(device.connections),
                "serial_number": device.serial_number,
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
    hub = find_device(hass, gateway_device_id(init_integration))
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
    before the platforms run (setup fetches it) and `_apply_device_info`
    has to carry it into rows already written.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)

    async def _version(_self, _host: str) -> dict[str, str]:
        return {"version_release": "2.1.3", "version_build": "2840"}

    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_version_from_api", _version
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


# --- Hardware identity (GET /project/junghome) -------------------------------
#
# Two synthetic nodes: NODE_A is a 2-gang push button backing a load (primary
# element, location 1) and a rocker (location 0x40); NODE_B a socket. Their
# function ids are computed the way the gateway computes them, so the device
# list below and the export below agree the way a real gateway's do. Keys are
# synthetic repeating patterns.
NODE_A = "AABBCCFF-FE01-0203-0000-000000000000"
MAC_A = "AA:BB:CC:01:02:03"
NODE_B = "DDEEFFFF-FE0A-0B0C-0000-000000000000"
MAC_B = "DD:EE:FF:0A:0B:0C"
NET_KEY = "0123456789ABCDEF0123456789ABCDEF"
APP_KEY = "FEDCBA9876543210FEDCBA9876543210"
DEV_KEY = "A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1"
HALL_LIGHT_ID = function_id_for(NODE_A, 1)
HALL_BUTTON_ID = function_id_for(NODE_A, 0x40)
DESK_SOCKET_ID = function_id_for(NODE_B, 1)
# From HA 2026.9 a device connection is unique per config entry (the scoped
# lookups arrived with that change), so a device of ours may share a Bluetooth
# address with another integration's; before it the address is registry-wide
# and the first holder keeps it. Feature-detected, like the integration does.
CONNECTIONS_ARE_PER_ENTRY = hasattr(dr.DeviceRegistry, "async_get_device_by_connection")


def _project_export() -> dict:
    """The app's ExportDto as the gateway serves it (Base64 CDB + meta)."""
    cdb = {
        "netKeys": [{"index": 0, "key": NET_KEY}],
        "appKeys": [{"index": 0, "key": APP_KEY}],
        "nodes": [
            {
                "UUID": NODE_A,
                "deviceKey": DEV_KEY,
                "unicastAddress": "00CF",
                "pid": "0002",
                "elements": [
                    {"index": 0, "location": "0001", "models": []},
                    {"index": 1, "location": "0040", "models": []},
                ],
            },
            {
                "UUID": NODE_B,
                "deviceKey": DEV_KEY,
                "unicastAddress": "0148",
                "pid": "0003",
                "elements": [{"index": 0, "location": "0001", "models": []}],
            },
        ],
    }
    return {
        "version": "1.1",
        "meta": {
            "devices": [
                {
                    "name": "Hall Light",
                    "macAddress": MAC_A,
                    "deviceId": {"nodeId": NODE_A, "locationIds": [1]},
                }
            ]
        },
        "network": base64.b64encode(json.dumps(cdb).encode()).decode(),
    }


def _identified_devices() -> list[dict]:
    """A device list whose ids the export above resolves — plus one it doesn't."""

    def switch(device_id: str) -> dict:
        return {
            "id": f"{device_id}-001",
            "type": "switch",
            "values": [{"key": "switch", "value": "1"}],
        }

    return [
        {
            "id": HALL_LIGHT_ID,
            "type": "OnOff",
            "label": "Hall Light",
            "datapoints": [switch(HALL_LIGHT_ID)],
        },
        {
            "id": HALL_BUTTON_ID,
            "type": "RockerSwitch",
            "label": "Hall Button",
            "datapoints": [
                {
                    "id": f"{HALL_BUTTON_ID}-00c",
                    "type": "up_request",
                    "values": [{"key": "up_request", "value": "0"}],
                },
                {
                    "id": f"{HALL_BUTTON_ID}-00e",
                    "type": "status_led",
                    "values": [{"key": "status_led", "value": "0"}],
                },
            ],
        },
        {
            "id": DESK_SOCKET_ID,
            "type": "Socket",
            "label": "Desk Socket",
            "datapoints": [switch(DESK_SOCKET_ID)],
        },
        {
            "id": "idorphan",
            "type": "OnOff",
            "label": "Orphan",
            "datapoints": [switch("idorphan")],
        },
    ]


def _device(hass: HomeAssistant, label: str) -> dr.DeviceEntry:
    device = find_device(hass, device_slug({"label": label}))
    assert device is not None, label
    return device


def _gateway_stubs(
    devices: list[dict] | None, export: object
) -> list[contextlib.AbstractContextManager]:
    """Class-level stubs, so an entry reload inside the block stays off the network."""
    return [
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(
                return_value=_identified_devices() if devices is None else devices
            ),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_project_export_from_api",
            AsyncMock(return_value=export),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ]


async def _setup_with_export(
    hass: HomeAssistant,
    export: object,
    devices: list[dict] | None = None,
    entry: MockConfigEntry | None = None,
) -> MockConfigEntry:
    """Set an entry up against ``export``; ``entry`` may be prepared (seeded) first."""
    if entry is None:
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
            AsyncMock(
                return_value=_identified_devices() if devices is None else devices
            ),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_project_export_from_api",
            AsyncMock(return_value=export),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_node_identity_reaches_the_device_registry(hass: HomeAssistant) -> None:
    """Serial number on every function of a node; the connection on one.

    The export is read after the first refresh and before the platforms build
    their ``device_info``, so the serial is right from the first registration
    and the connection lands the moment each device's row exists
    (``link_node_identity`` from ``async_added_to_hass``). A node backs
    several functions — here a load and a rocker on one radio — and Home
    Assistant resolves devices by connection, so the Bluetooth address may be
    a *connection* on exactly one of them (the primary element's function) or
    the registry would merge them into one device.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    assert len(coordinator.node_identities) == 3

    light = _device(hass, "Hall Light")
    button = _device(hass, "Hall Button")
    socket = _device(hass, "Desk Socket")
    orphan = _device(hass, "Orphan")

    assert light.serial_number == MAC_A
    assert light.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    assert button.serial_number == MAC_A
    assert button.connections == set()
    assert button.id != light.id, "functions of one node must stay separate devices"
    # No meta entry for the socket's node: the MAC comes from the EUI-64 UUID.
    assert socket.serial_number == MAC_B
    assert socket.connections == {(dr.CONNECTION_BLUETOOTH, MAC_B)}
    # A function the export does not cover is registered exactly as before.
    assert orphan.serial_number is None
    assert orphan.connections == set()
    # The slug identifier is untouched, so existing registrations merged.
    assert light.identifiers == {(DOMAIN, "hall_light")}

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_device_model_is_the_product_behind_the_function(
    hass: HomeAssistant,
) -> None:
    """``model`` names the node's product (``pid``); the function type is the fallback.

    Every function of a node shares its product — the load and the rocker of
    one 2-gang push button both read ``PushButton2gang`` — and a function the
    export does not cover, or a product id the gateway's table does not name,
    keeps the function type it always showed.
    """
    export = _project_export()
    cdb = json.loads(base64.b64decode(export["network"]))
    cdb["nodes"][1]["pid"] = "00FE"  # the socket's node: not in the table
    export["network"] = base64.b64encode(json.dumps(cdb).encode()).decode()
    entry = await _setup_with_export(hass, export)
    assert _device(hass, "Hall Light").model == "PushButton2gang"
    assert _device(hass, "Hall Button").model == "PushButton2gang"
    assert _device(hass, "Desk Socket").model == "Socket"
    assert _device(hass, "Orphan").model == "OnOff"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_node_identity_fetch_is_best_effort(hass: HomeAssistant) -> None:
    """No export (older firmware), a failed read, or garbage: setup still loads."""
    for export in (None, [], "not a dict", {"network": "@@@"}):
        entry = await _setup_with_export(hass, export)
        assert entry.state is ConfigEntryState.LOADED
        assert dict(entry.runtime_data.node_identities) == {}
        assert _device(hass, "Hall Light").serial_number is None
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = bare_coordinator(hass)
    for err in (aiohttp.ClientError("down"), TimeoutError(), ValueError("json")):
        with patch.object(
            coordinator, "_fetch_project_export_from_api", AsyncMock(side_effect=err)
        ):
            await coordinator.async_fetch_node_identities()
        assert dict(coordinator.node_identities) == {}
    # Best-effort covers gateway failures, not bugs in our own code.
    with (
        patch.object(
            coordinator,
            "_fetch_project_export_from_api",
            AsyncMock(side_effect=RuntimeError),
        ),
        pytest.raises(RuntimeError),
    ):
        await coordinator.async_fetch_node_identities()


@pytest.mark.real_project_fetch
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"json": _project_export}, 3),
        ({"status": 404, "json": {"error": "not found"}}, 0),
        ({"status": 501}, 0),
        ({"status": 500, "text": "boom"}, 0),
        ({"text": "<html>not json</html>"}, 0),
        ({"json": []}, 0),
        ({"exc": aiohttp.ClientError("refused")}, 0),
    ],
    ids=["200", "404", "501", "500", "not-json", "json-list", "client-error"],
)
async def test_project_export_rest_read(
    hass: HomeAssistant, aioclient_mock, response: dict, expected: int
) -> None:
    """``GET /project/junghome`` over the wire: 200 parses, everything else skips.

    404 is what firmware before API 1.5.0 answers (no ``project/*`` routes);
    a body that is not JSON, or JSON of the wrong shape, must not raise out
    of setup either — the export is an enrichment, never a requirement.
    """
    if callable(response.get("json")):
        response = {**response, "json": response["json"]()}
    aioclient_mock.get("https://1.2.3.4/api/junghome/project/junghome", **response)
    entry = await _setup_with_export(hass, export=None)
    # The conftest stub is bypassed by the marker, but ``_setup_with_export``
    # still patched the method; run the real one now.
    coordinator = entry.runtime_data
    await coordinator.async_fetch_node_identities()
    assert entry.state is ConfigEntryState.LOADED
    assert len(coordinator.node_identities) == expected
    request = aioclient_mock.mock_calls[-1]
    assert request[3]["token"] == "tok"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_project_export_is_never_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The export carries the mesh keys: not one byte of it may reach the log."""
    caplog.set_level(logging.DEBUG)
    entry = await _setup_with_export(hass, _project_export())
    for secret in (NET_KEY, APP_KEY, DEV_KEY, NODE_A, MAC_A):
        assert secret not in caplog.text, secret
    assert "Resolved hardware identity for 3 gateway functions" in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_carry_identities_but_never_keys(
    hass: HomeAssistant,
) -> None:
    """The identity map is in the dump; nothing else from the export is."""
    entry = await _setup_with_export(hass, _project_export())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["node_identity_count"] == 3
    assert diag["node_identities"][HALL_LIGHT_ID] == {
        "uuid": NODE_A,
        "location": 1,
        "mac": MAC_A,
        "unicast": 0xCF,
        "product_id": 2,
        "primary": True,
    }
    device_diag = await async_get_device_diagnostics(
        hass, entry, _device(hass, "Hall Button")
    )
    assert device_diag["node_identity"] == {
        "uuid": NODE_A,
        "location": 0x40,
        "mac": MAC_A,
        "unicast": 0xD0,
        "product_id": 2,
        "primary": False,
    }
    orphan_diag = await async_get_device_diagnostics(
        hass, entry, _device(hass, "Orphan")
    )
    assert orphan_diag["node_identity"] is None

    for dump in (json.dumps(diag, default=str), json.dumps(device_diag, default=str)):
        for secret in (NET_KEY, APP_KEY, DEV_KEY):
            assert secret not in dump, secret
        assert "netKeys" not in dump
        assert "deviceKey" not in dump
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_device_diagnostics_carry_properties_and_the_anchor(
    hass: HomeAssistant,
) -> None:
    """A device's report: its verbose properties, its node's revision, its anchor.

    The button's own revision is null — the gateway fills it on the node's
    main-element function only — so the resolved node revision differs from
    the raw one, which is exactly what the report must show. The anchor
    (function id, node MAC, element location) is an identity like
    ``node_identity``, not a secret, and a function the export does not cover
    has an id-only one.
    """

    def _verbose(device_id: str, revision: list[int] | None) -> dict:
        return {
            "device_id": device_id,
            "states": {},
            "property": {
                "software_revision": {
                    "state_type": "software_revision",
                    "value": revision,
                    "model": {"address": 0xCF, "category": "property"},
                }
            },
        }

    verbose = [_verbose(HALL_LIGHT_ID, [2, 2, 0, 2]), _verbose(HALL_BUTTON_ID, None)]
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_devices_verbose_from_api",
        AsyncMock(return_value=verbose),
    ):
        entry = await _setup_with_export(hass, _project_export())
    button = _device(hass, "Hall Button")
    assert button.sw_version == "2.2.0.2"
    device_diag = await async_get_device_diagnostics(hass, entry, button)
    assert device_diag["device_properties"] == {
        "has_energy": False,
        "energy_wh": None,
        "software_revision": None,
        "reachable": None,
        "node_address": 0xCF,
    }
    assert device_diag["node_software_revision"] == (2, 2, 0, 2)
    assert device_diag["function_anchor"] == {
        "id": HALL_BUTTON_ID,
        "mac": MAC_A,
        "location": 0x40,
    }

    orphan_diag = await async_get_device_diagnostics(
        hass, entry, _device(hass, "Orphan")
    )
    assert orphan_diag["device_properties"] is None
    assert orphan_diag["node_software_revision"] is None
    assert orphan_diag["function_anchor"] == {
        "id": "idorphan",
        "mac": None,
        "location": None,
    }
    assert "1.2.3.4" not in json.dumps(device_diag, default=str)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unidentified_function_triggers_a_debounced_refetch(
    hass: HomeAssistant,
) -> None:
    """A device list with an unknown id re-reads the export — not on every poll.

    Setup here found no export (the gateway had none yet), so every function
    is unidentified. The re-read must wait out the debounce, then run once in
    the background, then write the identities onto the devices that were
    registered without them — and not run again once everything is known.
    """
    entry = await _setup_with_export(hass, export=None)
    coordinator = entry.runtime_data
    assert _device(hass, "Hall Light").serial_number is None
    assert _device(hass, "Hall Light").model == "OnOff"
    fetch = AsyncMock(return_value=_project_export())
    with patch.object(coordinator, "_fetch_project_export_from_api", fetch):
        broadcast = json.dumps({"type": "functions", "data": _identified_devices()})
        # Inside the debounce window: an adoption schedules nothing.
        coordinator._dispatch_text_frame(broadcast)
        await hass.async_block_till_done()
        assert fetch.await_count == 0

        # Past it: the next adoption (a poll here) re-reads, in the background.
        coordinator._node_identity_fetched_at = (
            time.monotonic() - NODE_IDENTITY_REFETCH_INTERVAL - 1
        )
        with patch.object(
            coordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=_identified_devices()),
        ):
            await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert fetch.await_count == 1
        assert len(coordinator.node_identities) == 3
        # ...and the rows registered before the identity was known are filled.
        assert _device(hass, "Hall Light").serial_number == MAC_A
        assert _device(hass, "Hall Light").connections == {
            (dr.CONNECTION_BLUETOOTH, MAC_A)
        }
        assert _device(hass, "Hall Button").serial_number == MAC_A
        assert _device(hass, "Hall Button").connections == set()
        assert _device(hass, "Orphan").serial_number is None
        # The product names reach the pages registered with the fallback.
        assert _device(hass, "Hall Light").model == "PushButton2gang"
        assert _device(hass, "Orphan").model == "OnOff"

        # The orphan is still unidentified, but the clock was just reset.
        coordinator._dispatch_text_frame(broadcast)
        await hass.async_block_till_done()
        assert fetch.await_count == 1
        # Past the window with every function identified: nothing to do.
        coordinator._node_identity_fetched_at = (
            time.monotonic() - NODE_IDENTITY_REFETCH_INTERVAL - 1
        )
        coordinator._dispatch_text_frame(
            json.dumps({"type": "functions", "data": _identified_devices()[:3]})
        )
        await hass.async_block_till_done()
        assert fetch.await_count == 1
        # Past the window with the orphan back: one more read, and an
        # unchanged answer changes nothing.
        coordinator._dispatch_text_frame(broadcast)
        await hass.async_block_till_done()
        assert fetch.await_count == 2
        assert len(coordinator.node_identities) == 3

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_refetch_does_not_stack_while_one_is_in_flight(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_with_export(hass, export=None)
    coordinator = entry.runtime_data
    release = asyncio.Event()

    async def _slow(_host: str, _token: str) -> dict:
        await release.wait()
        return _project_export()

    fetch = AsyncMock(side_effect=_slow)
    broadcast = json.dumps({"type": "functions", "data": _identified_devices()})
    with patch.object(coordinator, "_fetch_project_export_from_api", fetch):
        coordinator._node_identity_fetched_at = (
            time.monotonic() - NODE_IDENTITY_REFETCH_INTERVAL - 1
        )
        coordinator._dispatch_text_frame(broadcast)
        await asyncio.sleep(0)
        assert fetch.await_count == 1
        # A second adoption while the first read is still waiting: no new task,
        # even though the clock (reset by the running read) is forced past
        # the window again.
        coordinator._node_identity_fetched_at = (
            time.monotonic() - NODE_IDENTITY_REFETCH_INTERVAL - 1
        )
        coordinator._dispatch_text_frame(broadcast)
        await asyncio.sleep(0)
        assert fetch.await_count == 1
        release.set()
        await hass.async_block_till_done()
    assert len(coordinator.node_identities) == 3
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_stop_cancels_an_in_flight_refetch(hass: HomeAssistant) -> None:
    """A full HA shutdown reaches ``stop`` without an unload: cancel it there."""
    entry = await _setup_with_export(hass, export=None)
    coordinator = entry.runtime_data
    started = asyncio.Event()

    async def _hang(_host: str, _token: str) -> dict:
        started.set()
        await asyncio.Event().wait()
        return {}  # pragma: no cover - cancelled before this

    with patch.object(
        coordinator, "_fetch_project_export_from_api", AsyncMock(side_effect=_hang)
    ):
        coordinator._node_identity_fetched_at = (
            time.monotonic() - NODE_IDENTITY_REFETCH_INTERVAL - 1
        )
        coordinator._dispatch_text_frame(
            json.dumps({"type": "functions", "data": _identified_devices()})
        )
        await started.wait()
        task = coordinator._node_identity_task
        assert task is not None
        assert not task.done()
        await coordinator.stop()
        await hass.async_block_till_done()
        assert task.cancelled()
        assert coordinator._node_identity_task is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _foreign_holder_of(hass: HomeAssistant, mac: str) -> dr.DeviceEntry:
    """Register another integration's device page for the radio at ``mac``.

    What the Bluetooth-direct sibling integration does for the same node.
    """
    foreign_entry = MockConfigEntry(domain="other")
    foreign_entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, mac)},
        name="Push-button 00CF",
        manufacturer="Other",
        model="BLE thing",
    )


def _assert_not_merged_into(
    hass: HomeAssistant, light: dr.DeviceEntry, foreign: dr.DeviceEntry
) -> None:
    """Our device and the foreign one stay two devices; the foreign one untouched.

    Whether our device ALSO carries the address depends on the core: from HA
    2026.9 a connection is unique per config entry, so both may hold it;
    before that it is unique registry-wide and the first holder keeps it.
    """
    assert light.id != foreign.id, "our function was merged into the foreign device"
    assert light.identifiers == {(DOMAIN, "hall_light")}
    assert light.serial_number == MAC_A
    foreign_now = dr.async_get(hass).async_get(foreign.id)
    assert foreign_now is not None
    assert (foreign_now.name, foreign_now.manufacturer, foreign_now.model) == (
        "Push-button 00CF",
        "Other",
        "BLE thing",
    )
    assert foreign_now.identifiers == set()
    assert foreign_now.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    expected = (
        {(dr.CONNECTION_BLUETOOTH, MAC_A)} if CONNECTIONS_ARE_PER_ENTRY else set()
    )
    assert light.connections == expected


async def test_apply_identities_leaves_a_connection_held_elsewhere(
    hass: HomeAssistant,
) -> None:
    """Another integration's device page for the same radio keeps the address.

    The identities arrive AFTER our device was registered (an export re-read),
    so this is the back-fill path. Before HA 2026.9 ``async_update_device``
    raises on the registry-wide collision — caught, the link skipped, the
    serial number still written; from 2026.9 the per-entry link succeeds.
    Either way the two devices are never merged.
    """
    entry = await _setup_with_export(hass, export=None)
    coordinator = entry.runtime_data
    foreign = _foreign_holder_of(hass, MAC_A)
    with patch.object(
        coordinator,
        "_fetch_project_export_from_api",
        AsyncMock(return_value=_project_export()),
    ):
        await coordinator.async_fetch_node_identities()
    _assert_not_merged_into(hass, _device(hass, "Hall Light"), foreign)
    # The socket's address is unclaimed, so it is linked as usual.
    assert _device(hass, "Desk Socket").connections == {
        (dr.CONNECTION_BLUETOOTH, MAC_B)
    }
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_fresh_function_is_not_merged_into_a_foreign_holder(
    hass: HomeAssistant,
) -> None:
    """A foreign device already holding the address does not absorb our function.

    The identities are known BEFORE the platforms register the device (the
    setup-time export read), so this is the ``device_info`` path — where the
    connection used to be, and where ``async_get_or_create`` resolved our new
    slug to the foreign device by connection on cores before 2026.9 and
    merged: a junghome light living on the sibling integration's device page,
    with its name, manufacturer and model.
    """
    foreign = _foreign_holder_of(hass, MAC_A)
    entry = await _setup_with_export(hass, _project_export())
    _assert_not_merged_into(hass, _device(hass, "Hall Light"), foreign)
    assert hass.states.get("light.hall_light") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(
    hass: HomeAssistant,
    coordinator: JungHomeDataUpdateCoordinator,
    devices: list[dict],
    times: int = 1,
) -> None:
    """Poll ``devices`` from the gateway ``times`` times, off the network.

    Patched for the whole loop rather than adopted directly, so any refresh
    an adoption itself requests reads the same list.
    """
    with patch.object(
        coordinator,
        "_fetch_devices_from_api",
        AsyncMock(return_value=copy.deepcopy(devices)),
    ):
        for _ in range(times):
            await coordinator.async_refresh()
            await hass.async_block_till_done()


def _relabelled(devices: list[dict], function_id: str, label: str) -> list[dict]:
    """The same device list with one function relabelled in the app."""
    devices = copy.deepcopy(devices)
    next(d for d in devices if d["id"] == function_id)["label"] = label
    return devices


async def test_renaming_a_primary_function_keeps_its_device(
    hass: HomeAssistant,
) -> None:
    """A rename in the app renames the Home Assistant device; nothing is replaced.

    Identity is label-derived, so a rename used to be a new device: the old
    one pruned after ``STALE_DEVICE_PRUNE_MISSES`` adoptions, entities,
    history and customisations with it (the b8 regression was the merge that
    masked that). The function id does not change on a rename, so
    ``follow_renames`` pairs the vanished label with the new one on the same
    element and rewrites the device identifier and every unique_id in place,
    before the platforms see the list: same registry device, same entity ids
    (Home Assistant never renames those), the Bluetooth connection never
    moves, the pruner has nothing to prune, and the entity keeps updating.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("light.hall_light").unique_id == "hall_light_001"
    # Another integration's entity on the same device is not ours to rename.
    foreign = ent_reg.async_get_or_create(
        Platform.SENSOR, "other", "hall_light_foreign", device_id=old.id
    )
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")

    await _poll(hass, coordinator, relabelled)
    renamed = _device(hass, "Hall Lamp")
    assert renamed.id == old.id
    assert renamed.identifiers == {(DOMAIN, "hall_lamp")}
    assert ent_reg.async_get(foreign.entity_id).unique_id == "hall_light_foreign"
    assert renamed.name == "Hall Lamp"
    assert renamed.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    assert find_device(hass, "hall_light") is None
    assert ent_reg.async_get("light.hall_light").unique_id == "hall_lamp_001"
    assert hass.states.get("light.hall_lamp") is None
    assert hass.states.get("light.hall_light").state == "on"
    assert coordinator.function_anchors["hall_lamp"].id == HALL_LIGHT_ID
    assert "hall_light" not in coordinator.function_anchors

    off = copy.deepcopy(relabelled)
    light = next(d for d in off if d["id"] == HALL_LIGHT_ID)
    light["datapoints"][0]["values"][0]["value"] = "0"
    await _poll(hass, coordinator, off, times=STALE_DEVICE_PRUNE_MISSES + 1)
    assert hass.states.get("light.hall_light").state == "off"
    assert dr.async_get(hass).async_get(old.id) is not None
    hall_lights = [
        e.unique_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.domain == Platform.LIGHT and e.unique_id.startswith("hall_l")
    ]
    assert hall_lights == ["hall_lamp_001"]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_renaming_a_non_primary_function_keeps_its_device(
    hass: HomeAssistant,
) -> None:
    """A rocker on the light's node is followed the same way; every entity moves."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Button")
    ent_reg = er.async_get(hass)
    led = ent_reg.async_get_entity_id(Platform.SWITCH, DOMAIN, "hall_button_00e_switch")
    up = ent_reg.async_get_entity_id(Platform.EVENT, DOMAIN, "hall_button_00c_event")
    assert led is not None
    assert up is not None
    relabelled = _relabelled(_identified_devices(), HALL_BUTTON_ID, "Hallway Button")
    await _poll(hass, coordinator, relabelled, times=STALE_DEVICE_PRUNE_MISSES + 2)
    renamed = _device(hass, "Hallway Button")
    assert renamed.id == old.id
    assert renamed.serial_number == MAC_A
    assert renamed.connections == set()
    assert (
        ent_reg.async_get_entity_id(Platform.SWITCH, DOMAIN, "hall_button_00e_switch")
        is None
    )
    assert (
        ent_reg.async_get_entity_id(
            Platform.SWITCH, DOMAIN, "hallway_button_00e_switch"
        )
        == led
    )
    assert (
        ent_reg.async_get_entity_id(Platform.EVENT, DOMAIN, "hallway_button_00c_event")
        == up
    )
    assert hass.states.get(up) is not None
    # The primary function keeps the node's connection throughout.
    assert _device(hass, "Hall Light").connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_manual_delete_of_a_stale_device_hands_over_the_connection(
    hass: HomeAssistant,
) -> None:
    """Deleting a stale device from the UI relinks the successor at once.

    A rename that could not be followed still leaves a stale device behind —
    here the new label already had a registry device, so the platforms
    registered under that one and the old device went stale. The user need
    not wait out the pruner: ``async_remove_config_entry_device`` removes the
    device itself and runs the same back-fill the pruner does.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "hall_lamp")},
        name="Hall Lamp",
    )
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    new = _device(hass, "Hall Lamp")
    assert new.id != old.id
    assert new.connections == set()
    assert dr.async_get(hass).async_get(old.id) is not None

    assert await async_remove_config_entry_device(hass, entry, old) is True
    assert dr.async_get(hass).async_get(old.id) is None
    assert _device(hass, "Hall Lamp").connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --- Rename following (2026-09-16) -------------------------------------------
#
# Measured across a real app-driven device-firmware update (the June and
# August dumps): the update re-provisioned nothing and changed no datapoint
# suffix; the only churn was the user's own renaming in the app — six devices,
# each a replaced HA device under the old contract. `follow_renames` pairs a
# vanished label with the new one on the same element and rewrites the
# registry in place. Anchors persist in the entry's store so the pairing also
# works for a rename made while Home Assistant was down.

NODE_C = "CCBBAAFF-FE03-0201-0000-000000000000"
MAC_C = "CC:BB:AA:03:02:01"


def _anchors_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}.functions"


def _seed_anchors(
    hass_storage: dict, entry: MockConfigEntry, functions: dict[str, dict]
) -> None:
    """Pre-write the store as a previous run of the integration left it."""
    key = _anchors_key(entry)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"functions": functions},
    }


def _seed_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    slug: str,
    name: str,
    entities: dict[str, str],
) -> dr.DeviceEntry:
    """A registry device with entities, as a previous run left them."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, slug)}, name=name
    )
    for unique_id, domain in entities.items():
        er.async_get(hass).async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=slug,
        )
    return device


def _prepared_entry(hass: HomeAssistant, **kwargs: object) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def test_rename_arriving_as_a_functions_broadcast_is_followed(
    hass: HomeAssistant,
) -> None:
    """The app's rename usually reaches Home Assistant as a WS broadcast first."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    coordinator._handle_websocket_message({"type": "functions", "data": relabelled})
    await hass.async_block_till_done()
    assert _device(hass, "Hall Lamp").id == old.id
    assert find_device(hass, "hall_light") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_old_label_reused_after_a_followed_rename_gets_entities(
    hass: HomeAssistant,
) -> None:
    """The renamed-away unique_id leaves discovery's known set.

    Otherwise a new function given the old label is never discovered — its
    unique_id looks registered already.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    reused = [
        *relabelled,
        {
            "id": "idnewhall",
            "type": "OnOff",
            "label": "Hall Light",
            "datapoints": [
                {
                    "id": "idnewhall-001",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "1"}],
                }
            ],
        },
    ]
    await _poll(hass, coordinator, reused)
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001")
    assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_lamp_001")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_keeps_foreign_identifiers(hass: HomeAssistant) -> None:
    """Only our own identifier is rewritten; one another integration added stays."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    dr.async_get(hass).async_update_device(
        old.id, merge_identifiers={("other_domain", "radio-1")}
    )
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    assert dr.async_get(hass).async_get(old.id).identifiers == {
        (DOMAIN, "hall_lamp"),
        ("other_domain", "radio-1"),
    }
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_anchor_of_a_live_label_follows_its_new_element(
    hass: HomeAssistant,
) -> None:
    """A node swapped under the same label re-anchors the label to the new element."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    swapped = _identified_devices()
    new_id = function_id_for(NODE_C, 1)
    light = next(d for d in swapped if d["id"] == HALL_LIGHT_ID)
    light["id"] = new_id
    light["datapoints"][0]["id"] = f"{new_id}-001"
    coordinator.follow_renames(swapped)
    assert coordinator.function_anchors["hall_light"].id == new_id
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_while_ha_was_down_is_followed_at_setup(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The store remembers each label's element, so setup pairs the rename."""
    entry = _prepared_entry(hass)
    seeded = _seed_device(
        hass, entry, "hall_light", "Hall Light", {"hall_light_001": Platform.LIGHT}
    )
    _seed_anchors(
        hass_storage,
        entry,
        {"hall_light": {"id": HALL_LIGHT_ID, "mac": MAC_A, "location": 1}},
    )
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _setup_with_export(hass, _project_export(), devices=relabelled, entry=entry)

    device = _device(hass, "Hall Lamp")
    assert device.id == seeded.id
    assert device.name == "Hall Lamp"
    assert find_device(hass, "hall_light") is None
    ent = er.async_get(hass).async_get("light.hall_light")
    assert ent is not None
    assert ent.unique_id == "hall_lamp_001"
    assert ent.device_id == device.id
    assert hass.states.get("light.hall_light").state == "on"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_with_reprovisioning_is_paired_by_hardware_identity(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Renamed AND re-provisioned while HA was down: the address pairs it.

    Re-provisioning gives the node a new UUID, so every function id changes
    and the id-based pairing has nothing to hold on to. The node's Bluetooth
    address and the element location survive, so setup's second pass — after
    the project export is read, still before the platforms — pairs the old
    label with the new one through them.
    """
    entry = _prepared_entry(hass)
    seeded = _seed_device(
        hass, entry, "hall_light", "Hall Light", {"hall_light_001": Platform.LIGHT}
    )
    _seed_device(
        hass,
        entry,
        "hall_button",
        "Hall Button",
        {"hall_button_00e_switch": Platform.SWITCH},
    )
    _seed_anchors(
        hass_storage,
        entry,
        {
            "hall_light": {"id": HALL_LIGHT_ID, "mac": MAC_A, "location": 1},
            "hall_button": {"id": HALL_BUTTON_ID, "mac": MAC_A, "location": 0x40},
        },
    )
    devices = _identified_devices()
    light = next(d for d in devices if d["id"] == HALL_LIGHT_ID)
    light["id"] = function_id_for(NODE_C, 1)
    light["label"] = "Hall Lamp"
    light["datapoints"][0]["id"] = f"{light['id']}-001"
    button = next(d for d in devices if d["id"] == HALL_BUTTON_ID)
    button["id"] = function_id_for(NODE_C, 0x40)
    for datapoint in button["datapoints"]:
        datapoint["id"] = f"{button['id']}-{datapoint['id'].rsplit('-', 1)[-1]}"
    export = _project_export()
    cdb = json.loads(base64.b64decode(export["network"]))
    cdb["nodes"][0]["UUID"] = NODE_C
    export["network"] = base64.b64encode(json.dumps(cdb).encode()).decode()
    export["meta"]["devices"][0]["deviceId"]["nodeId"] = NODE_C
    export["meta"]["devices"][0]["name"] = "Hall Lamp"

    # No refresh other than setup's own: the second pass alone must pair it.
    with patch.object(
        JungHomeDataUpdateCoordinator, "async_request_refresh", AsyncMock()
    ):
        await _setup_with_export(hass, export, devices=devices, entry=entry)
    device = _device(hass, "Hall Lamp")
    assert device.id == seeded.id
    assert device.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    assert find_device(hass, "hall_light") is None
    ent = er.async_get(hass).async_get("light.hall_light")
    assert ent is not None
    assert ent.unique_id == "hall_lamp_001"
    # The button kept its label: same slug, new id, no pairing needed.
    assert _device(hass, "Hall Button").serial_number == MAC_A
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_failed_export_read_keeps_the_anchored_address(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Without identities an unchanged element keeps its stored address and location.

    Rewriting the anchor address-less would quietly disable pairing a later
    rename combined with re-provisioning; a new element (another id) has
    nothing to keep.
    """
    entry = _prepared_entry(hass)
    _seed_anchors(
        hass_storage,
        entry,
        {
            "hall_light": {"id": HALL_LIGHT_ID, "mac": MAC_A, "location": 1},
            "desk_socket": {"id": "idreplaced", "mac": MAC_B, "location": 1},
        },
    )
    await _setup_with_export(hass, None, entry=entry)
    coordinator = entry.runtime_data
    assert coordinator.function_anchors["hall_light"] == FunctionAnchor(
        HALL_LIGHT_ID, MAC_A, 1
    )
    assert coordinator.function_anchors["desk_socket"] == FunctionAnchor(
        DESK_SOCKET_ID, None, None
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_is_not_followed_onto_a_taken_unique_id(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A target unique_id already registered elsewhere: the rename is not followed.

    All-or-nothing: nothing of the old device is touched, the platforms
    register the new label as a new device, and the old one goes stale.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    taken = er.async_get(hass).async_get_or_create(
        Platform.LIGHT, DOMAIN, "hall_lamp_001", config_entry=entry
    )
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    assert "already exists" in caplog.text
    assert dr.async_get(hass).async_get(old.id).identifiers == {(DOMAIN, "hall_light")}
    assert (
        er.async_get(hass).async_get("light.hall_light").unique_id == "hall_light_001"
    )
    assert er.async_get(hass).async_get(taken.entity_id).unique_id == "hall_lamp_001"
    await _poll(hass, coordinator, relabelled, times=STALE_DEVICE_PRUNE_MISSES + 1)
    assert dr.async_get(hass).async_get(old.id) is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_onto_another_gateways_label_is_not_followed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The new identifier belongs to another entry's device: nothing is touched.

    Before Home Assistant 2026.9 device identifiers are unique registry-wide,
    so a second gateway's device can already hold the new slug. The device
    identifier is claimed before any entity is rewritten; rewriting them first
    left the entities keyed to a device that never followed, and every later
    adoption retried the rename and failed the poll.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="5.6.7.8",
        data={CONF_HOST: "5.6.7.8", CONF_TOKEN: "tok"},
    )
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={(DOMAIN, "hall_lamp")},
        name="Hall Lamp",
    )
    old = _device(hass, "Hall Light")
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled, times=2)
    assert coordinator.last_update_success
    assert "treating it as a new device" in caplog.text
    assert dr.async_get(hass).async_get(old.id).identifiers == {(DOMAIN, "hall_light")}
    assert (
        er.async_get(hass).async_get("light.hall_light").unique_id == "hall_light_001"
    )
    assert dr.async_get(hass).async_get(foreign.id).name == "Hall Lamp"
    # Not followed means a new device, registered once — no `hall_light_2`.
    assert er.async_get(hass).async_get("light.hall_lamp").unique_id == "hall_lamp_001"
    assert er.async_get(hass).async_get("light.hall_light_2") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_new_function_is_not_mistaken_for_a_rename(
    hass: HomeAssistant,
) -> None:
    """A label that vanishes while another appears on a different element: two events."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    orphan = _device(hass, "Orphan")
    devices = [d for d in _identified_devices() if d["id"] != "idorphan"]
    devices.append(
        {
            "id": "idgarden",
            "type": "OnOff",
            "label": "Garden",
            "datapoints": [
                {
                    "id": "idgarden-001",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "0"}],
                }
            ],
        }
    )
    await _poll(hass, coordinator, devices)
    garden = _device(hass, "Garden")
    assert garden.id != orphan.id
    assert dr.async_get(hass).async_get(orphan.id) is not None
    assert coordinator.function_anchors["garden"].id == "idgarden"
    assert coordinator.function_anchors["orphan"].id == "idorphan"  # still registered
    await _poll(hass, coordinator, devices, times=STALE_DEVICE_PRUNE_MISSES + 1)
    assert dr.async_get(hass).async_get(orphan.id) is None
    assert "orphan" not in coordinator.function_anchors
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_after_the_old_device_was_deleted_is_a_new_device(
    hass: HomeAssistant,
) -> None:
    """The anchor outlives a manual delete by one adoption; there is nothing to rename."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    without = [d for d in _identified_devices() if d["id"] != HALL_LIGHT_ID]
    await _poll(hass, coordinator, without)
    assert "hall_light" in coordinator.function_anchors  # device still registered
    assert await async_remove_config_entry_device(hass, entry, old) is True
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    lamp = _device(hass, "Hall Lamp")
    assert lamp.id != old.id
    assert hass.states.get("light.hall_lamp").state == "on"
    assert "hall_light" not in coordinator.function_anchors
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rename_of_a_colliding_label_is_left_alone(hass: HomeAssistant) -> None:
    """Renaming onto a label another live device carries is the documented collision."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    old = _device(hass, "Hall Light")
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Desk Socket")
    await _poll(hass, coordinator, relabelled)
    assert dr.async_get(hass).async_get(old.id).identifiers == {(DOMAIN, "hall_light")}
    assert "desk_socket" in coordinator.function_anchors
    assert coordinator.function_anchors["desk_socket"].id == DESK_SOCKET_ID
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_function_anchors_are_persisted_and_removed_with_the_entry(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The map reaches the store after it changes; entry removal deletes it."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, coordinator, relabelled)
    # The save is delayed (`FUNCTION_ANCHORS_SAVE_DELAY`); flush it as core's
    # own tests do rather than racing the loop clock.
    await flush_store(coordinator._anchor_store)
    functions = hass_storage[_anchors_key(entry)]["data"]["functions"]
    assert functions["hall_lamp"] == {"id": HALL_LIGHT_ID, "mac": MAC_A, "location": 1}
    assert "hall_light" not in functions
    assert functions["desk_socket"] == {
        "id": DESK_SOCKET_ID,
        "mac": MAC_B,
        "location": 1,
    }
    assert functions["orphan"] == {"id": "idorphan", "mac": None, "location": None}

    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(relabelled, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert _anchors_key(entry) not in hass_storage


async def test_rename_keeps_a_cleared_area_cleared(hass: HomeAssistant) -> None:
    """The area assigner's once-only record follows the rename.

    A device considered once is never placed again — so an area the user
    cleared on purpose stays cleared. The record is slug-keyed; a rename that
    left it on the old slug made the device look new and re-placed it.
    """
    devices = _identified_devices()
    next(d for d in devices if d["id"] == HALL_LIGHT_ID)["parent_groups"] = ["g1"]
    entry = _prepared_entry(hass)
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_groups_from_api",
        AsyncMock(return_value=[{"id": "g1", "name": "Hallway"}]),
    ):
        await _setup_with_export(hass, _project_export(), devices=devices, entry=entry)
        coordinator = entry.runtime_data
        dev_reg = dr.async_get(hass)
        light = _device(hass, "Hall Light")
        assert light.area_id is not None
        dev_reg.async_update_device(light.id, area_id=None)
        relabelled = _relabelled(devices, HALL_LIGHT_ID, "Hall Lamp")
        await _poll(hass, coordinator, relabelled, times=2)
        renamed = dev_reg.async_get(light.id)
        assert renamed.identifiers == {(DOMAIN, "hall_lamp")}
        assert renamed.area_id is None
        assert "hall_lamp" in entry.data[DATA_AREA_ASSIGNED]
        assert "hall_light" not in entry.data[DATA_AREA_ASSIGNED]
        assert entry.runtime_data is coordinator
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_removed_before_the_delayed_save_leaves_no_store(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A save still pending at removal is flushed on unload, not resurrected later.

    Removal deletes the store through a fresh ``Store`` right after the
    unload; a delayed save still queued on the coordinator's own instance (or
    its final-write listener) used to write the file back afterwards.
    """
    entry = await _setup_with_export(hass, _project_export())
    relabelled = _relabelled(_identified_devices(), HALL_LIGHT_ID, "Hall Lamp")
    await _poll(hass, entry.runtime_data, relabelled)
    store = entry.runtime_data._anchor_store
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(relabelled, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert _anchors_key(entry) not in hass_storage
    # Whatever the old instance still holds, it writes now — as the delay
    # timer or Home Assistant's final write would.
    await flush_store(store)
    assert _anchors_key(entry) not in hass_storage


def _awning(label: str = "Patio Awning", function_id: str = "idawning") -> dict:
    """A position-only cover (an awning once the user flags it inverted)."""
    return {
        "id": function_id,
        "type": "Position",
        "label": label,
        "datapoints": [
            {
                "id": f"{function_id}-001",
                "type": "level",
                "values": [{"key": "level", "value": "0"}],
            }
        ],
    }


async def test_rename_carries_the_inverted_cover_flag(hass: HomeAssistant) -> None:
    """The inverted-covers option is keyed by unique_id, so it follows — no reload.

    The live cover keeps the flag it was built with; the next setup reads it
    under the new unique_id.
    """
    devices = [*_identified_devices(), _awning()]
    entry = _prepared_entry(hass, options={CONF_INVERTED_COVERS: ["patio_awning_001"]})
    renamed = _relabelled(devices, "idawning", "Terrace Awning")
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(devices, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            hass.states.get("cover.patio_awning").attributes["device_class"] == "awning"
        )
        coordinator = entry.runtime_data
        with patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(renamed)),
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert entry.options[CONF_INVERTED_COVERS] == ["terrace_awning_001"]
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data is coordinator, "a followed rename never reloads"
            ent = er.async_get(hass).async_get("cover.patio_awning")
            assert ent.unique_id == "terrace_awning_001"
            state = hass.states.get("cover.patio_awning")
            assert state.attributes["device_class"] == "awning"
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            state = hass.states.get("cover.patio_awning")
            assert state.attributes["device_class"] == "awning"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_rename_keeps_the_other_inverted_covers(hass: HomeAssistant) -> None:
    """Renaming one flagged awning re-points its flag and leaves the others."""
    devices = [
        *_identified_devices(),
        _awning(),
        _awning("Garage Awning", "idgarage"),
    ]
    entry = _prepared_entry(
        hass,
        options={CONF_INVERTED_COVERS: ["patio_awning_001", "garage_awning_001"]},
    )
    renamed = _relabelled(devices, "idawning", "Terrace Awning")
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(devices, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _poll(hass, entry.runtime_data, renamed)
        assert entry.options[CONF_INVERTED_COVERS] == [
            "terrace_awning_001",
            "garage_awning_001",
        ]
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_rename_and_capability_change_in_one_adoption_reloads(
    hass: HomeAssistant,
) -> None:
    """A cover renamed AND given slat tilt in one go still gets its tilt.

    The capability watcher's baseline is slug-keyed; the followed rename
    carries it to the new slug instead of seeding it from the changed list.
    """
    devices = [*_identified_devices(), _awning()]
    changed = _relabelled(devices, "idawning", "Terrace Awning")
    awning = next(d for d in changed if d["id"] == "idawning")
    awning["type"] = "PositionAndAngle"
    awning["datapoints"].append(
        {
            "id": "idawning-002",
            "type": "angle",
            "values": [{"key": "angle", "value": "0"}],
        }
    )
    entry = _prepared_entry(hass)
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(devices, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        with patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(changed)),
        ):
            for _ in range(2):  # a capability change is confirmed on the second
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            assert entry.runtime_data is not coordinator
            ent = er.async_get(hass).async_get("cover.patio_awning")
            assert ent.unique_id == "terrace_awning_001"
            features = hass.states.get("cover.patio_awning").attributes[
                "supported_features"
            ]
            assert features & CoverEntityFeature.SET_TILT_POSITION
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_inverted_cover_renamed_while_ha_was_down(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Followed at setup: the flag moves, and a later entry write does not reload."""
    entry = _prepared_entry(hass, options={CONF_INVERTED_COVERS: ["patio_awning_001"]})
    _seed_device(
        hass,
        entry,
        "patio_awning",
        "Patio Awning",
        {"patio_awning_001": Platform.COVER},
    )
    _seed_anchors(
        hass_storage,
        entry,
        {"patio_awning": {"id": "idawning", "mac": None, "location": None}},
    )
    devices = [*_identified_devices(), _awning("Terrace Awning")]
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(devices, _project_export()):
            stack.enter_context(stub)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.options[CONF_INVERTED_COVERS] == ["terrace_awning_001"]
        state = hass.states.get("cover.patio_awning")
        assert state.attributes["device_class"] == "awning"
        coordinator = entry.runtime_data
        # A data-only write (the area assigner, a fingerprint re-pin) must not
        # read as an options change.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "unrelated": 1}
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is coordinator
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_pruning_a_device_another_gateway_shares_only_detaches_it(
    hass: HomeAssistant,
) -> None:
    """Two gateways with the same label share one device before HA 2026.9.

    The pruner of the gateway that lost the function detaches its own entry
    (and its entities); the other gateway's entities stay.
    """
    entry = await _setup_with_export(hass, _project_export())
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="5.6.7.8",
        data={CONF_HOST: "5.6.7.8", CONF_TOKEN: "tok"},
    )
    other.add_to_hass(hass)
    other_devices = [
        {
            "id": "idotherhall",
            "type": "OnOff",
            "label": "Hall Light",
            "datapoints": [
                {
                    "id": "idotherhall-002",
                    "type": "switch",
                    "values": [{"key": "switch", "value": "0"}],
                }
            ],
        }
    ]
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(other_devices, None):
            stack.enter_context(stub)
        await hass.config_entries.async_setup(other.entry_id)
        await hass.async_block_till_done()
    shared = _device(hass, "Hall Light")
    assert shared.config_entries == {entry.entry_id, other.entry_id}
    ent_reg = er.async_get(hass)
    without = [d for d in _identified_devices() if d["id"] != HALL_LIGHT_ID]
    await _poll(hass, entry.runtime_data, without, times=STALE_DEVICE_PRUNE_MISSES)

    device = dr.async_get(hass).async_get(shared.id)
    assert device is not None
    assert device.config_entries == {other.entry_id}
    assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_001") is None
    assert ent_reg.async_get_entity_id(Platform.LIGHT, DOMAIN, "hall_light_002")
    for loaded in (entry, other):
        await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()


async def test_a_swapped_node_replaces_the_bluetooth_connection(
    hass: HomeAssistant,
) -> None:
    """The same label on a new radio shows the new address only, not both."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    light = _device(hass, "Hall Light")
    assert light.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    coordinator.node_identities = MappingProxyType(
        {
            HALL_LIGHT_ID: NodeIdentity(
                uuid=NODE_C, location=1, mac=MAC_C, unicast=0x0300, primary=True
            )
        }
    )
    coordinator.apply_node_identities()
    light = dr.async_get(hass).async_get(light.id)
    assert light.serial_number == MAC_C
    assert light.connections == {(dr.CONNECTION_BLUETOOTH, MAC_C)}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_swapped_node_keeps_other_kinds_of_connection(
    hass: HomeAssistant,
) -> None:
    """Only the Bluetooth connection is replaced; another kind is left alone."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    light = _device(hass, "Hall Light")
    dr.async_get(hass).async_update_device(
        light.id,
        merge_connections={(dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")},
    )
    coordinator.node_identities = MappingProxyType(
        {
            HALL_LIGHT_ID: NodeIdentity(
                uuid=NODE_C, location=1, mac=MAC_C, unicast=0x0300, primary=True
            )
        }
    )
    coordinator.apply_node_identities()
    assert dr.async_get(hass).async_get(light.id).connections == {
        (dr.CONNECTION_BLUETOOTH, MAC_C),
        (dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01"),
    }
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_pruned_device_reported_again_is_restored_with_its_connection(
    hass: HomeAssistant,
) -> None:
    """The registry's identifier-based restore still applies; the link follows.

    A device absent past the prune threshold and then reported again under the
    SAME label matches its deleted registry row by identifier, so the user's
    area and custom name come back with the same device id — and, because the
    connection is written on entity add rather than restored from the deleted
    row, the successor's link is established the moment the row is back.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    dev_reg = dr.async_get(hass)
    light = _device(hass, "Hall Light")
    area = ar.async_get(hass).async_get_or_create("Hallway")
    dev_reg.async_update_device(light.id, area_id=area.id, name_by_user="Ceiling")
    full = _identified_devices()
    without_light = [d for d in full if d["id"] != HALL_LIGHT_ID]
    await _poll(hass, coordinator, without_light, times=STALE_DEVICE_PRUNE_MISSES)
    assert dev_reg.async_get(light.id) is None

    await _poll(hass, coordinator, full)
    restored = _device(hass, "Hall Light")
    assert restored.id == light.id
    assert restored.area_id == area.id
    assert restored.name_by_user == "Ceiling"
    assert restored.serial_number == MAC_A
    assert restored.connections == {(dr.CONNECTION_BLUETOOTH, MAC_A)}
    assert hass.states.get("light.hall_light").state == "on"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("identities_at_setup", [False, True])
async def test_apply_identities_skips_colliding_slugs(
    hass: HomeAssistant, identities_at_setup: bool
) -> None:
    """Two functions on one registry device cannot take turns writing serials.

    Both coordinator write paths skip the colliding slug: the back-fill after
    a later export read, and the entity-add link when the identities were
    known at registration. In the latter case ``device_info`` still carries
    the registering function's serial number — informational, written once
    by whichever function won the slug — but the connection is never linked.
    """
    devices = _identified_devices()
    devices[3]["label"] = "Hall-Light"  # slugs to hall_light, like "Hall Light"
    entry = await _setup_with_export(
        hass,
        export=_project_export() if identities_at_setup else None,
        devices=devices,
    )
    coordinator = entry.runtime_data
    assert "hall_light" in duplicate_slugs(coordinator.data)
    if not identities_at_setup:
        with patch.object(
            coordinator,
            "_fetch_project_export_from_api",
            AsyncMock(return_value=_project_export()),
        ):
            await coordinator.async_fetch_node_identities()
    shared = _device(hass, "Hall Light")
    assert shared.serial_number == (MAC_A if identities_at_setup else None)
    assert shared.connections == set()
    assert _device(hass, "Hall Button").serial_number == MAC_A
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_node_identity_for_ignores_malformed_ids(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    assert coordinator.node_identity_for({"id": None}) is None  # type: ignore[typeddict-item]
    assert coordinator.node_identity_for({"id": ["x"]}) is None  # type: ignore[typeddict-item]
    assert coordinator.node_identity_for({"label": "no id"}) is None  # type: ignore[typeddict-item]
    assert coordinator.node_identity_for({"id": "idorphan"}) is None  # type: ignore[typeddict-item]


def test_apply_identities_is_a_no_op_without_identities(hass: HomeAssistant) -> None:
    coordinator = bare_coordinator(hass)
    with patch.object(dr, "async_get") as registry:
        coordinator.apply_node_identities()
        coordinator.link_node_identity("some-device", {"id": "idorphan"})  # type: ignore[typeddict-item]
        coordinator.config_entry = None
        coordinator.link_node_identity("some-device", {"id": HALL_LIGHT_ID})  # type: ignore[typeddict-item]
    registry.assert_not_called()


async def test_identity_without_an_address_writes_nothing(hass: HomeAssistant) -> None:
    """A node whose address is unknown (non-EUI-64 UUID, no meta) links nothing.

    Both write paths, plus the link for a registry row that does not exist —
    an entity whose device vanished between registration and its add.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    orphan_device = next(d for d in coordinator.data if d["id"] == "idorphan")
    coordinator.node_identities = MappingProxyType(
        {
            **coordinator.node_identities,
            "idorphan": NodeIdentity(
                uuid="not-an-eui-64-uuid", location=1, primary=True
            ),
        }
    )
    coordinator.apply_node_identities()
    orphan = _device(hass, "Orphan")
    coordinator.link_node_identity(orphan.id, orphan_device)
    coordinator.link_node_identity("no-such-device", orphan_device)
    orphan = _device(hass, "Orphan")
    assert orphan.serial_number is None
    assert orphan.connections == set()


# --- Input hardening (review 2026-09-16) -----------------------------------


@pytest.mark.parametrize(
    ("export", "identified"),
    [
        # A Unicode "digit" in a CDB element index: ``int("²")`` raised out of
        # the parser and setup ended in SETUP_ERROR (no retry, no entities).
        # Now the index reads as absent and the element is still identified.
        (
            {
                "meta": {"devices": []},
                "network": base64.b64encode(
                    json.dumps(
                        {
                            "nodes": [
                                {
                                    "UUID": NODE_A,
                                    "unicastAddress": "00CF",
                                    "elements": [{"index": "²", "location": "0040"}],
                                }
                            ]
                        }
                    ).encode()
                ).decode(),
            },
            [function_id_for(NODE_A, 0x40)],
        ),
        # An inner CDB nested past the JSON parser's stack (RecursionError):
        # nothing learned, setup unaffected.
        (
            {
                "meta": {"devices": []},
                "network": base64.b64encode(
                    ("[" * 1_000_000 + "]" * 1_000_000).encode()
                ).decode(),
            },
            [],
        ),
    ],
    ids=["unicode_digit_index", "deeply_nested_cdb"],
)
async def test_a_malformed_export_does_not_fail_setup(
    hass: HomeAssistant,
    export: dict,
    identified: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The export is an optional enrichment; a bad one costs identities only."""
    caplog.set_level(logging.DEBUG)
    entry = await _setup_with_export(hass, export=export)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("light.hall_light") is not None
    assert list(entry.runtime_data.node_identities) == identified
    assert "Error setting up entry" not in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entity_without_a_device_row_links_nothing(hass: HomeAssistant) -> None:
    """``async_added_to_hass`` only links when the platform gave the entity a device."""
    coordinator = bare_coordinator(hass)
    device = _identified_devices()[0]
    light = JungHomeLight(coordinator, device, device["datapoints"][0])
    light.hass = hass
    assert light.device_entry is None
    with patch.object(coordinator, "link_node_identity") as link:
        await light.async_added_to_hass()
    link.assert_not_called()
    # Adding a listener armed the poll timer (landmine 2 in CLAUDE.md).
    await coordinator.async_shutdown()


@pytest.mark.parametrize("name", list(_MALFORMED_DEVICES))
async def test_setup_survives_one_malformed_device_from_the_gateway(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One malformed device object in ``GET /functions`` costs that device only.

    Through the real REST path (the trust boundary sits in
    ``_fetch_devices_from_api``): every other device's entities come up, no
    platform fails to set up, nothing logs a traceback, the sanitiser warns
    exactly once, and the entry ends LOADED. Before the boundary existed each
    of these shapes failed differently — a non-string label made every poll
    raise (SETUP_RETRY forever), non-list datapoints raised out of
    ``async_setup_entry`` (SETUP_ERROR), a datapoint without an id lost the
    whole light platform, and ``sw_version: ["1"]`` reached the device
    registry as a deprecation report.
    """
    caplog.set_level(logging.WARNING)
    aioclient_mock.get(
        "https://1.2.3.4/api/junghome/functions",
        json=[*copy.deepcopy(PRISTINE_DEVICES), malformed_device(name)],
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    with patch.object(
        JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("light.strip").state == "on"
        assert hass.states.get("climate.living_room") is not None
        assert hass.states.get("sensor.boiler_power").state == "5.0"
        assert hass.states.get("event.button_a_up") is not None
        assert hass.states.get("binary_sensor.jung_home_gateway_connection") is not None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert "Error while setting up junghome platform" not in caplog.text
        # One WARNING per adoption, each naming the count only. Setup polls
        # once: the platforms add from the adopted list, no refresh of their own.
        repairs = [r for r in caplog.records if "malformed item(s)" in r.getMessage()]
        assert aioclient_mock.call_count == 1
        assert len(repairs) == aioclient_mock.call_count
        assert {r.getMessage() for r in repairs} == {repairs[0].getMessage()}
        assert "Fuzz" not in caplog.text

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_removing_an_entry_that_never_loaded_withdraws_its_issues(
    hass: HomeAssistant,
) -> None:
    """A certificate mismatch keeps an entry in SETUP_RETRY; deleting it must not leave the issue behind.

    ``stop()`` deletes the coordinator's issues on unload, but an entry that never loaded never ran it.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    registry = ir.async_get(hass)
    for key in (ISSUE_TLS_MISMATCH, ISSUE_PUSH_FAILURE):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{key}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=key,
        )
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get_issue(DOMAIN, f"{ISSUE_TLS_MISMATCH}_{entry.entry_id}")
        is None
    )
    assert (
        registry.async_get_issue(DOMAIN, f"{ISSUE_PUSH_FAILURE}_{entry.entry_id}")
        is None
    )


async def test_an_empty_export_re_read_keeps_the_known_identities(
    hass: HomeAssistant,
) -> None:
    """A re-read that resolves nothing must not wipe the identities already held.

    The debounced re-read (an unidentified function appeared) can come back
    with a well-formed export the parser finds nothing in — an empty
    ``meta`` and an empty ``network`` — and replacing the map with that
    would strip every serial number and connection the registry carries
    until a later read succeeds. Keeping the previous map is strictly better.
    """
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    before = dict(coordinator.node_identities)
    assert len(before) == 3
    with patch.object(
        coordinator,
        "_fetch_project_export_from_api",
        AsyncMock(return_value={"version": "1.1", "meta": {}, "network": ""}),
    ):
        await coordinator.async_fetch_node_identities()
    assert dict(coordinator.node_identities) == before
    assert _device(hass, "Hall Light").serial_number == MAC_A
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_apply_identities_skips_devices_whose_labels_collide(
    hass: HomeAssistant,
) -> None:
    """Two functions on one registry device never take turns writing their MACs.

    ``"Lamp 1"`` (a light) and ``"Lamp-1"`` (a socket) slug identically, so
    they share one registry device — the documented limitation. They sit on
    different nodes here, each the node's primary element: without the
    ``duplicate_slugs`` skip, ``apply_node_identities`` would key both by the
    one slug, the last one listed would win, and the shared device would be
    linked by Bluetooth connection to whichever node the gateway's list order
    favoured on that pass. (The serial number is informational and each
    entity's ``device_info`` writes its own node's at registration, so the
    shared row shows one of the two; the connection is what resolves devices
    and is never written for a colliding slug.)
    """
    devices = _identified_devices()
    devices[0]["label"] = "Lamp 1"  # NODE_A, primary element
    devices[2]["label"] = "Lamp-1"  # NODE_B, primary element
    entry = await _setup_with_export(hass, _project_export(), devices=devices)
    coordinator = entry.runtime_data
    assert len(coordinator.node_identities) == 3
    shared = _device(hass, "Lamp 1")
    assert shared is _device(hass, "Lamp-1")
    assert shared.connections == set()

    coordinator.apply_node_identities()
    shared = _device(hass, "Lamp 1")
    assert shared.connections == set()
    # The non-colliding device on NODE_A still gets its node's address.
    assert _device(hass, "Hall Button").serial_number == MAC_A
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_hub_keeps_its_version_when_the_setup_time_read_fails(
    hass: HomeAssistant,
) -> None:
    """A setup that cannot read the version leaves the hub's stored one alone.

    The state DB answers ``"0.0.0"`` until the board controller has reported
    the version (right after a gateway reboot), and the read can time out. The
    hub's ``DeviceInfo`` used to pass ``sw_version=None`` then, which the
    registry applies as a change — blanking the version an earlier run wrote,
    until the next stable WebSocket session re-read it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1.2.3.4",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)

    async def _version(_self, _host: str) -> dict[str, str]:
        return {"version_release": "2.1.3", "version_build": "2840"}

    devices = AsyncMock(return_value=copy.deepcopy(PRISTINE_DEVICES))
    with (
        patch.object(JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", devices),
        patch.object(
            JungHomeDataUpdateCoordinator, "_fetch_version_from_api", _version
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hub = find_device(hass, gateway_device_id(entry))
        assert hub is not None
        assert hub.sw_version == "2.1.3 (2840)"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    # Second start: the version read yields nothing (the autouse default).
    with (
        patch.object(JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", devices),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hub = find_device(hass, gateway_device_id(entry))
        assert hub is not None
        assert hub.sw_version == "2.1.3 (2840)"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


# --- A reload started inside an adoption ------------------------------------
#
# The id-churn check and the capability watcher schedule a reload from inside
# a device-list adoption, and it runs eagerly up to its first suspension: the
# platforms are reset while their discovery listeners are still attached. A
# device new in that same list must not be added to the dead platform.


def _garden(value: str = "0") -> dict:
    return {
        "id": "idgarden",
        "type": "OnOff",
        "label": "Garden",
        "datapoints": [
            {
                "id": "idgarden-001",
                "type": "switch",
                "values": [{"key": "switch", "value": value}],
            }
        ],
    }


def _live_light(hass: HomeAssistant, entity_id: str) -> JungHomeLight:
    entity = hass.data[DATA_INSTANCES]["light"].get_entity(entity_id)
    assert isinstance(entity, JungHomeLight)
    return entity


async def test_device_new_in_an_id_churn_adoption_joins_the_reloaded_entry(
    hass: HomeAssistant,
) -> None:
    """Id churn reloads; a device new in the same list lives on the new coordinator."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    devices = _identified_devices()
    orphan = next(d for d in devices if d["id"] == "idorphan")
    orphan["id"] = "idorphan2"
    orphan["datapoints"][0]["id"] = "idorphan2-001"
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs([*devices, _garden("0")], _project_export()):
            stack.enter_context(stub)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        reloaded = entry.runtime_data
        assert reloaded is not coordinator, "id churn reloads"
        assert _live_light(hass, "light.garden").coordinator is reloaded
    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs([*devices, _garden("1")], _project_export()):
            stack.enter_context(stub)
        await reloaded.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("light.garden").state == "on"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_device_new_in_a_capability_reload_broadcast_joins_the_reloaded_entry(
    hass: HomeAssistant,
) -> None:
    """A confirmed capability change and a new device in one `functions` broadcast."""
    entry = await _setup_with_export(hass, _project_export())
    coordinator = entry.runtime_data
    changed = _identified_devices()
    light = next(d for d in changed if d["id"] == HALL_LIGHT_ID)
    light["datapoints"].append(
        {
            "id": f"{HALL_LIGHT_ID}-002",
            "type": "brightness",
            "values": [{"key": "brightness", "value": "50"}],
        }
    )
    with_garden = [*copy.deepcopy(changed), _garden()]

    async def slow_fetch(*_args: object, **_kwargs: object) -> list[dict]:
        await asyncio.sleep(0)  # a real HTTP round trip suspends
        return copy.deepcopy(with_garden)

    with contextlib.ExitStack() as stack:
        for stub in _gateway_stubs(with_garden, _project_export()):
            stack.enter_context(stub)
        stack.enter_context(
            patch.object(
                JungHomeDataUpdateCoordinator, "_fetch_devices_from_api", slow_fetch
            )
        )
        coordinator._handle_functions_broadcast(copy.deepcopy(changed))  # 1st sighting
        await hass.async_block_till_done()
        coordinator._handle_functions_broadcast(with_garden)  # confirms, adds Garden
        await hass.async_block_till_done()
        reloaded = entry.runtime_data
        assert reloaded is not coordinator, "the capability change reloads"
        assert _live_light(hass, "light.garden").coordinator is reloaded
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
