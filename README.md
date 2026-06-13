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

# Setup

Adding the integration is now a two-step, app-driven flow — you no longer need to
fetch a token by hand:

1. In Home Assistant, go to **Settings → Devices & Services → Add Integration**
   and pick **Jung Home**.
2. Enter your gateway address — the IP (e.g. `192.168.1.50`) or `junghome.local`.
3. Home Assistant requests access from the gateway and waits. Open the **Jung
   Home mobile app** and approve the request under
   **Settings → Gateway → Access Permissions → Open Requests**.
4. Once you approve (within ~3 minutes), setup completes automatically and your
   devices appear. If it times out, just submit again and re-approve.

Behind the scenes this calls the gateway's `POST /api/junghome/register`
endpoint; the issued token is stored in the config entry. Devices added or
removed in the Jung Home app afterwards are picked up automatically.

# Gateway internals (for contributors)

The local gateway API (REST + WebSocket), its registration flow, and the
device-mesh architecture are documented in **[docs/](docs/README.md)**.

# TODO
- Make setup easier.
- Bring back binary sensors (motion/presence, which was previously removed).
- Puck support.

Unlikely to be completed by me, since I don't have the devices:
- Curtain control.
- Thermostats.

# Development / Testing

You can run a throwaway Home Assistant instance with this integration loaded, without touching a real deployment.

## Docker Compose (no local Python needed)

```bash
docker compose up          # Home Assistant at http://localhost:8123
docker compose down        # stop
docker compose down -v     # stop and wipe HA state
```

The repo's `custom_components/` is bind-mounted into the container, so editing the
integration and running `docker compose restart` picks up changes without a rebuild.
Pin a specific HA version by replacing `stable` in `docker-compose.yml`.

## Local (devcontainer / venv)

```bash
scripts/setup     # install dependencies
scripts/develop   # run Home Assistant against ./config with the integration on PYTHONPATH
scripts/lint      # ruff
```

# Coffee
If you've enjoyed this integration, feel free to buy me a cup of coffee. My BTC address is `bc1qlpvgqzr0y09a4zhez94sjl6539ptk0l9rdy2jm`.
