# Repository guide

Home Assistant custom integration for **JUNG HOME** (HACS). It talks to a local
JUNG HOME Gateway over its REST API and WebSocket.

## Layout

- `custom_components/junghome/` — the integration.
  - `__init__.py` — setup/unload, one-time stable-ID registry migrations,
    stale-device pruner, area auto-assignment, capability-change reload.
  - `coordinator.py` — 60 s REST poll + WebSocket push and commands. The WS
    `functions` broadcast (the authoritative device list, sent on connect and
    on change) is adopted exactly like a poll result, so device add/remove is
    push-driven; the poll is the backstop.
  - `config_flow.py` — zeroconf + manual setup (app-approval or network-key
    password), reauth (confirm form first — registration opens the gateway's
    single 180 s approval window the moment it runs), reconfigure, options
    (inverted covers).
  - `const.py` — `DOMAIN` and the stable-ID helpers (`device_slug`,
    `datapoint_suffix`, `stable_unique_id`, `duplicate_slugs`,
    `scene_unique_id`, `is_presence_quantity`).
  - `light.py`, `switch.py`, `sensor.py`, `binary_sensor.py`, `event.py`,
    `cover.py`, `climate.py`, `scene.py` — platforms; each discovers devices
    added at runtime via a coordinator listener.
- `blueprints/automation/junghome/button_gestures.yaml` — shipped blueprint
  deriving single/double/hold from raw press/release edges. Imported by URL;
  **not** distributed by HACS (HACS only installs `custom_components/`).
- `docs/` — reverse-engineered gateway reference (see below) plus
  `docs/example-button-automation.md` (user-facing guide).
- `config/`, `docker-compose.yml`, `scripts/` — local test harness.
- `disk_dump/` — gateway microSD image, **gitignored** (tokens + mesh keys;
  never commit it). `jung/sdc2` is the **current** firmware (v2.1.x, API
  1.5.0); `sdc3` is the older A/B partition — quote evidence from sdc2.

## Protocol facts the platforms encode (all firmware-verified)

- Function-type → platform: `OnOff`/`DimmerLight`/`ColorLight` → light;
  `Socket` → switch + sensor; `Measurement` → sensor + binary_sensor;
  `Position`/`PositionAndAngle` → cover; `Thermostat` → climate;
  `RockerSwitch` → event + switch (status LED). Rockers report only raw
  `pressed`/`depressed` edges and alternate between `up_request` and
  `down_request` on consecutive presses.
