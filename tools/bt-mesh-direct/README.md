# BT-Mesh direct — reference prototypes

Proof-of-concept code for controlling JUNG HOME devices **without the gateway**,
by joining their Bluetooth Mesh network from your own radio. Read
[../../docs/bt-mesh-direct.md](../../docs/bt-mesh-direct.md) first — it's the
protocol spec these prototypes implement.

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
implemented/shown. The JUNG **vendor model** (`0x0527`) for rocker buttons, status
LED and parameters is documented in the spec but left as a stub — it's the one
part that needs further reverse engineering of the over-the-air opcodes.

## Legal / interoperability note

This code is original and implements **standard Bluetooth Mesh** plus documented
facts about JUNG's models. It contains no JUNG or Silicon Labs source code and no
keys. See the IP note in [../../docs/README.md](../../docs/README.md) before
publishing. You must supply your own network keys (your data); never commit them.
