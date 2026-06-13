# JUNG HOME Gateway — internals

Notes reverse-engineered from a microSD disk image of a JUNG HOME Gateway
(firmware 1.5.0, API 1.5.0). The gateway is a Raspberry Pi Zero–based board
running Debian (bullseye) with JUNG's services in `/opt`. This document is for
contributors; it is not needed to use the integration.

## microSD partition layout

The card has four partitions (seen in the dump as `sdc1`–`sdc4`):

| Partition | Type | Role |
|-----------|------|------|
| **sdc1** | FAT (boot) | Raspberry Pi boot partition — kernel, `overlays/`, update staging. |
| **sdc2** | ext4 (rootfs A) | A full Debian root filesystem. |
| **sdc3** | ext4 (rootfs B) | A second full Debian root filesystem. |
| **sdc4** | ext4 (data) | Persistent per-service data, shared across rootfs updates. |

**sdc2 and sdc3 are an A/B (dual) root filesystem pair.** One is active while
the other receives an OTA update, then the bootloader switches over — this makes
firmware updates power-fail safe. The two are near-identical; the active one in
the dump was sdc3 (newer mtimes).

**sdc4 is the data partition.** It holds state that must survive a rootfs
update: API tokens, the BT-Mesh database, Matter commissioning data, logger
config, etc. Each service's `res/` directory under `/opt` is symlinked to its
counterpart on the data partition. (The `api-server/res/README.md` explicitly
warns that anything in a rootfs `res/` is overwritten on update and must be
linked to the data partition.)

> This A/B design is also *why device IDs can change across firmware updates* —
> see the stable-ID handling in the integration (`const.py`, `__init__.py`).

## Service components (`/opt`)

| Component | What it is |
|-----------|-----------|
| **api-server** | Node.js / Express app. Serves the REST API (`127.0.0.1:3000`) and the WebSocket server (`127.0.0.1:8080`). Handles token auth. Talks to the middleware over TCP `localhost:1024`. See [gateway-rest-api.md](gateway-rest-api.md) and [gateway-websocket.md](gateway-websocket.md). |
| **middleware** | Node.js app, internal name **"bluetooth"** — *"bluetooth mesh logic and communication to mesh network co-processor (ncp)"*. Runs the BT-Mesh host stack and talks to the radio NCP over UART `/dev/ttyAMA0`. Exposes `localhost:1024` to the api-server. |
| **wireless_module** | Firmware images (`.gbl`, Silicon Labs Gecko Bootloader format) for the radio co-processor. It is a **Silicon Labs EFR32** running the **Silabs Bluetooth Mesh SDK v4.4.6** as an NCP, flashed/updated over UART (`lbc_uart_update.json`). |
| **bt_tunnel** | `lbc-gw-bt-tunnel_pi-zero` binary. A BLE GATT tunnel exposed via the unix socket `/tmp/lbc-bt-tunnel.soc` — used for the mobile app's direct Bluetooth connection and provisioning. |
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
    middleware  ("bluetooth", BT-Mesh host stack)
        │  UART /dev/ttyAMA0
        ▼
   EFR32 NCP  (Silabs BT-Mesh SDK v4.4.6)
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

Devices observed use `cid 0x0527` / `pid 0x000B`. (The dump's own copy was a
factory/empty CDB; a live gateway's copy contains all provisioned nodes.)

> ⚠ The CDB contains the network's secret keys. Never commit a real
> `bt_mesh_project.json` (or the whole `disk_dump/`, which is `.gitignore`d).

## Could you self-host without the JUNG gateway?

**Yes, in principle — it's a real (but non-trivial) project.** JUNG devices are
standard Bluetooth Mesh nodes; the gateway is "just" a mesh provisioner/proxy
plus the HTTP/WS façade. To control them directly you need:

1. **The mesh keys** — NetKey + AppKey(s) + IV index. These live in the CDB on
   the gateway and can also be **exported from the JUNG HOME app**, so this is
   not a blocker.
2. **A BT-Mesh-capable radio + host stack** on your own hardware — e.g. a Silabs
   EFR32 or Nordic nRF52 acting as an NCP, or BlueZ-mesh on a Linux BT adapter
   (BlueZ mesh provisioner/CDB support is workable but rough). You'd add a *new*
   provisioner/node address rather than reuse the gateway's.
3. **Respect IV index & sequence numbers** to avoid tripping replay protection.
4. **The model/opcode mapping.** Core control is standard mesh models (Generic
   OnOff, Light Lightness, Light CTL); rocker/button *events* come from devices
   publishing to group addresses (subscribe to receive them). Scenes and any
   vendor models would need some reverse engineering.

What you'd give up: cloud/remote access, the gateway's central relay coverage,
and easy provisioning/firmware updates of new devices. For most users the
gateway-backed integration here is the practical path; a direct-mesh integration
is a worthwhile experiment if you want to remove the gateway entirely.
