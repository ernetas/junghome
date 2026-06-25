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
  - `light.py`, `switch.py`, `sensor.py`, `event.py`, `cover.py`, `climate.py`,
    `scene.py` — platforms (each does live discovery of devices added at
    runtime). `event.py` exposes RockerSwitch buttons; the gateway only reports
    raw `pressed` / `depressed` edges (no native single/double/hold) and
    alternates a button between its `up_request` and `down_request` events on
    consecutive presses. Function-type → platform map:
    `OnOff`/`DimmerLight`/`ColorLight` → light (capabilities follow the
    datapoints present, not the type name); `Socket` → switch + sensor;
    `Measurement` → sensor; `Position`/`PositionAndAngle` → cover;
    `Thermostat` → climate; `RockerSwitch` → event + switch (status LED).
    Scenes come from the WebSocket `scenes` broadcast and recall over REST
    (`POST /scenes/{id}`; the WebSocket `scene` command is unimplemented).
    **Cover position convention is confirmed against gateway firmware** — a
    *close* maps to BT-Mesh "down" (`0x7FFF`, drives `level`→100%) and an *open*
    to "up" (`0x8000`, →0%), so `level` is percent-*closed* (HA position =
    `100 - level`), correct for roller shutters/blinds. **Awnings are mounted
    the opposite way** and read inverted; users flag them in the options flow
    (`CONF_INVERTED_COVERS`), which makes that cover use an identity mapping. The
    single inversion point is `_to_ha`/`_to_device` in `cover.py` (both take an
    `inverted` flag). Changing the inverted set reloads the entry (see
    `async_reload_entry`, gated on an options snapshot in the coordinator).
- `blueprints/automation/junghome/button_gestures.yaml` — shipped HA blueprint
  deriving single/double/hold from those raw edges. Users import it by URL; it is
  **not** distributed by HACS (HACS only installs `custom_components/`).
- `docs/` — **reverse-engineered gateway reference** plus
  `docs/example-button-automation.md` (user-facing button-automation guide).
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
- **Entity naming.** Entities set `_attr_has_entity_name = True` and a short
  `_attr_name` (or `None` for a device's main feature, e.g. light/socket). The
  **device** carries the label; never bake the label into the entity name — doing
  so makes Home Assistant compose the label twice (the old
  `event.<label>_<label>_..._event` bug). Naming changes only affect new entities;
  existing `entity_id`s are sticky.
- **Registration.** Tokens are obtained via `POST /api/junghome/register`
  (`{"user_name": ...}`), which blocks up to 180 s until the user approves the
  request in the JUNG HOME app (Settings → Gateway → Access Permissions → Open
  Requests).

## Conventions

- Match Home Assistant integration patterns; keep `strings.json` and
  `translations/en.json` in sync (no `<...>` in text — it breaks the translation
  parser).
- Reuse the shared aiohttp session via `async_get_clientsession(hass,
  verify_ssl=False)` (the gateway's cert is self-signed); don't create
  per-request `ClientSession`s or build SSL contexts on the event loop.
- Validate with hassfest + HACS (see `.github/workflows/validate.yml`).
- Tests for the pure helpers live in `tests/` (`pytest`, needs the pinned
  `homeassistant`; uses Python 3.14 like the other workflows). Run `pytest`;
  it's wired into `.github/workflows/test.yml`.
