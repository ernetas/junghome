# Repository guide

Home Assistant custom integration for **JUNG HOME** (HACS). It talks to a local
JUNG HOME Gateway over its REST API and WebSocket.

## Layout

- `custom_components/junghome/` — the integration.
  - `__init__.py` — setup/unload, one-time stable-ID registry migrations,
    stale-device pruner, area auto-assignment, capability-change reload,
    manual device delete, repair-issue withdrawal and store deletion on
    entry removal; loads the rename-following store before the first refresh.
  - `coordinator.py` — REST poll (default 60 s, options-configurable) +
    WebSocket push and commands. The WS
    `functions` broadcast (the authoritative device list, sent on connect and
    on change) is adopted exactly like a poll result, so device add/remove is
    push-driven; the poll is the backstop. Also the two best-effort
    enrichments read after the first refresh: node identities from the
    project export (`node_identities`) and device properties from the
    deprecated verbose device endpoint (`device_properties`: energy counters
    re-read every 5 min, firmware revisions, reachability, light Kelvin
    ranges). And rename
    following (`follow_renames`, on every device-list adoption before the
    listeners run): `function_anchors` (slug → `models.FunctionAnchor`:
    function id, node MAC, element location), persisted in the entry's
    `Store` (`function_anchors_store`), pairs a vanished label with a new one
    on the same element and rewrites the registry in place.
  - `config_flow.py` — zeroconf + manual setup (app-approval or network-key
    password), reauth (confirm form first — registration opens the gateway's
    single 180 s approval window the moment it runs), reconfigure, options
    (REST poll interval, duplicate-press suppression, inverted covers).
  - `tls.py` — TOFU certificate pinning: `async_learn_fingerprint` reads the
    gateway certificate's SHA-256 through a deliberately mismatching pin (no
    request, no token leaves), `fingerprint_ssl` builds the cached
    `aiohttp.Fingerprint` every REST call and the WS upgrade carry. The pin
    lives in `entry.data[CONF_TLS_FINGERPRINT]`; a mismatch raises the fixable
    `tls_certificate_changed` issue handled by `repairs.py` (confirm → re-pin
    against the serial). Discovery never rewrites a healthy entry's host, and
    a failing entry adopts an announced address only if it presents the pin
    (an entry with no pin yet trusts the announcement — its first contact).
    The certificate lives in `/data/etc/nginx` and **survives a factory reset
    and firmware updates** (`generate_ssl_key.sh` regenerates only missing or
    corrupt files; the reset sequence in `board_ctrl` wipes the four `res`
    trees and nothing else), so a changed certificate means a replaced
    gateway or a re-imaged card — never "expected after a reset".
  - `repairs.py` — the fix flow for `tls_certificate_changed`.
  - `const.py` — `DOMAIN`, the stable-ID helpers (`device_slug`,
    `datapoint_suffix`, `stable_unique_id`, `duplicate_slugs`,
    `scene_unique_id`, `is_presence_quantity`), the option keys and the
    button gesture constants (`BUTTON_EVENT_TYPES`, `BUTTON_HOLD_THRESHOLD`,
    `BUTTON_DUPLICATE_WINDOW`, `CONF_SUPPRESS_DUPLICATE_PRESSES`).
  - `light.py`, `switch.py`, `sensor.py`, `binary_sensor.py`, `event.py`,
    `cover.py`, `climate.py`, `scene.py` — platforms; each discovers devices
    added at runtime via a coordinator listener.
- `tools/ws-capture/capture_ws.py` — read-only WS capture + analysis tool.
  Records frames **with timestamps** and walks the user through a scripted
  gesture set (`--script rocker` / `cover`), then `analyze` derives per-gesture
  edge sequences, the burst shape (presses per gesture — the doubled-firmware
  diagnostic), which channels fired inside one gesture (a single-key element's
  alternating copies) and the timing bounds `const.py`'s
  `BUTTON_HOLD_THRESHOLD` / `BUTTON_DUPLICATE_WINDOW` rest on. This is how the
  two open evidence items (the hardware verification of the gesture rebuild
  and the cover travel question) get settled; the old `disk_dump/ws-capture*/`
  dumps have no timing.
