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
  parser).
- Reuse the shared aiohttp session via `async_get_clientsession(hass,
  verify_ssl=False)` (the gateway's cert is self-signed); don't create
  per-request `ClientSession`s or build SSL contexts on the event loop.
- Validate with hassfest + HACS (see `.github/workflows/validate.yml`).
- `tests/` covers more than pure helpers: `test_const.py` (stable-ID helpers),
  `test_config_flow.py`, `test_coordinator.py`, `test_websocket.py`, and
  `test_init.py` (setup/entity-lifecycle, the largest file). Uses
  `pytest_homeassistant_custom_component`'s `hass` fixture, `MockConfigEntry`,
  `aioclient_mock`. Needs the pinned `homeassistant`; runs on Python 3.14 like
  the other workflows. Run `pytest`; wired into `.github/workflows/test.yml`.
- **Snapshot tests.** Each platform test file ends with a
  `test_all_<platform>_entities` case that runs
  `pytest_homeassistant_custom_component.common.snapshot_platform` over a
  single-platform setup (the `init_platform` fixture in `tests/conftest.py`
  patches `PLATFORMS` down to one, because `snapshot_platform` refuses a mixed
  entry). The committed `tests/snapshots/*.ambr` pin every entity's registry
  entry — `unique_id` included — plus its state and attributes, so a change to
  `stable_unique_id`/`_scene_slug` or to a platform's published attributes shows
  up as a diff instead of silently re-keying users' entities. Regenerate with
  `pytest --snapshot-update` and **review the diff** before committing it.
  Snapshot setups start from `PRISTINE_DEVICES`, an import-time deep copy of
  `DEVICES`: the coordinator merges pushes into the device dicts it is handed,
  so tests that actuate an entity mutate the shared list, and without the copy
  the snapshots would depend on test execution order.

## Ideas from comparing against Shelly (core reference integration)

Findings from reading `homeassistant/components/shelly` (HA core's most
mature local-push integration) against this integration, filtered to what's
genuinely applicable to a single local-gateway integration (Shelly's
multi-generation/cloud complexity mostly doesn't apply). Treat as a backlog,
not commitments — evaluate cost/value per item before implementing.

**Connection resilience**
- No `EVENT_HOMEASSISTANT_STOP` listener: `coordinator.stop()` only runs from
  `async_unload_entry`, so a full HA shutdown (not an entry unload) skips the
  orderly `ws.close()`/task-cancel. Shelly registers this per-coordinator
  (`_handle_ha_stop`).
- Repeated WebSocket reconnect failures in `_websocket_loop` only log a
  warning; there's no user-visible signal that the integration has silently
  fallen back to 60 s REST-only polling. Shelly raises a repair issue after
  `MAX_PUSH_UPDATE_FAILURES`. A repair issue here (fixable=False is fine) would
  cover both this and the device-id-churn reload path in
  `_reload_if_device_ids_changed`, which also only logs today.
- Minor: add jitter to the reconnect backoff (avoids synchronized reconnect
  storms if multiple gateways share a network blip); surface "last successful
  WS connection" timestamp in `diagnostics.py` alongside the existing
  `ws_frame_log`.

**Config flow**
- `_async_apply_host` (manual `app_approval`/`password` steps) only calls
  `async_set_unique_id` + `_abort_if_unique_id_configured`; unlike
  `async_step_zeroconf`, it never cross-checks `CONF_HOST` against existing
  entries. A gateway already added via zeroconf (unique_id = mDNS hostname)
  can be re-added manually by typing its IP. Fix: reuse the same
  `CONF_HOST`-collision check in both paths.
- `async_step_reconfigure` updates the stored host without verifying the new
  host is reachable or is the same gateway (no connect-then-commit, unlike
  Shelly's MAC re-check + `_abort_if_unique_id_mismatch`). A typo or wrong IP
  is accepted and only surfaces as a later reauth/connect failure.
- Minor: `zeroconf_confirm` could show more device identity (model/serial) if
  the mDNS TXT records carry it, for multi-gateway networks.

**Entity model**
- `JungHomeSwitch` (the rocker status-LED toggle) has no `entity_category`;
  it's a device-configuration control, not a primary function, and should be
  `EntityCategory.CONFIG` (matches Shelly's LED/eco switches). Currently it's
  the only entity_category gap — diagnostic entities are already tagged
  correctly.
- `JungHomeQuantity`/`JungHomePresence` set `_attr_name` directly from the
  gateway's raw label instead of routing through `translation_key` +
  `translation_placeholders`. Those entity names bypass HA localization even
  though the rest of the integration uses `strings.json` translations
  consistently.

**Repairs, logbook, services**
- No `repairs.py`. The two silent-degradation paths above (WS
  reconnect-failure fallback, device-id-churn reload) are the concrete
  candidates — turn the existing log warnings into dismissible repair issues.
- No `logbook.py`. The coordinator already fires a `junghome_scene_recalled`
  bus event (`coordinator.py`); without `async_describe_events` it shows up
  raw in the HA logbook instead of "Scene 'Evening' was recalled" — cheap win
  since the event already exists.
- No `services.py`/`services.yaml` (`quality_scale.yaml` marks this exempt).
  Worth reconsidering only if a real use case shows up — e.g. a
  `recall_scene`/`send_datapoint_value` service scoped by device, following
  Shelly's `ServiceValidationError` translation-key pattern.
- `diagnostics.py` has no `last_error`/`last_exception` field capturing the
  most recent coordinator failure (Shelly's `diagnostics.py` includes
  `repr(device.last_error)`); would help triage a downloaded report without
  cross-referencing logs.
- Bare `except Exception` in `__init__.py`'s migration code and
  `coordinator.async_fetch_groups` is broader than the specific
  `aiohttp`/`asyncio` exception types used elsewhere in the coordinator; worth
  narrowing so unexpected bugs aren't swallowed as "best-effort."

**Testing**
- No per-platform test files — `test_init.py` (2765 lines) covers most
  platform/entity-lifecycle behavior in one file, unlike Shelly's
  one-file-per-platform convention (`test_sensor.py`, `test_switch.py`, etc.).
  Splitting would improve maintainability as the suite grows.
- ~~No snapshot testing~~ — **done**: `syrupy` is pinned in
  `requirements_test.txt` and every platform has a `snapshot_platform` case
  backed by a committed `tests/snapshots/*.ambr` (see Conventions above).
- No reusable JSON device/API fixtures (Shelly keeps per-model fixtures under
  `tests/fixtures/`); current tests build device/datapoint dicts inline.
- `--cov-report=term-missing` is collected in CI but not gated with
  `--cov-fail-under=`, so coverage can silently regress.
