# JUNG HOME Gateway — internals

Notes reverse-engineered from a microSD disk image of a JUNG HOME Gateway.
The image carries **two firmware generations** (A/B rootfs pair, see below):
the current build is **firmware v2.1.3, API 1.5.0, Raspberry Pi OS 13 Trixie**
(`sdc2` in the dump) and the previous one is **API 1.1.0 on Debian 11
bullseye** (`sdc3`). "1.5.0" is the **API/package** version, not the firmware
version — [gateway-system-analysis.md](gateway-system-analysis.md) documents
the current build in detail. The gateway is a Raspberry Pi Zero–based board
with JUNG's services in `/opt`. This document is for contributors; it is not
needed to use the integration.

## microSD partition layout

The card has four partitions (seen in the dump as `sdc1`–`sdc4`). There are
two dumps of the same card: `disk_dump/jung/` (2026-06-13, `sdc*`) and
`disk_dump/jung-20260801/` (`sdb*`) — the rootfs partitions are
**byte-identical builds** in both, but the 2026-08-01 extraction is the
higher-fidelity one (ext4, `rsync -aHAX`; the June one went through a
case-insensitive filesystem and dropped device nodes and some symlinks — see
its `NOTES.md`), so prefer `jung-20260801/sdb2` when quoting evidence:

| Partition | Type | Role |
|-----------|------|------|
| **sdc1** | FAT (boot) | Raspberry Pi boot partition — kernel, `overlays/`, update staging. |
| **sdc2** | ext4 (rootfs A) | A full Debian root filesystem. |
| **sdc3** | ext4 (rootfs B) | A second full Debian root filesystem. |
| **sdc4** | ext4 (data) | Persistent per-service data, shared across rootfs updates. |

**sdc2 and sdc3 are an A/B (dual) root filesystem pair.** One is active while
the other receives an OTA update, then the bootloader switches over — this
makes firmware updates power-fail safe. They are **not** near-identical: they
hold different firmware generations. In this dump the **active/current one is
sdc2** (Trixie, api-server 1.5.0, middleware restructured into
`models/device_states/*State.js`) and sdc3 is the previous build (bullseye,
api-server 1.1.0). An earlier revision of this doc claimed sdc3 was active
from mtimes alone — `etc/os-release` and the package versions say otherwise.
When quoting firmware evidence, cite **sdc2** paths.

**sdc4 is the data partition.** It holds state that must survive a rootfs
update: API tokens, the BT-Mesh database, Matter commissioning data, logger
config, etc. Each service's `res/` directory under `/opt` is symlinked to its
counterpart on the data partition. (The `api-server/res/README.md` explicitly
warns that anything in a rootfs `res/` is overwritten on update and must be
linked to the data partition.)

> Device ids changing across (app-driven) firmware updates is **not** a
> consequence of this A/B design, as an earlier revision of this doc claimed:
> per the 2026-09-15 audit a function id is `"id"` + `md5(node UUID +
> hex(location))[:15]` and a scene id is `"id"` + hex(scene number), so an id
> moves when a node is re-provisioned or its location/element mapping is
> re-enumerated, a label is moved to another element, or the hardware is
> swapped. Across the one measured device-firmware update (app 2.1.0 → 2.2.0)
> no surviving node's id changed; every id that moved belonged to a label moved
> or hardware swapped in the app in that window. The integration's stable-ID
> handling (`const.py`, `__init__.py`) keys on the label regardless.

## Service components (`/opt`)

