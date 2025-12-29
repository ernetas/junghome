import asyncio
import json
import logging
import ssl
import aiohttp
import websockets
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

class JungHomeDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Jung Home API."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the coordinator."""
        self.hass = hass
        self.config = config
        self.websocket = None
        super().__init__(hass, _LOGGER, name="Jung Home", update_interval=timedelta(minutes=1))

    async def _async_update_data(self):
        """Fetch data from the API."""
        _LOGGER.debug("Fetching new device data from Jung Home API")
        try:
            response = await self._fetch_devices_from_api(self.config['host'], self.config['token'])
            if response is None:
                _LOGGER.error("Received None response from API")
                return []  # Returning empty list ensures entities don't break

            _LOGGER.debug("API Response: %s", response)
            return response  # `async_set_updated_data` is automatically called with this
        except Exception as e:
            _LOGGER.error("Error fetching data from Jung Home API: %s", e)
            raise  # Raising exception allows Home Assistant to handle errors properly

    async def _fetch_devices_from_api(self, host, token):
        """Fetch devices from the Jung Home API."""
        ssl_context = await asyncio.to_thread(ssl.create_default_context)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        url = f"https://{host}/api/junghome/functions"
        headers = {
            "token": f"{token}",
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=ssl_context) as response:
                response.raise_for_status()
                data = await response.json()

        devices = []
        for device in data:
            devices.append({
                "id": device["id"],
                "label": device["label"],
                "type": device["type"],
                "datapoints": device["datapoints"]
            })

        return devices

    async def _connect_websocket(self):
        """Connect to the WebSocket and handle incoming messages using aiohttp."""
        url = f"wss://{self.config['host']}/ws"
        headers = {
            "token": f"{self.config['token']}"
        }
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, headers=headers, ssl=ssl_context) as ws:
                    self.websocket = ws
                    _LOGGER.debug("WebSocket connected (aiohttp)")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            _LOGGER.debug("Received WebSocket message: %s", msg.data)
                            try:
                                data = json.loads(msg.data)
                                if isinstance(data, list):
                                    _LOGGER.error("Received WebSocket message is a list: %s", data)
                                    continue
                                if data.get("type") in ["message", "version"]:
                                    _LOGGER.debug("Received initial message: %s", data)
                                    continue
                                self._handle_websocket_message(data)
                            except json.JSONDecodeError as e:
                                _LOGGER.error("Error decoding WebSocket message: %s", e)
                            except Exception as e:
                                _LOGGER.error("Unexpected error handling WebSocket message: %s", e)
                                _LOGGER.error("Message content: %s", msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.error("WebSocket error: %s", msg)
                            break
        except Exception as e:
            _LOGGER.error("Error connecting to WebSocket (aiohttp): %s", e)
            self.websocket = None

    def _handle_websocket_message(self, message):
        """Handle incoming WebSocket messages."""
        if not isinstance(message, dict):
            _LOGGER.error("Received WebSocket message is not a dictionary: %s", message)
            return

        data = message.get("data")
        msg_type = message.get("type")
        # Fire dispatcher signal for datapoint messages
        if msg_type == "datapoint":
            _LOGGER.debug("[JUNGHOME] Dispatching button event for message: %s", message)
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            async_dispatcher_send(self.hass, "jung_home_button_event", message)
        # Call button callback for datapoint messages if registered
        if msg_type == "datapoint" and hasattr(self, "_button_callback"):
            self._button_callback(message)
        # ...existing code for dict/list handling...
        if isinstance(data, dict):
            datapoint_id = data.get("id")
            if not datapoint_id:
                _LOGGER.error("Received WebSocket message without datapoint_id: %s", message)
                return
            updated = False
            for device in self.data:
                for datapoint in device["datapoints"]:
                    if datapoint["id"] == datapoint_id:
                        # Update all keys in the datapoint with the new data
                        for key, value in data.items():
                            if key != "id":
                                datapoint[key] = value
                        _LOGGER.debug("Updated datapoint for device %s: %s", device["id"], datapoint)
                        updated = True
                        break
                if updated:
                    break
            if updated:
                self.async_set_updated_data(self.data)
            else:
                _LOGGER.warning("No matching datapoint found for id %s", datapoint_id)
        elif isinstance(data, list):
            if msg_type == "groups":
                self.groups = data
                _LOGGER.debug("Updated groups: %s", data)
            elif msg_type == "scenes":
                self.scenes = data
                _LOGGER.debug("Updated scenes: %s", data)
            self.async_set_updated_data(self.data)
        elif not isinstance(data, dict):
            _LOGGER.warning("Received WebSocket message with unknown data type: %s", message)

    async def start(self):
        """Start the coordinator by fetching initial data and connecting to the WebSocket."""
        _LOGGER.debug("Starting coordinator: fetching initial data and connecting to WebSocket")
        await self.async_refresh()
        self.hass.loop.create_task(self._connect_websocket())

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
            _LOGGER.error("WebSocket is not connected or is closed. Attempting to reconnect...")
            # Try to reconnect
            self.hass.loop.create_task(self._connect_websocket())

    async def turn_on_switch(self, datapoint_id):
        """Turn on the switch."""
        _LOGGER.debug("Turning on switch with datapoint_id: %s", datapoint_id)
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "switch",
                "values": [{"key": "switch", "value": "1"}]
            }
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
                "values": [{"key": "switch", "value": "0"}]
            }
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
                "values": [{"key": "switch", "value": "1"}]
            }
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
                "values": [{"key": "switch", "value": "0"}]
            }
        }
        await self.send_websocket_message(message)

    async def set_brightness(self, datapoint_id, brightness):
        """Set the brightness of the light."""
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "brightness",
                "values": [{"key": "brightness", "value": str(brightness)}]
            }
        }
        await self.send_websocket_message(message)

    async def set_color_temp(self, datapoint_id, color_temp):
        """Set the color temperature of the light."""
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "color_temperature",
                "values": [{"key": "color_temperature", "value": str(color_temp)}]
            }
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
                "values": [{"key": "status_led", "value": value}]
            }
        }
        await self.send_websocket_message(message)