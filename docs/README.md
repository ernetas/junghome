# JUNG HOME Gateway documentation

Reverse-engineered reference for the JUNG HOME Gateway, for contributors to this
integration. Sourced from a gateway microSD image (gateway firmware v2.1.3 build
2840, API 1.5.0) and the live local API. None of this is required to *use* the
integration.

- **[gateway-architecture.md](gateway-architecture.md)** — hardware, microSD
  partition layout (four partitions), the on-board services, the Bluetooth-Mesh
  stack, the gateway's own role on the mesh (an ordinary node `0x00DC`, not a
  provisioner), and self-hosting without the gateway.
- **[gateway-system-analysis.md](gateway-system-analysis.md)** — the current
  (v2.1.3, build 2840) root-partition image in detail: the multi-process
  Node.js stack under `board_ctrl`, its build projects and versions, and the
  services' configuration.
- **[gateway-rest-api.md](gateway-rest-api.md)** — REST API: auth, the
  unauthenticated `/apidoc` spec endpoint, **client registration** (token), and
  the full endpoint list.
- **[gateway-websocket.md](gateway-websocket.md)** — the WebSocket protocol: all
  message types (server→client and client→server) and command formats.
- **[bt-mesh-direct.md](bt-mesh-direct.md)** — **how the gateway talks to the
  devices on the mesh**: the function→model map, send/receive protocol, the
  JUNG vendor property models with their on-air opcodes, the gateway's own node
  role, and what a gateway-free client needs (keys, own address, own sequence
  counter). The **working gateway-free client** is the Bluetooth-direct sibling
  project `junghome-bt-mesh` (a Mesh Proxy client over a plain BLE adapter or
  an ESPHome Bluetooth proxy — no mesh chip); the sketches in
  [`../tools/bt-mesh-direct/`](../tools/bt-mesh-direct/) are stale and kept for
  reference only.
- **[cross-repo-analysis.md](cross-repo-analysis.md)** — 2026-09-15 audit
  against the Bluetooth-direct sibling project and the firmware dump: the
  established mechanism of the double-reporting rockers, other settled
  gateway facts, and what is still open (a few improvements and the
  captures that would close the remaining questions; the hardware
  verification of the gesture rebuild it asked for was done on 2026-09-16).
  Every bug and doc correction it raised has landed.
- **[upstream-report-button-double-reporting.md](upstream-report-button-double-reporting.md)** —
  draft report to JUNG on the doubled push-button events (device firmware
  2.2.0.x publishes twice, the gateway ignores the `0x5012` counter), with
  the suggested one-line gateway fix. Ready to send.
- **[matter-bridge.md](matter-bridge.md)** — getting JUNG devices into Matter
  (the gateway's built-in Matter is inactive; bridge from Home Assistant
  instead).
- **[example-button-automation.md](example-button-automation.md)** —
  user-facing guide to button automations: the `click` / `hold_start` /
  `hold_end` events, device triggers and the shipped blueprint.
- **[publishing.md](publishing.md)** — how releases are cut and how the
  integration is distributed (HACS default store).
- **[../tools/ws-capture/](../tools/ws-capture/README.md)** — read-only
  WebSocket capture + analysis tool: timestamps every frame, walks a scripted
  gesture session, and reports the per-gesture timings and burst shapes the
  rocker/cover evidence in these docs comes from.

Quick facts:

- Base URL: `https://<gateway>/api/junghome` (TLS, self-signed). `<gateway>` may
  be the IP or the announced mDNS name `junghome-<mac>.local` (`junghome.local`
  is only the certificate's CN and resolves only where local DNS serves it).
- Auth: `token` header (HS256 JWT). All endpoints need it except `version`,
  `register`, `register/by-password`, `apidoc`.
- Full live spec: `GET https://<gateway>/api/junghome/apidoc` (no auth).
- WebSocket: `wss://<gateway>/ws` (same token).
- Get a token: `POST /api/junghome/register` `{"user_name":"..."}` then approve
  in the app, or `POST /api/junghome/register/by-password` `{"password":"..."}`.

> The full disk image lives in `disk_dump/` (gitignored — it contains tokens and
> mesh keys; never commit it).

## Legal / interoperability note (not legal advice)

This folder documents **facts** about the gateway's interfaces (endpoints,
message formats, mesh model IDs, opcodes, constants) gathered for
**interoperability**, and the code under `tools/` is original. Facts and
interfaces are generally not protected by copyright, and reverse engineering for
interoperability is broadly permitted (e.g. the EU Software Directive Art. 6; a
US DMCA §1201(f) interoperability exemption). What you should **not** publish:

- JUNG's or Silicon Labs' **source code or firmware** (the Node.js apps, the
  `.gbl` radio images, the Silabs SDK) — proprietary; keep `disk_dump/` private.
- Your **keys/tokens** (API tokens, BT-Mesh NetKey/AppKey/device keys) — these
  are secrets, not redistributable material.

Other notes: "JUNG" / "JUNG HOME" are trademarks — use them only descriptively
(to say this project interoperates), not in a way implying endorsement. Patents
may exist but Bluetooth Mesh is an open SIG standard. This is an unofficial,
community project. For anything commercial or high-stakes, consult a lawyer.
