# Cross-repo audit & work tracker (2026-09-15)

Result of a parallel audit of this integration, the sibling **Bluetooth-direct** project (`junghome-bt-mesh`: its own
mesh stack `jhmesh` + HA integration `junghome_ble`, talking to the devices through any node's GATT proxy) and the
gateway firmware dump. This file tracks the work that came out of it for *this* repo; the sibling project keeps the
full protocol comparison and its own list in its `docs/cross-repo-analysis.md`. Tick items as they land.

Firmware citations use the `disk_dump/jung-20260801/sdb2` convention from `CLAUDE.md` (`MW` =
`sdb2/opt/middleware/dist`, `BT` = `sdb2/opt/bt_tunnel/lbc-gw-bt-tunnel_pi-zero`). `sdb2` was verified byte-identical
to the June `jung/sdc2` extraction; only the data partition differs.

Health at audit time: 453 tests + 35 snapshots pass, 98.56 % branch coverage, ruff + mypy clean, `main` @ `54266c8`
clean. Legend: `[ ]` open · `[x]` done. Wave 1 (2026-09-15) landed on branch `audit-2026-09-15`: 473 tests, ruff/format/mypy clean.

---

## 1. Facts the audit settled (update the docs, §4)

### 1.1 The "two press/release pairs per tap" regression — mechanism established
`MW/services/btmesh_property_service.js:186-219` handles vendor property `0x5012 KEY_EVT` (`[counter][event]`):

| event | gateway output | `buttonState` |
|---|---|---|
| 0 / 1 | `pushed_down` / `pushed_up` | `[1, 0]` — **the release is synthesised by the gateway** |
| 2 / 3 | `held_down` / `held_up` | `[1]` |
| 4 | `released`, side = `prevButtonType` | `[0]` |
| 5 | `pushed`, side = **toggle of `prevButtonType`** ("for downwards compatibility") | `[1, 0]` |
| 6 | `held`, side toggled | `[1]` |

- The device emits *gestures* (click / hold-start / hold-end), not edges; the 0.40–0.53 s tap pulse is the gateway's
  own `[1,0]` timer, not "device reporting granularity".
- Device firmware 2.2.0.2 (shipped by app 2.2.0) publishes **every access message twice, ~1 s apart, fresh SEQ,
  same counter** (seen on air by the sibling project for button events *and* OnOff statuses; CDB publish-retransmit is
  0 everywhere, so it is not network retransmission and not configuration). The gateway **ignores the counter byte**
  (`Number(values[1])`) and has no dedupe (only scene status has a 1 s debounce, `MW/handler/bt_event_handler.js:237-251`)
  → each copy of a click re-triggers `[1,0]` = two pairs; a hold's second copy is state-unchanged → suppressed = one pair.
- `_prevButtonType` is **one field on the service, shared by all buttons**. For event 5 (single *key* elements, layout 0)
  the two copies therefore land on **alternating sides** (`up` then `down`); rocker halves (events 0/1) repeat the
  same side. So "same channel" (the 2026-08-02 capture) and "alternating" are both right, per element type.
- Upstream report material: a one-line counter-based dedupe in the middleware would fix it.

### 1.2 Other gateway facts
- Radio = Silicon Labs EFR32 NCP (Mesh SDK 4.4.6) over UART; `bt_tunnel` is the UART↔NCP BGAPI bridge exposing
  `/tmp/lbc-bt-tunnel.soc` (**not** an app tunnel — `gateway-architecture.md:57` is wrong, `gateway-system-analysis.md:50`
  is right). Pi onboard BT/WiFi disabled.
- The gateway is an **ordinary node** (`0x00DC`, pid `0x0B`) provisioned by the phone; not a provisioner, not a Config
  Client (only local `test_*` BGAPI calls, `MW/services/ncp_service.js:299-351`). Its element group is `C005`; buttons
  in KeyMode 6 publish `User Property Set Unack` (`D0 27 05`) there.
- Vendor opcodes (from `BT` .rodata): Admin `C0–C5`, Manufacturer `C6–CB`, User `CC–D1` (ListGet, ListStatus, Get, Set,
  SetUnack, Status) + `27 05`. Payload `pid u16 LE` + `[access u8]` (Admin/Mfr only) + value.
- Status LED = `0x5013 KEY_STATUS` (1 byte), written with a User Property **Status** (op 0x11) to the button element.
- `KEY_MODE 0x5003`: 0 light, 1 blinds, 2 scene, 3 property, 4 thermostat, 5 switch, **6 gateway**.
- Colour temperature: `ColorTemperatureState.js:110-122` uses the CTL-Temperature state when the device has one, else
  Generic Level on element+1. 2000–6000 K is a middleware clamp; `cdb_types_datapoints.json` allows 2000–10000.
- State acquisition = self-config subscribes client models to every element group (`self_config_service.js:104-158,279-346`)
  **and** polls every 15 s (`config.json device_state_poll_interval_sec`). `generic_client_set` uses flags=1 (Silabs
  "response required" → acked Set); devices answer only with the group publication (~1 s), which explains command
  confirmation latency.
- Time: `publish_time_interval_minutes = 0` → the gateway never publishes Time; only the phone sets device clocks.
- Sequence number persisted hourly (`seq_number_service.js`), 0x9FC000 (June) → 0xA68000 (Aug) → ~0xB0D6xx (Sep) =
  9–18 k msgs/day; IV-update request (< 128 reboots-worth left) is ~6–12 months out.
- Security: the app obtains the gateway's API token / IP / TLS fingerprint **over the mesh** from vendor props
  `0xC001–0xC003` (`btmesh_property_service.js:38-45,151-170`) readable by anyone holding AppKey 0; `GET /project/cdb`
  returns the CDB including keys.
- Cross-mapping to the sibling project: gateway function = one mesh element; group `id` = `"id"` + decimal group
  address; scene `value` = mesh scene number; `GET /project/junghome` (fw 1.5.0+) exposes node UUID / MAC / unicast /
  locations.
- Matter: nothing implemented in this firmware (`sdb2/opt/matter-interface/` empty).

---

## 2. Bugs

- [x] **P1** `custom_components/junghome/config_flow.py:501-518` — reauth on an entry whose *setup* failed with 401
  updates the token but never reloads: `async_update_and_abort` only updates data, and the update listener is
  registered (`__init__.py:480`) *after* the first refresh that raised `ConfigEntryAuthFailed` (`coordinator.py:382-386`).
  Entry stays `SETUP_ERROR` until a manual reload. **Reproduced** with a probe test. Mirror `async_step_reconfigure`
  (`config_flow.py:605-611`): `if entry.state is not LOADED: async_schedule_reload(...)`. Add a test that sets up with
  a 401, completes reauth, asserts `LOADED`. `quality_scale.yaml` `reauthentication-flow: done` is over-claimed until then. *(landed `4c1ccea`; the same commit fixes reconfigure double-reloading a loaded entry — the listener dispatches eagerly, so "did the listener reload?" must be read before `async_update_entry`)*
- [x] `entity.py:60,97-99` — event entities are available on the REST poll alone but edges only arrive over WS →
  silently deaf while the socket is down; the blueprint's unavailable guard (`button_gestures.yaml:139-153`) never
  engages. Gate `JungHomeEventEntity` availability on `ws_connected` (rename the flag to `_needs_websocket`). *(landed `33845e6`: flag renamed `_needs_websocket`)*
- [x] `scene.py:50-58,103-111` — a scene deleted in the app is removed with `Entity.async_remove()` but its registry
  entry survives → permanent `unavailable` entity (docstring says the opposite; `tests/test_scene.py:90-95` accepts
  the wrong outcome). Call `er.async_remove(entity_id)`; assert it is gone. *(landed `66c9a8e`)*
- [x] `coordinator.py:42-59,930-957` — `websocket_push_failure` repair fires after 5 failures ≈ 15–20 s of backoff,
  i.e. on every ordinary gateway reboot (~2 min), then self-clears. Escalate on elapsed outage (≥ ~90 s) instead. *(landed `09b5daa`: `WEBSOCKET_OUTAGE_REPAIR_AFTER` = 180 s, not 90 — the backoff quantises attempts at ~63/123/183 s, so ≤123 s still fires inside a Pi-Zero reboot)*
- [x] `coordinator.py:1026-1036,1183-1192` — any frame carrying our `message_id` resolves the pending command as
  success (including a correlated `error:` frame), and dict-data frames of unhandled types (`config`) are routed as
  datapoint pushes → ERROR "without datapoint_id". Resolve only on `type == "datapoint"`, `set_exception` on `error:`. *(landed `09b5daa`; correlated `error:` raises `invalid_response` — a dedicated `command_rejected` key is a follow-up across 26 locales)*
- [x] `__init__.py:104-190` — capability watcher reloads on the first signature change with no debounce; a datapoint set
  that flaps between adoptions causes a reload per adoption. Require two consecutive identical signatures. *(landed `33845e6`)*
- [x] `light.py:304-336` — optimistic writes overwrite the gateway-confirmed value the awaited reply already merged
  (`_set_color_temp` stores 6500 K after the gateway clamped to 6000). Clamp before sending; re-read the confirmed
  datapoint after the awaited command. *(landed `66c9a8e`)*
- [x] `diagnostics.py:213` — hub device diagnostics emit raw identifiers containing the anchor (host / mDNS name /
  serial) that `TO_REDACT` scrubs elsewhere. No test covers hub diagnostics. *(landed `66c9a8e`)*
- [x] tests `tests/test_init.py:222-262,1919-1925` — the pruner's hub protection (`__init__.py:234`) and empty-poll
  guard (`:229-230`) are executed but never asserted; deleting either keeps the suite green. Assert the hub survives 10
  adoptions and that `data=[]` prunes nothing. *(landed `33845e6`)*
## 3. Improvements

- [ ] **Button handling for double-reporting firmware** (backlog item) — with §1.1 established: suppression must be
  **per device**, not per datapoint (key elements alternate `up`/`down` across the two copies); window ≥ 1.2 s;
  derived `click`/`hold` on pulse width; keep `double_action` for old firmware. The blueprint's 400 ms window
  (`button_gestures.yaml:170-194`) cannot work against 0.11–1.03 s gaps. Verify on a key element *and* a rocker
  element (they differ). Include the counter-dedupe suggestion in the upstream report.
- [ ] `entity.py:121` — `via_device` tuple is deprecated in HA 2026.9 (removed 2027.8, `device_registry.py:270`);
  switch to `via_device_id` when the floor allows (≥ 2026.8).
- [ ] Hardware identity — `const.py:299-334` says the gateway exposes none, but `GET /project/junghome` (fw 1.5.0+)
  carries node UUID / MAC / unicast / locations (the same `ExportDto` the sibling project parses with `jhmesh.cdb` /
  `jhmesh.devices.Metadata`). Add `connections={(bluetooth, mac)}`, `serial_number`, and a stable join key; scene
  `value` (mesh scene number) is a stabler scene key than the label. Keys inside the export must never reach logs or
  diagnostics.
- [ ] `sensor.py:136` — sensor names are raw gateway labels (untranslatable); use `SensorEntityDescription` with
  `translation_key`, `suggested_display_precision`, and `entity_registry_enabled_default=False` for noisy diagnostics
  (voltage/current), as the sibling does (`sensor.py:25-42` there).
- [ ] `manifest.json` — add `loggers` (relevant once `jhmesh` is shared).
- [ ] mypy config in-repo (`[tool.mypy] strict = true` + overrides) instead of CLI-only (`test.yml:33`).
- [ ] `__init__.py:46-47`, README "10 consecutive polls" — the debounce counts `data_generation`, which `functions`
  broadcasts also bump; reword or count polls only.
- [ ] `logbook.py:28` "was recalled" is untranslated English.
- [ ] `tools/ws-capture/capture_ws.py:15-17,459` still carries the refuted "sibling-channel echo" model; the `cover`
  script measures an API-driven move whose `level` reports the *target* for ~4 s, so as written it would answer the
  cover backlog wrongly.
- [x] `const.py:302,318`, `models.py:45`, `scene.py:4-5`, `CLAUDE.md:127-129` — the "ids regenerate / no hardware id"
  rationale is contradicted by the firmware (device id = `"id"+md5(UUID+hex(location))[:15]`, scene id = `"id"+hex(scene no.)`);
  the stable-id design stays correct, the rationale needs rewording. *(CLAUDE.md/README reworded in `887e0f8`; code comments still to follow)*
- [ ] Share code with the sibling project long-term: `jhmesh` `Metadata`/`CDB.parse`, and the cover/climate/trigger/
  logbook/diagnostics boilerplate. HA ≥ 2026.8 binds a device to one config entry, so one integration with two
  transports is the only way to get one device page.

## 4. Documentation corrections

- [x] `docs/bt-mesh-direct.md` — largely superseded: `:8-10,27-39,175-176` add the GATT-proxy-client route (plain BLE
  adapter / ESPHome proxy, no mesh chip; working in the sibling project) and mark the EFR32/ESP32 sketches
  unnecessary; `:16-19,168-169` replace "continue the gateway's sequence number" with "own unicast outside every
  `allocatedUnicastRange` and `networkExclusions`, own sequence counter, only the IV index must match, follow Secure
  Network Beacons"; `:53,122-147` real vendor opcode table + framing + button event `D0 27 05` prop `0x5012
  [counter][event]` to `C005`, drop "re-route publications with device keys", correct "0x1013 client model" (it is the
  User Property *Server*); `:61-69` vs `tools/bt-mesh-direct/junghome_mesh.py:43-46,90-107` (prototype still blasts
  3 × 15 ms from v2.0.0 — update or delete); `:105-107` CT range assumption (`0x8262` Temperature Range) and the app's
  `Light CTL Set` 2000–10000 K; `:144-147` button events are Set-Unack not Status; add "acked Sets get no unicast
  reply, only a doubled group publication". *(landed `887e0f8`)*
- [x] `docs/gateway-websocket.md:216-294` and `CLAUDE.md:59-73` — replace "mechanism unestablished" with §1.1; correct
  "no native click/hold" (`:220`) and "device reporting granularity" (`:266-270`); keep the ≥ 1.2 s guidance but make
  it per device; re-examine `:88-92` (scene-frame duplicates are most likely the same doubling — TID capture pending). *(landed `887e0f8`)*
- [x] `CLAUDE.md:74-82` / `docs/gateway-websocket.md:219` — add the KeyMode table (0..6); reconcile "both up/down
  always" with `trigger_request` (a descriptor promise no device model produces); a `RockerSwitch` function = one mesh
  button element. `CLAUDE.md:77-79` "manufacturer_property never becomes a datapoint" is false (PushedUp/PushedDown/
  StatusLed are manufacturer_property with explicit cases; the real gate is `createFunctionListByDevices`). *(landed `887e0f8`)*
- [x] `docs/gateway-architecture.md:57` (bt_tunnel = UART↔NCP bridge), `:116-117,125` (node, not provisioner),
  `:129-130` (scenes/vendor models are reverse-engineered now). *(landed `887e0f8`)*
- [x] `docs/gateway-rest-api.md` — the app's own use of the API: `POST /config` bodies (`project_file`, cloud
  credentials, `api_client_accept`, `api_client_reset`, `ip_dhcp`…), the `GatewayConfigDTO` fields, token/IP/fingerprint
  over the mesh (`0xC001–0xC003`) and `GET /project/cdb` exposing keys — security note. *(landed `887e0f8`)*
- [x] `docs/matter-bridge.md:62-66`, `docs/README.md:15-18` — the gateway-free BT-Mesh route exists now. *(landed `887e0f8`)*
- [x] `CLAUDE.md` backlog — cover item: firmware evidence says `level_move` is structurally always 0
  (`extractTargetValue` slices the last 2 octets), so "half-unblocked" is optimistic; no cover exists in any capture. *(landed `887e0f8`)*
## 5. Captures that would close open questions

- [ ] Simultaneous ms-resolution capture: `tools/ws-capture/capture_ws.py` + the sibling's mesh sniffer — match each WS
  pair to one mesh copy (expect press at copy arrival, release at +0.4–0.5 s, second pair at copy 2).
- [ ] Same on a **key** element (event 5) to confirm the alternating side, and on a **rocker** element (events 0/1).
- [ ] Whether `Scene Recall` from a scene key is doubled (same TID) — settles `gateway-websocket.md:88-92`.
- [ ] Toggle `status_led` from HA while sniffing — confirm `0x5013` via User Property Status.
- [ ] A moving blind (cover backlog) — none exists in this network.
- [ ] Real RTR composition and the numeric ids behind `LBC_PROP_RTR_SCHEDULER_ENABLE_ID` / `_HVACMODE_DISPLAY_ID`
  (firmware table says `0x1246` / `0x120B`).