| Component | What it is |
|-----------|-----------|
| **api-server** | Node.js / Express app. Serves the REST API (`127.0.0.1:3000`) and the WebSocket server (`127.0.0.1:8080`). Handles token auth. Talks to the middleware over TCP `localhost:1024`. See [gateway-rest-api.md](gateway-rest-api.md) and [gateway-websocket.md](gateway-websocket.md). |
| **middleware** | Node.js app (compiled TypeScript, no npm dependencies), internal name **"bluetooth"** — *"bluetooth mesh logic and communication to mesh network co-processor (ncp)"*. Drives the mesh *client* models on the NCP through `bt_tunnel`'s unix socket (`const/config.json` `bt_adapter.socket_path`, line-delimited JSON BGAPI commands/events; BGAPI ids in `const/bt_api_ids.js`). Exposes `localhost:1024` to the api-server. |
| **wireless_module** | Firmware images (`.gbl`, Silicon Labs Gecko Bootloader format) for the radio co-processor. It is a **Silicon Labs EFR32** running the **Silabs Bluetooth Mesh SDK v4.4.6** as an NCP, flashed/updated over UART (`lbc_uart_update.json`). |
| **bt_tunnel** | `lbc-gw-bt-tunnel_pi-zero` binary (ARM ELF with symbols, built against Mesh SDK 4.4.6). The **UART ↔ EFR32-NCP BGAPI bridge**: it owns `/dev/ttyAMA0` and exposes the BGAPI host side to the middleware on the unix socket `/tmp/lbc-bt-tunnel.soc` (`config.json` `bt_adapter`). JUNG's vendor property models (`0x0527xxxx`, `sl_btmesh_vendor_model_*`) are implemented in here; the middleware only sees them as `lbc_cmd` user messages. It is **not** an app-facing BLE/GATT tunnel — an earlier revision of this doc said so; [gateway-system-analysis.md](gateway-system-analysis.md) had it right. The app provisions the gateway like any other node (PB-ADV + PB-GATT unprovisioned beaconing for 20 min, `services/ncp_service.js:427-437`) and afterwards reaches it **over the mesh** (vendor props `0xC000–0xC003`, see [gateway-rest-api.md](gateway-rest-api.md)) through whichever proxy node it is connected to — the NCP's own GATT proxy included; none of that goes through this socket. |
| **jungremote-client** | Cloud link (socket.io) to the JUNG OpenAPI portal for remote (off-LAN) access. |
| **matter-interface** | Matter bridge. Provisioned (`sdc4/matter-interface/matter_setup_data.json`): `vendor_id 5161` (0x1429), `product_id 11`, `discriminator 1538`, SPAKE2+ salt/verifier, `commissioning_flow 0`, `discovery_capability 4`. Lets the gateway expose JUNG devices to Matter controllers (incl. Home Assistant's own Matter integration). It is a separate interface (UDP 5540 + mDNS); **not** part of the REST API. The daemon binary was not present in the dump, so default-enabled status couldn't be confirmed from disk alone. |
| **board_ctrl / system_information / tools** | Board control, diagnostics, and shell helpers (`gpio_init.sh`, `led.sh`, `firewall.sh`, …). LEDs: BT on `gpio17`, Cloud on `gpio27`, LAN on `led0`. |

Services run as user `service` and are launched by `middleware/start.sh` (which
also fixes ownership and starts logging to `/var/log/*.log`).

## End-to-end data path

```
Home Assistant / mobile app
        │  HTTPS / WSS  (TLS, port 443)
        ▼
      nginx  (reverse proxy, DNS-rebind protection)
        │  /ws → :8080            / → :3000
        ▼
    api-server  (REST + WebSocket, token auth)
        │  TCP localhost:1024
        ▼
    middleware  ("bluetooth", mesh client models / state logic)
        │  unix socket /tmp/lbc-bt-tunnel.soc  (line-delimited JSON BGAPI)
        ▼
    bt_tunnel  (BGAPI host bridge, vendor property models)
        │  UART /dev/ttyAMA0
        ▼
   EFR32 NCP  (Silabs BT-Mesh SDK v4.4.6 — the mesh stack itself)
        │  Bluetooth Mesh radio
        ▼
   JUNG HOME devices (mesh nodes)
```

## Bluetooth Mesh database (CDB)

The middleware persists the mesh state on the data partition under
`middleware/res/`:

- `bt_mesh_project.json` — the **Bluetooth SIG Mesh Configuration Database
  (CDB)**, the same JSON schema used by nRF Mesh / Silabs tooling. Top-level:
  `netKeys[]`, `appKeys[]` (with `boundNetKey`/`index`), `nodes[]`
  (`unicastAddress`, `UUID`, `cid`, `pid`, `elements[].models[].modelId`,
  `features` relay/proxy), `groups[]` (`address`, `parentAddress`),
  `scenes[]`, `provisioners[]`.
- `cdb_functions.json`, `cdb_groups.json`, `cdb_scenes.json` — JUNG's mapping
  from API functions/groups/scenes onto mesh addresses and models.
- `btmesh_iv_index`, `btmesh_iv_index_birthday`, `btmesh_sequence_number` — IV
  index and sequence-number state (mesh replay protection).

The live CDB on the data partition (`middleware/res_6/` — the schema-v6
directory the middleware actually uses) contains the full provisioned mesh:
~30 nodes with JUNG's `cid 0x0527` and per-product `pid`s (`0x0001`–`0x0004`,
`0x000B`), plus the provisioning iPhone at `cid 0x004C` on unicast `0001`.
Only the leftover *unnumbered* `middleware/res/` (untouched factory state
from 2023) is near-empty — an earlier revision of this doc mistook that copy
for "the" CDB and called the gateway reset/empty.

> ⚠ The CDB contains the network's secret keys. Never commit a real
> `bt_mesh_project.json` (or the whole `disk_dump/`, which is `.gitignore`d).
> `GET /project/cdb` hands the same file, keys included, to any API client.

## The gateway's own role on the mesh

