# BT-Mesh direct — reference prototypes (stale)

> **Superseded — kept as a BGAPI reference only.** A working gateway-free
> client exists in the Bluetooth-direct sibling project `junghome-bt-mesh`
> (`jhmesh` + `custom_components/junghome_ble`): a Mesh Proxy client over a
> plain BLE adapter or an ESPHome Bluetooth proxy, **no mesh chip needed**.
> These sketches predate it and were never updated:
>
> - `junghome_mesh.py:43-46,90-107` still implements the gateway's **v2.0.0**
>   send path — every command blasted 3 × at 15 ms spacing with staggered
>   `delay_ms` (`RETRANSMISSIONS = 3`, `INTERVAL_MS = 15`). Current gateway
>   firmware (v2.1.3) sends one acked Set; those config keys no longer exist.
> - Colour temperature is hard-coded as Generic Level on element+1 with a
>   fixed 2000–6000 K (`:47-49`). The real path is conditional (CTL
>   Temperature when the device has one) and 2000–6000 is a middleware clamp.
> - There is **no vendor-model path** (buttons, status LED, parameters) — the
>   opcodes are now known (see the doc) but not implemented here.
> - The ESP32 sketch (`esp32/junghome_mesh_esp32.c:14-16`) assumes running as
>   a provisioner that imports the CDB and reconfigures nodes with their
>   device keys; neither is needed — a proxy client with its own unicast
>   address (outside the provisioners' ranges) and its own sequence counter
>   operates the devices as they are.
>
> Read [../../docs/bt-mesh-direct.md](../../docs/bt-mesh-direct.md) for the
> current protocol facts; it points to the sibling project for the client.

Proof-of-concept code for controlling JUNG HOME devices **without the gateway**,
by joining their Bluetooth Mesh network from your own radio.

Both are **reference sketches**: they need real hardware and an already-provisioned
node (NetKey/AppKey from the JUNG HOME app, AppKey bound to the client models).
They are not built or tested in CI.

## Option A — Silicon Labs EFR32 (`junghome_mesh.py`)

Same silicon + Mesh SDK as the JUNG gateway, so the commands map 1:1 to the
gateway's own code.

1. Flash an EFR32 dev kit (xG21/xG24) with the Silabs **"Bluetooth Mesh - NCP"**
   example (Simplicity Studio).
2. `pip install -r requirements.txt`
3. Run:
   ```sh
   python junghome_mesh.py --port /dev/ttyACM0 onoff 0x0007 on
   python junghome_mesh.py --port /dev/ttyACM0 brightness 0x0007 40
   python junghome_mesh.py --port /dev/ttyACM0 ct 0x0007 3000
   python junghome_mesh.py --port /dev/ttyACM0 scene 1
   python junghome_mesh.py --port /dev/ttyACM0 listen
   ```
   (`0x0007` = a node's unicast address from your CDB / app export.)

## Option B — ESP32 (`esp32/junghome_mesh_esp32.c`)

ESP-IDF / ESP-BLE-MESH. Cheapest hardware, but provisioning/key import and
vendor-model support take more work than on the EFR32.

1. Create an ESP-IDF project; in `menuconfig` enable Bluetooth, BLE Mesh, and the
   Generic / Lighting / Time-Scene **client** models.
2. Drop `junghome_mesh_esp32.c` into `main/`, wire up provisioning + the client
   model elements, and call `jung_set_onoff()` / `jung_set_brightness()` / etc.

## Option C — no extra chip (BlueZ mesh)

The HA host's own Bluetooth 5 adapter via `bluetooth-meshd` + the Python
`bluetooth-mesh` library. No code here yet; the same access messages from the
spec apply. Most fiddly of the three (provisioner/CDB handling).

## Scope

Standard SIG models (on/off, dimming, tunable white, blinds, sensors, scenes) are
implemented/shown, with the caveats in the banner above. The JUNG **vendor
property models** (`0x0527`) for rocker buttons, status LED and parameters are
left as a stub here; their over-the-air opcodes are no longer unknown — the
table is in the doc (Admin `C0–C5`, Manufacturer `C6–CB`, User `CC–D1`, each
followed by `27 05`), confirmed on air by the sibling project, which also has
the working implementation.

## Legal / interoperability note

This code is original and implements **standard Bluetooth Mesh** plus documented
facts about JUNG's models. It contains no JUNG or Silicon Labs source code and no
keys. See the IP note in [../../docs/README.md](../../docs/README.md) before
publishing. You must supply your own network keys (your data); never commit them.
