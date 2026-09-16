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
2. `{ "type": "version", "data": "1.5.0" }` — **the API version, not the
   gateway's software version.** It is `api-junghome`'s own package version
   (`packageJson.version`, matching `apidoc.json` `info.version`), so a gateway
   running firmware 2.1.3 build 2840 announces `"1.5.0"` here. The software
   version is a REST read: the unauthenticated `GET /version/` reply (`version_release` (+
   `version_build`) — see [gateway-rest-api.md](gateway-rest-api.md).
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
to physical scene buttons. The scene `id` is `"id"` + the mesh scene number in
hex (`id0001` ↔ `value` `"0001"`). Back-to-back duplicate `scene` frames from a
physical scene key are **most likely not a double press**: on current device
firmware (2.2.0.x) every publication — including the key's `Scene Recall` —
goes out twice ≈1 s apart (see the rocker section below), and the middleware's
only dedupe here is a 1 s debounce on scene status
(`handler/bt_event_handler.js:237-251`, per the 2026-09-15 audit tracker), so a
second copy landing just outside that window produces a second frame. An
earlier revision of this doc attributed the duplicates to a double press; a
capture of the recall's TID (same TID ⇒ same recall) is still pending to
settle it (`docs/cross-repo-analysis.md` §5).

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
| Status LED (rocker) | `status_led` = `"0"` / `"1"` — on the mesh this is vendor property `0x5013 KEY_STATUS` (1 byte), written by the gateway as a User Property *Status* (`D1 27 05`) to the button element; see [bt-mesh-direct.md](bt-mesh-direct.md) |
| Rocker press (read-only events) | `up_request` / `down_request` = `"1"` / `"0"` (`trigger_request` exists in the datapoint descriptor only — no device model produces it) |

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
>
> That datapoint is also the only thing the gateway offers to tell a venetian
> blind from a roller shutter — the middleware calls both a `WindowCover`, and
> neither the function `type` nor any datapoint carries a product hint. So the
> integration derives HA's cover device class from it (`_device_class` in
> `cover.py`): `angle` present ⇒ slats ⇒ `blind`; position only ⇒ `shutter`; and
> a cover the user flagged as inverted ⇒ `awning` (that flag wins, an awning
> having no slats). Hard-coding `blind`, as the integration first did, gave
> every roller shutter slat-oriented controls and icons in the HA UI.

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
> HVAC-mode property (`HvacModeState`, `LBC_PROP_RTR_HVACMODE_DISPLAY_ID` =
> `0x120B`: 0 "", 1 heat, 2 cool, 3 frost) but it is read-only, display-oriented
> and a *property* state (`device_property_states/`), and the function
> assembly (`createFunctionListByDevices`) only maps `device.states` into a
> function's datapoints — property states never reach the API that way. (Its
> category `manufacturer_property` is *not* the gate, as an earlier revision
> said: `PushedUp`/`PushedDown`/`StatusLed` are `manufacturer_property` too and
> have explicit cases in `getDatapointTypeByState`.) v2.0.0's
> `Property_RTR_SensorHVAC_Mode` was commented out entirely — so the `frost`
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

A rocker reaches the API as **raw edges only** — `"1"` on press, `"0"` on
release — on `up_request` / `down_request`, one pair of datapoints per
`RockerSwitch` function, and **one `RockerSwitch` function is one mesh button
element**. Depending on the device's key layout an element is either a whole
rocker (its events carry the side, so both datapoints are physical) or a
single key (both datapoints still exist, but the side the gateway reports is
not physical — see below); a multi-gang panel is several elements, hence
several functions and HA devices. `trigger_request` is a descriptor entry no
device model produces. The API has
no native click, double-click or hold — but the **device does send gestures**:
a button element publishes vendor property `0x5012 KEY_EVT`
(`[counter u8][event u8]`) to the gateway's element group, and the gateway
flattens each gesture into edges (`services/btmesh_property_service.js:184-256`):

| `event` | gateway output | `buttonState` emitted |
|---|---|---|
| `0` / `1` | `pushed_down` / `pushed_up` (rocker halves) | `[1, 0]` — **the release is synthesised by the gateway** |
| `2` / `3` | `held_down` / `held_up` | `[1]` |
| `4` | `released`, side = `_prevButtonType` | `[0]` |
| `5` | `pushed`, side = **toggle of `_prevButtonType`** ("for downwards compatibility") | `[1, 0]` |
| `6` | `held`, side toggled | `[1]` |