- `blueprints/automation/junghome/button_gestures.yaml` — shipped blueprint
  mapping the event platform's `click`/`hold_start` events to actions (plus
  an opt-in legacy double-click path for pre-2.2.0 device firmware). Imported
  by URL; **not** distributed by HACS (HACS only installs `custom_components/`).
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
  `Position`/`PositionAndAngle` → cover; `Thermostat` → climate + sensor (its
  room temperature, so the reading has long-term statistics);
  `RockerSwitch` → event + switch (status LED; on the mesh that is vendor
  property `0x5013 KEY_STATUS`, which the gateway writes with a User Property
  *Status* `D1 27 05` to the button element). A `RockerSwitch` function is
  **one mesh button element** — a whole rocker (events carry the side) or a
  single key (side not physical), by the device's key layout. The
  API carries only raw `pressed`/`depressed` edges (`up_request` /
  `down_request`), but the *device* sends gestures: vendor property `0x5012
  KEY_EVT` `[counter][event]` (0/1 click down/up, 2/3 hold-start, 4 release,
  5 click / 6 hold-start on single-key elements), which the gateway flattens
  (`services/btmesh_property_service.js:184-256`) — a click becomes a
  synthesised `[1, 0]` pair (~0.4 s: two 200 ms delays in the emitter loop).
