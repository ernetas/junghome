# Driving JUNG HOME devices directly over Bluetooth Mesh (gateway-free)

Reverse-engineered from the gateway's `middleware` ("bluetooth") component and
its `bt_tunnel` NCP host binary, cross-checked against on-air captures by the
Bluetooth-direct sibling project (`junghome-bt-mesh`: its own mesh stack
`jhmesh` + HA integration `junghome_ble`). This document describes **how the
gateway itself talks to the devices** — the model map, the vendor property
models and the gateway's own node role — so that a gateway-free client can do
the same. **A working gateway-free client exists in the sibling project** (a
Mesh Proxy *client* over a plain BLE adapter or an ESPHome Bluetooth proxy, no
mesh chip); the sketches in `tools/bt-mesh-direct/` predate it and are stale
(see the end of this document). See
[gateway-architecture.md](gateway-architecture.md) for how the gateway uses
this internally.

> Everything here is standard Bluetooth Mesh plus JUNG's vendor property models
> (company `0x0527`). To *operate* devices you need the NetKey, AppKey 0 and
> the current IV index (exportable from the JUNG HOME app; also in the
> gateway's CDB via `GET /project/cdb`). Device keys are only needed to
> *reconfigure* nodes.

## What you need

1. **Keys & state**, from the app's export (`MeshNetwork.json`) or the gateway
   CDB (`bt_mesh_project.json`, `GET /project/cdb`):
   - NetKey and AppKey (index 0 is the one every JUNG model is bound to).
   - **IV index** (`btmesh_iv_index`; 0 on the reference network). The IV
     index is the *only* counter that has to match the network. It is carried
     in every Secure Network Beacon (a proxy node sends one right after the
     GATT connection), so follow the beacons and take an IV Update from there.
   - Each node's `unicastAddress`, element layout, and the group addresses.
   - Per-node **device keys** (only needed to *reconfigure* nodes — bind app
     keys, set publish/subscribe — not to operate them).
2. **Your own unicast address**, chosen outside every provisioner's
   `allocatedUnicastRange` in the CDB (`provisioners[]`; the phone's is
   `0001–0CCC` on the reference network) and outside `networkExclusions`. No
   provisioning step is needed — nodes accept any source that holds the
   NetKey/AppKey (sibling project, on air).
3. **Your own sequence counter**, starting at 0 and persisted (with a restart
   margin). Replay protection is per *source address*, so you do **not**
   continue the gateway's `btmesh_sequence_number` — that counter belongs to
   the gateway's own address `00DC`; the only way to "continue" it would be to
   reuse that address and collide with the gateway. (An earlier revision of
   this document said to continue the gateway's number. That was wrong.)
4. **A radio** (see [Hardware](#hardware)) — a plain BLE adapter is enough.

## Hardware

**No mesh chip is required.** Every JUNG node runs the GATT Proxy feature, so a
plain Bluetooth LE adapter can act as a **Mesh Proxy client**: connect to any
node's Mesh Proxy service (`0x1828`; the Network ID in its service data
identifies *this* network's nodes), do the network/transport/access crypto on
the host and exchange network PDUs through the proxy characteristics (`2ADD`
Data In / `2ADE` Data Out). With the proxy filter set to an empty *blacklist*
the node forwards every network PDU it hears — the whole flat, because all JUNG
nodes are relays. This is what the sibling project implements (`jhmesh`, ~600
lines of Python over bleak; runs unchanged over an ESPHome Bluetooth proxy
because HA's bleak backend spans proxies). Round trip for a Set including its
status confirmation ≈ 1 s on air.

The options below are therefore **unnecessary for control** and kept for
reference only — a full mesh stack buys relay/friend features and
provisioning, none of which a controller needs:

| Option | Notes |
|--------|-------|
| **Silabs EFR32** (xG24/xG21 dev kit) as a BGAPI NCP | *Same silicon + same Bluetooth Mesh SDK (v4.4.6) as the JUNG gateway.* The middleware's BGAPI commands map 1:1 (this doc quotes them). The sketch in `tools/bt-mesh-direct/` targets this but is stale (v2.0.0 send path, no vendor models). |
| **Nordic nRF52840 dongle** (Zephyr / nRF Connect SDK) | Full mesh stack; you implement the mesh access opcodes (below). |
| **BlueZ mesh** on any Bluetooth 5 adapter | `bluetooth-meshd`; provisioner/CDB import is fiddly, and the proxy-client route above uses the same adapter with none of that. |
| **ESP32** (ESP-BLE-MESH) | Weaker proxy/relay/vendor-model support. |

The gateway's own radio is an EFR32 NCP reached over UART (`/dev/ttyAMA0`)
through the `bt_tunnel` **Silabs BGAPI** bridge; the host (`middleware`) drives
mesh *client* models on the NCP (see
[gateway-architecture.md](gateway-architecture.md)).

## Function → mesh model map

From `middleware/dist/const/cdb_types_datapoints.json`:

| Datapoint | SIG model (server / client) | Set kind |
|-----------|------------------------------|----------|
| `switch` | Generic OnOff `0x1000` / `0x1001` | `RequestOnOff` (0) |
| `brightness` | Light Lightness Actual `0x1300` / `0x1302` | `RequestLightnessActual` (128) |
| `color_temperature` | Light CTL Temperature `0x1306` / `0x1305` **when the device has one**, else Generic Level `0x1002` on element+1 — conditional, see below | Light CTL / `RequestLevel` (2) |
| `level`, `angle`, `temperature_ctrl` | Generic Level `0x1002` / `0x1003` | `RequestLevel` / `RequestLevelMove` |
| `quantity` (energy/sensor) | Sensor `0x1100` / `0x1102` | read via Sensor Client (the gateway also subscribes to the sensor publications) |
| `scene` | Scene `0x1203` / `0x1205` | Scene Recall |
| `status_led`, `up_request`, `down_request`, `parameter` (`trigger_request` is a descriptor promise no device model produces) | **JUNG vendor property models, company `0x0527`**: Admin `0x05271011`, Manufacturer `0x05271012`, User `0x05271013` (*servers*, on every device element) and the Property *Client* `0x05271015` | vendor (see below) |

So lights, dimmers, tunable-white, sockets, blinds, energy and scenes are all
**standard SIG models**. Only rocker buttons, the status LED, and device
parameters use the **vendor property models**. The gateway reads state two
ways at once: its self-configuration subscribes its client models to every
element group the devices publish to, *and* it polls every device state with a
Get every 15 s (`services/self_config_service.js:104-158` for the
subscriptions, `services/device_state_service.js:37` for the poll loop;
`const/config.json` `btmesh.device_state_poll_interval_sec`). A gateway-free client only needs the
subscription half — a proxy client with an empty blacklist filter hears every
publication in the flat.

## Sending commands

On current firmware (v2.1.3) the middleware sends each command **once, ACKed**
(`flags = 1`), serialised through a publish mutex with a **50 ms** pause
between datapoints (`request_pause_ms`), incrementing an 8-bit transaction id
(`tid`) once per logical command (`util/device_state_helper.js`,
`sendIntervalGenericClientSet`). Reliability lives one layer up: the command
handler retries a failed publish up to 3 times, 3 s apart
(`ip_event_handler.js`). The **previous** build (v2.0.0) instead blasted every
command 3× at 15 ms spacing (`publish_retransmissions` /
`publish_interval_ms` — those config keys no longer exist in v2.1.3).

> `tools/bt-mesh-direct/junghome_mesh.py:43-46,90-107` still implements that
> v2.0.0 blast (`RETRANSMISSIONS = 3`, `INTERVAL_MS = 15`, staggered `delay_ms`).
> It was never updated to the v2.1.3 single acked Set; treat it as a BGAPI
> reference, not as current behaviour.

> **Acknowledged Sets to JUNG devices get no unicast reply.** On air (capture
> by the Bluetooth-direct sibling project) a state-changing acked `Generic OnOff
> Set` is answered only by the server's *publication* of the new status to its
> element group, sent **twice ≈1 s apart with fresh sequence numbers** (device
> fw 2.2.0.x). The unicast Status to the sender appears only when nothing
> changed (a Get, or a Set retransmitted with the same TID). That is why the
> gateway's command confirmation — the WS `datapoint` reply — lags by about a
> second: it waits for the group publication its client models are subscribed
> to. A controller must accept a status *from* the target element, whatever
> its destination, as the acknowledgement.

### Generic / lighting (the common path)

The gateway calls the Silabs BGAPI `sl_btmesh_cmd_generic_client_set` with:

```
server_address   = node/group unicast (uint16)
elem_index       = 0
model_id         = client model (e.g. 0x1001 OnOff, 0x1302 Lightness, 0x1003 Level)
appkey_index     = 0
tid              = transaction id (uint8, increments per command)
transition_ms    = 0 (or 0xFFFE for "move")
delay_ms         = 0 (v2.0.0 staggered this per retransmission)
flags            = 1
type             = MeshModelSetKind (see table)
parameters       = little-endian value, length bytes
```

On any other mesh stack, this is the equivalent **standard mesh access message**
to the node/group address with the device's AppKey:

| Action | Value encoding | SIG access opcode* |
|--------|----------------|--------------------|
| OnOff | 1 byte `00`/`01` | Generic OnOff Set `0x8202` (unack `0x8203`) |
| Brightness | uint16, value scaled to model range (0…0xFFFF) | Light Lightness Set `0x824C` |
| Level | int16 | Generic Level Set `0x8206` |
| Color temperature | **see below** | Light CTL Temperature Set `0x8264` when the device has a CTL Temperature server; else Generic Level Set `0x8206` on element+1 |
| Blinds move | int16 `7FFF`=down, `8000`=up, `0`=stop, `transition=0xFFFE` | Generic Level Move Set `0x820B` |

*Standard Bluetooth Mesh Model opcodes — verify against the Mesh Model spec for
your stack. The BGAPI command above is what the gateway uses on its EFR32 NCP.

**Value scaling:** linear `convertRange(value, input_range, output_range)` then
to unsigned. OnOff is a single byte; level/lightness are little-endian uint16.

**Colour temperature — a conditional path, not a rule.** The middleware's
`ColorTemperatureState.publishValue`
(`models/device_states/ColorTemperatureState.js:95-122`) first clamps the
value to the state profile's range (2000–6000 K, `:60`), then, **if the device
has a `ColorTemperature2` state** (a Light CTL Temperature server, `0x1306`),
delegates the publish to it; **otherwise** it falls back to **Generic Level on
`address + 1`**, mapping the clamped Kelvin onto int16 `-0x8000…0x7FFF`. Range
facts: 2000–6000 K is a **middleware clamp** (the descriptor
`cdb_types_datapoints.json` declares 2000–10000); the JUNG app itself sends
`Light CTL Set` with 2000–10000 K (sibling project's app analysis). A device's
real range comes from `Light CTL Temperature Range Get` (`0x8262` → Status
`0x8263`) — read that instead of hard-coding either figure.

### Scenes

`sl_btmesh_cmd_scene_client_recall` → standard **Scene Recall `0x8242`**:

```
server_address = 0xFFFF (broadcast) or a group
elem_index     = 0
scene_number   = uint16
appkey_index   = 0
flags          = 1
tid, transition_ms = 0, delay_ms
```

### Vendor property models: status LED, buttons, parameters (company `0x0527`)

JUNG's "LBC properties" ride on three vendor **property server** models present
on every device element — Admin `0x05271011`, Manufacturer `0x05271012`, User
`0x05271013` — plus a Property **Client** `0x05271015`. The gateway hosts all
four on its own element as well (`services/self_config_service.js:93-96`, bound
to AppKey 0 with `test_bind_local_model_app`); its client is subscribed to every
element group so it hears the devices' publications. Two more vendor models
(`0x05271016` "JH Scheduler", `0x05271017` "Scene Action Setup" — names from
the sibling project's app analysis) sit on every element but are unknown to the
middleware.

The vendor models are implemented inside the `bt_tunnel` binary (Silabs
`sl_btmesh_vendor_model_*`), not in the middleware, which only sees them as
`lbc_cmd` user messages 0–6 through the host↔NCP passthrough
`sl_bt_cmd_user_message_to_target` (`services/btmesh_property_service.js:71-76`
for the gateway's own property replies; `sendKeyStatus` in
`util/device_state_helper.js` for the LED). Host-side framing:

```
commandId = 2      (STATUS_LBC_PROP_SEND_ID)
elementId = 0
dest      = node unicast address
propId    = property id
appKey    = 0
modelId   = vendor model (model_ctrl & 0xFFFF)
value     = the property value
```

**Opcodes** (from the `bt_tunnel` `.rodata`; each 6-bit vendor opcode `op` goes
on air as `(0xC0 | op) 27 05` — the three-byte vendor opcode with company id
`0x0527` little-endian):

| server | ListGet | ListStatus | Get | Set | SetUnack | Status |
|---|---|---|---|---|---|---|
| Admin `0x05271011` | `C0` | `C1` | `C2` | `C3` | `C4` | `C5` |
| Manufacturer `0x05271012` | `C6` | `C7` | `C8` | `C9` | `CA` | `CB` |
| User `0x05271013` | `CC` | `CD` | `CE` | `CF` | `D0` | `D1` |

The client `0x05271015` receives the six Status / ListStatus opcodes.
**Payload:** Get = `pid u16 LE`. Set / SetUnack = `pid u16 LE` + `[access u8]`
(Admin and Manufacturer servers only; a User Set carries none — the `0x5012`
event value follows the pid directly) + value bytes. Status = `pid u16 LE` +
`access u8` + value on all three servers (the User Status answering a KeyMode
Get carries access `3` on air — sibling capture and its
`jhmesh/messages.py` codec). Property ids and
value encodings are the middleware's catalogue
`models/btmesh_property_ids.js` (`0x50xx` keys: `0x5003 KEY_MODE`,
`0x5012 KEY_EVT`, `0x5013 KEY_STATUS`; `0xC000–0xC003` are the gateway's own
`api_status` / `api_token` / `ip_v4` / `fingerprint_sha256`,
`services/btmesh_property_service.js:38-45`). An earlier revision of this
document said the opcode bytes were unknown; they are now confirmed on air by
the sibling project.

**Button events.** A button element in KeyMode 6 ("gateway") publishes a
**User Property Set Unacknowledged** — `D0 27 05` — of property `0x5012` with
a 2-byte value `[counter u8][event u8]` to the gateway's element group `C005`
(on-air capture by the sibling project; the middleware's decoder is
`services/btmesh_property_service.js:184-228`). It is a *Set*, not a Status:
the *device* is the client here (its `0x05271015`) and the gateway's User
Property **Server** `0x05271013` is the target — an earlier revision called
this a "vendor property status" and `0x1013` the client model. Event codes:
`0`/`1` click on the down/up half of a rocker, `2`/`3` hold-start down/up,
`4` hold-end (release), `5` click and `6` hold-start on a single-key element;
the counter increments once per event per element. On device fw 2.2.0.x every
event is published **twice**, ≈1 s apart, with a fresh sequence number and the
same counter — dedupe on `(src, counter)`. (The gateway does not: it ignores
the counter byte, which is where its doubled press/release pairs come from —
see [gateway-websocket.md](gateway-websocket.md).) Whether elements in the
other key modes (which act on the mesh directly: OnOff / Level / Scene Recall
to their groups) also emit `0x5012` has not been captured. To receive the
events, a proxy client with an empty blacklist filter already hears `C005` —
it hears the whole flat. **Do not** re-route the devices' publications with
their device keys, as an earlier revision suggested: that would take the
events away from the gateway.

**Status LED.** `0x5013 KEY_STATUS` (1 byte, Manufacturer category, read-only
for users) is the button's status LED; the gateway writes it with a **User
Property Status** — vendor opcode `0x11`, on air `D1 27 05` — to the button
element (`sendKeyStatus`, `lbc_cmd 2`). It is not `0xA003` (an earlier guess in
the sibling project's property table).

**Key mode.** `0x5003 KEY_MODE` (`models/jung-home-state-mode.js:39-47`):
`0` light, `1` blinds, `2` scene, `3` property, `4` thermostat, `5` switch,
`6` gateway. It is readable through the User server as well (`CE 27 05` Get,
answered with access `3` and the mode byte — sibling capture; the app writes
it through the Admin server).

## Receiving state & events

The gateway listens for these NCP events (`handler/bt_event_handler.js`); the
equivalent on any stack is the matching mesh **Status** message:

| BGAPI event | Carries | Standard message |
|-------------|---------|------------------|
| `sl_btmesh_evt_generic_client_server_status` | `{server_address, model_id, parameters}` | Generic/Lighting *Status* (OnOff `0x8204`, Level `0x8208`, Lightness `0x824E`) |
| `sl_btmesh_evt_sensor_client_status` | `{server_address, sensor_data}` | Sensor Status `0x52` (energy/quantity) |
| `sl_btmesh_evt_scene_client_status` | `{current_scene, target_scene, server_address}` | Scene Status `0x5E` |
| vendor user-message events (`lbc_cmd` 0–6) | property id + value | vendor **Set Unack** `D0 27 05` (button events, prop `0x5012`) / vendor **Status** `C5`/`CB`/`D1 27 05` (property reads) |

Map `server_address` (+ element offset) back to a function/datapoint using the
CDB (`cdb_functions.json` / `bt_mesh_project.json`). On device fw 2.2.0.x every
publication arrives twice (≈1 s apart, fresh SEQ — sibling capture; CDB
publish-retransmit is 0 everywhere, so this is application behaviour, not
network retransmission). SIG statuses are idempotent, so the duplicate is
harmless; the vendor button events (`counter` byte) and the SIG client
messages keys send directly (OnOff Set, Scene Recall — they carry a TID) are
the ones to dedupe.

## Practical notes

- **Coverage / relaying:** all JUNG nodes relay, and one proxy connection sees
  the whole flat (PDUs arrive relayed, TTL 1–4). Pick the strongest proxy;
  with ESPHome proxies HA's bleak backend spans several.
- **IV index & sequence numbers:** match the network's IV index (follow the
  Secure Network Beacons the proxy sends on connect and whenever it changes);
  keep your **own** sequence counter for your own address, persisted across
  restarts with a margin — a lower number is dropped silently by every node.
  The gateway's own counter sits at ~`0xB0D6xx` (Sep 2026, sibling sniff) and
  it will request an IV Update within roughly 6–12 months; a client must
  follow that beacon rather than assume IV index 0 forever.
- **Don't double-drive:** running your stack *and* the gateway on the same mesh
  is fine (mesh is multi-master), but use distinct unicast addresses.
- The control surface is ~90 % standard models + the `0x0527` property models
  for buttons/LED/parameters (opcodes above, on-air verified).

The working gateway-free client is the sibling project's `jhmesh` (Mesh Proxy
client; plain BLE adapter or ESPHome Bluetooth proxy). The sketches in
[`tools/bt-mesh-direct/`](../tools/bt-mesh-direct/) target an EFR32/ESP32 mesh
NCP, still send the v2.0.0 blast and have no vendor-model path — they are
stale and kept only as a BGAPI reference.
