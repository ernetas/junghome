# What?

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/ernetas/junghome)](https://github.com/ernetas/junghome/releases)

This is a custom Jung Home integration based on WebSocket communication with Jung Home Gateway. A gateway is required.

Currently functional things:
- On/Off light switches.
- BT S1 B2 U switch actuators.
- Dimmers (DALI, etc.) - color and brightness as well as On/Off.
- Sockets - On/Off, energy statistics, etc.
- Blinds / shutters (covers) - open/close/stop, position, and slat tilt. Awnings,
  which report position inverted, can be flagged in the integration's options.
- Thermostats (room temperature regulators) - target temperature and presets.
- Scenes - recall any Jung Home scene from Home Assistant.
- Measurement sensors (e.g. ambient brightness on presence detectors).
- IoT integration for Rocker Switches - allows triggering any script or automation in HomeAssistant via button presses.
- Button LED On/Off (unfortunately, color can only be configured via app or BT Mesh/NRF).
- Rooms - each device is placed in the Home Assistant area matching its Jung
  Home group. A device is only ever placed if it has no area yet, and each
  device is considered just once, so this never moves a device you placed
  yourself and never re-adds one whose area you deliberately cleared. A group
  matching an existing area links to it instead of creating a duplicate.

> Note: thermostats, scenes and measurement sensors are implemented from the
> gateway protocol but have **not yet been fully verified against real hardware**,
> so feedback is very welcome if you own one. Cover position direction is now
> confirmed against the gateway firmware (percent-closed) and reads correctly for
> roller shutters/blinds; **awnings** report it inverted, so flag them under
> Settings → Devices & Services → Jung Home → **Configure**.

All communication is via WebSockets. I've managed to reliably automate:
- Single click
- Double click
- Triple click
- Hold

Any feedback is welcome, this is my first integration with HomeAssistant.

# Installation

## HACS (recommended)

Until this is in the HACS default store, add it as a custom repository:

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ernetas&repository=junghome&category=integration)

1. HACS → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/ernetas/junghome`, Category: **Integration**. Add.
3. Find **Jung Home** in HACS, **Download** the latest release, then restart Home Assistant.
4. Settings → Devices & Services → **Add Integration** → Jung Home (see [Setup](#setup)).

## Manual

Copy `custom_components/junghome/` into your Home Assistant `config/custom_components/` directory and restart.

# Setup

You no longer need to fetch a token by hand. In most cases the gateway is
**discovered automatically** over mDNS and appears under
**Settings → Devices & Services** as a discovered device — just click
**Configure**. If it isn't discovered, go to **Add Integration → Jung Home** to
start setup manually.

Either way you then pick **how to connect**:

- **Approve the connection in the Jung Home app.** Home Assistant asks the
  gateway for access and waits; open the **Jung Home app** and approve the
  request under **Settings → Gateway → Access Permissions → Open Requests**.
  Setup finishes automatically once you approve (within ~3 minutes) — if it times
  out, submit again and re-approve. (Uses `POST /api/junghome/register`.)
- **Enter the gateway network-key password.** Connects immediately, with no app
  approval, by exchanging the gateway's network-key password for a token (uses
  `POST /api/junghome/register/by-password`). You can find the password in the
  Jung Home app.

The gateway address is filled in for you when it was discovered; otherwise it
defaults to `junghome.local` (which works on many networks) and you can change it
to your gateway's IP (e.g. `192.168.1.50`) if that name doesn't resolve.

The issued token is stored in the config entry. Devices added or removed in the
Jung Home app afterwards are picked up automatically. If the gateway's IP later
changes, discovery updates it automatically, or you can **Reconfigure** the entry
to point at the new address.

# Button automations (rocker switches)

Rocker buttons show up as Home Assistant **event entities** (one per up/down
side). The gateway only reports raw press/release, so single/double/hold gestures
are derived in an automation — a ready-made **blueprint** does this for you:

- Blueprint: [`blueprints/automation/junghome/button_gestures.yaml`](blueprints/automation/junghome/button_gestures.yaml)
- Full guide + copy-paste recipes: [`docs/example-button-automation.md`](docs/example-button-automation.md)

Import the blueprint by URL (Settings → Automations & scenes → Blueprints →
Import), select **all** of the button's event entities (JUNG alternates between
the up/down events), and assign actions for single / double / hold.

# Scenes

Scenes defined in the JUNG app appear as Home Assistant **`scene.*` entities** —
activating one (or calling `scene.turn_on`) recalls it on the gateway.

The gateway also reports when a scene is recalled **by any source**, including a
physical wall button. The integration re-emits that as a Home Assistant event,
`junghome_scene_recalled`, so you can trigger automations from a physical scene
button:

```yaml
automation:
  - trigger:
      - platform: event
        event_type: junghome_scene_recalled
        event_data:
          label: "Išjungti WC"
    action:
      - service: notify.notify
        data:
          message: "WC scene was triggered"
