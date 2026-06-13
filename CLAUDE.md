# Repository guide

Home Assistant custom integration for **JUNG HOME** (HACS). It talks to a local
JUNG HOME Gateway over its REST API and WebSocket.

## Layout

- `custom_components/junghome/` — the integration.
  - `__init__.py` — setup/unload; one-time registry migration to stable IDs.
  - `coordinator.py` — REST fetch + WebSocket connection and commands.
  - `config_flow.py` — setup flow; requests a token via the gateway's
    app-approval registration.
  - `const.py` — `DOMAIN` and the stable-ID helpers (`device_slug`,
    `datapoint_suffix`, `stable_unique_id`).
  - `light.py`, `switch.py`, `sensor.py`, `event.py` — platforms (each does live
    discovery of devices added at runtime).
- `docs/` — **reverse-engineered gateway reference** (see below).
- `config/`, `docker-compose.yml`, `scripts/` — local test harness.
- `disk_dump/` — gateway microSD image, **gitignored** (contains tokens + mesh
  keys; never commit it).

## Gateway reference — read `docs/` first

When working on anything that touches the gateway protocol, consult
[docs/README.md](docs/README.md) instead of re-deriving:

- [docs/gateway-rest-api.md](docs/gateway-rest-api.md) — endpoints, auth, the
  unauthenticated `GET /api/junghome/apidoc` spec, and client registration.
- [docs/gateway-websocket.md](docs/gateway-websocket.md) — all WebSocket message
  types and command formats.
- [docs/gateway-architecture.md](docs/gateway-architecture.md) — partitions,
  services, the BT-Mesh stack, self-hosting analysis.
- [docs/bt-mesh-direct.md](docs/bt-mesh-direct.md) — gateway-free BT-Mesh control
  (function→model map, vendor model, hardware); prototypes in
  `tools/bt-mesh-direct/`.
- [docs/matter-bridge.md](docs/matter-bridge.md) — Matter options (gateway's own
  is inactive; bridge from HA).

## Key behaviours to preserve

- **Stable identity.** The gateway regenerates device/datapoint `id`s on firmware
  updates, so entity `unique_id`s and device identifiers are derived from the
  device **label** + datapoint **suffix** (`stable_unique_id`), never the raw id.
  Don't reintroduce id-based identifiers.
- **Registration.** Tokens are obtained via `POST /api/junghome/register`
  (`{"user_name": ...}`), which blocks up to 180 s until the user approves the
  request in the JUNG HOME app (Settings → Gateway → Access Permissions → Open
  Requests).

## Conventions

- Match Home Assistant integration patterns; keep `strings.json` and
  `translations/en.json` in sync (no `<...>` in text — it breaks the translation
  parser).
- Validate with hassfest + HACS (see `.github/workflows/validate.yml`).
