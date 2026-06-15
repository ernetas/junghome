"""Data update coordinator for Jung Home (REST polling + WebSocket push)."""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, device_slug
from .models import Device

_LOGGER = logging.getLogger(__name__)

# WebSocket reconnect backoff bounds (seconds).
INITIAL_RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60

# Config entry carrying the coordinator as runtime_data.
type JungHomeConfigEntry = ConfigEntry[JungHomeDataUpdateCoordinator]


class JungHomeDataUpdateCoordinator(DataUpdateCoordinator[list[Device]]):
    """Class to manage fetching data from the Jung Home API."""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], config_entry: ConfigEntry
    ) -> None:
        """Initialize the coordinator."""
        self.config = config
        self.websocket: aiohttp.ClientWebSocketResponse | None = None
        self.ws_connected: bool = False
        # The datapoint id whose WebSocket push is being dispatched right now, or
        # None for REST-poll-driven updates. Event entities read this to fire on
        # a genuine push edge rather than diffing snapshots (see event.py). It is
        # set only for the duration of one synchronous `async_set_updated_data`
        # dispatch, so REST re-reads (which leave it None) never fire events.
        self.pushed_datapoint_id: str | None = None
        # Gateway firmware version, reported by the WebSocket "version" frame.
        self.gateway_version: str | None = None
        # Stable-slug -> volatile device id, to detect firmware-update id changes.
        self._device_ids: dict[str, str] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._closing = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Jung Home",
            update_interval=timedelta(minutes=1),
        )

    async def _async_update_data(self) -> list[Device]:
        """Fetch data from the API."""
        _LOGGER.debug("Fetching new device data from Jung Home API")
        try:
            response = await self._fetch_devices_from_api(
                self.config["host"], self.config["token"]
            )
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                # Token revoked/expired — trigger Home Assistant's reauth flow.
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except TimeoutError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        if response is None:
            _LOGGER.error("Received None response from API")
            return []  # Returning empty list ensures entities don't break
        _LOGGER.debug("API Response: %s", response)
        self._reload_if_device_ids_changed(response)
        # `async_set_updated_data` is automatically called with this.
        return response

    def _reload_if_device_ids_changed(self, devices: list[Device]) -> None:
        """Reload the entry if the gateway regenerated its device ids.

        The gateway assigns new volatile device/datapoint ids on a firmware
        update; entities cache those ids, so without a reload they can no longer
        find their datapoint (state stops updating, commands target dead ids).
        unique_ids are label-based and survive the reload.
        """
        new_ids = {device_slug(d): d["id"] for d in devices if d.get("id")}
        changed = any(
            self._device_ids.get(slug) not in (None, dev_id)
            for slug, dev_id in new_ids.items()
        )
        self._device_ids = new_ids
        if changed and self.config_entry is not None:
            _LOGGER.warning(
                "Jung Home gateway device ids changed (firmware update?); "
                "reloading the integration to re-resolve entities"
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _fetch_devices_from_api(self, host: str, token: str) -> list[Device]:
        """Fetch devices from the Jung Home API."""
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/functions"
        headers = {"token": f"{token}", "Content-Type": "application/json"}

        async with asyncio.timeout(30):
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

        # The functions endpoint must return a JSON array of device objects; an
        # error/object response would otherwise degrade into a list of dict keys
        # and crash the platforms downstream.
        if not isinstance(data, list):
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="invalid_response"
            )
        # Keep the full device payload so any firmware-stable identifier
        # (serial / address / etc.) is available for building unique IDs,
        # and is visible in the debug log above for inspection. This is the
        # trust boundary: untyped gateway JSON becomes the typed `Device` model.
        # Downstream code keeps defensive `.get(...)` access for malformed items.
        return cast("list[Device]", [d for d in data if isinstance(d, dict)])

    async def _websocket_loop(self) -> None:
        """Keep a WebSocket connection alive, reconnecting with backoff on drop.

        The gateway pushes state via WebSocket; without this loop a single
        network blip would silently stop live updates until the next command.
        """
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        while not self._closing:
            try:
                await self._run_websocket()
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                if err.status in (401, 403):
                    # A revoked/expired token is rejected at the WS upgrade.
                    # Reconnecting can't fix that, so stop and let Home Assistant
                    # drive reauth instead of hammering the gateway with a token
                    # it already refused. (The REST poll maps 401/403 to reauth
                    # too, but this surfaces it immediately.)
                    _LOGGER.warning(
                        "Jung Home WebSocket rejected the token (HTTP %s); "
                        "starting reauthentication",
                        err.status,
                    )
                    if self.config_entry is not None:
                        self.config_entry.async_start_reauth(self.hass)
                    return
                _LOGGER.warning("Jung Home WebSocket disconnected: %s", err)
            except Exception as err:
                _LOGGER.warning("Jung Home WebSocket disconnected: %s", err)
            if self._closing:
                break
            _LOGGER.debug(
                "Reconnecting to Jung Home WebSocket in %ss", self._reconnect_delay
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def _run_websocket(self) -> None:
        """Open one WebSocket session and pump messages until it closes."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"wss://{self.config['host']}/ws"
        headers = {"token": f"{self.config['token']}"}
        async with session.ws_connect(url, headers=headers, heartbeat=30) as ws:
            self.websocket = ws
            # Connected: reset the backoff and resync state we may have missed
            # while disconnected. Logged at INFO (paired with the WARNING on
            # disconnect) so the drop/recover story is visible without enabling
            # debug logging during a long soak.
            self._reconnect_delay = INITIAL_RECONNECT_DELAY
            _LOGGER.info("Jung Home WebSocket connected")
            self.ws_connected = True
            await self.async_request_refresh()
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        _LOGGER.debug("Received WebSocket message: %s", msg.data)
                        try:
                            data = json.loads(msg.data)
                            if isinstance(data, list):
                                _LOGGER.error(
                                    "Received WebSocket message is a list: %s", data
                                )
                                continue
                            if data.get("type") == "version":
                                self.gateway_version = data.get("data")
                                _LOGGER.info(
                                    "Jung Home gateway firmware version: %s",
                                    self.gateway_version,
                                )
                                self._apply_gateway_version()
                                continue
                            if data.get("type") == "message":
                                text = data.get("data")
                                if isinstance(text, str) and text.startswith("error:"):
                                    # The gateway reports a rejected command (e.g.
                                    # a bad set) as an `error:` message frame. There
                                    # is no message_id correlation, but surfacing it
                                    # at WARNING beats silently dropping it.
                                    _LOGGER.warning(
                                        "Jung Home gateway reported an error: %s",
                                        text,
                                    )
                                else:
                                    _LOGGER.debug("Received message frame: %s", data)
                                continue
                            self._handle_websocket_message(data)
                        except json.JSONDecodeError as e:
                            _LOGGER.error("Error decoding WebSocket message: %s", e)
                        except Exception as e:
                            _LOGGER.error(
                                "Unexpected error handling WebSocket message: %s", e
                            )
                            _LOGGER.error("Message content: %s", msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"WebSocket error frame: {msg}")
            finally:
                self.websocket = None
                self.ws_connected = False

    def _handle_websocket_message(self, message: dict[str, Any]) -> None:
        """Handle incoming WebSocket messages."""
        if not isinstance(message, dict):
            _LOGGER.error("Received WebSocket message is not a dictionary: %s", message)
            return

        data = message.get("data")
        msg_type = message.get("type")
        if isinstance(data, dict):
            datapoint_id = data.get("id")
            if not datapoint_id:
                _LOGGER.error(
                    "Received WebSocket message without datapoint_id: %s", message
                )
                return
            updated = False
            for device in self.data or []:
                for datapoint in device.get("datapoints", []):
                    if datapoint.get("id") == datapoint_id:
                        # Merge the pushed keys into the stored datapoint. The push
                        # carries arbitrary keys (typically `values`), so mutate via
                        # a dict view rather than the TypedDict.
                        dp_dict = cast("dict[str, Any]", datapoint)
                        for key, value in data.items():
                            if key != "id":
                                dp_dict[key] = value
                        _LOGGER.debug(
                            "Updated datapoint for device %s: %s",
                            device.get("id"),
                            datapoint,
                        )
                        updated = True
                        break
                if updated:
                    break
            if updated:
                # Flag the pushed datapoint for the duration of this dispatch so
                # event entities fire on the push itself. `async_set_updated_data`
                # notifies listeners synchronously, so the flag is valid for
                # exactly this push and is cleared immediately afterwards; REST
                # polls never set it and therefore never fire phantom events.
                self.pushed_datapoint_id = datapoint_id
                try:
                    self.async_set_updated_data(self.data)
                finally:
                    self.pushed_datapoint_id = None
            else:
                _LOGGER.warning("No matching datapoint found for id %s", datapoint_id)
        elif isinstance(data, list):
            # groups / scenes broadcasts — not consumed by any entity; ignore.
            _LOGGER.debug("Received %s broadcast (%d items)", msg_type, len(data))
        else:
            _LOGGER.warning(
                "Received WebSocket message with unknown data type: %s", message
            )

    def _apply_gateway_version(self) -> None:
        """Push the gateway firmware version onto our devices in the registry.

        An entity's ``device_info`` is only read when it is first added, which
        may happen before the WebSocket ``version`` frame arrives. Update the
        registry directly so the device page shows the version without needing a
        reload. Combined with the ``device_info`` fallback this covers either
        ordering (entities created before or after the frame).
        """
        if self.gateway_version is None or self.config_entry is None:
            return
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if device.sw_version != self.gateway_version:
                registry.async_update_device(device.id, sw_version=self.gateway_version)

    async def start(self) -> None:
        """Connect to the WebSocket.

        Initial device data is fetched separately during setup via
        async_config_entry_first_refresh() so that a failure aborts setup
        correctly (retry on connection error, reauth on a rejected token).
        """
        _LOGGER.debug("Starting coordinator: connecting to WebSocket")
        self._closing = False
        entry = self.config_entry
        if entry is None:  # pragma: no cover - an entry coordinator always has one
            return
        self._ws_task = entry.async_create_background_task(
            self.hass, self._websocket_loop(), name="junghome_ws"
        )

    async def stop(self) -> None:
        """Stop the coordinator and close the WebSocket connection."""
        _LOGGER.debug("Stopping coordinator and closing WebSocket")
        self._closing = True
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close()
        self.websocket = None

    async def send_websocket_message(self, message: dict[str, Any]) -> None:
        """Send a message via WebSocket."""
        _LOGGER.debug("Sending WebSocket message: %s", message)
        if self.websocket and not self.websocket.closed:
            try:
                await self.websocket.send_str(json.dumps(message))
                _LOGGER.debug("WebSocket message sent successfully")
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="cannot_send"
                ) from err
        else:
            # The reconnect loop in _websocket_loop() will restore the connection,
            # but surface the failure now so the command isn't silently treated as
            # applied (callers optimistically update state only on success).
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_send"
            )

    async def turn_on_switch(self, datapoint_id: str) -> None:
        """Turn on the switch."""
        _LOGGER.debug("Turning on switch with datapoint_id: %s", datapoint_id)
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
        }
        await self.send_websocket_message(message)

    async def turn_off_switch(self, datapoint_id: str) -> None:
        """Turn off the switch."""
        _LOGGER.debug("Turning off switch with datapoint_id: %s", datapoint_id)
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            },
        }
        await self.send_websocket_message(message)

    async def turn_on_light(self, datapoint_id: str) -> None:
        """Turn on the light."""
        _LOGGER.debug("Turning on light with datapoint_id: %s", datapoint_id)
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}],
            },
        }
        await self.send_websocket_message(message)

    async def turn_off_light(self, datapoint_id: str) -> None:
        """Turn off the light."""
        _LOGGER.debug("Turning off light with datapoint_id: %s", datapoint_id)
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "switch",
                "values": [{"key": "switch", "value": "0"}],
            },
        }
        await self.send_websocket_message(message)

    async def set_brightness(self, datapoint_id: str, brightness: int) -> None:
        """Set the brightness of the light."""
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "brightness",
                "values": [{"key": "brightness", "value": str(brightness)}],
            },
        }
        await self.send_websocket_message(message)

    async def set_color_temp(self, datapoint_id: str, color_temp: int) -> None:
        """Set the color temperature of the light."""
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": str(color_temp)}],
            },
        }
        await self.send_websocket_message(message)

    async def set_status_led(self, datapoint_id: str, state: bool) -> None:
        """Set the status LED on (True) or off (False)."""
        value = "1" if state else "0"
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "status_led",
                "values": [{"key": "status_led", "value": value}],
            },
        }
        await self.send_websocket_message(message)
