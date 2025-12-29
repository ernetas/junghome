# What?

This is a custom Jung Home integration based on WebSocket communication with Jung Home Gateway. A gateway is required.

Currently functional things:
- On/Off light switches.
- BT S1 B2 U switch actuators.
- Dimmers (DALI, etc.) - color and brightness as well as On/Off.
- Sockets - On/Off, energy statistics, etc.
- IoT integration for Rocker Switches - allows triggering any script or automation in HomeAssistant via button presses.
- Button LED On/Off (unfortunately, color can only be configured via app or BT Mesh/NRF).

All communication is via WebSockets. I've managed to reliably automate:
- Single click
- Double click
- Triple click
- Hold

Any feedback is welcome, this is my first integration with HomeAssistant.

# How
When adding the integration, you will need to know your Jung Home gateway address and get an API token from https://<JUNGHOME>/api/junghome/swagger/ (using User Registration action).

You will need to confirm the token in the Jung Home mobile app.

# TODO
- Make setup easier.
- Bring back binary sensors (motion/presence, which was previously removed).
- Puck support.

Unlikely to be completed by me, since I don't have the devices:
- Curtain control.
- Thermostats.

# Coffee
If you've enjoyed this integration, feel free to buy me a cup of coffee. My BTC address is `bc1qlpvgqzr0y09a4zhez94sjl6539ptk0l9rdy2jm`.