```

The event data is `{ scene_id, label, entry_id }`.

# How updates work

The integration is **local push**: it holds a WebSocket to the gateway and
applies state changes the moment the gateway broadcasts them, so device states
update in real time. It also re-fetches the full device list over REST once a
minute as a backstop, and on every WebSocket reconnect. If the WebSocket drops it
reconnects automatically with backoff. No cloud and no account are involved.

# Known limitations

- **Room → area placement is one-time per device.** Each device is placed in the
  Home Assistant area matching its JUNG app group only the first time it's seen,
  and never moved again. This is deliberate: it can't tell "the user moved this
  in Home Assistant" from "the room changed in the app", so it never overrides
  your choice — but it also means **re-grouping a device in the JUNG app later
  won't move it** in Home Assistant. Move it yourself in HA if you need to.
  (Scenes are exposed too — see [Scenes](#scenes).)
- **Thermostats, scenes and measurement sensors are not yet fully verified
  against real hardware** — they're implemented from the gateway protocol but I
  don't own those devices. Feedback welcome. Cover position direction is
  confirmed (percent-closed); **awnings** read inverted and can be flagged in the
  integration's options to flip them.
- **Metering sockets report instantaneous power (W) and current (A), not
  cumulative energy (kWh)**, so they can't go straight onto the Energy Dashboard.
  To track energy/cost, add a Riemann-sum
  [Integration helper](https://www.home-assistant.io/integrations/integration/)
  on the socket's power sensor (Settings → Devices & Services → Helpers → Riemann
  sum), then add that kWh sensor to the Energy Dashboard.
- **Button gestures** (single/double/hold) aren't native — derive them with the
  [blueprint](#button-automations-rocker-switches).
- The rocker **status-LED colour** can't be set from here (on/off only); colour
  is configured in the JUNG app or over BT-Mesh.
- The **puck** isn't supported yet.
- Standalone **presence/motion sensors** (e.g. a JUNG "daviklis") aren't exposed
  yet — they live in the gateway's lower-level `/devices` view, which this
  integration doesn't read.
- The gateway uses a **self-signed certificate**, so TLS verification is disabled
  for the local connection (expected for a LAN device).

# Removing the integration

Settings → Devices & Services → **Jung Home** → ⋮ → **Delete**. This removes all
of its devices and entities. The access token is dropped with the config entry;
to also revoke it on the gateway, remove "Home Assistant" under **Settings →
Gateway → Access Permissions** in the JUNG app.

# Gateway internals (for contributors)

The local gateway API (REST + WebSocket), its registration flow, and the
device-mesh architecture are documented in **[docs/](docs/README.md)**. Release
and HACS-publishing steps are in **[docs/publishing.md](docs/publishing.md)**.

# TODO
- Presence/motion binary sensors, sourced from the gateway's `/devices` view
  (standalone sensors don't appear in the `/functions` data this integration
  currently uses).
- Puck support.
- Hardware verification of covers, thermostats and measurement sensors (these are
  implemented but I don't own the devices — testers welcome).

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
