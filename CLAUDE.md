# Repository guide

Home Assistant custom integration for **JUNG HOME** (HACS). It talks to a local
JUNG HOME Gateway over its REST API and WebSocket.

## Layout

- `custom_components/junghome/` — the integration.
  - `__init__.py` — setup/unload, one-time stable-ID registry migrations,
    stale-device pruner, area auto-assignment, capability-change reload.
  - `coordinator.py` — REST poll (default 60 s, options-configurable) +
    WebSocket push and commands. The WS
    `functions` broadcast (the authoritative device list, sent on connect and
    on change) is adopted exactly like a poll result, so device add/remove is
    push-driven; the poll is the backstop.
  - `config_flow.py` — zeroconf + manual setup (app-approval or network-key
    password), reauth (confirm form first — registration opens the gateway's
    single 180 s approval window the moment it runs), reconfigure, options
    (REST poll interval, inverted covers).
  - `const.py` — `DOMAIN` and the stable-ID helpers (`device_slug`,
    `datapoint_suffix`, `stable_unique_id`, `duplicate_slugs`,
    `scene_unique_id`, `is_presence_quantity`).
  - `light.py`, `switch.py`, `sensor.py`, `binary_sensor.py`, `event.py`,
    `cover.py`, `climate.py`, `scene.py` — platforms; each discovers devices
    added at runtime via a coordinator listener.
- `tools/ws-capture/capture_ws.py` — read-only WS capture + analysis tool.
  Records frames **with timestamps** and walks the user through a scripted
  gesture set (`--script rocker` / `cover`), then `analyze` derives per-gesture
  edge sequences, channel-echo detection and the timing bounds the button
  blueprint's defaults rest on. This is how the two evidence-blocked backlog
  items get unblocked; the old `disk_dump/ws-capture*/` dumps have no timing.
- `blueprints/automation/junghome/button_gestures.yaml` — shipped blueprint
  deriving single/double/hold from raw press/release edges. Imported by URL;
  **not** distributed by HACS (HACS only installs `custom_components/`).
- `docs/` — reverse-engineered gateway reference (see below) plus
  `docs/example-button-automation.md` (user-facing guide).
- `config/`, `docker-compose.yml`, `scripts/` — local test harness.
- `disk_dump/` — gateway microSD dumps + live WS captures, **gitignored**
  (tokens + mesh keys; never commit it). Two dumps of the same card:
  `jung/` (2026-06-13, `sdc*`) and `jung-20260801/` (`sdb*`) — same builds
  byte-for-byte, but the 2026-08-01 extraction is higher fidelity (see its
  `NOTES.md`). **Quote evidence from `jung-20260801/sdb2`** (current
  firmware, v2.1.3 build 2840, API 1.5.0; `jung/sdc2` is the same build);
  `sdb3`/`sdc3` are the older v2.0.0 A/B partition — evidence found *only*
  there is stale (v2.1.3 refactored the middleware into
  `models/device_states/*State.js`). `ws-capture/` and `ws-capture-20260727/`
  are live production WS sessions; the data partition's live mesh DB is
  `sd?4/middleware/res_6/` (the unnumbered `res/` is empty factory state).

## Protocol facts the platforms encode (all firmware-verified)

- Function-type → platform: `OnOff`/`DimmerLight`/`ColorLight` → light;
  `Socket` → switch + sensor; `Measurement` → sensor + binary_sensor;
  `Position`/`PositionAndAngle` → cover; `Thermostat` → climate;
  `RockerSwitch` → event + switch (status LED). Rockers report only raw
  `pressed`/`depressed` edges (`up_request` / `down_request`, one datapoint
  per physical side — **not** alternating channels per press; that earlier
  belief was refuted by a timestamped capture).
