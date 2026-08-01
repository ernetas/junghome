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
  - `light.py`, `switch.py`, `sensor.py`, `binary_sensor.py`, `event.py`,
    `cover.py`, `climate.py`, `scene.py` — platforms (each does live discovery of
    devices added at runtime). `event.py` exposes RockerSwitch buttons; the
    gateway only reports raw `pressed` / `depressed` edges (no native
    single/double/hold) and alternates a button between its `up_request` and
    `down_request` events on consecutive presses. Function-type → platform map:
    `OnOff`/`DimmerLight`/`ColorLight` → light (capabilities follow the
    datapoints present, not the type name); `Socket` → switch + sensor;
    `Measurement` → sensor + binary_sensor; `Position`/`PositionAndAngle` →
    cover; `Thermostat` → climate; `RockerSwitch` → event + switch (status LED).
    Datapoint mapping is not purely by function/datapoint *type*: a `quantity`
    datapoint whose label denotes presence/occupancy (empty unit, 0/1 value, e.g.
    a "BWM" detector's `Presence Detected`) becomes an **occupancy
    binary_sensor**, while other `quantity` datapoints become numeric sensors.
    `is_presence_quantity` (in `const.py`) is the single split point: the
    binary_sensor platform claims those labels and `sensor.py` skips them.
    Likewise a **`Thermostat`'s `switch` datapoint is not an on/off**: the gateway
    re-labels the RTR's `automatic_mode` state as type `switch`, and in the field
    it tracks the regulator's momentary heating output — it flips on its own
    several times an hour. A room regulator has no on/off at all, so the climate
    entity is permanently `HVACMode.HEAT` (`hvac_modes = [HEAT]`) and that
    datapoint only feeds `hvac_action` (heating/idle); never map it to
    `hvac_mode` again (issue #121, evidence in
    [docs/gateway-websocket.md](docs/gateway-websocket.md)).
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
- **Capabilities follow datapoints, and can change at runtime.** Each platform
  freezes an entity's supported features at construction from the datapoints
  present (cover tilt ← `angle`, light brightness/colour ← `brightness`/
  `color_temperature`, climate off ← `switch`), and `_discover_*` is add-only, so
  a live entity never re-derives them. The gateway *can* add or drop a datapoint
  for an existing device (a firmware update re-enumerates and datapoints arrive
  over several polls; the slat channel is toggled in the app — see the tilt note
  in [docs/gateway-websocket.md](docs/gateway-websocket.md)). `__init__.py`'s
  `_register_capability_reload` reloads the entry when a device's datapoint-type
  set changes so features are rebuilt — the reason the tilt-lost-after-update
  regression can't recur. Keep capabilities gated on datapoint *presence*, not
  the function-type name.

## Conventions

- Match Home Assistant integration patterns; keep `strings.json` and
  `translations/en.json` in sync (no `<...>` in text — it breaks the translation
  parser). `translations/` carries 26 locales; a new or changed string means
  updating the other 25 files too, or they fall back to English key-by-key.
- Reuse the shared aiohttp session via `async_get_clientsession(hass,
  verify_ssl=False)` (the gateway's cert is self-signed); don't create
  per-request `ClientSession`s or build SSL contexts on the event loop.
- Validate with hassfest + HACS (see `.github/workflows/validate.yml`).
- `tests/` covers more than pure helpers: `test_const.py` (stable-ID helpers),
  `test_config_flow.py`, `test_coordinator.py`, `test_websocket.py`,
  `test_init.py` (setup/entity-lifecycle), `test_logbook.py`, and one file per
  platform (`test_light.py`, `test_switch.py`, `test_sensor.py`,
  `test_binary_sensor.py`, `test_cover.py`, `test_climate.py`, `test_event.py`,
  `test_scene.py`). New platform behaviour goes in that platform's file. Uses
  `pytest_homeassistant_custom_component`'s `hass` fixture, `MockConfigEntry`,
  `aioclient_mock`. Needs the pinned `homeassistant`; runs on Python 3.14 like
  the other workflows. Run `pytest`; wired into `.github/workflows/test.yml`.
  Snapshot tests are still absent — see the Shelly-comparison notes below.

## Ideas from comparing against Shelly (core reference integration)

Findings from reading `homeassistant/components/shelly` (HA core's most
mature local-push integration) against this integration, filtered to what's
genuinely applicable to a single local-gateway integration (Shelly's
multi-generation/cloud complexity mostly doesn't apply).

**Most of the original list has since been implemented.** What follows is split
into what is already done (don't re-do it) and what is still open. The open
items are a backlog, not commitments — evaluate cost/value per item before
implementing.

### Already done — do not re-open

- **`EVENT_HOMEASSISTANT_STOP` listener.** A full HA shutdown now runs the
  orderly `ws.close()`/task-cancel, not just entry unload
  (`__init__.py:238`, `hass.bus.async_listen_once`).
- **Reconnect-backoff jitter.** `RECONNECT_JITTER` (`coordinator.py:32`) is
  added to each reconnect wait at `coordinator.py:342`, so gateways sharing a
  network blip don't reconnect in lockstep.
- **Failure context in diagnostics.** `diagnostics.py:92-94` reports
  `ws_last_connected`, `last_error` and `last_error_at`, so a downloaded dump
  shows how long it has been degraded and why, without cross-referencing logs.
- **`logbook.py` exists.** `junghome_scene_recalled` renders as a readable
  logbook line via `async_describe_events`.
- **Per-platform test files.** `tests/` now has one file per platform plus
  `test_logbook.py`; `test_init.py` is down to setup/entity-lifecycle.
- **Status-LED `entity_category`: decided against.** The rocker status LED is
  deliberately left a primary switch rather than an `EntityCategory.CONFIG`
  entity so it stays directly controllable — this is a recorded decision, not a
  gap (`quality_scale.yaml:53-58`).

### Deliberate non-goals

- **Raw gateway labels in entity names are not a localization bug.**
  `JungHomeQuantity` (`sensor.py:128`) and `JungHomePresence`
  (`binary_sensor.py:103`) assign `_attr_name = label` straight from the
  gateway, bypassing HA localization, so those names stay in whatever language
  the gateway serves. That is intentional: the labels are **user-authored data
  typed into the JUNG HOME app**, not integration strings, so there is nothing
  correct to translate them *to*. Routing them through `translation_key` would
  only add indirection. Leave them alone.
- **No `services.py`/`services.yaml`** — `quality_scale.yaml` marks this
  exempt. Worth reconsidering only if a real use case shows up, e.g. a
  `recall_scene`/`send_datapoint_value` service scoped by device, following
  Shelly's `ServiceValidationError` translation-key pattern.

### Still open

Several of these already have a PR in flight; check the linked issue/PR before
starting so work isn't duplicated.

- **Repair issues for silent degradation** (PR branch
  `repair-websocket-degraded`). There is no `repairs.py`. Repeated WebSocket
  reconnect failures in `_websocket_loop` only log a warning, with no
  user-visible signal that the integration has silently fallen back to 60 s
  REST-only polling (Shelly raises a repair issue after
  `MAX_PUSH_UPDATE_FAILURES`). A repair issue (`fixable=False` is fine) would
  cover both this and the device-id-churn reload in
  `_reload_if_device_ids_changed`, which also only logs today.
- **Config-flow host collision** (#131, `fix-manual-host-duplicate`).
  `_async_apply_host` (`config_flow.py:238`) only calls `async_set_unique_id` +
  `_abort_if_unique_id_configured`; unlike `async_step_zeroconf` it never
  cross-checks `CONF_HOST` against existing entries, so a gateway already added
  via zeroconf (unique_id = mDNS hostname) can be re-added manually by typing
  its IP. Fix: reuse the same `CONF_HOST`-collision check in both paths.
- **Reconfigure connect-then-commit** (#132, `verify-reconfigure-host`).
  `async_step_reconfigure` (`config_flow.py:381`) checks the new host against
  other entries but never verifies it is reachable or is the *same* gateway
  (unlike Shelly's MAC re-check + `_abort_if_unique_id_mismatch`). A typo or
  wrong IP is accepted and only surfaces as a later reauth/connect failure.
- **Broad `except Exception` handlers** (#133, `narrow-broad-excepts`). The
  best-effort handlers in `__init__.py`'s migration code (376, 400, 408) and in
  `coordinator.py` (237, 333, 399, 624) are broader than the specific
  `aiohttp`/`asyncio` types used elsewhere; narrowing them stops real bugs being
  swallowed as "best-effort".
- **Per-device diagnostics** (#134, `device-diagnostics`).
  `diagnostics.py` implements only `async_get_config_entry_diagnostics`; adding
  `async_get_device_diagnostics` would let a user download one device's data.
- **Device triggers for button gestures** (#125,
  `it-rec:claude/button-device-triggers`). No `device_trigger.py`; the shipped
  blueprint is currently the only path from raw edges to single/double/hold.
- **Snapshot testing** (PR branch `snapshot-tests`). No `syrupy`, no `.ambr`
  files — snapshots would catch unintended entity-attribute/unique-id
  regressions more cheaply than hand-written assertions, especially now that
  the platform files exist.
- **Reusable JSON device/API fixtures.** No `tests/fixtures/` (Shelly keeps
  per-model fixtures there); current tests build device/datapoint dicts inline.
- **Coverage is not gated.** `.github/workflows/test.yml` collects
  `--cov-report=term-missing` but passes no `--cov-fail-under=`, so coverage
  can silently regress.
- **Minor: richer `zeroconf_confirm`.** Could show more device identity
  (model/serial) if the mDNS TXT records carry it, for multi-gateway networks.
