# What?

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/ernetas/junghome)](https://github.com/ernetas/junghome/releases)

This is a custom Jung Home integration based on WebSocket communication with Jung Home Gateway. A gateway is required.

Currently functional things:
- On/Off light switches.
- BT S1 B2 U switch actuators.
- Dimmers (DALI, etc.) - color and brightness as well as On/Off.
- Sockets - On/Off, energy statistics, etc.
- Blinds / shutters (covers) - open/close/stop, position, and slat tilt. Awnings,
  which report position inverted, can be flagged in the integration's options.
- Thermostats (room temperature regulators) - target temperature, presets, and
  heating activity (`hvac_action`). The gateway offers no on/off for a regulator,
  so these entities are heat-only; the **frost protection** preset is the closest
  thing to "off".
- Scenes - recall any Jung Home scene from Home Assistant.
- Measurement sensors (e.g. ambient brightness on presence detectors).
- IoT integration for Rocker Switches - allows triggering any script or automation in HomeAssistant via button presses.
- Button LED On/Off (unfortunately, color can only be configured via app or BT Mesh/NRF).
- Rooms - each device is placed in the Home Assistant area matching its Jung
  Home group. A device is only ever placed if it has no area yet, and each
  device is considered just once, so this never moves a device you placed
  yourself and never re-adds one whose area you deliberately cleared. A group
  matching an existing area links to it instead of creating a duplicate.

> Note: **awnings** report cover position inverted, so flag them inverted under
> Settings → Devices & Services → Jung Home → **Configure**.

All communication is via WebSockets. I've managed to reliably automate (using the provided blueprint):
- Single click
- Double click
- Triple click
- Hold

Any feedback is welcome, this is my first integration with HomeAssistant.

# Installation

## HACS (recommended)

**Jung Home is in the HACS default store** — no custom repository needed.

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ernetas&repository=junghome&category=integration)

1. **HACS** → search for **Jung Home**.
2. **Download**, then restart Home Assistant.
3. Settings → Devices & Services → **Add Integration** → Jung Home (see
   [Setup](#setup)). In most cases the gateway is discovered for you and appears
   there on its own, so you can just click **Configure**.

The button above opens the repository straight in your HACS. If HACS is new to
you, install it first: <https://hacs.xyz/docs/use/download/download/>.

### Updating

HACS notifies you when a new release is out (**HACS → Jung Home → Update**).
Restart Home Assistant afterwards. Config entries, entity IDs and automations
survive updates — entity `unique_id`s are derived from device labels rather than
the gateway's internal ids precisely so they stay put.

### Beta releases

Pre-releases are hidden by default. To test one, open **HACS → Jung Home → ⋮ →
Redownload** and enable **Show beta versions**.

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
changes, discovery updates it automatically **for gateways that were added via
discovery**; an entry you added manually is keyed by the address you typed, so
use **Reconfigure** to point it at the new one.

# Button automations (rocker switches)

Rocker buttons show up as Home Assistant **event entities** (one per up/down
side), and each button also offers **device triggers** — open the button's device
page, add an automation, and pick e.g. *"Up button pressed"*. That's the quickest
route for a simple "press this, do that" automation.

The gateway only reports raw press/release, so single/double/hold gestures are
derived in an automation — a ready-made **blueprint** does this for you:

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

# Removing the integration

1. **Settings → Devices & Services → Jung Home → ⋮ → Delete.** This removes the
   config entry and every device and entity it created.
2. **Revoke Home Assistant's access in the Jung Home app**, under
   **Settings → Gateway → Access Permissions**. The gateway keeps the token it
   issued until you revoke it there — deleting the entry in Home Assistant does
   not tell the gateway to forget it.
3. Optionally remove the repository from HACS (**HACS → Jung Home → ⋮ →
   Remove**) if you don't intend to reinstall.

Automations that referenced the removed entities keep their (now missing)
entity IDs — Home Assistant will flag them as unavailable until you edit them.

# Troubleshooting

**The gateway isn't discovered / `junghome.local` doesn't resolve.**
mDNS doesn't cross VLANs or most VPNs. Add the integration manually with
**Add Integration → Jung Home** and type the gateway's IP (e.g.
`192.168.1.50`). A fixed DHCP lease for the gateway is worth setting up.

**Setup times out waiting for approval.**
The gateway only holds the request open for about three minutes. Open the Jung
Home app *first* (**Settings → Gateway → Access Permissions → Open Requests**),
then submit the form and approve straight away. If it times out, just submit
again. The alternative is the **network-key password** option, which connects
immediately with no approval step.

**"Live updates have stopped" repair notice.**
The WebSocket that carries live push has been down for several consecutive
reconnect attempts. The integration keeps working on a 60-second REST poll, so
states stay correct but stop being instant, and controllable entities read
unavailable because commands only travel over the WebSocket. It clears itself
once the connection is genuinely back. If it persists, check that the gateway is
reachable and hasn't been rebooting.

**Entities are unavailable but the gateway is up.**
Controllable entities (lights, sockets, covers, thermostats, status LEDs)
require the live WebSocket, since that is the only path commands take. Read-only
entities (sensors, binary sensors, events) stay available on the REST poll
alone. So "sensors fine, lights unavailable" points at the WebSocket
specifically — see the repair notice above.

**Home Assistant asks you to re-authenticate.**
The gateway rejected the stored token, usually because it was revoked in the app
or the gateway was factory-reset. Follow the reauth prompt to issue a new one.

**A device you added in the app doesn't show up.**
New devices are picked up on the next REST poll (within a minute). If it still
doesn't appear, download diagnostics (**⋮ → Download diagnostics** on the entry)
— `support_summary.unhandled_function_types` and `unhandled_datapoint_types`
list anything the gateway reports that this integration doesn't yet map, which
is exactly what an issue report needs.

**Filing a bug.** Attach the diagnostics download. The gateway token and host
are redacted; device labels are kept because they are the identity anchor.

# Known limitations

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
- The **puck** isn't supported/validated yet.
- **Two devices with the same label collide.** The gateway exposes no hardware
  identifier and regenerates its device ids on firmware updates, so the device
  *label* is the only stable identity anchor available. Devices whose labels are
  identical — or that slug identically, e.g. `Lamp 1` and `Lamp-1` — map to the
  same id and only the first one gets entities. Give each device a distinct
  label in the Jung Home app.

# Gateway internals (for contributors)

The local gateway API (REST + WebSocket), its registration flow, and the
device-mesh architecture are documented in **[docs/](docs/README.md)**. Release
and HACS-publishing steps are in **[docs/publishing.md](docs/publishing.md)**.

# TODO
- Puck support.

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
