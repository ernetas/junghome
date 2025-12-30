"""
Coordinator for the Junghome integration.

Handles polling the REST API and maintaining a websocket connection for
real-time updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

class JungHomeDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Jung Home API."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.config = config
        self.websocket: Any = None
        super().__init__(
            hass,
            _LOGGER,
            name="Jung Home",
            update_interval=timedelta(minutes=1),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch data from the API."""
        _LOGGER.debug("Fetching new device data from Jung Home API")
        try:
            response = await self._fetch_devices_from_api(
                self.config["host"], self.config["token"]
            )
        except Exception:
            _LOGGER.exception("Error fetching data from Jung Home API")
            raise

        if response is None:
            _LOGGER.exception("Received None response from API")
            return []  # Returning empty list ensures entities don't break

        _LOGGER.debug("API Response: %s", response)
        return response

    async def _fetch_devices_from_api(
        self, host: str, token: str
    ) -> list[dict[str, Any]]:
        """Fetch devices from the Jung Home API."""
        ssl_context = await asyncio.to_thread(ssl.create_default_context)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        url = f"https://{host}/api/junghome/functions"
        headers = {"token": f"{token}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session, session.get(
            url, headers=headers, ssl=ssl_context
        ) as response:
            response.raise_for_status()
            data = await response.json()

        return [
            {
                "id": device["id"],
                "label": device["label"],
                "type": device["type"],
                "datapoints": device["datapoints"],
            }
            for device in data
        ]

    async def _connect_websocket(self) -> None:
        """Connect to the WebSocket and handle incoming messages using aiohttp."""
        url = f"wss://{self.config['host']}/ws"
        headers = {"token": f"{self.config['token']}"}
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            async with aiohttp.ClientSession() as session, session.ws_connect(
                url, headers=headers, ssl=ssl_context
            ) as ws:
                self.websocket = ws
                _LOGGER.debug("WebSocket connected (aiohttp)")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        _LOGGER.debug("Received WebSocket message: %s", msg.data)
                        try:
                            data = json.loads(msg.data)
                            if isinstance(data, list):
                                _LOGGER.exception(
                                    "Received WebSocket message is a list: %s", data
                                )
                                continue
                            if data.get("type") in ["message", "version"]:
                                _LOGGER.debug("Received initial message: %s", data)
                                continue
                            self._handle_websocket_message(data)
                        except json.JSONDecodeError:
                            _LOGGER.exception("Error decoding WebSocket message")
                        except Exception:
                            _LOGGER.exception(
                                "Unexpected error handling WebSocket message"
                            )
                            _LOGGER.exception("Message content: %s", msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        _LOGGER.exception("WebSocket error: %s", msg)
                        break
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.exception("Error connecting to WebSocket (aiohttp)")
            self.websocket = None

    def _handle_websocket_message(self, message: Any) -> None:
        """Handle incoming WebSocket messages."""
        if not isinstance(message, dict):
            _LOGGER.exception(
                "Received WebSocket message is not a dictionary: %s", message
            )
            return
        data = message.get("data")
        msg_type = message.get("type")

        # Fire dispatcher signal for datapoint messages and call an optional
        # registered callback. Keep this lightweight and delegate heavier
        # processing to a separate helper to reduce branching complexity.
        if msg_type == "datapoint":
            _LOGGER.debug(
                "[JUNGHOME] Dispatching button event for message: %s",
                message,
            )
            async_dispatcher_send(self.hass, "jung_home_button_event", message)
            if hasattr(self, "_button_callback"):
                self._button_callback(message)

        # Delegate processing of the data payload to a helper.
        self._process_websocket_data(data, msg_type, message)

    def _process_websocket_data(
        self,
        data: Any,
        msg_type: Any,
        raw_message: Any,
    ) -> None:
        """
        Process the data payload from a WebSocket message.

        This helper keeps the main handler smaller and reduces cyclomatic
        complexity for linting.
        """
        if isinstance(data, dict):
            self._handle_datapoint_dict(data, raw_message)
            return

        if isinstance(data, list):
            self._handle_data_list(data, msg_type)
            return

        _LOGGER.warning(
            "Received WebSocket message with unknown data type: %s",
            raw_message,
        )

    def _handle_datapoint_dict(self, data: dict[str, Any], raw_message: Any) -> None:
        """Handle a datapoint update represented as a dictionary."""
        datapoint_id = data.get("id")
        if not datapoint_id:
            _LOGGER.exception(
                "Received WebSocket message without datapoint_id: %s", raw_message
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

    def _handle_data_list(self, data: list[Any], msg_type: Any) -> None:
        """Handle WebSocket messages where `data` is a list (groups/scenes)."""
        if msg_type == "groups":
            self.groups = data
            _LOGGER.debug("Updated groups: %s", data)
        elif msg_type == "scenes":
            self.scenes = data
            _LOGGER.debug("Updated scenes: %s", data)
        self.async_set_updated_data(self.data)

    async def start(self) -> None:
        """
        Start the coordinator.

        Fetch initial data and connect to the WebSocket.
        """
        _LOGGER.debug(
            "Starting coordinator: fetching initial data and connecting to WebSocket"
        )
        await self.async_refresh()
        self.hass.loop.create_task(self._connect_websocket())

    async def send_websocket_message(self, message: Any) -> None:
        """Send a message via WebSocket."""
        _LOGGER.debug("Sending WebSocket message: %s", message)
        if self.websocket and not getattr(self.websocket, "closed", False):
            try:
                await self.websocket.send_str(json.dumps(message))
                _LOGGER.debug("WebSocket message sent successfully")
            except (aiohttp.ClientError, TimeoutError):
                _LOGGER.exception("Error sending WebSocket message")
        else:
            _LOGGER.exception(
                "WebSocket is not connected or is closed. Attempting to reconnect..."
            )
            # Try to reconnect
            self.hass.loop.create_task(self._connect_websocket())

    async def turn_on_switch(self, datapoint_id: str) -> None:
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


    async def turn_off_switch(self, datapoint_id: str) -> None:
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

    async def turn_on_light(self, datapoint_id: str) -> None:
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

    async def turn_off_light(self, datapoint_id: str) -> None:
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

    async def set_brightness(self, datapoint_id: str, brightness: int) -> None:
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

    async def set_color_temp(self, datapoint_id: str, color_temp: int) -> None:
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

    async def set_status_led(self, datapoint_id: str, *, is_on: bool) -> None:
        """Set the status LED on (`is_on=True`) or off (`is_on=False`)."""
        value = "1" if is_on else "0"
        message = {
            "type": "datapoint",
            "data": {
                "id": datapoint_id,
                "type": "status_led",
                "values": [{"key": "status_led", "value": value}]
            }
        }
        await self.send_websocket_message(message)