- **On current DEVICE firmware, one tap is reported as TWO press/release
  pairs — same channel; a hold as ONE.** Labelled capture (2026-08-02, 16
  taps + 5 holds): tap pulse 0.40–0.53 s (near-constant — the device's
  reporting granularity, not the finger), hold pulse 2.44–3.11 s, intra-burst
  gap 0.11–1.03 s. Single vs double click is **indistinguishable** (both = 2
  identical pairs, overlapping gap ranges); tap vs hold separates perfectly on
  **pulse width** (5× empty band). **This is a regression**: the gateway's own
  archived logs (2026-06-20→07-28, ~450 bursts) show 1.00 presses/burst on
  the same buttons; gateway fw unchanged across the window, JUNG app went
  2.1.0→2.2.0 (app 2.2.x updates device firmware — issue #66). Mechanism
  unestablished — do NOT present it as BT-Mesh retransmission (gaps up to
  1 s refute that); gesture logic must tolerate both one and two pairs per
  tap. A duplicate-suppression window must be **≥ ~1.2 s** (earlier
  0.15–0.25 s guidance came from a mis-segmented unlabelled capture —
  refuted). Evidence + tables in docs/gateway-websocket.md.
- **Every button gang exposes BOTH `up_request` and `down_request`**, even a
  single-action one: the firmware's `JungHome_PushButton` model always creates
  PushedUp + PushedDown + StatusLed states. The gateway knows the difference
  (a `KeyMode` property) but that is category `manufacturer_property`, which
  `getDatapointTypeByState` never turns into an API datapoint — and JUNG's own
  code carries a `// TODO: set visibility here based on mode` for exactly
  this. So a single-action gang unavoidably gets one dead event entity; there
  is nothing on the wire to suppress it with. Multi-gang panels report each
  gang as a **separate function**, hence a separate HA device.
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
- **Thermostat presets: the API descriptor's `none` is a lie.** Writes accept
  exactly `frost`/`eco`/`comfort` (the firmware throws on anything else,
  surfacing as an uncorrelated error → command timeout); "no preset" reads
  back as the **empty string**, never `"none"` (a preset is derived — target
  temperature == a configured threshold). climate.py maps `""` → PRESET_NONE
  on read and treats selecting PRESET_NONE as a local no-op; never send
  `"none"` (preset note in docs/gateway-websocket.md).
- **Cover `level` is percent-closed**: close ⇒ BT-Mesh "down" (`0x7FFF`,
  level→100 %), open ⇒ "up" (`0x8000`, →0 %); HA position = `100 - level`.
  Correct for shutters/blinds; **awnings mount the motor the opposite way** and
  read inverted — users flag them in the options flow
  (`CONF_INVERTED_COVERS`), which switches that cover to an identity mapping.
  The single inversion point is `_to_ha`/`_to_device` in `cover.py`. Changing
  the flagged set reloads the entry (options snapshot in the coordinator).
- **A cover's HA device class comes from its datapoints, not its function type**
  (`_device_class` in `cover.py`): an `angle` datapoint means slats, so `blind`;
  position only means a roller shutter, so `shutter`; a cover the user flagged
  as inverted is an `awning` (that flag wins — an awning has no slats). The
  gateway calls every cover a `WindowCover`, so there is nothing else to key on.
  Don't hard-code `blind` again: it gave every roller shutter slat-oriented
  controls and icons.
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
- **Entry identity vs. entity identity are decoupled.** Entries are keyed
  (`unique_id`) on the gateway hardware serial when known (mDNS TXT
  `serial=`, or REST `config/parameter/system_serial`), and legacy entries
  are migrated to it on rediscovery/reconfigure — but ids derived from the
  *entry* (the hub device, scene unique_id scope) anchor on
  `entry_anchor()`/`entry.data["identity_anchor"]`, frozen at
  creation/migration. Never derive an entity or device id from
  `entry.unique_id` directly, and never change an existing entry's frozen
  anchor — either re-keys the hub device and every scene entity.
- **Slugs can collide — never key per-device state by slug without guarding.**
  Two labels that slug identically (`"Lamp 1"`/`"Lamp-1"`) share one
  `device_slug`; identity survives (the second device loses), but a *map keyed
  by slug* does not — the second overwrites the first each pass and looks like
  a changed device. This exact bug produced endless reload loops twice (the
  capability watcher, then `_reload_if_device_ids_changed` on list-order
  changes). Guard every such map with `duplicate_slugs()`; its three current
  users are `_register_capability_reload`, `_reload_if_device_ids_changed`
  and `_make_area_assigner` (the device-identifier migration guards the same
  hazard differently — a registry `async_get_device` clash check before each
  write).
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
  Pushes that land while a poll is in flight are recorded and re-applied over
  the poll's snapshot (`_poll_push_overlay`) — the snapshot predates them, so
  adopting it as-is briefly reverted pushed/command-confirmed values.
  Membership is covered separately: a `functions` broadcast adopted while a
  poll's fetch is in flight supersedes that poll — the poll discards its
  older snapshot (`_functions_broadcasts_seen` in `_async_update_data`),
  skipping the overlay re-apply, the id-churn check and the
  `data_generation` bump with it. The broadcast re-ran the latter two on the
  fresher list (bumping again would double-count one membership change in
  the pruner's poll-based debounce); the **overlay** it never touches — it
  has nothing left to re-apply, because the gateway composes the broadcast
  *after* every push already delivered on that same ordered session, so the
  broadcast's own values postdate them. The counter is bumped immediately
  before the adoption, never before the id-churn check that can raise on a
  malformed frame.
- **Availability**: entities key off `last_update_success` and never OR in
  `ws_connected` (a stale-True socket flag froze energy readings — issue
  #120); controllable entities additionally require the live WS because
  commands only travel over it.
- **Entities skip state writes for other devices' pushes — but only while
  already shown available.** A per-datapoint push dispatch used to write
  every entity of the entry; `JungHomeEntity._skip_foreign_device_push`
  (fed by `coordinator.pushed_device_id`) skips entities of other devices.
  Two load-bearing details: the scope is the *device*, not the entity's
  stored datapoint ids (climate reads its ambient temperature from a sibling
  `quantity` datapoint it holds no id for), and the skip is forbidden while
  the state machine shows the entity unavailable — a push proves the gateway
  alive, so it must be the thing that flips a post-failed-poll "unavailable"
  back. Everything without a push marker fails open, and the connectivity
  sensor/scenes (state not derived from device datapoints) don't inherit the
  helper.
- **Commands await the gateway's confirmation, not fire-and-forget.** Every
  datapoint set (`_send_datapoint_command` in `coordinator.py`) is tagged with
  a `message_id`; a successful set is answered with a `datapoint` reply
  echoing it back (firmware-verified, `websocket-server-service.js`), which
  `_dispatch_text_frame` routes to `_resolve_pending_reply` to resolve the
  future the command method is awaiting — then falls through to the normal
  merge path, so `coordinator.data` holds the *confirmed* value before the
  entity's own optimistic write runs. A rejected set produces only an
  `error:` message frame with **no `message_id`** to correlate against, so a
  rejection surfaces as a `COMMAND_REPLY_TIMEOUT` (5 s; the middleware itself
  gives up on the BT-Mesh node after 3 s — `config.btmesh.response_timeout_ms`
  in `config.json`) rather than the gateway's specific error text. Do not try
  to attribute an uncorrelated `error:` frame to whichever command is
  in-flight — with concurrent commands from different entities that would
  misattribute someone else's failure. The reply only ever arrives on the
  session that sent the command (`socket.send`, not a broadcast), so the
  `_run_websocket` finally block fails all in-flight futures (`cannot_send`)
  the moment the session ends — never leave them to sit out the timeout.

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
  The shared gateway payload is `tests/fixtures/functions.json` (wire-shaped,
  loaded by conftest as `DEVICES`; `bare_coordinator` is the shared bare
  setup). One-off device dicts stay inline in the test that uses them —
  visible inputs beat indirection for single-use data.
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
  `coordinator.data`** — deliberate: the pruner's 10-poll debounce
  (`STALE_DEVICE_PRUNE_MISSES`) bounds the stale window, and a naive check
  would flap on every partial poll. Revisit only by sharing the debounce
  counter.
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
- **The three broad excepts in `coordinator.py` stay broad** (reconnect loop,
  frame-handler catch-all, WS send path) — wontfix. Each is load-bearing
  containment: the reconnect loop must retry through *any* failure class, a
  malformed frame must never tear down a healthy session, and every send
  failure must surface as `cannot_send` to the calling service. The
  narrowing that mattered was already done in PR #133 (best-effort
  fetch/parse handlers); narrowing these three trades crash-risk for no
  diagnostic gain.

## Maximum-effort review protocol

Run this whenever asked to review the repo or a PR ("run the review
protocol", "review PR #N"). The bar: **only verified findings count**, and
repeated runs must converge — an empty report is a success state, not a
failure to try hard enough. Padding a clean run with nits poisons every
future run.

**Setup.** Record findings incrementally to `CLAUDE-fable.md` (kept out of
git via `.git/info/exclude` — never commit it; a dead session must not lose
progress). If the file exists from a previous run, first re-verify its open
findings — fixed → mark fixed, still open → carry forward — before hunting
new ones. PR scope = the diff plus every invariant it touches; repo scope =
all passes below.

**What counts as a finding.** A defect with (a) concrete evidence
(file:line, firmware path, capture frame), (b) a failure scenario a real
user or contributor can hit, and (c) a proposed fix. Actively try to
*refute* every candidate before recording it — read the callers, trace the
wire path, run the test. What does NOT count: style ruff doesn't enforce,
speculative rewrites, unreachable hypotheticals, anything under "Settled
decisions" without new evidence, anything already in the Backlog. A
candidate that survives refutation but can't be fully proven goes in a
separate, clearly-marked "unproven" list. Severity: P0 user-visible
breakage · P1 correctness under race/edge conditions · P2 wrong or stale
evidence in docs/comments · P3 polish worth doing while nearby.

**Pass 1 — firmware-evidence accuracy.** The current build is v2.1.3
(2840): `disk_dump/jung-20260801/sdb2` (highest-fidelity extraction;
`jung/sdc2` is the same build). `sdb3`/`sdc3` are v2.0.0 — evidence found
*only* there is stale (v2.1.3 refactored the middleware into
`models/device_states/*State.js`; services the docs once cited exist only
in v2.0.0). Every firmware citation in docs/, CLAUDE.md and code comments
must resolve in the current build — file exists, behaviour matches.
Descriptor files (`cdb_types_*.json`, `/apidoc`) are *promises*; the
`dist/` implementation is the truth — when they disagree, the
implementation wins and the disagreement is a finding (the preset-"none"
bug shipped because the descriptor was trusted). Cross-check the live
captures (`disk_dump/ws-capture*/`) whenever a claim concerns what the wire
actually carries.

**Pass 2 — wire contracts, both directions.** For every datapoint type the
integration writes, trace the full path: HA service → coordinator command →
`ip_event_handler` routing → state-class publish, and confirm every value
sent is one the firmware accepts (it throws on anything else, which
surfaces as an uncorrelated error → command timeout). For every read, trace
state class → `composeDatapointByState` → the value set that can actually
appear — including `""`, `"NaN"`, trailing-space labels and boundary
numbers — and confirm the platform parses all of them.

**Pass 3 — concurrency interleavings.** Enumerate the concurrent actors:
the REST poll, unmatched-push debounced refresh, connect-time refresh,
`functions` broadcast, per-datapoint pushes, command sends + correlated
replies, the reconnect loop, entry reload/unload. For each documented
invariant (push-overlay refcount, pending replies failed on session end, no
poll starvation, broadcast-supersedes-racing-poll), pick the pairwise
interleavings that could violate it and check the *code*, not the comment.

**Pass 4 — identity & HA-contract invariants.** No unique_id or device
identifier from a volatile gateway id, and none derived from
`entry.unique_id` (only `entry_anchor`). Every map keyed by device slug is
guarded with `duplicate_slugs`. Availability: `last_update_success` only;
`ws_connected` may gate controllable entities, never grant availability.
Optimistic entity writes happen only after the awaited command returns.
Quantified claims in comments ("10 consecutive polls", "5 s", "60 s") must
match what the code actually measures — same quantity, same unit, same
trigger. When two sibling code paths handle the same untrusted input, their
hardening must match — an asymmetry is a latent finding. `strings.json` and
all 26 translations move together.

**Pass 5 — tests.** Fixture and test wire values must be values the current
firmware can emit (`"comfort"`-only coverage is how the `""` preset gap
survived). Every P0/P1 fix gets a test that fails before and passes after.
Respect the two test landmines (flow-manager auto-advance, bare-coordinator
shutdown) and the 95 % branch gate.

**Pass 6 — hygiene.** Secrets stay out of git (`disk_dump/` gitignored;
diagnostics redact hosts/tokens/serials, including inside free-form text).
CI pins consistent (ruff version, HA floor). `manifest.json`/`hacs.json`
coherent.

**Report.** Severity-ordered; each finding with evidence, failure scenario
and proposed fix. Close with an explicit verdict: the list of open P0–P2s,
or "clean — nothing above P3 survived verification."

## Backlog (open, in rough value order)

- **Cover travel states** — half-unblocked by firmware evidence: every
  composed `level` datapoint always carries a `level_move` value (−1/1/0)
  derived from current-vs-target (`PositionState.fromMeshMessage` computes
  mode opening/closing/stopped), so `is_opening`/`is_closing` could be read
  from `level` pushes today. Still needed before building it: a capture of a
  blind actually moving, to learn whether intermediate `level` pushes stream
  during travel (drives whether position can track live or only jump).
  Capture it with `tools/ws-capture/capture_ws.py capture --script cover`.
- **Button gesture handling must be rebuilt for double-reporting firmware**
  — labelled capture done (see the rocker protocol bullet; numbers final).
  On affected firmware every blueprint path is wrong: single fires twice
  (or as double when the burst gap lands inside the 0.4 s window), double
  fires single twice, only hold works. Since single-vs-double is provably
  unrecoverable there, the plan (user decision pending on the double-click
  strategy): (1) integration-level duplicate suppression in `event.py` —
  fire on the FIRST press, ignore a press within ~1.2 s of the previous
  release on the same datapoint; opt-in via options flow, covers device
  triggers and hand-written automations, no added latency; (2) derived
  `click`/`hold` event types classified on pulse width (taps ≤0.53 s, holds
  ≥2.44 s — clean 5× band), replacing the blueprint's timing gymnastics;
  (3) keep `double_action` for unaffected firmware, documented as such.
  First: user checks the JUNG app for the button key-mode / device-fw
  change and reports the regression upstream — a config fix at source
  beats all of this. Verify any change on a second rocker before shipping
  (all measurements so far are one button).