- A `quantity` datapoint whose label denotes presence/occupancy (empty unit,
  0/1 value — a BWM detector's `Presence Detected`) becomes an **occupancy
  binary_sensor**; other quantities become numeric sensors.
  `is_presence_quantity` is the single split point: binary_sensor claims those
  labels, `sensor.py` skips them.
- A **`Thermostat`'s `switch` datapoint is not an on/off** — the gateway
  re-labels the RTR's `automatic_mode` state, and it tracks the regulator's
  momentary heating output (flips on its own several times an hour). A room
  regulator has no on/off at all, so the climate entity is permanently
  `HVACMode.HEAT` and that datapoint only feeds `hvac_action`; never map it to
  `hvac_mode` again (issue #121; evidence in docs/gateway-websocket.md).
- **Cover `level` is percent-closed**: close ⇒ BT-Mesh "down" (`0x7FFF`,
  level→100 %), open ⇒ "up" (`0x8000`, →0 %); HA position = `100 - level`.
  Correct for shutters/blinds; **awnings mount the motor the opposite way** and
  read inverted — users flag them in the options flow
  (`CONF_INVERTED_COVERS`), which switches that cover to an identity mapping.
  The single inversion point is `_to_ha`/`_to_device` in `cover.py`. Changing
  the flagged set reloads the entry (options snapshot in the coordinator).
- **Colour temperature is 2000–6000 K, enforced by the gateway**: the
  middleware hard-codes that range and clamps every tunable-white write (see
  the `DEFAULT_MAX_KELVIN` comment in `light.py`). Do not widen it.
- Scenes arrive over the WS `scenes` broadcasts (plus a setup-time REST fetch)
  and recall over REST `POST /scenes/{id}` — the WS `scene` *command* is
  unimplemented on the gateway. Scene identity is the **label** (ids
  regenerate like device ids); recalls re-resolve the id at call time.
- The gateway lists **unreachable devices too** (no `isOnline` filter in the
  firmware's function assembly) — absence from `/functions/` means
  deleted/relabelled or a partial poll, which is why the pruner debounces
  `STALE_DEVICE_PRUNE_MISSES` polls before removing anything.

## Gateway reference — read `docs/` first

When touching the gateway protocol, consult [docs/README.md](docs/README.md)
instead of re-deriving:

- [docs/gateway-rest-api.md](docs/gateway-rest-api.md) — endpoints, auth, the
  unauthenticated `/apidoc` spec, client registration.
- [docs/gateway-websocket.md](docs/gateway-websocket.md) — all WS message
  types and command formats.
- [docs/gateway-architecture.md](docs/gateway-architecture.md) — partitions,
  services, BT-Mesh stack, self-hosting analysis.
- [docs/gateway-system-analysis.md](docs/gateway-system-analysis.md) — the
  current (v2.1.3) firmware image in detail.
- [docs/bt-mesh-direct.md](docs/bt-mesh-direct.md) — gateway-free BT-Mesh
  control; prototypes in `tools/bt-mesh-direct/`.
- [docs/matter-bridge.md](docs/matter-bridge.md) — Matter options.

## Key behaviours to preserve

- **Stable identity.** The gateway regenerates device/datapoint `id`s on
  firmware updates, so entity `unique_id`s and device identifiers derive from
  the device **label** + datapoint **suffix** (`stable_unique_id`), never the
  raw id. Don't reintroduce id-based identifiers.
- **Slugs can collide — never key per-device state by slug without guarding.**
  Two labels that slug identically (`"Lamp 1"`/`"Lamp-1"`) share one
  `device_slug`; identity survives (the second device loses), but a *map keyed
  by slug* does not — the second overwrites the first each pass and looks like
  a changed device. This exact bug produced endless reload loops twice (the
  capability watcher, then `_reload_if_device_ids_changed` on list-order
  changes). Guard every such map with `duplicate_slugs()`; its three current
  users are `_register_capability_reload`, the device-identifier migration and
  `_reload_if_device_ids_changed`.
- **Entity naming.** `_attr_has_entity_name = True` with a short `_attr_name`
  (`None` for the device's main feature). The **device** carries the label;
  baking it into the entity name makes HA compose it twice (the old
  `event.<label>_<label>_…` bug). `entity_id`s are sticky for existing
  installs.
- **Capabilities follow datapoints and can change at runtime.** Platforms
  freeze supported features at construction from the datapoints present
  (tilt ← `angle`, brightness/CT ← `brightness`/`color_temperature`), and
  discovery is add-only. `_register_capability_reload` reloads the entry when
  a device's datapoint-type set changes so features are rebuilt (the
  tilt-lost-after-update regression). Gate capabilities on datapoint
  *presence*, never on the function-type name.
- **Push handling must not starve the poll.** The per-datapoint push path
  deliberately avoids `async_set_updated_data` (it re-arms the poll a full
  interval out; a chatty gateway would defer polling forever — the old
  poll-starvation P0). It sets `last_update_success` + `async_update_listeners`
  instead. The `functions`-broadcast path *does* use `async_set_updated_data`,
  correctly: it carries poll-equivalent data and only arrives on change.
- **Availability**: entities key off `last_update_success` and never OR in
  `ws_connected` (a stale-True socket flag froze energy readings — issue
  #120); controllable entities additionally require the live WS because
  commands only travel over it.

## Conventions

- Match HA integration patterns. `strings.json` and all 26 `translations/`
  locales move together (a new/changed key means 26 edits; `<`/`>` breaks the
  parser). `tests/test_translations.py` enforces key parity, placeholders and
  duplicate keys.
- Reuse the shared session: `async_get_clientsession(hass, verify_ssl=False)`
  (self-signed gateway cert); never build SSL contexts on the event loop.
- CI: `test.yml` (pytest + mypy --strict), `lint.yml` (ruff, pinned),
  `validate.yml` (hassfest + HACS), `floor.yml` (imports the integration
  against the `hacs.json` minimum HA — a floor break means *raise the floor*,
  not block the release), `release.yml` (tag-gated on all checks). Coverage
  gate: 95 % branch (`.coveragerc`). Renovate owns pip (the
  pytest-homeassistant-custom-component stack moves as one group and is
  version-capped); Dependabot deliberately does not watch pip.
- Tests: one file per platform plus flow/coordinator/init/blueprint/
  translations/device-trigger files; new platform behaviour goes in that
  platform's file. Uses `pytest_homeassistant_custom_component` (`hass`
  fixture, `MockConfigEntry`, `aioclient_mock`); Python 3.14, pinned HA.
- **Snapshot tests** pin every entity's registry entry (`unique_id` included),
  state and attributes (`tests/snapshots/*.ambr`). Regenerate with
  `pytest --snapshot-update` and **review the diff** — a `unique_id` change is
  a bug, not a snapshot to accept. A deleted entity surfaces as
  `N snapshots unused` with non-zero exit but **no `FAILED` line**. Fixtures
  hand the coordinator a `deepcopy` of `PRISTINE_DEVICES` (the coordinator
  mutates the dicts it is given).
- **Test landmines**: (1) HA's flow manager auto-advances `SHOW_PROGRESS_DONE`
  **re-passing the same `user_input`** — a register mock that fails
  synchronously (no await) silently retries instead of showing the failure
  form; park flow mocks on `asyncio.sleep(0)`/`Event` like a real HTTP call.
  (2) A bare-coordinator test that triggers `async_request_refresh` must end
  with `await coordinator.async_shutdown()` or the debouncer timer lingers and
  fails teardown.

## Settled decisions — do not re-litigate

Each of these was investigated (several across multiple audits); re-raising
them without new evidence wastes a session.

- **Zeroconf host update does NOT double-reload** — measured, refuted: the
  update listener dispatches synchronously inside `async_update_entry`, so
  core re-reads UNLOAD_IN_PROGRESS and its own reload never fires.
  `reload_on_update=False` is passed to state intent only.
- **Raw gateway labels in sensor names stay untranslated** — they are
  user-authored app data; there is nothing correct to translate them to.
- **Status LED is an `EntityCategory.CONFIG` switch** — it configures the
  button's look, not a load; still fully actuable.
- **Scene entities set `has_entity_name = False`** — no backing device to
  carry the label.
- **No `services.py`** — exempt in `quality_scale.yaml`; reconsider only with
  a real use case.
- **`JungHomeEntity.available` does not check the entity's own device against
  `coordinator.data`** — deliberate: the pruner's 3-poll debounce bounds the
  stale window, and a naive check would flap on every partial poll. Revisit
  only by sharing the debounce counter.
- **A partial push cannot blank sibling `values` keys** — the merge is
  per-key, not a list replacement. `ws_last_frame_by_type` is bounded by the
  gateway's frame-type vocabulary.
- **`climate.set_temperature` ignoring `target_temp_low/high`** is correct
  for a single-setpoint regulator.
- **ruff `target-version` stays `py313`** — bumping to py314 flips
  TC001/TC002/UP037 semantics (PEP 649 lazy annotations) and would churn every
  module for zero behavioural gain; revisit when HA core moves.
- **The group `color_temperature_range` parser stays unwired** — no captured
  firmware sends the field (`disk_dump/ws-capture*/groups.json`); the gateway
  clamps CT to 2000–6000 K anyway.

## Backlog (open, in rough value order)

- **Key entries on the gateway serial instead of host/hostname.** The mDNS TXT
  record carries `serial=`/`mac=`/`version=` (verified:
  `etc/avahi/services/junghome.service` in the dump) via
  `discovery_info.properties`. Fixes reconfigure identity (a wrong-but-live
  address currently passes the probe), `discovery-update-info` for manual
  entries, and manual-vs-discovered duplicates. The work is the entry
  migration, not the data.
- **Narrow the three broad excepts** in `coordinator.py` (#133): reconnect
  loop, frame-handler catch-all, WS send path.
- **A REST poll in flight when a push lands can briefly revert the pushed
  value** (poll snapshot predates the push). Small window, self-heals on the
  next push/poll.
- **Covers report the target position immediately** (optimistic write, no
  travel tracking); gateway pushes correct it live.
- **Reusable JSON device/API fixtures** (`tests/fixtures/`); tests build
  device dicts inline.
- **Richer `zeroconf_confirm`** — show serial/model in the confirm dialog for
  multi-gateway networks.

## History

Two full audits (an 11-agent platinum review and a 2026-08-02 line-by-line
review with disk-dump verification of every firmware claim — all of which
held) produced the settled-decisions and backlog lists above. The audit
documents themselves (`CLAUDE-review.md`, `CLAUDE-fable.md`) were local-only
working files and have been deleted; everything durable from them lives in
this file, in code comments at the relevant sites, and in the regression
tests named after the bugs they pinned.