The gateway is an **ordinary provisioned node** — unicast `0x00DC`, `cid 0x0527`,
`pid 0x0B`, one element — not a provisioner and not a Config Client. The phone
provisions it (unprovisioned beaconing on PB-ADV + PB-GATT for 20 minutes,
`services/ncp_service.js:427-437`) and later uploads the project file
(`POST /config` `project_file`, see [gateway-rest-api.md](gateway-rest-api.md));
that upload is why the gateway holds the network keys at all. From that file the
middleware installs its *own* node data with **local-only** BGAPI `test_*`
calls (`ncp_service.js:299-351`): `node_set_provisioning_data` (device key,
NetKey, unicast), `test_set_iv_index`, `test_set_element_seqnum`,
`test_add_local_key` (AppKey 0), `test_set_gatt_proxy`, `test_set_relay`. All
configuration of *other* nodes — key binding, publish/subscribe — is done by
the app. Its element group is `C005`; button elements in KeyMode 6 publish
their events there.

- **Features:** relay on (retransmit count 0, `config.json`
  `relay_transmissions_*`), GATT proxy on (`ncp_service.js:333-343`; both
  default to 1 when the CDB node entry says nothing), no Friend/LPN, no
  heartbeat. TTL is never set by the middleware (project default 5; the
  sibling project sees its Sets on air with TTL 2).
- **State acquisition, both ways at once:** self-configuration binds AppKey 0
  to the gateway's client models (`self_config_service.js:68-96` — SIG clients
  plus the vendor property servers `0x05271011/12/13` and client `0x05271015`)
  and subscribes them to **every element group the devices publish to**
  (`:104-158,279-346`; the June log shows 221 desired / 202 current
  subscriptions), **and** it re-reads stale states with Gets: a sweep every
  120 s (`device_state_service.js:41-46`) over only the states that are
  dirty — untouched by any report or request for `dirtyAfterSeconds ×
  2^retries`, at most 3600 s (`models/device-states.js:364-388`; 300 s for
  most states) — one Get every 15 s (`config.json`
  `btmesh.device_state_poll_interval_sec` is the pause between Gets, not a
  per-state period; details in [bt-mesh-direct.md](bt-mesh-direct.md)).
  Because acked Sets
  to JUNG devices are answered only by the group publication (see
  [bt-mesh-direct.md](bt-mesh-direct.md)), the subscription is also what
  confirms commands.
- **Time:** it hosts a Time Server (`0x1200`) but `config.json`
  `btmesh.publish_time_interval_minutes` is `0` — the gateway **never
  publishes Time**; only the phone sets device clocks.
- **Sequence / IV:** the sequence number is persisted hourly, rounded up to the
  next 0x4000 (`services/seq_number_service.js`), and was at `0x9FC000` in
  June 2026, `0xA68000` in August, ~`0xB0D6xx` in September (sibling sniff) —
  9–18 k messages/day. The middleware requests an IV Update when fewer than
  128 reboots' worth (128 × 0x4000) remain, i.e. in roughly 6–12 months, with
  warnings a few months earlier. IV index is persisted in
  `middleware/res_6/btmesh_iv_index` (0 so far).

## Could you self-host without the JUNG gateway?

**Yes — and it has been done.** JUNG devices are standard Bluetooth Mesh nodes
plus JUNG's vendor property models; the gateway is an ordinary node with an
HTTP/WS façade (above). The Bluetooth-direct sibling project
(`junghome-bt-mesh`) controls the devices from Home Assistant with **no mesh
chip**: it acts as a **Mesh Proxy client** over a plain BLE adapter or an
ESPHome Bluetooth proxy, connecting to any node's GATT proxy service. What it
needs:

1. **The mesh keys** — NetKey + AppKey 0 + the IV index. These live in the CDB
   on the gateway (`GET /project/cdb`) and can also be **exported from the JUNG
   HOME app**, so this is not a blocker.
2. **Its own unicast address** outside every provisioner's
   `allocatedUnicastRange` and the CDB's `networkExclusions`, and **its own
   sequence counter** — replay protection is per source address, so nothing
   of the gateway's needs continuing. Only the IV index must match, and it
   arrives in every Secure Network Beacon. No mesh NCP (EFR32 / nRF52 /
   BlueZ-mesh) is required; a full stack only adds relay/friend/provisioning,
   which a controller does not need.
3. **The model/opcode mapping** — now fully reverse-engineered in
   [bt-mesh-direct.md](bt-mesh-direct.md): core control is standard models
   (Generic OnOff, Light Lightness, Light CTL / Generic Level, Scene Recall);
   rocker/button *events* are vendor `User Property Set Unack` publications of
   property `0x5012` to the gateway's group (a proxy client with an empty
   blacklist filter hears them); the status LED is property `0x5013`; the
   vendor opcode table is confirmed on air.

What you'd give up: cloud/remote access and the app-driven provisioning /
firmware-update path (the app still needs the gateway for those). For most
users the gateway-backed integration here is the practical path; the sibling
project is the working alternative, and the long-term plan is one integration
with both transports (HA ≥ 2026.8 binds a device to a single config entry, so
the two can never share a device page otherwise — tracker §3).
