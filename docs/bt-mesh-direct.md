# Driving JUNG HOME devices directly over Bluetooth Mesh (gateway-free)

Reverse-engineered from the gateway's `middleware` ("bluetooth") component. This
is the spec for controlling JUNG devices without the gateway, by joining their
Bluetooth Mesh network yourself. See [gateway-architecture.md](gateway-architecture.md)
for how the gateway uses this internally.

> Everything here describes standard Bluetooth Mesh plus one JUNG vendor model
> (company `0x0527`). You need the network keys + device keys (exportable from
> the JUNG HOME app) to participate.

## What you need

1. **Keys & state**, exported from the app or read from the gateway CDB
   (`bt_mesh_project.json`):
   - NetKey(s) and AppKey(s) (with their key indexes).
   - **IV index** (`btmesh_iv_index`) and current **sequence number**
     (`btmesh_sequence_number`) — you must continue from these or replay
     protection will drop your messages.
   - Each node's `unicastAddress`, element layout, and the group addresses.
   - Per-node **device keys** (only needed to *reconfigure* nodes — bind app
     keys, set publish/subscribe — not to operate them).
2. **A Bluetooth-Mesh radio + host stack** (see [Hardware](#hardware)).
3. Add yourself as a **new node/provisioner** with its own unicast address;
   don't reuse the gateway's address.

## Hardware

An nRF chip is **not** mandatory. In rough order of fidelity to JUNG's own stack:

| Option | Notes |
|--------|-------|
| **Silabs EFR32** (xG24/xG21 dev kit) as a BGAPI NCP | *Same silicon + same Bluetooth Mesh SDK (v4.4.6) as the JUNG gateway.* The middleware's commands map 1:1 (this doc uses them). Best fidelity; the prototype in `tools/bt-mesh-direct/` targets this. |
| **Nordic nRF52840 dongle** (Zephyr / nRF Connect SDK) | Robust, ~$10, extremely well documented. Great choice; you implement the mesh access opcodes (below). |
| **BlueZ mesh** on any Bluetooth 5 adapter | *No extra chip* — runs on the HA host's own Bluetooth via `bluetooth-meshd`. Cheapest, but BlueZ mesh provisioner/CDB handling is fiddly. |
| **ESP32** (ESP-BLE-MESH) | Cheap, but weaker proxy/relay/vendor-model support. |

The gateway radio is an EFR32 reached over UART (`/dev/ttyAMA0`) using **Silabs
BGAPI**; the host (`middleware`) drives mesh *client* models on the NCP.

## Function → mesh model map

From `middleware/dist/const/cdb_types_datapoints.json`:

| Datapoint | SIG model (server / client) | Set kind |
|-----------|------------------------------|----------|
| `switch` | Generic OnOff `0x1000` / `0x1001` | `RequestOnOff` (0) |
| `brightness` | Light Lightness Actual `0x1300` / `0x1302` | `RequestLightnessActual` (128) |
| `color_temperature` | Light CTL Temperature `0x1306` / `0x1305` — **but JUNG sets it via Generic Level on element+1** (see below) | `RequestLevel` (2) |
| `level`, `angle`, `temperature_ctrl` | Generic Level `0x1002` / `0x1003` | `RequestLevel` / `RequestLevelMove` |
| `quantity` (energy/sensor) | Sensor `0x1100` / `0x1102` | read via Sensor Client |
| `scene` | Scene `0x1203` / `0x1205` | Scene Recall |
| `status_led`, `up_request`, `down_request`, `trigger_request`, `parameter` | **JUNG vendor "Property" model, company `0x0527`** (`0x05271015` / `0x05271013`, etc.) | vendor (see below) |

So lights, dimmers, tunable-white, sockets, blinds, energy and scenes are all
**standard SIG models**. Only rocker buttons, the status LED, and device
parameters use the **vendor model**.

## Sending commands

The middleware sends every command **3×** (`publish_retransmissions`) at **15 ms**
spacing (`publish_interval_ms`), with **50 ms** pause between datapoints
(`request_pause_ms`), incrementing an 8-bit transaction id (`tid`) once per
logical command.

### Generic / lighting (the common path)

The gateway calls the Silabs BGAPI `sl_btmesh_cmd_generic_client_set` with:

```
server_address   = node/group unicast (uint16)
elem_index       = 0
model_id         = client model (e.g. 0x1001 OnOff, 0x1302 Lightness, 0x1003 Level)
appkey_index     = 0
tid              = transaction id (uint8, increments per command)
transition_ms    = 0 (or 0xFFFE for "move")
delay_ms         = staggered per retransmission
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
| Color temperature | **see below** | Generic Level Set `0x8206` on the CTL-temperature element |
| Blinds move | int16 `7FFF`=down, `8000`=up, `0`=stop, `transition=0xFFFE` | Generic Level Move Set `0x820B` |

*Standard Bluetooth Mesh Model opcodes — verify against the Mesh Model spec for
your stack. The BGAPI command above is what the gateway uses on its EFR32 NCP.

**Value scaling:** linear `convertRange(value, input_range, output_range)` then
to unsigned. OnOff is a single byte; level/lightness are little-endian uint16.

**Color temperature quirk:** JUNG does *not* use Light CTL Temperature Set.
Instead it targets **Generic Level on `address + 1`** (the CTL temperature
element), mapping Kelvin `2000…6000` onto int16 `-0x8000…0x7FFF`.

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

### Vendor model: status LED, buttons, parameters (company `0x0527`)

These use a JUNG vendor model. The gateway reaches it through the Silabs
host↔NCP passthrough `sl_bt_cmd_user_message_to_target` with a custom payload:

```
commandId = 2      (STATUS_LBC_PROP_SEND_ID)
elementId = 0
dest      = node unicast address
propId    = model_kind / property id
appKey    = 0
modelId   = vendor client model (model_ctrl & 0xFFFF, e.g. 0x1013/0x1015)
value     = the property value
```

Over the air this is a **vendor access message** (3-byte opcode = `0b11xxxxxx`
| company `0x0527`) carrying the property id + value. On a non-Silabs stack you
send/parse the vendor opcodes directly; the exact opcode bytes live in the EFR32
NCP firmware, but the host-side framing above (from
`btmesh_set_datapoint_service.js`) tells you the command/property/value fields.

Button **events** (`up_request`/`down_request`/`trigger_request`) are the device
*publishing* vendor property status to a group; subscribe to that group to
receive them. With the device keys you can (re)bind your AppKey and set the
publish target, so you can route button presses to an address you listen on.

## Receiving state & events

The gateway listens for these NCP events (`handler/bt_event_handler.js`); the
equivalent on any stack is the matching mesh **Status** message:

| BGAPI event | Carries | Standard message |
|-------------|---------|------------------|
| `sl_btmesh_evt_generic_client_server_status` | `{server_address, model_id, parameters}` | Generic/Lighting *Status* (OnOff `0x8204`, Level `0x8208`, Lightness `0x824E`) |
| `sl_btmesh_evt_sensor_client_status` | `{server_address, sensor_data}` | Sensor Status `0x52` (energy/quantity) |
| `sl_btmesh_evt_scene_client_status` | `{current_scene, target_scene, server_address}` | Scene Status `0x5E` |
| vendor user-message events | property id + value | vendor status (buttons / LED / parameters) |

Map `server_address` (+ element offset) back to a function/datapoint using the
CDB (`cdb_functions.json` / `bt_mesh_project.json`).

## Practical notes

- **Coverage / relaying:** the gateway is centrally placed and relays. A single
  USB dongle may not reach every node; enable relay/proxy or add a second node.
- **IV index & sequence numbers** must be respected (export and continue from
  the gateway's values), or replay protection will silently drop your traffic.
- **Don't double-drive:** running your stack *and* the gateway on the same mesh
  is fine (mesh is multi-master), but use distinct unicast addresses.
- The control surface is ~90% standard models (trivial) + the `0x0527` vendor
  model for buttons/LED (bounded RE; the host-side framing is documented above).

A reference prototype targeting an EFR32 NCP lives in
[`tools/bt-mesh-direct/`](../tools/bt-mesh-direct/).
