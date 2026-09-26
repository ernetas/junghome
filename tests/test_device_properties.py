"""The verbose device endpoint: energy counters, firmware revisions, reachability.

``GET /devices/?verbose=true`` (deprecated/experimental, live on 2.1.3) returns
the middleware's raw device objects, keyed by the function id. The
integration reads three things out of them: a metering socket's cumulative
``total_device_energy_use`` (the Energy Dashboard's sensor), every device's
``software_revision`` (a button on firmware older than 2.2.0 reports each tap
once, so duplicate suppression is skipped for it — see ``test_event.py``), and
per-state reachability (diagnostics). Shapes below mirror the 2026-09-16 probe.
"""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import (
    DEVICE_PROPERTIES_REFRESH_INTERVAL,
    JungHomeDataUpdateCoordinator,
)
from custom_components.junghome.diagnostics import async_get_config_entry_diagnostics
from custom_components.junghome.models import (
    DeviceProperties,
    parse_device_properties,
    parse_devices_verbose,
)
from tests.conftest import PRISTINE_DEVICES, _fake_run_websocket, bare_coordinator

SOCKET = "idsock1"  # the fixture's metering socket ("Boiler")
LIGHT = "idlight1"


def _verbose(  # noqa: PLR0913 - one keyword per wire field under test
    device_id: str,
    *,
    device_type: str = "SocketEnergy",
    energy: float | None = 209655,
    energy_present: bool = True,
    revision: object = (2, 2, 0, 1),
    reachable: bool = True,
) -> dict:
    """One verbose device object, in the wire shape of the 2026-09-16 probe."""
    props: dict = {}
    if energy_present:
        props["total_device_energy_use"] = {
            "state_type": "total_device_energy_use",
            "value": energy,
            "profile": {"unit": "Wh", "range": {"min": 0, "max": 4294967293}},
            "statistics": {"reachable": reachable, "last_seen": 1789549028},
        }
    if revision is not None:
        props["software_revision"] = {
            "state_type": "software_revision",
            "value": list(revision) if isinstance(revision, tuple) else revision,
            "profile": {"unit": ""},
        }
    return {
        "device_id": device_id,
        "device_type": device_type,
        "label": "x",
        "groups": [],
        "scenes": [],
        "hasBattery": False,
        "states": {
            "switch": {
                "state_id": f"{device_id}-001",
                "state_type": "switch",
                "value": 1,
                "statistics": {"reachable": reachable, "connection_quality": 100},
            }
        },
        "property": props,
    }


def _everything_but(*ids: str) -> list[dict]:
    """Verbose objects for every fixture device except ``ids``: no counter."""
    return [
        _verbose(d["id"], energy_present=False, device_type="Other")
        for d in PRISTINE_DEVICES
        if d["id"] not in ids
    ]


def test_parse_reads_energy_revision_and_reachability() -> None:
    """The three fields, from the probe's shape."""
    light = _verbose(
        LIGHT,
        energy_present=False,
        device_type="OnOffLight",
        revision=(2, 2, 0, 2),
        reachable=False,
    )
    assert parse_devices_verbose([_verbose(SOCKET), light]) == {
        SOCKET: DeviceProperties(
            has_energy=True,
            energy_wh=209655.0,
            software_revision=(2, 2, 0, 1),
            reachable=True,
        ),
        LIGHT: DeviceProperties(
            has_energy=False,
            energy_wh=None,
            software_revision=(2, 2, 0, 2),
            reachable=False,
        ),
    }


