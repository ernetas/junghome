# JUNG HOME integration for Home Assistant

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/ernetas/junghome)](https://github.com/ernetas/junghome/releases)

A custom integration for **JUNG HOME** devices. It talks to the JUNG HOME
Gateway entirely locally — live state and commands over a WebSocket, with a
REST poll as backstop (and for scene recall). No cloud, no account; a gateway
is required.

> **Unofficial project.** Not affiliated with, authorized by, or endorsed by
> Albrecht JUNG GmbH & Co. KG. "JUNG" and "JUNG HOME" are trademarks of their
> owner, used here only to identify compatible hardware. See
> [Disclaimer & legal](#disclaimer--legal).

## What works

- **Lights** — on/off switch actuators (e.g. BT S1 B2 U) and dimmers
  (DALI, etc.) with brightness and colour *temperature* (tunable white,
  within the range the fixture reports to the gateway — 2000–6000 K on every
  fixture seen so far). Full RGB colour is not exposed by the gateway.
- **Sockets** — on/off plus their live meter readings (power, current, …)
  and, on gateway firmware 2.1.x+, the socket's **cumulative energy counter**
  as a `total_increasing` sensor — add it to the Energy Dashboard directly.
- **Blinds / shutters (covers)** — open/close/stop, position, and slat tilt.
  Covers that expose slat tilt show up as blinds; position-only ones as roller
  shutters, with the matching icons and controls.
  **Awnings** report position inverted (their motor mounts the opposite way);
  flag them under Settings → Devices & Services → Jung Home → **Configure**
  and they read correctly, with an awning icon.
- **Thermostats** (room temperature regulators) — target temperature, presets,
  and heating activity (`hvac_action`). The gateway offers no on/off for a
  regulator, so these entities are heat-only; the **frost protection** preset
  is the closest thing to "off". The room temperature is also a standalone
  **temperature sensor**, so it keeps long-term statistics (a climate
  entity's own reading has none).
- **Scenes** — every JUNG HOME scene appears as a `scene.*` entity, and scene
  recalls from *any* source (including physical buttons) fire a Home Assistant
  event — see [Scenes](#scenes).
- **Rocker switches (buttons)** — each button side is an **event entity** and
  offers **device triggers** for `click`, `hold_start` and `hold_end` (plus
  the raw press/release edges), so a click or a hold can start any automation
  or script — see [Button automations](#button-automations-rocker-switches).
  The status LED is switchable (colour is app/BT-Mesh only — see limitations).
- **Presence/motion detectors ("BWM")** — detection surfaces as an
  **occupancy binary sensor** next to the detector's ambient readings (e.g.
  illuminance).
- **Rooms** — each device is placed in the Home Assistant area matching its
  JUNG HOME group. A device is only ever placed if it has no area yet, and
  each device is considered just once, so this never moves a device you placed
  yourself and never re-adds one whose area you deliberately cleared. A group
  matching an existing area links to it instead of creating a duplicate.
- **Gateway connectivity** — a diagnostic sensor on the hub device shows
  whether the live WebSocket link is up.

Feedback and issue reports are welcome — see
[Filing a bug](#troubleshooting).

## Installation

### HACS (recommended)

**Jung Home is in the HACS default store** — no custom repository needed.

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ernetas&repository=junghome&category=integration)

1. **HACS** → search for **Jung Home**.
2. **Download**, then restart Home Assistant.
3. Settings → Devices & Services → **Add Integration** → Jung Home (see
   [Setup](#setup)). In most cases the gateway is discovered for you and
   appears there on its own, so you can just click **Configure**.

The button above opens the repository straight in your HACS. If HACS is new to
you, install it first: <https://hacs.xyz/docs/use/download/download/>.

#### Updating

HACS notifies you when a new release is out (**HACS → Jung Home → Update**).
Restart Home Assistant afterwards. Config entries, entity IDs and automations
survive updates — entity `unique_id`s are derived from device labels rather
than the gateway's internal ids precisely so they stay put.

#### Beta releases

Pre-releases are hidden by default. To test one, open **HACS → Jung Home → ⋮ →
Redownload** and enable **Show beta versions**.

### Manual

Copy `custom_components/junghome/` into your Home Assistant
`config/custom_components/` directory and restart. (Repeat on every update —
HACS automates this for you.)

## Setup

In most cases the gateway is **discovered automatically** over mDNS and
appears under **Settings → Devices & Services** — just click **Configure**.
If it isn't discovered, go to **Add Integration → Jung Home** to start setup
manually.

Either way you then pick **how to connect**:

- **Approve the connection in the Jung Home app.** Home Assistant asks the
  gateway for access and waits; open the **Jung Home app** and approve the
  request under **Settings → Gateway → Access Permissions → Open Requests**.
  Setup finishes automatically once you approve (within ~3 minutes) — if it
  times out, submit again and re-approve. (Uses `POST /api/junghome/register`.)
- **Enter the gateway network-key password.** Connects immediately, with no
  app approval, by exchanging the gateway's network-key password for a token
  (uses `POST /api/junghome/register/by-password`). You can find the password
  in the Jung Home app.

The gateway address is filled in for you when it was discovered; otherwise it
defaults to `junghome.local` (the name on the gateway's certificate). That
name resolves only on networks whose DNS happens to serve it — the gateway
itself announces `junghome-<mac>.local` (its MAC address without colons) —
so change it to that name or to your gateway's IP (e.g. `192.168.1.50`) if it
doesn't resolve.

The issued token is stored in the config entry. Devices added or removed in
the Jung Home app afterwards are picked up automatically. The entry is keyed
on the gateway's **hardware serial** (read from mDNS or the gateway itself),
so if the gateway's IP later changes while Home Assistant cannot reach it at
the old one, discovery updates the stored address automatically — however
the entry was added — provided the gateway at the new address presents the
pinned certificate (see [Security](#security); an entry that has not pinned
yet trusts the announcement, as its first contact). An entry that is connected
and healthy is never moved by a discovery packet. On networks without mDNS
(e.g. across VLANs) use **Reconfigure** to point the entry at the new
address; it verifies the address actually belongs to *this* gateway before
saving.

### Security

The gateway's HTTPS certificate is self-signed, so it cannot be verified
against a certificate authority. Instead the integration **pins** it: the
certificate's SHA-256 fingerprint is learned the first time an entry
connects (at registration for new entries; on the next successful
connection for entries created before this existed) and stored in the
entry. From then on every request and the WebSocket connection is made
only to a server presenting that exact certificate — the check happens at
the TLS handshake, before the access token or anything else is sent — so
a device impersonating the gateway on your network, or a forged mDNS
announcement naming the gateway's (public) serial, cannot obtain the token.
This is what the JUNG HOME app does too, with a fingerprint it reads over
the mesh. The remaining assumption is the **first connection**: whatever
answers at the gateway's address when an entry first pins is trusted, so
set up (and upgrade) on a network you trust.

If the gateway ever presents a different certificate — a replaced gateway,
or a wiped or re-imaged storage card; the certificate lives on the gateway's
data partition and survives a factory reset and firmware updates — the
integration stops talking to it (its entities become unavailable) and
raises a **"Jung Home gateway certificate changed"** repair issue under
**Settings → System → Repairs**. Confirm it there only if you know why the
certificate changed; the fix re-pins the new certificate and reconnects.
If nothing changed on your side, another device is answering at the
gateway's address — check your network instead. **Reconfigure** to an
address that presents a different certificate asks for the same
confirmation before anything is sent to it.

## Options

**Settings → Devices & Services → Jung Home → Configure:**

- **Poll interval (seconds)** — how often the gateway's device list is re-read
  over REST, between 30 seconds and 1 hour (default 60). This is only the
  backstop: live state keeps arriving over the WebSocket regardless, so a
  longer interval mainly reduces gateway load. It does stretch everything the
  poll drives — a device added while the WebSocket is down appears up to one
  interval later, and the ten-miss debounce before a removed device disappears
  scales with it (up to ten hours at the maximum).
- **Ignore duplicate button presses** (on by default) — current JUNG device
  firmware reports every tap twice; the integration drops the copy (a press
  on the same button within 1.2 s of a click). A button whose firmware the
  gateway reports as older than 2.2.0 is exempt automatically (it reports
  each tap once), so the switch only matters for buttons whose firmware is
  unknown or current; turn it off only if you need presses closer together
  than 1.2 s (double-clicks) on such a button — details under
  [Button automations](#button-automations-rocker-switches).
- **Inverted covers (awnings)** — flag covers whose position is reported
  backwards, as described under [What works](#what-works).

Saving reloads the integration; entities, history and automations are kept.

## Button automations (rocker switches)

Rocker buttons show up as Home Assistant **event entities** (one per up/down
side). Every press is classified for you: a press released within a second
fires a **`click`** event, a longer one fires **`hold_start`** after one
second and **`hold_end`** at the release (the raw `pressed`/`depressed`
edges still fire too). Each button also offers the same events as **device
triggers** — open the button's device page, add an automation, and pick e.g.
*"Up button clicked"* or *"Up button hold started"*. That's the quickest
route for a "press this, do that" automation; no timing to tune.

- Full guide + copy-paste recipes (click, hold-to-dim): [`docs/example-button-automation.md`](docs/example-button-automation.md)
- Blueprint (a form for click + hold actions): [`blueprints/automation/junghome/button_gestures.yaml`](blueprints/automation/junghome/button_gestures.yaml)
  — HACS installs only the integration, never blueprints, so import it with
  the button below (or by URL: Settings → Automations & scenes → Blueprints
  → Import).

  [![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fernetas%2Fjunghome%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fjunghome%2Fbutton_gestures.yaml)

**Why no double-click?** Current JUNG device firmware (2.2.0.x, mid-2026)
reports one tap as *two* press/release pairs, which makes a single click
indistinguishable from a double over the gateway. The integration drops the
duplicate (the *Ignore duplicate button presses* [option](#options), on by
default) so a tap fires once — the trade-off is that two presses on one
rocker less than 1.2 s apart count as one. On older device firmware that
reports each tap once you can turn the option off and use the blueprint's
legacy double-click path; the [guide](docs/example-button-automation.md)
shows how to measure your buttons.

## Scenes

Scenes defined in the JUNG app appear as Home Assistant **`scene.*`
entities** — activating one (or calling `scene.turn_on`) recalls it on the
gateway.

The gateway also reports when a scene is recalled **by any source**, including
a physical wall button. The integration re-emits that as a Home Assistant
event, `junghome_scene_recalled` (and as a clickable logbook entry), so you
can trigger automations from a physical scene button:

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

The event data is `{ scene_id, label, entry_id, entity_id }` (`entity_id` is
the matching `scene.*` entity, present when one exists — match on it instead
of `label` if you prefer stable ids over labels).

## How updates work

The integration is **local push**: it holds a WebSocket to the gateway and
applies state changes the moment the gateway broadcasts them, so device states
update in real time. Devices added or removed in the app are push-driven too —
the gateway broadcasts its device list on change and the integration adopts it
immediately. A full REST re-fetch runs every 60 seconds as a backstop — the
interval is adjustable, see [Options](#options) — and on every WebSocket
reconnect. If the WebSocket drops it reconnects automatically with backoff. No
cloud and no account are involved.

**Renaming a device in the Jung Home app renames it here.** Devices are
identified by their label, so a rename used to look like a removal plus a new
device. The integration now remembers which mesh element each label was on
(the gateway's function id, and the node's Bluetooth address plus element
location once the project export has been read) and, when a label disappears
while a new one appears on the same element, renames the Home Assistant device
and moves its entities over in place — history, area, customisations and
automations included. Entity ids stay as they were (Home Assistant never
renames those on its own; rename them yourself if you want them to follow).
This also works for a rename made while Home Assistant was off. What is still
a new device: a label moved to a *different* element (the old name given to
another device, two names swapped) — the entities follow the name, as before —
and a rename combined with re-provisioning the node while Home Assistant is
running (the hardware identity of the new function is not known yet when its
list arrives; at startup that case is paired too).

## Removing the integration

1. **Settings → Devices & Services → Jung Home → ⋮ → Delete.** This removes
   the config entry and every device and entity it created.
2. **Revoke Home Assistant's access in the Jung Home app**, under
   **Settings → Gateway → Access Permissions**. The gateway keeps the token it
   issued until you revoke it there — deleting the entry in Home Assistant
   does not tell the gateway to forget it.
3. Optionally remove the repository from HACS (**HACS → Jung Home → ⋮ →
   Remove**) if you don't intend to reinstall.

Automations that referenced the removed entities keep their (now missing)
entity IDs — Home Assistant will flag them as unavailable until you edit them.

## Troubleshooting

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
The WebSocket that carries live push has been down for more than three
minutes (a normal gateway reboot or firmware update is shorter and never
triggers this). The integration keeps working on the REST poll (60 seconds
by default — see [Options](#options)), so states stay correct but stop being
instant, and controllable and button-event entities read unavailable because
commands and button presses only travel over the WebSocket. It clears itself once the connection is genuinely
back. If it persists, check that the gateway is reachable and hasn't been
rebooting.

**Voltage, current and frequency sensors are missing.**
They register disabled by default on new installs (they are noisy diagnostics);
enable them from the device page. Existing installs keep them as they were.

**Devices show a serial number.**
On gateway firmware 2.1.x+ (API 1.5.0) each device carries its node's
Bluetooth address as serial number, read from the gateway's project export;
older firmware shows none.

**Entities are unavailable but the gateway is up.**
Controllable entities (lights, sockets, covers, thermostats, status LEDs)
and button event entities require the live WebSocket: commands only go out
over it and button presses only arrive over it. Sensors and binary sensors
stay available on the REST poll alone. So "sensors fine, lights unavailable" points at the WebSocket
specifically — see the repair notice above.

**"Jung Home gateway certificate changed" repair notice.**
The device answering at the gateway's address presents a TLS certificate
other than the one pinned when the entry first connected, so the integration
has stopped sending anything to it — the access token included — and the
entities are unavailable. Expected only after a gateway replacement or a
wiped/re-imaged storage card (the certificate survives a factory reset and
firmware updates): confirm
the repair to pin the new certificate and reconnect (it still checks the
gateway's serial, so a *different* gateway is refused — use Reconfigure for
that). If none of that happened, do not confirm; something else is answering
at that address. See [Security](#security).

**Home Assistant asks you to re-authenticate.**
The gateway rejected the stored token, usually because it was revoked in the
app or the gateway was factory-reset. Follow the reauth prompt: press submit
to send a new access request, then approve it in the Jung Home app.

**A device disappeared from Home Assistant.**
The integration removes a device once it has been missing from ten consecutive
device lists — REST polls, plus the list the gateway pushes over the WebSocket
on connect and on every change — so at most about ten minutes at the default
[poll interval](#options), longer if you raised it. That is how a device you
delete in the JUNG HOME app also leaves Home Assistant. A removal is logged as
a warning naming the device, so check the log if one goes unexpectedly. If the
device is still installed, make sure it is powered and in range of the mesh;
it is re-added automatically once the gateway reports it again under the
same name, and Home Assistant brings back the custom name, area and
`entity_id` you had set (it keeps those for removed devices and entities), so
only automations that fired while it was gone notice the gap. You can also
remove a stale device yourself from its device page (**⋮ → Delete**); Home
Assistant refuses this while the gateway is still reporting the device, since
it would simply come straight back.

**A device you added in the app doesn't show up.**
New devices normally appear within seconds (the gateway pushes its device
list on change), and within one [poll interval](#options) at worst — 60
seconds by default — via the REST poll. If one still doesn't appear, download
diagnostics (**⋮ → Download diagnostics** on the entry) —
`support_summary.unhandled_function_types` and
`unhandled_datapoint_types` list anything the gateway reports that this
integration doesn't yet map, which is exactly what an issue report needs.

**Filing a bug.** Attach the diagnostics download. The gateway token and host
are redacted; device labels are kept because they are the identity anchor,
and the pinned certificate fingerprint is listed (it is public — every TLS
handshake presents it).

## Known limitations

- **The energy counter comes from a deprecated gateway endpoint.** The
  gateway's function list carries a socket's instantaneous readings only; the
  cumulative Wh counter lives in the device's *properties*, which only the
  `/devices/?verbose=true` endpoint (marked deprecated/experimental in the
  gateway's own API spec) exposes. It works on 2.1.3. The gateway reads the
  counter from the socket only about **once an hour**, so the sensor rises in
  hourly steps (the integration re-reads the gateway every five minutes, so
  a step shows up within minutes of the gateway's read). A future firmware
  could drop the endpoint, in which case the sensor simply disappears and the Riemann-sum
  [Integration helper](https://www.home-assistant.io/integrations/integration/)
  on the power sensor is the fallback. Firmware without the endpoint shows no
  energy sensor at all.
- **No double-click on current device firmware** — it reports every tap
  twice, so a double is indistinguishable from a single; click and hold are
  what the buttons offer (see
  [Button automations](#button-automations-rocker-switches)).
- The rocker **status-LED colour** can't be set from here (on/off only);
  colour is configured in the JUNG app or over BT-Mesh.
- **Colour temperature is limited to the range the fixture reports** — the
  gateway clamps every tunable-white command to it (2000–6000 K on every
  fixture seen so far, and the gateway's default until a fixture has
  answered).
- The **puck** isn't supported/validated yet.
- **Thermostat temperature moves in 0.5 °C steps, a few times an hour.** That
  is the device's reporting (the BT-Mesh temperature property it publishes
  has 0.5 °C resolution, and the gateway re-reads it only when it has heard
  nothing for five minutes), not
  something the integration can refine.
- **Two devices with the same label collide.** The gateway's device ids are
  derived from each node's mesh identity and location, so they change when a
  device is re-provisioned or re-enumerated, and the device list carries no
  hardware identifier — so the device *label* is the identity anchor the
  integration uses (renames are followed, see [How updates
  work](#how-updates-work)). Devices whose labels are identical — or that slug
  identically, e.g. `Lamp 1` and `Lamp-1` — map to the same id and only the
  first one gets entities. Give each device a distinct label in the Jung Home
  app. With more than one gateway, keep labels distinct across gateways too:
  two gateways' devices with the same label collide the same way.

## Disclaimer & legal

This is an **independent, unofficial** project. It is **not** affiliated with,
authorized, sponsored, or endorsed by Albrecht JUNG GmbH & Co. KG. "JUNG" and
"JUNG HOME" are trademarks of their respective owner and are used here **only
descriptively** (nominative use) to identify the devices and gateway this
software interoperates with.

- **Purpose — interoperability.** This integration talks to the JUNG HOME
  Gateway's local API so that owners can operate **their own** devices from
  Home Assistant. It is an independently created program that interoperates
  with the gateway; it does not modify the gateway or its firmware.
- **No vendor material is redistributed here.** This repository contains no
  vendor firmware, no decompiled app or gateway software, and no vendor logo or
  brand artwork. The integration's icon is served by Home Assistant's own
  brands repository, not bundled here. Development inputs that contain vendor
  software or private keys (such as a gateway disk dump) are kept locally and
  are git-ignored — do not commit them.
- **Use with your own gateway only.** Use this only with a JUNG HOME gateway
  and devices that you own or are authorized to administer. You are responsible
  for your use of it.
- **No warranty.** Provided "as is" under the MIT License, without warranty of
  any kind. **Use at your own risk.**

See [DISCLAIMER.md](DISCLAIMER.md) for the full notice. This is not legal
advice.

## Gateway internals (for contributors)

The local gateway API (REST + WebSocket), its registration flow, and the
device-mesh architecture are documented in **[docs/](docs/README.md)**.
Release and HACS-publishing steps are in
**[docs/publishing.md](docs/publishing.md)**.

## Development / Testing

You can run a throwaway Home Assistant instance with this integration loaded,
without touching a real deployment.

### Docker Compose (no local Python needed)

```bash
docker compose up          # Home Assistant at http://localhost:8123
docker compose down        # stop
docker compose down -v     # stop and wipe HA state
```

The repo's `custom_components/` is bind-mounted into the container, so editing
the integration and running `docker compose restart` picks up changes without
a rebuild. Pin a specific HA version by replacing `stable` in
`docker-compose.yml`.

### Local (devcontainer / venv)

```bash
scripts/setup     # install dependencies
scripts/develop   # run Home Assistant against ./config with the integration on PYTHONPATH
scripts/lint      # ruff
pytest            # full test suite (see requirements_test.txt)
```
