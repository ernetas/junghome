# JUNG HOME Gateway — WebSocket protocol

The gateway pushes live state and accepts commands over a WebSocket. This is
what the integration's coordinator uses.

## Connecting

```
wss://<gateway>/ws
```

nginx proxies `/ws` to the internal WebSocket server (`127.0.0.1:8080`) with a
24-hour idle timeout. Authentication uses the **same token as the REST API**,
sent as the `token` header (or a `token` cookie) on the upgrade request. A bad
token is rejected with `401 Unauthorized`.

Every frame is a JSON object of the form:

```jsonc
{ "type": "<string>", "data": <any>, "message_id": "<optional>" }
```

## Handshake (server → client, on connect)

In order:

1. `{ "type": "message", "data": "Hello from JUNG HOME Gateway" }`
2. `{ "type": "version", "data": "1.5.0" }`
3. After ~1 s, the current state is pushed:
   - `{ "type": "functions", "data": [ ...all functions... ] }`
   - `{ "type": "groups", "data": [ ...all groups... ] }`
   - `{ "type": "scenes", "data": [ ...all scenes... ] }`

(The `functions` payload matches `GET /functions/` — see
[gateway-rest-api.md](gateway-rest-api.md).)

## Server → client message types

| `type` | `data` | Meaning |
|--------|--------|---------|
| `message` | string | Info / error text. Errors look like `"error: <reason>"`. |
| `version` | string | Gateway API version. |
| `functions` | array | Full list of functions (sent on connect and on change). |
| `datapoint` | object | A single datapoint changed (broadcast to all clients), **or** the reply to a client `datapoint` command. |
| `scene` | object | A scene was recalled. |
| `groups` / `groups-new` / `groups-deleted` | array | Full groups list / added / removed. |
| `scenes` / `scenes-new` / `scenes-deleted` | array | Full scenes list / added / removed. |
| `devices-new` / `devices-deleted` | array | Lower-level device ids added / removed. (A full `devices` list type exists in the server too, but its emit call is commented out on current firmware — only the deltas can arrive.) |
| `config` | object | Configuration (currently not emitted). |

The `*-new` / `*-deleted` variants are how the gateway signals that nodes,
groups, or scenes were added or removed at runtime (e.g. provisioning a new
device in the app).

A pushed `datapoint` frame carries the updated datapoint object, e.g.:

```jsonc
{ "type": "datapoint",
  "data": {
    "id": "id5f09764942a70ce-001",
    "type": "switch",
    "values": [ { "key": "switch", "value": "1" } ]
  } }
```

### Scene recall (`scene`, singular)

When a scene is activated — including by a **physical button**, not just via the
REST recall — the gateway broadcasts a `scene` frame whose `data` is the recalled
scene object (note: singular `scene` with an object, distinct from the plural
`scenes` list broadcast):

```jsonc
{ "type": "scene",
  "data": {
    "id": "id0001",
    "label": "Išjungti WC",
    "related_functions": [ "id9dc9e42e3bbb3da", "idef507c9c9a01d16" ],
    "value": "0001"
  } }
```

The integration re-emits each recall on the Home Assistant event bus as
`junghome_scene_recalled` (`{scene_id, label, entry_id}`) so automations can react
to physical scene buttons. (One recall produces one frame — back-to-back
duplicates you may see while testing are just the scene being triggered twice,
e.g. a double press.)

## Client → server commands

Send a JSON frame with a `type`. An optional `message_id` is echoed back on the
matching reply.

### Set / get a datapoint

```jsonc
{ "type": "datapoint",
  "data": {
    "id": "id5f09764942a70ce-001",
    "values": [ { "key": "switch", "value": "1" } ]
  },
  "message_id": "abc"            // optional
}
```

- If `data.values` is present, the gateway **sets** those values, then re-reads.
- The gateway replies with the fresh datapoint:
  `{ "type": "datapoint", "data": { ...datapoint... }, "message_id": "abc" }`.
- `data.id` is required. (A `type` field inside `data` is ignored by the gateway,
  which looks the datapoint up by `id`; the integration includes one anyway.)

Common `values` keys by device type:

| Device | key / value |
|--------|-------------|
| Switch / light on-off | `switch` = `"0"` / `"1"` |
| Dimmer | `brightness` = `"0".."100"` (device scale) |
| Tunable white | `color_temperature` = Kelvin, e.g. `"2700"` |
| Cover position | `level` = `"0".."100"` (device scale, percent-*closed*; see note) |
| Cover move / stop | `level_move` = `"1"` (closing/down) / `"-1"` (opening/up) / `"0"` (stop) |
| Cover slat tilt | `angle` = `"0".."100"` |
| Thermostat target | `temperature_ctrl` = °C, e.g. `"21.5"` (range 5..30) |
| Thermostat preset | `temperature_ctrl_preset` = `frost` / `eco` / `comfort` (write); reads report the matching preset or `""` — see note below |
| Status LED (rocker) | `status_led` = `"0"` / `"1"` |
| Rocker press (read-only events) | `up_request` / `down_request` / `trigger_request` = `"1"` |

> **Cover `level` is percent-*closed* (confirmed from gateway firmware).** In the
> middleware (v2.1.3: `models/device_states/PositionState.js` `publishMode` —
> the v2.0.0 build kept this in `btmesh_set_datapoint_service.js`) a
> *close* maps to BT-Mesh "down" (`0x7FFF` ⇒ drives the Generic Level toward
> 100 %) and an *open* to "up" (`0x8000` ⇒ toward 0 %). So `level` 100 = fully
> closed, 0 = fully open, and the integration uses HA position = `100 - level`.
> This is correct for roller shutters/blinds. **Awnings (Markise) mount the motor
> the opposite way** — fully retracted reports `level` 0 yet the user calls that
> "closed" — so they read inverted. The gateway exposes no awning hint (both are
> `Position`) and has a per-device `Blinds Invert Output` firmware property, so
> direction is genuinely per-device: the integration lets users flag such covers
> in its options flow, which switches that cover to an identity mapping. The
> inversion lives in one place (`cover.py` `_to_ha`/`_to_device`).

> **Whether a cover exposes slat tilt is per-device and can change.** In
> `function_helper_methods.js` a `WindowCover` is reported as **`PositionAndAngle`
> with an `angle` datapoint** only when its angle state is visible
> (`device.states.angle?.profile.visible === true`); otherwise it is reported as
> **`Position`** with just a `level` datapoint and no tilt. `level` and `angle`
> are the *same* BT-Mesh model (Generic Level `0x1002`) — only the datapoint
> `type` string (`"level"` vs `"angle"`) distinguishes them. So the presence of
> the `angle` datapoint is the single source of truth for tilt, and it can appear
> or disappear across firmware updates (which re-enumerate devices, their
> datapoints sometimes arriving over several polls) or when the slat channel is
> toggled in the JUNG HOME app. The integration gates HA's tilt features on that
> datapoint and reloads the entry when a device's datapoint set changes so the
> capability is rebuilt (see `_register_capability_reload` in `__init__.py`).