- **On current DEVICE firmware, one tap is reported as TWO press/release
  pairs; a hold as ONE.** Mechanism (2026-09-15 audit): device fw 2.2.0.2
  (shipped by app 2.2.0) publishes every access message **twice, ~1 s apart,
  fresh SEQ, same counter** — an on-air capture by the Bluetooth-direct
  sibling project (`junghome-bt-mesh`), CDB publish-retransmit 0 everywhere,
  so not BT-Mesh retransmission and not configuration. The gateway ignores
  the counter (`Number(values[1])`), has no dedupe, and turns each copy of a
  click into a `[1, 0]` pair; on a rocker a hold's second copy is
  value-and-mode-unchanged and suppressed. Side: rocker elements (events
  0/1) carry the side in the event byte, so both copies hit the **same
  channel** (the 2026-08-02 rocker capture); single-key elements (events
  5/6) get the side toggled per reception via one service-wide
  `_prevButtonType`, so their two copies **alternate `up`/`down`** (on-air
  capture by the sibling; a key-element *hold* through the gateway is
  uncaptured and by the code differs). Labelled capture (2026-08-02, 16 taps + 5
  holds): tap pulse 0.40–0.53 s (the gateway's synthesised release, not the
  finger and not "device granularity"), hold pulse 2.44–3.11 s (the finger),
  intra-burst gap 0.11–1.03 s. Single vs double click is
  **indistinguishable** (both = 2 identical pairs, overlapping gap ranges);
  tap vs hold separates perfectly on **pulse width** (5× empty band). **This
  is a regression**: the gateway's own archived logs (2026-06-20→07-28, ~450
  bursts) show 1.00 presses/burst on the same buttons; gateway fw unchanged
  across the window, JUNG app went 2.1.0→2.2.0 (app 2.2.x updates device
  firmware — issue #66). Gesture logic must tolerate both one and two pairs
  per tap. A duplicate-suppression window must be **≥ ~1.2 s and per
  device, not per datapoint** (a key element's second copy lands on the
  other datapoint; earlier 0.15–0.25 s guidance came from a mis-segmented
  unlabelled capture — refuted). Evidence + tables in
  docs/gateway-websocket.md.
- **Button gestures are derived in `event.py`, and duplicates dropped there
  (settled 2026-09-16).** Every entity re-fires the raw `pressed`/`depressed`
  edges and adds `click` (release before `BUTTON_HOLD_THRESHOLD` = 1.0 s),
  `hold_start` (an `async_call_later` timer at the threshold; cancelled on
  release/unload/unavailable) and `hold_end` (at the release; if the release
  was never reported, on the next edge on that side — the pair is always
  balanced). Duplicate suppression is **on by default**
  (`CONF_SUPPRESS_DUPLICATE_PRESSES`, options flow): after a click, the next
  press on the same **device** — a `ButtonGestureTracker` shared by both
  sides — within `BUTTON_DUPLICATE_WINDOW` = 1.2 s is dropped with its
  release, edges included; a dropped press still down at the hold threshold
  is reinstated as a hold (a copy is a ~0.4 s pulse). Tap vs hold is pulse
  width only; there is **no double-click detector** and none is possible over
  the API — the blueprint's `double_action` is a legacy opt-in for
  pre-2.2.0 device firmware with suppression turned off. A **hold on a
  single-key element** can be copied to the *other* side (captured
  2026-09-16, 1 of 4 holds: press, other-side press +1.4 s, the finger's
  release on the copy's side, the first side never released): a press on the
  other side of a device whose one side has been down 0.6–2.5 s
  (`BUTTON_HOLD_COPY_AFTER`/`BUTTON_HOLD_COPY_WINDOW`) is dropped as that
  copy and its release completes the hold on the side that is down — one
  `hold_start`/`hold_end` pair, on the held side. The held side's own release
  ends it just as well (the toggle is one field shared by every button, so an
  unrelated key pressed mid-hold flips it back); the copy's marker then does
  not outlive the hold — a genuine press on the copy's side clears it and a
  late copy release is dropped whole. A "press while down" still restarts
  the measurement for any shape that slips past.
- **Every button element exposes BOTH `up_request` and `down_request`**, even
  a single-key one: the firmware's `JungHome_PushButton` model always creates
  PushedUp + PushedDown + StatusLed states (`trigger_request` exists only in
  the datapoint descriptor — no device model produces it). The gateway knows
  the difference — property `0x5003 KEY_MODE`: 0 light, 1 blinds, 2 scene,
  3 property, 4 thermostat, 5 switch, 6 gateway
  (`models/jung-home-state-mode.js:39-47`; mode 6 publishes `0x5012` to the
  gateway's group, the other modes act on the mesh directly) — but never
  exposes it: the function assembly
  (`createFunctionListByDevices`) maps only `device.states` into datapoints,
  and KeyMode is a *property* state — exposed only by the deprecated
  `GET /devices/?verbose=true` (bullet below). (Its `manufacturer_property` category is
  not the reason — PushedUp/PushedDown/StatusLed are `manufacturer_property`
  too, with explicit cases in `getDatapointTypeByState`; an earlier revision
  of this file said otherwise.) JUNG's own code carries a `// TODO: set
  visibility here based on mode` for exactly this. So a single-key element
  unavoidably exposes two event entities for one key — and since the gateway
  toggles the side on every event-5 reception, *both* fire (alternately) on
  doubled firmware; neither is physically "up" or "down". Multi-gang panels
  report each gang as a **separate function**, hence a separate HA device.
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
- **Colour temperature: the gateway clamps to the device's own range, and
  the light declares that range.** `ColorTemperatureState.publishValue`
  clamps every tunable-white write to the state's `profile.range`
  (`models/device_states/ColorTemperatureState.js:94-103`); 2000–6000 K is
  only its constructor default (`:60`). The middleware reads the node's
  Light CTL Temperature Range (`ColorTemperatureStateRange.js`, state
  `color_temperature_range`) and binds it into that profile range
  (`services/device_state_service.js:664-687` →
  `fromState_ColorTemperatureRange`, `:190-197`). `/functions/` carries
  neither (`getDatapointTypeByState` maps the range state to `null`), but
  the verbose endpoint does: `models.color_temp_range` reads
  `states.color_temperature.profile.range` (the effective clamp — probe:
  2000–6000 on all four lights) into `DeviceProperties.color_temp_range`.
  `light.py` reads it **live** (`_kelvin_range`, falling back to
  `DEFAULT_MIN/MAX_KELVIN` 2000–6000 K when unknown or implausible —
  outside the spec's 800–20000 K), so a range read after the entity exists
  reaches it on the properties refresh's listener dispatch; min/max and
  both clamp directions (`_clamp_kelvin`) use it. The `/types/datapoints`
  catalog's 2000–10000 is a descriptor, not the enforcement. The write
  path is conditional (`:107-122`): the CTL-Temperature state when the
  device has one, else Generic Level on element+1.
- **The WS handshake's `version` frame is the API version, not the firmware.**
  It carries `api-junghome`'s own package version (`"1.5.0"`, matching
  `apidoc.json` `info.version`); the gateway's *software* version is the
  middleware's `version` topic (`version_release` + `version_build`, live
  values `"2.1.3 Release"` / `"2840"`, populated from the board controller's
  `MSG_SW_VERSION_IND`), read from the **unauthenticated** `GET /version/`
  reply, which carries both next to `api_version` (one token-less request;
  the two `config/parameter` reads it replaced returned the same values). The two were conflated, so every device page showed
  `1.5.0` as its `sw_version`. `coordinator.api_version` holds the former
  (diagnostics only); `gateway_version` holds the latter and is what reaches
  `DeviceInfo`. The state DB's defaults `"0.0.0"`/`"0"` mean "not read yet".
- Scenes arrive over the WS `scenes` broadcasts (plus a setup-time REST fetch)
  and recall over REST `POST /scenes/{id}` — the WS `scene` *command* is
  unimplemented on the gateway. Scene identity is the **label**; recalls
  re-resolve the id at call time. (The scene `id` is in fact derived —
  `"id"` + hex(mesh scene number), `id0001` ↔ `value` `"0001"` — so it is
  stabler than the device ids; the label-keyed design stays for existing
  installs' `unique_id`s, and the tracker lists `value` as a candidate
  stable join key.)
- The gateway lists **unreachable devices too** (no `isOnline` filter in the
  firmware's function assembly) — absence from `/functions/` means
  deleted/relabelled or a partial poll, which is why the pruner debounces
  `STALE_DEVICE_PRUNE_MISSES` polls before removing anything.
- **`GET /devices/?verbose=true` (deprecated/experimental in the OpenAPI,
  probed live 2026-09-16 on 2.1.3/2840, 49 devices, 187 KB) returns the raw
  middleware device objects** — everything `/functions/` drops. Per state:
  `statistics.reachable` / `last_seen` / `connection_quality` (per-device
  reachability at last — 19 of 49 devices were unreachable at probe time),
  `profile.index` (the datapoint suffix in hex: `input_power` idx 16 =
  `-010`), `model.address` (element unicast). Per device `property`:
  `total_device_energy_use` in **Wh** and `total_device_power_on_time` in h
  on `SocketEnergy` (cumulative energy the README says is missing — it is a
  property, never a state, so `/functions/` cannot carry it),
  `software_revision` `[2, 2, 0, 2]` on every push button (device firmware
  2.2.0.2 confirmed per device; `[2, 2, 0, 1]` on lights — a per-device
  doubled-firmware detector for the duplicate-suppression default), `key_mode`
  (6 = gateway on 19 of 20 buttons), `switch_operation_mode`,
  `device_key_lock`. No cover in the network, so `move_operation_mode` is
  still unverified. Raw sample: `disk_dump/devices-verbose-20260916.json`
  (gitignored — labels). Reference in docs/gateway-rest-api.md. **Read by
  the integration** (`models.parse_devices_verbose`,
  `coordinator.async_fetch_device_properties`): the full list once after the
  first refresh (and again only when a function appears that the last answer
  did not list — an omitted function is not re-asked, a missing endpoint or
  failed read is retried each interval), then
  `GET /devices/{id}?verbose=true` (~8 KB) per energy device every
  `DEVICE_PROPERTIES_REFRESH_INTERVAL` = 300 s. Drives the `total_energy`
  sensor (Wh, `TOTAL_INCREASING`, `sensor.<socket>_total_energy`) and the
  per-device duplicate-suppression exemption
  (`button_reports_each_tap_once`: revision known AND < 2.2.0) and each
  tunable-white light's Kelvin range (colour-temperature bullet above). Reachability
  is diagnostics-only — availability semantics are a settled decision.

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
- [docs/bt-mesh-direct.md](docs/bt-mesh-direct.md) — how the gateway talks
  to the devices on the mesh (model map, vendor property opcodes, its own
  node role); the working gateway-free client is the sibling project
  `junghome-bt-mesh` (Mesh Proxy client). `tools/bt-mesh-direct/` holds stale
  EFR32/ESP32 sketches (v2.0.0 send path, no vendor models).
- [docs/matter-bridge.md](docs/matter-bridge.md) — Matter options.

## Key behaviours to preserve

- **Stable identity.** Device/datapoint `id`s have been observed to change
  across app-driven firmware updates, so entity `unique_id`s and device
  identifiers derive from the device **label** + datapoint **suffix**
  (`stable_unique_id`), never the raw id. The ids are not random: per the
  2026-09-15 audit a device id is `"id"` + `md5(node UUID + hex(location))[:15]`
  and a scene id is `"id"` + hex(scene number) — so an id changes whenever a
  node is re-provisioned (new UUID) or its location/element mapping is
  re-enumerated. **Measured across the app 2.1.0 → 2.2.0 device-firmware
  update** (the June and August dumps of the same card, 2026-09-16): the
  update itself changed nothing HA keys on — 26 of 26 surviving nodes kept
  UUID and MAC, all 30 kept labels kept their datapoint suffix sets and
  function types, and every id that did change belonged to a label the user
  moved to another node in the app (7) or to swapped hardware (4 nodes out,
  4 in). Six devices were renamed in that window — the one event the
  label-keyed design turns into a new HA device (old one pruned, history and
  customisations not carried over — **no longer**: renames are followed, next
  bullet). The gateway *does* expose
  hardware identity on fw 1.5.0+ (`GET /project/junghome`: node UUID / MAC /
  unicast / locations — tracker §3), but the label-keyed design stays.
  Don't reintroduce id-based identifiers. That export IS read at setup
  (`coordinator.async_fetch_node_identities`, `models.parse_project_export`,
  re-read debounced when a function has no identity) and attached as
  `serial_number` on every function of the node plus a `CONNECTION_BLUETOOTH`
  connection on the node's primary-element function ONLY — the registry also
  resolves devices by connection, so a node-wide connection would merge a
  multi-gang push-button into one HA device. **The connection is written by
  the coordinator after registration** (`link_node_identity` from
  `async_added_to_hass`, `apply_node_identities` on identity resolution and
  after a device removal), **never through `device_info`**, and only when no
  other live device of the entry holds it: a connection in `device_info` made
  a relabelled function's new slug resolve to its OLD device by connection
  and merge (old entity live forever, pruner never fired — the b8
  regression). Never as an identifier. The identities (UUID/MAC/unicast) are
  deliberately not redacted in diagnostics. The device's Bluetooth
  connection is **replaced**, never added to, when the node behind a label
  changes (a swap under the same name used to keep both addresses).
- **Renames are followed, not replaced (2026-09-16).** A function renamed in
  the app keeps its HA device: `coordinator.follow_renames` runs on every
  device-list adoption *before* the listeners (so before discovery), and on
  setup's second pass after the identities are read. It keeps
  `function_anchors` (slug → function id + node MAC + element location,
  `models.FunctionAnchor`, persisted per entry in
  `.storage/junghome.<entry_id>.functions`) and treats a slug with no
  registry device whose element carries a vanished slug's anchor as a
  rename: the device identifier, every entity `unique_id` (prefix rewrite,
  all-or-nothing after checking each target is free and claiming the new
  identifier first — before HA 2026.9 another gateway's device may hold it),
  the discovery `known` sets, the area assigner's once-only record and the
  `inverted_covers` option (snapshot included, so no reload) are rewritten
  in place; entity ids stay. The function id does not change on a
  rename (it is `md5(UUID + location)`), which is the id-based half; MAC +
  location pairs a rename combined with re-provisioning, but only at setup
  (live, the new id has no identity yet — it becomes a new device).
  Deliberately not followed: a label moved to another element (name reuse,
  swaps — entities follow the name), colliding slugs, and a target
  `unique_id` already taken (warned, left as a new device). The old contract
  ("a relabel is a new device") survives only for those; the manual delete
  and the pruner cover them. Tests: the "Rename following" section of
  `tests/test_init.py`.
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
  a device's datapoint-type set changes — once the new set has been seen on
  two consecutive adoptions — so features are rebuilt (the
  tilt-lost-after-update regression). Gate capabilities on datapoint
  *presence*, never on the function-type name. That reload (like the
  id-churn one) starts *inside* an adoption and runs eagerly: every
  `_discover_*` listener returns early while the entry is
  `UNLOAD_IN_PROGRESS` (`entity.entry_unloading`), and the platforms add
  without `update_before_add` — either gap put a device new in that list on
  the dead coordinator (an orphan frozen at its first state).
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
  #120); entities whose function needs the socket — controllables and
  button events — additionally require the live WS because commands only go
  out over it and button edges only arrive over it.
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
- CI: `test.yml` (pytest + mypy, strict via `pyproject.toml`), `lint.yml` (ruff, pinned),
  `validate.yml` (hassfest + HACS), `floor.yml` (imports the integration
  against the `hacs.json` minimum HA on every branch — a floor break means
  *raise the floor*, not block the release, unless the missing name is
  type-only, which goes under `TYPE_CHECKING` as `repairs.py` does),
  `release.yml` (tag-gated on all checks). Coverage
  gate: 95 % branch (`.coveragerc`). Renovate owns pip (the
  pytest-homeassistant-custom-component stack moves as one group and is
  version-capped); Dependabot deliberately does not watch pip.
- Tests: one file per platform plus flow/coordinator/websocket/init/blueprint/
  translations/device-trigger/diagnostics/models/project-export/tls/const
  files; new platform behaviour goes in that platform's file. Uses `pytest_homeassistant_custom_component` (`hass`
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
- **Only unknown sensor labels stay untranslated.** The known quantities
  (`QUANTITY_DESCRIPTIONS` in `sensor.py`: power, energy, voltage, current,
  frequency, temperature, illuminance, present illuminance — the BWM's
  ambient reading, its own key — and humidity, matched on unit AND label)
  are named via `entity.sensor.<key>` translation keys whose English text is
  the gateway's own label, so nothing changes for English installs; any other
  label is user-authored app data with nothing correct to translate it to and
  keeps today's raw name. Voltage/current/frequency register disabled by
  default; a correlated `error:` reply raises `command_rejected` with the
  gateway's text.
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
  per-key, not a list replacement. `ws_last_frame_by_type` keeps the known
  frame types in full and caps unknown ones (`WS_FRAME_TYPES_MAX`, truncated
  previews) — a peer minting types must not grow the diagnostics dump.
- **`climate.set_temperature` ignoring `target_temp_low/high`** is correct
  for a single-setpoint regulator.
- **ruff `target-version` stays `py313`** — bumping to py314 flips
  TC001/TC002/UP037 semantics (PEP 649 lazy annotations) and would churn every
  module for zero behavioural gain; revisit when HA core moves.
- **Groups carry no colour-temperature range — the group parser is gone.**
  `color_temperature_range` in a `groups` frame's `function_types` is only
  a member state's *type name*: the list is the set of visible state types
  of the group's members (`services/groups_service.js:37-58`), and a group
  (`models/jung-home-group.js`) has no other field. No capture carries a
  value. The per-device range comes from the verbose endpoint instead (the
  colour-temperature bullet above).
- **The three broad excepts in `coordinator.py` stay broad** (reconnect loop,
  frame-handler catch-all, WS send path) — wontfix. Each is load-bearing
  containment: the reconnect loop must retry through *any* failure class, a
  malformed frame must never tear down a healthy session, and every send
  failure must surface as `cannot_send` to the calling service. The
  narrowing that mattered was already done in PR #133 (best-effort
  fetch/parse handlers); narrowing these three trades crash-risk for no
  diagnostic gain.
- **Per-device availability is NOT derived from the verbose endpoint's
  `statistics.reachable`** (closed 2026-09-16). That flag is per *state* and
  means "the last request for this state was answered"
  (`models/device-states.js:596-619`: true on any success, false after
  `state_acceptable_request_fails` failed requests, which also resets the
  value to `NaN`). Push buttons never answer requests for their key states,
  so 13 of the network's 20 mains-powered buttons read "unreachable" while
  working perfectly (probe of 2026-09-16; `hasBattery` was false on all 49
  devices, so it is not a sleepy-node effect). For actuators the same
  mechanism already reaches the integration for free: the reset writes
  `"NaN"` into `/functions/` and every push, which the platforms show as
  unknown (`const.py` `datapoint_value` note). Nothing the deprecated
  endpoint adds is worth an availability rule that flags buttons.

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
`ws_connected` may gate socket-dependent entities (controllables, button
events), never grant availability. A flow-side "did the listener reload?"
check reads `entry.state` BEFORE `async_update_entry` (listeners dispatch
eagerly — the zeroconf bullet seen from the other side).
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
coherent. **Run the suite on a real floor venv**, not just the import check:
no `pytest-homeassistant-custom-component` release pins the floor, so install
the one pinning the nearest older core (0.13.300 for 2025.12.4) and then
`pip install --only-binary litellm homeassistant==<floor>`; the only expected
floor-only failures are the eight `test_all_*_entities` snapshots
(`aliases: list([None])` vs `set({})`, a core-owned serializer delta).
Anything else is a finding — a name that exists only on newer cores
(`RepairsFlowResult`, HA 2026.6) shipped through eight betas because
`floor.yml` ran on `main` only and never on the integration branch.

**Report.** Severity-ordered; each finding with evidence, failure scenario
and proposed fix. Close with an explicit verdict: the list of open P0–P2s,
or "clean — nothing above P3 survived verification."

## Backlog (open, in rough value order)

- **Audit tracker** — `docs/cross-repo-analysis.md` (2026-09-15) keeps the
  facts the cross-repo audit settled (the mechanism of the double-reporting
  rockers: gateway synthesises the release; device fw 2.2.0.2
  double-publishes; key elements alternate sides — plus the gateway's mesh
  role, opcodes, timers) and what is still open: the hardware verification
  below, a scene-`value` join key, code sharing with the sibling project,
  and the captures (§5) that would close the remaining questions. Every bug
  and doc correction it raised has landed. Prefer it over re-deriving those
  facts.
- **Cover travel states** — less unblocked than it looked: a composed
  `level` datapoint carries a `level_move` value (−1/1/0) derived from
  current-vs-target (`PositionState.fromMeshMessage` computes mode
  opening/closing/stopped), but per the audit `extractTargetValue` slices
  the *last two octets* of the status parameters, which makes `level_move`
  **structurally always 0** — `is_opening`/`is_closing` cannot be read from
  `level` pushes as they stand. No cover exists in any capture or in the
  reference network. Still needed before building anything: a capture of a
  blind actually moving (`tools/ws-capture/capture_ws.py capture --script
  cover` — note its script drives an API move whose `level` reports the
  *target* for ~4 s, so read the result with that in mind), to learn whether
  intermediate `level` pushes stream during travel.
- **Button gestures — upstream report.** The rebuild is verified on hardware
  (2026-09-16, three rocker elements and two single-key elements — the live
  verification table in docs/gateway-websocket.md): rocker taps/holds/double
  taps behave exactly as modelled, key-element taps alternate sides, and the
  copied key-element hold was captured once in four and is now handled.
  The upstream report is drafted —
  `docs/upstream-report-button-double-reporting.md`, ready to send to JUNG
  (the fix at source is a one-line counter dedupe in the gateway's
  `btmesh_property_service.js`, which ignores the `0x5012` counter byte, or
  the device firmware's double publication) — sending it is the user's
  call. Optionally, more key-element hold samples to learn why three of four
  carried no copy (the §5 mesh capture would settle it).
