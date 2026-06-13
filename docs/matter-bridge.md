# Exposing JUNG HOME to Matter

Two separate questions:

1. **Can the JUNG gateway itself be a Matter bridge?** — Not on current firmware.
2. **Can you expose JUNG devices to Matter anyway?** — Yes, from the Home
   Assistant side.

## 1. The gateway's built-in Matter — present but inactive

The gateway has a scaffolded `matter-interface` component and pre-staged
commissioning data (`sdc4/matter-interface/matter_setup_data.json`: vendor
`0x1429`, product `11`, discriminator `1538`, SPAKE2+ verifier), but on the
firmware imaged here the **bridge daemon is not installed** — only an empty
`res/` directory, and there are no references to it from the api-server or
middleware. So there is no switch to flip; the process that would advertise and
commission simply isn't there. It looks reserved for a future JUNG firmware. If
JUNG ships it, commissioning would use the pre-staged discriminator + a passcode
(the stored SPAKE2+ verifier can't be reversed to the passcode — it would come
from JUNG's app/QR).

## 2. Bridge from Home Assistant (works today)

Since this integration already brings JUNG devices into Home Assistant, the
practical route is a **Home Assistant → Matter bridge**, which exposes selected
HA entities as a Matter bridged device that Apple Home, Google Home, Alexa, or
another HA can commission.

> Note: HA's *built-in* Matter support is a **controller/commissioner** (via the
> Matter Server add-on) — it lets HA control Matter devices. It does **not**
> expose HA entities outward. For that you need a bridge project.

### Option: `home-assistant-matter-hub`

A community project that runs alongside HA, reads entities via the HA API, and
advertises them as a Matter bridge.

Sketch (`docker compose`):

```yaml
services:
  matter-hub:
    image: ghcr.io/t0bst4r/home-assistant-matter-hub:latest
    network_mode: host                # Matter needs IPv6 + mDNS on the LAN
    environment:
      HAMH_HOME_ASSISTANT_URL: http://homeassistant.local:8123
      HAMH_HOME_ASSISTANT_ACCESS_TOKEN: <long-lived-access-token>
    volumes:
      - ./matter-hub:/data
    restart: unless-stopped
```

Then add a bridge in its UI filtered to your `light.*` / `switch.*` /
`sensor.*` JUNG entities, and commission it from your Matter controller with the
pairing code it shows.

Caveats: Matter requires **host networking / IPv6 / mDNS** reachable on your LAN;
bridged-device support for some domains (e.g. events from rocker switches) is
limited by what Matter device types exist; check the project's current support
matrix.

### Option: roll your own with `matter.js`

If you go the [gateway-free BT-Mesh route](bt-mesh-direct.md), you can wrap that
layer in a [`matter.js`](https://github.com/project-chip/matter.js) bridge to
expose devices to Matter natively, end to end, without HA in the path.

## Recommendation

- Want Matter now, least effort: **`home-assistant-matter-hub`** on top of this
  integration.
- Want it native/gateway-free: **BT-Mesh direct + a `matter.js` bridge** (a real
  project).
- Want JUNG's own bridge: **wait for firmware** that ships `matter-interface`.
