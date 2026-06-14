import asyncio
import json
import logging
from datetime import timedelta

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

# WebSocket reconnect backoff bounds (seconds).
INITIAL_RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 60


class JungHomeDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Jung Home API."""

    def __init__(self, hass: HomeAssistant, config: dict, config_entry):
        """Initialize the coordinator."""
        self.hass = hass
        self.config = config
        self.websocket = None
        self._ws_task = None
        self._closing = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Jung Home",
            update_interval=timedelta(minutes=1),
        )

    async def _async_update_data(self):
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
                    "Jung Home gateway rejected the token"
                ) from err
            raise UpdateFailed(f"Error fetching data from Jung Home: {err}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error connecting to Jung Home: {err}") from err

        if response is None:
            _LOGGER.error("Received None response from API")
            return []  # Returning empty list ensures entities don't break
        _LOGGER.debug("API Response: %s", response)
        # `async_set_updated_data` is automatically called with this.
        return response

    async def _fetch_devices_from_api(self, host, token):
        """Fetch devices from the Jung Home API."""
        # Shared HA session; verify_ssl=False tolerates the gateway's self-signed
        # cert without building an SSL context on the event loop.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"https://{host}/api/junghome/functions"
        headers = {"token": f"{token}", "Content-Type": "application/json"}

        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()

        # Keep the full device payload so any firmware-stable identifier
        # (serial / address / etc.) is available for building unique IDs,
        # and is visible in the debug log above for inspection.
        return list(data)

    async def _websocket_loop(self):
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
            except Exception as err:
                _LOGGER.warning("Jung Home WebSocket disconnected: %s", err)
            if self._closing:
                break
            _LOGGER.debug(
                "Reconnecting to Jung Home WebSocket in %ss", self._reconnect_delay
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def _run_websocket(self):
        """Open one WebSocket session and pump messages until it closes."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"wss://{self.config['host']}/ws"
        headers = {"token": f"{self.config['token']}"}
        async with session.ws_connect(url, headers=headers, heartbeat=30) as ws:
            self.websocket = ws
            # Connected: reset the backoff and resync state we may have missed
            # while disconnected.
            self._reconnect_delay = INITIAL_RECONNECT_DELAY
            _LOGGER.debug("WebSocket connected (aiohttp)")
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
                            if data.get("type") in ["message", "version"]:
                                _LOGGER.debug("Received initial message: %s", data)
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

    def _handle_websocket_message(self, message):
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
            for device in self.data:
                for datapoint in device["datapoints"]:
                    if datapoint["id"] == datapoint_id:
                        # Update all keys in the datapoint with the new data
                        for key, value in data.items():
                            if key != "id":
                                datapoint[key] = value
                        _LOGGER.debug(
                            "Updated datapoint for device %s: %s",
                            device["id"],
                            datapoint,
                        )
                        updated = True
                        break
                if updated:
                    break
            if updated:
                self.async_set_updated_data(self.data)
            else:
                _LOGGER.warning("No matching datapoint found for id %s", datapoint_id)
        elif isinstance(data, list):
            # groups / scenes broadcasts — not consumed by any entity; ignore.
            _LOGGER.debug("Received %s broadcast (%d items)", msg_type, len(data))
        else:
            _LOGGER.warning(
                "Received WebSocket message with unknown data type: %s", message
            )

    async def start(self):
        """Connect to the WebSocket.

        Initial device data is fetched separately during setup via
        async_config_entry_first_refresh() so that a failure aborts setup
        correctly (retry on connection error, reauth on a rejected token).
        """
        _LOGGER.debug("Starting coordinator: connecting to WebSocket")
        self._closing = False
        self._ws_task = self.hass.loop.create_task(self._websocket_loop())

    async def stop(self):
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

    async def send_websocket_message(self, message):
        """Send a message via WebSocket."""
        _LOGGER.debug("Sending WebSocket message: %s", message)
        if self.websocket and not self.websocket.closed:
            try:
                await self.websocket.send_str(json.dumps(message))
                _LOGGER.debug("WebSocket message sent successfully")
            except Exception as e:
                _LOGGER.error("Error sending WebSocket message: %s", e)
        else:
            # The reconnect loop in _websocket_loop() will restore the connection;
            # this command is dropped rather than queued.
            _LOGGER.error(
                "WebSocket is not connected; command dropped (reconnect in progress)"
            )

    async def turn_on_switch(self, datapoint_id):
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

    async def turn_off_switch(self, datapoint_id):
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

    async def turn_on_light(self, datapoint_id):
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

    async def turn_off_light(self, datapoint_id):
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

    async def set_brightness(self, datapoint_id, brightness):
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

    async def set_color_temp(self, datapoint_id, color_temp):
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

    async def set_status_led(self, datapoint_id, state):
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