The emitter loop (`:249-256`) sends each value to the API and waits 2 × 200 ms
before the next, so a click's synthesised release follows its press by ~0.4 s
plus API latency. `_prevButtonType` is **one field on the service** (initial
`"down"`, `:35`), shared by every button in the network, so on a single-key
element (events 5/6) the reported side is whatever the previous event on *any*
button was not — it carries no physical meaning. The counter byte is ignored
(`Number(values[1])`, `:185`) and nothing in the middleware dedupes button
events. The key mode a button is in (`0x5003 KEY_MODE`: 0 light, 1 blinds,
2 scene, 3 property, 4 thermostat, 5 switch, 6 gateway —
`models/jung-home-state-mode.js:39-47`) is what makes it publish `0x5012` to
the gateway's group (mode 6); the other modes act on the mesh directly
(whether they also emit `0x5012` is uncaptured). It is not exposed as a
datapoint. Two properties of the pipeline matter for anyone deriving gestures
from these frames:

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

> The gateway therefore already knows a tap from a hold (the device told it), but
> **does not expose that classification over the API**. Any integration has to
> re-derive from raw edge timing something the gateway knew and discarded. If
> JUNG ever surfaced the mode, native hold detection would become trivial.

**2. On current DEVICE firmware, one physical tap is reported as TWO
press/release pairs.** On a rocker half they land on the same channel; on a
single-key element they alternate `up`/`down` (see below). Measured with a
labelled capture (`tools/ws-capture/capture_ws.py`, 2026-08-02, one rocker,
gateway 2.1.3; each gesture group separated by silence so nothing is
mis-attributed):

| Gesture (labelled) | Pairs emitted | Pulse width (press→release) | Gap within the burst (release→press) |
|---|---|---|---|
| single click ×6 | **2** every time | 0.40 – 0.49 s | 0.11 – 0.95 s |
| double click ×5 | **2** every time | 0.40 – 0.53 s | 0.73 – 1.03 s |
| hold ~3 s ×5 | **1** every time | 2.44 – 3.11 s | — |

```jsonc
// ONE physical quick click on the up side arrives as:
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"1"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"0"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"1"}]}}
{"type":"datapoint","data":{"id":"id...-00c","type":"up_request","values":[{"key":"up_request","value":"0"}]}}
```

**The mechanism (established by the 2026-09-15 cross-repo audit).** Device
firmware 2.2.0.2 — shipped by app 2.2.0 — publishes **every access message
twice, ≈1 s apart, with a fresh sequence number and the same `counter`**. This
is an on-air capture by the Bluetooth-direct sibling project (seen for button
events *and* for OnOff status publications), not a firmware-code finding; the
CDB's publish-retransmit count is 0 on every model, so it is neither network
retransmission nor a configuration. The gateway then does the rest itself:
it ignores the counter byte, has no dedupe, and turns **each copy** of a click
(events 0/1/5) into a synthesised `[1, 0]` pair — two pairs per tap. On a
rocker element a hold's second copy (events 2/3, then 4) carries the same
value *and* mode as the first, so rule 1 suppresses it — one pair per hold,
whose pulse width *is* the finger (the 2026-08-02 capture). On a single-key
element (events 5/6) the side is toggled on every reception through the
service-wide `_prevButtonType`, so the two copies of one click land on
**alternating datapoints** (`up` then `down`); on a rocker element (events
0/1) the event byte carries the side, so both copies repeat the same channel
— the "same channel, not alternating" verdict of the 2026-08-02 capture (a
rocker) and the alternating pattern the sibling captured on key elements are
both right, per element type. A *hold* on a single-key element has not been
captured through the gateway; by the code its second event-6 copy lands on
the other datapoint (a value change, so not suppressed), so expect a different
shape there. Upstream report material: a one-line counter dedupe in
`btmesh_property_service.js` would fix all of it.

Three facts follow, and they set the design space for any gesture logic:

- **A single click and a double click are indistinguishable.** Both produce
  two structurally identical pairs, and their intra-burst gap ranges overlap
  (0.11–0.95 s vs 0.73–1.03 s). No threshold separates them; nothing recovers
  the distinction after the fact. (By the mechanism a double click should be
  two events × two copies = four pairs; the capture's two pairs are consistent
  with the copies overlapping inside the gateway's sequential `[1, 0]` emitter
  and rule 1 collapsing the repeats, but that has not been pinned — the
  simultaneous ms-resolution capture in `docs/cross-repo-analysis.md` §5 would.)
