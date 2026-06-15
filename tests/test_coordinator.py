"""Tests for the Jung Home data update coordinator."""

from unittest.mock import Mock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.junghome.const import DOMAIN
from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


def _coordinator(hass: HomeAssistant) -> JungHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    return JungHomeDataUpdateCoordinator(hass, {"host": "h", "token": "t"}, entry)


async def test_update_raises_auth_failed_on_401(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    err = aiohttp.ClientResponseError(Mock(), (), status=401)
    with (
        patch.object(coordinator, "_fetch_devices_from_api", side_effect=err),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()


async def test_update_raises_update_failed_on_client_error(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    with (
        patch.object(
            coordinator,
            "_fetch_devices_from_api",
            side_effect=aiohttp.ClientError("boom"),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()


async def test_reload_scheduled_when_device_ids_change(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._device_ids = {"katilas": "idOLD"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed([{"id": "idNEW", "label": "Katilas"}])
    reload.assert_called_once()


async def test_no_reload_when_device_ids_stable(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._device_ids = {"katilas": "idSAME"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._reload_if_device_ids_changed(
            [{"id": "idSAME", "label": "Katilas"}]
        )
    reload.assert_not_called()