def test_parse_tolerates_every_shape_seen_or_plausible() -> None:
    """Dict or list containers, a null counter, kWh scaling, string revisions, junk."""
    # `property`/`states` as lists, a counter the middleware has not polled
    # yet (null), a revision spelled as a string.
    doc = _verbose(SOCKET, energy=None, revision="2.1.9")
    doc["property"] = list(doc["property"].values())
    doc["states"] = list(doc["states"].values())
    assert parse_device_properties(doc) == DeviceProperties(
        has_energy=True, energy_wh=None, software_revision=(2, 1, 9), reachable=True
    )
    # A kWh-labelled counter is scaled to Wh; a negative one is not a counter.
    doc = _verbose(SOCKET, energy=12.5)
    doc["property"]["total_device_energy_use"]["profile"]["unit"] = "kWh"
    assert parse_device_properties(doc).energy_wh == 12500.0
    doc["property"]["total_device_energy_use"]["value"] = -1
    doc["property"]["total_device_energy_use"]["profile"]["unit"] = "Wh"
    assert parse_device_properties(doc).energy_wh is None
    # Unparseable revisions read as unknown; booleans are not numbers.
    for bad in ([2, "x"], [True, 2], -1, "", [], "2..2"):
        doc = _verbose(SOCKET, revision=bad)
        assert parse_device_properties(doc).software_revision is None, bad
    # A property of another kind (key_mode, ...) is passed over.
    doc = _verbose(SOCKET)
    doc["property"]["key_mode"] = {"state_type": "key_mode", "value": 6}
    assert parse_device_properties(doc).software_revision == (2, 2, 0, 1)
    # No states -> reachability unknown; no property map -> nothing known.
    doc = _verbose(SOCKET)
    doc["states"] = {}
    doc["property"] = "nope"
    assert parse_device_properties(doc) == DeviceProperties(reachable=None)
    # Not a device object at all.
    assert parse_device_properties({"device_type": "x"}) is None
    assert parse_device_properties("x") is None
    assert parse_devices_verbose({"not": "a list"}) == {}
    assert parse_devices_verbose([1, None, {"device_id": 3}, _verbose(LIGHT)]) == {
        LIGHT: parse_device_properties(_verbose(LIGHT))
    }


@asynccontextmanager
async def _running(
    hass: HomeAssistant, verbose: object, unique_id: str = "1.2.3.4"
) -> AsyncIterator[MockConfigEntry]:
    """A running entry whose REST reads stay stubbed for the whole test.

    The stubs must outlive setup: the REST poll (60 s) fires on every clock
    tick the tests below make, and an unstubbed fetch would open a socket
    and mark every entity unavailable.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: "tok"},
    )
    entry.add_to_hass(hass)
    verbose_mock = (
        AsyncMock(side_effect=verbose)
        if isinstance(verbose, Exception)
        else AsyncMock(return_value=verbose)
    )
    with (
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_from_api",
            AsyncMock(return_value=copy.deepcopy(PRISTINE_DEVICES)),
        ),
        patch.object(
            JungHomeDataUpdateCoordinator,
            "_fetch_devices_verbose_from_api",
            verbose_mock,
        ),
        patch.object(
            JungHomeDataUpdateCoordinator, "_run_websocket", _fake_run_websocket
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=DEVICE_PROPERTIES_REFRESH_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_setup_reads_properties_and_creates_the_energy_sensor(
    hass: HomeAssistant,
) -> None:
    """A socket with the counter gets a TOTAL_INCREASING Wh sensor; a light does not."""
    async with _running(
        hass, [_verbose(SOCKET), _verbose(LIGHT, energy_present=False)]
    ) as entry:
        coordinator = entry.runtime_data
        assert coordinator.device_properties[SOCKET].energy_wh == 209655.0
        state = hass.states.get("sensor.boiler_total_energy")
        assert state is not None
        assert state.state == "209655.0"
        assert state.attributes["unit_of_measurement"] == "Wh"
        assert state.attributes["device_class"] == "energy"
        assert state.attributes["state_class"] == "total_increasing"
        assert hass.states.get("sensor.hall_light_total_energy") is None
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["device_properties"][SOCKET] == {
            "has_energy": True,
            "energy_wh": 209655.0,
            "software_revision": (2, 2, 0, 1),
            "reachable": True,
        }


async def test_counter_not_yet_polled_reads_unknown(hass: HomeAssistant) -> None:
    """The property exists (so the sensor does) but its value is still null."""
    async with _running(hass, [_verbose(SOCKET, energy=None)]):
        assert hass.states.get("sensor.boiler_total_energy").state == "unknown"


async def test_periodic_refresh_re_reads_each_counter_on_its_own_endpoint(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Every interval: one small read per energy device, a state write on change.

    The full list is NOT re-read while every live device is covered, and an
    unchanged counter writes nothing; a failed or empty read leaves the value.
    """
    single = AsyncMock(return_value=_verbose(SOCKET, energy=209700))
    full = AsyncMock(return_value=[])
    async with _running(hass, [_verbose(SOCKET), *_everything_but(SOCKET)]) as entry:
        coordinator = entry.runtime_data
        with (
            patch.object(coordinator, "_fetch_device_verbose_from_api", single),
            patch.object(coordinator, "_fetch_devices_verbose_from_api", full),
        ):
            await _tick(hass, freezer)
            assert single.await_count == 1
            assert single.await_args.args[2] == SOCKET
            assert full.await_count == 0
            assert hass.states.get("sensor.boiler_total_energy").state == "209700.0"
            last_updated = hass.states.get("sensor.boiler_total_energy").last_updated

            # Same value again: read, nothing dispatched.
            await _tick(hass, freezer)
            assert single.await_count == 2
            assert (
                hass.states.get("sensor.boiler_total_energy").last_updated
                == last_updated
            )

            # A transport failure, then a missing body: the value stays.
            single.side_effect = aiohttp.ClientError()
            await _tick(hass, freezer)
            single.side_effect = None
            single.return_value = None
            await _tick(hass, freezer)
            assert hass.states.get("sensor.boiler_total_energy").state == "209700.0"
            assert single.await_count == 4
    # Unloaded: the timer is gone.
    with patch.object(coordinator, "_fetch_device_verbose_from_api", single):
        await _tick(hass, freezer)
    assert single.await_count == 4