> **A Thermostat's `switch` datapoint is *not* the regulator's on/off — and a room
> regulator has no on/off at all.** The middleware builds a `Thermostat` from
> exactly three device states — `SetPoint`, `sensor_ambient_temperature` and
> `AutomaticMode` (`jung-home-device.js`, `JungHome_Thermostat`) — and
> `datapoint_helper_methods.js` re-labels the third one on the way out
> (`case StateType.AutomaticMode: return DatapointType.Switch`), so it reaches the
> API as an ordinary `switch` = `"0"` / `"1"` datapoint with nothing to
> distinguish it from a light's or a socket's. Internally it is a Generic OnOff
> `0x1000` server state the gateway reads as `"manu"` (0) / `"auto"` (1)
> (v2.1.3: `models/device_states/AutomaticModeState.js`; the v2.0.0 build's
> `btmesh_get_datapoint_service.js`), and the RTR scheduler property
> `LBC_PROP_RTR_SCHEDULER_ENABLE_ID` writes into that *same* state
> (v2.1.3: `models/device_property_states/SchedulerEnableState.js`, bound
> `scheduler_enable → automatic_mode`; v2.0.0's `handleThermostatAutomaticMode`)
> — two sources feeding one value. In the field it flips on its own several
> times an hour, tracking the regulator's momentary heating output (these RTRs
> drive heating with a ~15-minute PWM cycle) while setpoint, preset and ambient
> temperature stay unchanged — see
> [issue #121](https://github.com/ernetas/junghome/issues/121). Nothing in the
> state set switches a regulator off, either: v2.1.3 carries an implemented
> HVAC-mode property (`HvacModeState`, `LBC_PROP_RTR_HVACMODE_DISPLAY_ID`) but
> it is read-only, display-oriented and category `manufacturer_property`, which
> `getDatapointTypeByState` never maps to an API datapoint (v2.0.0's
> `Property_RTR_SensorHVAC_Mode` was commented out entirely) — so the `frost`
> preset remains the closest equivalent. The integration therefore reads this
> datapoint as HA's `hvac_action` (`heating` / `idle`) only, holds `hvac_mode`
> at `heat`, and never writes it. Treating it as an on/off is what made every
> thermostat entity flap between `off` and `heat`.

> **Thermostat presets: the API descriptor lies about `none`.**
> `cdb_types_datapoints.json` (and `/apidoc`) advertise
> `temperature_ctrl_preset` values `["none","frost","eco","comfort"]`, but the
> implementation contradicts the descriptor in both directions. **Writes**: the
> middleware routes any present `temperature_ctrl_preset` value to the preset
> publisher, which throws "does not set a valid preset" for anything but
> `frost`/`eco`/`comfort` — including `"none"` (`ip_event_handler.js` +
> `SetPointState.publishMode`); the retry loop then re-throws after 3 attempts
> and the client sees only an uncorrelated `error:` frame. **Reads**: a preset
> is a *derived* fact — `getRTRTemperatureMode` compares the target temperature
> against the device's three configured thresholds and returns the matching
> name or the **empty string** (`property_helper_methods.js`; its own JSDoc
> says "none" but the code returns `""`), which
> `datapoint_helper_methods.js` forwards verbatim. So `""`, not `"none"`, is
> what "no preset" looks like on the wire — it is the common steady state for
> any manually chosen target. The integration maps `""` to HA's `PRESET_NONE`
> on read and never writes `"none"` (selecting "None" in HA is a local no-op).

### Rocker buttons: what a press actually looks like on the wire

A rocker reports **only raw edges** — `"1"` on press, `"0"` on release — on
`up_request` / `down_request` (`trigger_request` for a single-action device).
There is no native click, double-click or hold. Two properties of the pipeline
matter for anyone deriving gestures from these frames:

**1. The gateway suppresses a message only when *nothing* changed.**
`communicateToAPI` (`services/device_state_service.js`) returns early unless
`hasChanged`, and `hasChanged` is `isNewValue || isNewMode || isNewVisibility`
(`device-states.js` `update`). A button state carries a **mode** as well as a
value — `pushed`, `held` or `released` (`ButtonModes`) — so a *mode* change
alone is enough to emit a frame. Because the API representation of a rocker
datapoint carries only the value (`composeDatapointByState` adds extra keys for
quantity/temperature/level datapoints, but not for button ones), **a mode-only
change appears on the wire as a repeated identical value**. Archived gateway
logs show exactly that, e.g. `pushed=1 held=1 pushed=0 released=0` — which
reaches a client as `1, 1, 0, 0`.

> The gateway therefore already classifies a tap versus a hold internally, but
> **does not expose that classification over the API**. Any integration has to
> re-derive from raw edge timing something the gateway knew and discarded. If
> JUNG ever surfaced the mode, native hold detection would become trivial.

**2. The device publishes each edge more than once, so a fast tap produces
two press/release pairs.** BT-Mesh publish retransmissions carry their own
sequence numbers, so replay protection does not drop them, and they are
delivered as independent messages. Captured from real hardware (JUNG rocker,
gateway 2.1.3):

```jsonc
// ONE physical quick click on the up side:
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"1"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"0"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"1"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"0"}]}}

// ONE physical press-and-hold, then release:
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"1"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"0"}]}}
```

The asymmetry identifies the mechanism. During a **hold**, the repeat of
`"1"` lands while the state is still `1` — no change, so rule 1 suppresses it,
and the gesture yields exactly one pair. During a **quick tap**, the repeat of
`"1"` lands *after* the first `"0"` has already been applied, so it reads as a
brand-new press and is emitted. Same for the release repeat. The duplicate is
on the **same** datapoint id, not the sibling channel.

> **Consequence for gesture logic: a single click and a double click can be
> byte-identical.** Both produce `1,0,1,0` on one channel. Any rule that treats
> "a second `pressed` shortly after a release" as a double-click will report a
> *single* click as a double. The only thing that separates them is the
> **interval**: retransmission repeats follow the previous edge far faster than
> a human can tap twice, so a minimum-gap filter is the fix — see the
> integration note in `docs/example-button-automation.md`.

**Measured on real hardware** (JUNG rocker, gateway 2.1.3, timestamped capture
of ~20 gestures via `tools/ws-capture/capture_ws.py`):

| Quantity | Observed |
|---|---|
| press → release, ordinary tap | 0.37 – 0.50 s |
| press → release, deliberate hold | 2.38 – 2.51 s |
| release → duplicate press (the artifact) | **0.078 s** |
| release → next *human* press | ≥ 0.52 s |

Two things follow. First, the duplicate is an order of magnitude closer to the
preceding release than any human-produced gap, so a cooldown of roughly
0.15–0.25 s separates them cleanly. Second, **the duplication is
intermittent** — it appeared once in ~20 gestures in this capture, while an
earlier hand-recorded sample showed it on every quick tap. That fits the
retransmission explanation (whether the repeat lands before or after the
release depends on radio timing and relaying) and means gesture logic cannot
assume either behaviour: it must tolerate a duplicate that *may* appear.

Note also that an ordinary tap holds for ~0.4 s as the gateway reports it,
which is far longer than the physical contact time — so a hold threshold must
sit well above 0.5 s (the shipped blueprint's 2 s is safe).

Note also that a rocker's press datapoints are **read-only**
(`writeable: false`, `UserPermission.ReadOnly` in `PushedUpState.js`) — nothing
can inject a button edge over the API, so duplicated edges always originate at
the device or the mesh, never from a client.

### Other command types

| `type` | Behaviour |
|--------|-----------|
| `message`, `version` | Logged by the gateway; no reply. |
| `api_version` | Reserved / not implemented. |
| `scene`, `functions`, `get_devices` | **Not implemented** — return `{"type":"message","data":"error: ... not implemented ..."}`. Use the REST API for scenes. |
| anything else | `{"type":"message","data":"error: ...message type is unknown"}` |

### Errors

Any failure is returned as a `message` frame:

```json
{ "type": "message", "data": "error: could not set datapoint (id...-001) value, ..." }
```

## Notes for the integration

- State updates arrive as `datapoint` broadcasts; the coordinator matches them to
  entities. Commands are sent as `datapoint` set frames, each tagged with a
  `message_id`; the coordinator awaits the matching reply (short timeout)
  instead of firing and forgetting, so a rejected command now surfaces as a
  real service error and the confirmed re-read value — not just an
  optimistic guess — lands before the awaiting service call returns. A
  rejection itself is not correlatable (see "Errors" above), so it surfaces
  as a timeout rather than the gateway's own error text.
- The coordinator consumes the `scenes` / `scenes-new` / `scenes-deleted`
  broadcasts to populate the scene platform (recall is REST-only), and the
  `groups` broadcasts for room→area assignment and diagnostics. The singular
  `scene` recall frame is re-emitted as a `junghome_scene_recalled` HA event.
- Reconnect on drop: the gateway sends the full `functions`/`groups`/`scenes`
  snapshot again on every new connection, so re-syncing is automatic.
- The `functions` broadcast (the authoritative device list, sent on connect
  and on change) is adopted by the coordinator exactly like a REST poll
  result, so devices added or removed at runtime appear/prune push-driven;
  the 60 s REST poll remains as the backstop. The lower-level `devices` /
  `*-new` / `*-deleted` frames are not consumed.
