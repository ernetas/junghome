# JUNG HOME Gateway documentation

Reverse-engineered reference for the JUNG HOME Gateway, for contributors to this
integration. Sourced from a gateway microSD image (firmware/API 1.5.0) and the
live local API. None of this is required to *use* the integration.

- **[gateway-architecture.md](gateway-architecture.md)** — hardware, microSD
  partition layout (sdc1–sdc4), the on-board services, the Bluetooth-Mesh stack,
  and an analysis of self-hosting without the gateway.
- **[gateway-rest-api.md](gateway-rest-api.md)** — REST API: auth, the
  unauthenticated `/apidoc` spec endpoint, **client registration** (token), and
  the full endpoint list.
- **[gateway-websocket.md](gateway-websocket.md)** — the WebSocket protocol: all
  message types (server→client and client→server) and command formats.
- **[bt-mesh-direct.md](bt-mesh-direct.md)** — controlling devices over Bluetooth
  Mesh **without the gateway**: the function→model map, send/receive protocol,
  the JUNG vendor model, hardware options, and what keys you need. Prototypes in
  [`../tools/bt-mesh-direct/`](../tools/bt-mesh-direct/).
- **[matter-bridge.md](matter-bridge.md)** — getting JUNG devices into Matter
  (the gateway's built-in Matter is inactive; bridge from Home Assistant
  instead).

Quick facts:

- Base URL: `https://<gateway>/api/junghome` (TLS, self-signed). `<gateway>` may
  be the IP or `junghome.local`.
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
