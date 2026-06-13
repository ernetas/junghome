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