async def test_refresh_re_reads_the_full_list_for_a_function_added_since(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A function that appeared after the last full read triggers one full read."""
    initial = [_verbose(SOCKET), *_everything_but(SOCKET)]
    async with _running(hass, initial) as entry:
        coordinator = entry.runtime_data
        added = {**copy.deepcopy(PRISTINE_DEVICES[0]), "id": "idnew1", "label": "New"}
        full = AsyncMock(
            return_value=[
                *initial,
                _verbose("idnew1", energy_present=False, device_type="Other"),
            ]
        )
        single = AsyncMock(return_value=_verbose(SOCKET))
        grown = [*copy.deepcopy(PRISTINE_DEVICES), added]
        with (
            patch.object(coordinator, "_fetch_devices_verbose_from_api", full),
            patch.object(coordinator, "_fetch_device_verbose_from_api", single),
            # The tick also fires the REST poll; it must agree with the broadcast.
            patch.object(
                coordinator,
                "_fetch_devices_from_api",
                AsyncMock(return_value=copy.deepcopy(grown)),
            ),
        ):
            # The app added a device: the gateway broadcasts the new list.
            coordinator._handle_websocket_message({"type": "functions", "data": grown})
            await hass.async_block_till_done()
            await _tick(hass, freezer)
            assert full.await_count == 1
            single.assert_not_awaited()
            assert "idnew1" in coordinator.device_properties
            # Now everything is covered: the next tick reads the counter alone.
            await _tick(hass, freezer)
            assert full.await_count == 1
            assert single.await_count == 1


async def test_a_function_adopted_during_the_full_read_is_read_next_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The answer speaks for the functions live when it was asked, not after.

    A function the app adds while the ~190 KB body is in flight is not in the
    answer; counting it as asked would mean it is never read (a new metering
    socket without its energy sensor until a restart).
    """
    initial = [_verbose(SOCKET), *_everything_but(SOCKET)]
    async with _running(hass, initial) as entry:
        coordinator = entry.runtime_data

        def added(function_id: str) -> dict:
            return {
                **copy.deepcopy(PRISTINE_DEVICES[0]),
                "id": function_id,
                "label": function_id,
            }

        with_a = [*copy.deepcopy(PRISTINE_DEVICES), added("idnewA")]
        with_ab = [*with_a, added("idnewB")]
        listed = [
            *initial,
            _verbose("idnewA", energy_present=False, device_type="Other"),
        ]

        async def full_read(*_args: object) -> list[dict]:
            if full.await_count == 1:  # B is added while the body is in flight
                await asyncio.sleep(0)
                coordinator._handle_websocket_message(
                    {"type": "functions", "data": copy.deepcopy(with_ab)}
                )
                return listed
            return [
                *listed,
                _verbose("idnewB", energy_present=False, device_type="Other"),
            ]

        full = AsyncMock(side_effect=full_read)
        with (
            patch.object(coordinator, "_fetch_devices_verbose_from_api", full),
            patch.object(
                coordinator,
                "_fetch_device_verbose_from_api",
                AsyncMock(return_value=_verbose(SOCKET)),
            ),
            patch.object(
                coordinator,
                "_fetch_devices_from_api",
                AsyncMock(side_effect=lambda *_: copy.deepcopy(coordinator.data)),
            ),
        ):
            coordinator._handle_websocket_message({"type": "functions", "data": with_a})
            await hass.async_block_till_done()
            await _tick(hass, freezer)
            assert full.await_count == 1
            assert "idnewB" not in coordinator.device_properties
            await _tick(hass, freezer)
            assert full.await_count == 2
            assert "idnewB" in coordinator.device_properties


async def test_an_empty_answer_keeps_the_properties(hass: HomeAssistant) -> None:
    """An answered-but-empty list is not "no properties": nothing is wiped."""
    async with _running(hass, [_verbose(SOCKET)]) as entry:
        coordinator = entry.runtime_data
        before = coordinator.device_properties
        assert SOCKET in before
        with patch.object(
            coordinator, "_fetch_devices_verbose_from_api", AsyncMock(return_value=[])
        ):
            await coordinator.async_fetch_device_properties()
        assert coordinator.device_properties == before


async def test_a_function_the_list_omits_is_not_asked_for_every_interval(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The endpoint answered once without it: no ~190 KB re-read every 5 min.

    Only a function that appeared after that answer asks for the list again;
    one the list left out will be left out next time too.
    """
    async with _running(hass, _everything_but(SOCKET)) as entry:
        coordinator = entry.runtime_data
        full = AsyncMock(return_value=_everything_but(SOCKET))
        with patch.object(coordinator, "_fetch_devices_verbose_from_api", full):
            await _tick(hass, freezer)
            await _tick(hass, freezer)
        full.assert_not_awaited()
        assert SOCKET not in coordinator.device_properties
        assert hass.states.get("sensor.boiler_total_energy") is None


async def test_a_missing_endpoint_is_asked_again_each_interval(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Firmware without the endpoint (or a failed read) is retried: one small request."""
    async with _running(hass, None) as entry:
        coordinator = entry.runtime_data
        full = AsyncMock(return_value=None)
        with patch.object(coordinator, "_fetch_devices_verbose_from_api", full):
            await _tick(hass, freezer)
            await _tick(hass, freezer)
        assert full.await_count == 2


@pytest.mark.parametrize(
    "verbose", [None, aiohttp.ClientError(), TimeoutError(), "junk", [1, 2]]
)
async def test_properties_are_best_effort(hass: HomeAssistant, verbose: object) -> None:
    """No endpoint, a transport failure or junk: no sensor, setup unaffected."""
    async with _running(hass, verbose) as entry:
        assert dict(entry.runtime_data.device_properties) == {}
        assert hass.states.get("sensor.boiler_total_energy") is None
        assert hass.states.get("switch.boiler") is not None


async def test_refresh_runs_are_not_stacked(hass: HomeAssistant) -> None:
    """A tick landing while a refresh is still reading is skipped."""
    async with _running(hass, [_verbose(SOCKET), *_everything_but(SOCKET)]) as entry:
        coordinator = entry.runtime_data
        calls = 0

        async def _slow(*_args: object) -> dict:
            nonlocal calls
            calls += 1
            await coordinator._async_refresh_device_properties(None)  # re-entrant tick
            return _verbose(SOCKET, energy=1)

        with patch.object(coordinator, "_fetch_device_verbose_from_api", _slow):
            await coordinator._async_refresh_device_properties(None)
        assert calls == 1
        assert coordinator.device_properties[SOCKET].energy_wh == 1.0


@pytest.mark.real_device_properties_fetch
async def test_fetches_treat_non_200_and_odd_bodies_as_nothing(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Older firmware (404) and unexpected bodies read as "no properties"."""
    coordinator = bare_coordinator(hass)
    base = f"https://{coordinator.config['host']}/api/junghome/devices"
    aioclient_mock.get(f"{base}/?verbose=true", status=404)
    aioclient_mock.get(f"{base}/idx?verbose=true", status=404)
    assert await coordinator._fetch_devices_verbose_from_api("h", "t") is None
    assert await coordinator._fetch_device_verbose_from_api("h", "t", "idx") is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/?verbose=true", json={"not": "a list"})
    aioclient_mock.get(f"{base}/idx?verbose=true", json=["not", "a dict"])
    assert await coordinator._fetch_devices_verbose_from_api("h", "t") is None
    assert await coordinator._fetch_device_verbose_from_api("h", "t", "idx") is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{base}/?verbose=true", json=[_verbose(SOCKET)])
    aioclient_mock.get(f"{base}/idx?verbose=true", json=_verbose("idx"))
    assert await coordinator._fetch_devices_verbose_from_api("h", "t") == [
        _verbose(SOCKET)
    ]
    assert await coordinator._fetch_device_verbose_from_api(
        "h", "t", "idx"
    ) == _verbose("idx")
    # A device without a string id has no properties to look up.
    assert coordinator.device_properties_for({"label": "no id"}) is None
    await coordinator.async_shutdown()


@pytest.mark.real_device_properties_fetch
@pytest.mark.parametrize(
    "failure",
    [
        {"exc": aiohttp.ClientError()},
        {"exc": TimeoutError()},
        # A truncated body: `response.json()` raises JSONDecodeError — a
        # ValueError, not a ClientError.
        {"text": '[{"device_id": "idso'},
    ],
    ids=["unreachable", "timeout", "not-json"],
)
async def test_a_failed_read_leaves_the_properties(
    hass: HomeAssistant, aioclient_mock, failure: dict
) -> None:
    """Each gateway failure is caught on both reads, and the properties stay.

    On the full list (setup, membership change) and on the per-device counter
    re-read alike, nothing propagates out of the best-effort enrichment.
    """
    coordinator = bare_coordinator(hass)
    base = f"https://{coordinator.config['host']}/api/junghome/devices"
    known = parse_devices_verbose([_verbose(SOCKET)])
    coordinator.device_properties = MappingProxyType(known)
    aioclient_mock.get(f"{base}/?verbose=true", **failure)
    await coordinator.async_fetch_device_properties()
    assert coordinator.device_properties == known
    # Not answered: the next interval asks for the full list again.
    assert coordinator._properties_listed_for is None

    # Every live function listed: the interval re-reads the counter alone.
    coordinator._properties_listed_for = frozenset()
    aioclient_mock.get(f"{base}/{SOCKET}?verbose=true", **failure)
    await coordinator._refresh_device_properties()
    assert aioclient_mock.call_count == 2
    assert coordinator.device_properties == known
    await coordinator.async_shutdown()


async def test_properties_refresh_notifies_only_on_a_change(
    hass: HomeAssistant,
) -> None:
    """Listeners hear about a re-read only when it changed something.

    Each notification rewrites every entity of the entry. An unchanged state
    write leaves ``last_updated`` alone, so a state-based check cannot see a
    spurious one; the listener calls are counted instead — on both paths, the
    full list and the per-device counter re-read.
    """
    coordinator = bare_coordinator(hass)
    calls: list[None] = []
    unsub = coordinator.async_add_listener(lambda: calls.append(None))
    full = AsyncMock(return_value=[_verbose(SOCKET)])
    single = AsyncMock(return_value=_verbose(SOCKET))
    with (
        patch.object(coordinator, "_fetch_devices_verbose_from_api", full),
        patch.object(coordinator, "_fetch_device_verbose_from_api", single),
    ):
        # Nothing known yet: the full list, which teaches something.
        await coordinator._refresh_device_properties()
        assert (full.await_count, len(calls)) == (1, 1)
        # The full list again (as after a membership change), same answer.
        coordinator._properties_listed_for = None
        await coordinator._refresh_device_properties()
        assert (full.await_count, len(calls)) == (2, 1)
        # Covered: the counter alone, unchanged, then changed.
        await coordinator._refresh_device_properties()
        assert (single.await_count, len(calls)) == (1, 1)
        single.return_value = _verbose(SOCKET, energy=209700)
        await coordinator._refresh_device_properties()
        assert (single.await_count, len(calls)) == (2, 2)
    assert coordinator.device_properties[SOCKET].energy_wh == 209700.0
    unsub()
    await coordinator.async_shutdown()


async def test_energy_sensor_skips_another_devices_push(hass: HomeAssistant) -> None:
    """A per-datapoint push for the light does not rewrite the socket's counter."""
    async with _running(hass, [_verbose(SOCKET)]) as entry:
        before = hass.states.get("sensor.boiler_total_energy").last_updated
        entry.runtime_data._handle_websocket_message(
            {
                "type": "datapoint",
                "data": {
                    "id": f"{LIGHT}-001",
                    "values": [{"key": "switch", "value": "1"}],
                },
            }
        )
        await hass.async_block_till_done()
        assert hass.states.get("sensor.boiler_total_energy").last_updated == before