- **Tap vs hold separates perfectly — on pulse *width*, not gaps.** Tap
  pulses top out at 0.53 s, hold pulses start at 2.44 s: a five-fold empty
  band. The tap pulse is ~0.42 s regardless of physical contact time because
  it is the **gateway's own synthesised release** (two 200 ms delays in the
  emitter loop plus API latency) — not "device reporting granularity", as an
  earlier revision of this doc said. A hold threshold anywhere in ~1–2 s is
  safe; the shipped blueprint's 2 s is fine.
- **A duplicate-suppression window must be ~1.2 s and scoped per *device*,
  not per datapoint** (cover the 1.03 s worst gap plus margin; on a key element
  the second copy arrives on the *other* datapoint, so a per-datapoint window
  would let it through). Anything shorter lets some duplicates through;
  earlier revisions of this doc suggested 0.15–0.25 s from a mis-segmented
  unlabelled capture — refuted by the labelled one.

#### Live verification, 2026-09-16 (gateway 2.1.3/2840, device firmware 2.2.0.2)

Three recordings with `tools/ws-capture/capture_ws.py --script none` on the
reference network, one 2-gang rocker (two rocker elements, 4 sides) and one
1-gang "rocker" that the gateway sees as **two single-key elements** (two
function ids; each tap alternates its `up`/`down` datapoints). Raw files in
`disk_dump/ws-capture-live-20260916-*/` (gitignored).

| gesture | element type | n | shape on the wire |
|---|---|---|---|
| tap | rocker | 8 | **2 pairs, same side**, pulses 0.416–0.442 s, gap 0.175–0.784 s (one tap arrived as a single pair — copies do get lost) |
| double tap | rocker | 1 | 2 pairs — identical to a single tap |
| hold | rocker | 2 | **1 pulse**, 2.64 s and 3.25 s |
| tap | single key | 4 | `up` pair then `down` pair (or the reverse), copy 0.11–0.85 s after the first release — the per-device window's case |
| double tap | single key | 1 | 3 pairs (`down`, `up`, `down`), the fourth missing |
| hold | single key | 4 | 3 × a single 1.5–2.1 s pulse on one side; **1 × press, other-side press +1.4 s, release on the copy's side at +2.55 s, first side never released** |
| ~1 s press | single key | 1 | one 0.51 s pair: the device reported a click, no copy |
| A then B within 0.45 s | rocker | 1 | pairs interleaved (`up`P `down`P `up`r `down`r) then one copy of the second tap — two clicks, correct |

The copied hold is the one shape the gesture logic had predicted but never
seen: the gateway toggles a key element's side on every reception, so the
hold's second copy lands as a press on the *other* datapoint while the first
is still down, and the finger's release (event 4, `prevButtonType`) then
lands there too. `event.py` treats a press on the other side of a device
whose one side has been down for 0.6–2.5 s as that copy and completes the
hold with the copy's release (`BUTTON_HOLD_COPY_AFTER` /
`BUTTON_HOLD_COPY_WINDOW`). Why three of four holds carried no copy is
unknown (mesh loss, or the device re-publishing the *current* key state
rather than the message — the simultaneous mesh capture in
`docs/cross-repo-analysis.md` §5 would tell).

**This is a regression, and the gateway's own logs prove it.** The gateway
middleware logs every button state change, and the 2026-08-01 dump carries an
archived support snapshot with logs from 2026-06-20 → 2026-07-28 (~900
events, ~450 press bursts): **1.00 presses per burst** throughout — including
the very button measured above, clean single pairs on every click. Between
2026-07-29 and 2026-08-02 something changed it to 2.00. The gateway firmware
did not change (the June and August dumps are byte-identical builds); the
JUNG app went 2.1.0 → 2.2.0 in that window, and app 2.2.x updates *device*
firmware (issue #66) — the double publication above is that device firmware's
behaviour. Gesture logic must tolerate BOTH reporting styles: one pair per
tap (pre-2.2.0.x device firmware) and two (current).

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
  the REST poll (60 s by default, configurable in the options flow) remains
  as the backstop. The lower-level `devices` / `*-new` / `*-deleted` frames
  are not consumed.
