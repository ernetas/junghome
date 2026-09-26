# Cross-repo audit & work tracker (2026-09-15)

Result of a parallel audit of this integration, the sibling **Bluetooth-direct** project (`junghome-bt-mesh`: its own
mesh stack `jhmesh` + HA integration `junghome_ble`, talking to the devices through any node's GATT proxy) and the
gateway firmware dump. This file keeps the facts the audit settled (§1, referenced from `const.py` and the capture
tool) and what is still open (§3, §5). The sibling project keeps the full protocol comparison and its own list in its
`docs/cross-repo-analysis.md`.

Firmware citations use the `disk_dump/jung-20260801/sdb2` convention from `CLAUDE.md` (`MW` =
`sdb2/opt/middleware/dist`, `BT` = `sdb2/opt/bt_tunnel/lbc-gw-bt-tunnel_pi-zero`). `sdb2` was verified byte-identical
to the June `jung/sdc2` extraction; only the data partition differs.

Status: every bug (§2) and every documentation correction (§4) the audit raised has landed — wave 1 on
`audit-2026-09-15` (2026-09-15), the rest on `phase2` and `fixes-b9` (2026-09-16); the release review of 2026-09-16
re-verified all of them against the dump. Legend: `[ ]` open · `[x]` done.

---

## 1. Facts the audit settled (now carried by the docs — see §4)

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
  `/tmp/lbc-bt-tunnel.soc` (**not** an app tunnel). Pi onboard BT/WiFi disabled.
- The gateway is an **ordinary node** (`0x00DC`, pid `0x0B`) provisioned by the phone; not a provisioner, not a Config
  Client (only local `test_*` BGAPI calls, `MW/services/ncp_service.js:299-351`). Its element group is `C005`; buttons
  in KeyMode 6 publish `User Property Set Unack` (`D0 27 05`) there.
- Vendor opcodes (from `BT` .rodata): Admin `C0–C5`, Manufacturer `C6–CB`, User `CC–D1` (ListGet, ListStatus, Get, Set,
  SetUnack, Status) + `27 05`. Payload `pid u16 LE` + `[access u8]` (Admin/Mfr only) + value.
- Status LED = `0x5013 KEY_STATUS` (1 byte), written with a User Property **Status** (op 0x11) to the button element.
- `KEY_MODE 0x5003`: 0 light, 1 blinds, 2 scene, 3 property, 4 thermostat, 5 switch, **6 gateway**.
- Colour temperature: `ColorTemperatureState.js:110-122` uses the CTL-Temperature state when the device has one, else
  Generic Level on element+1. The middleware clamps to the device's own Light CTL Temperature Range, read from the mesh
  and bound into the profile (`device_state_service.js:664-686`, `ColorTemperatureState.js:94-103,190-197`);
  2000–6000 K is only the constructor default (and what the reference fixtures report). `cdb_types_datapoints.json`
  allows 2000–10000.
- State acquisition = self-config subscribes client models to every element group (`self_config_service.js:104-158,279-346`)
  **and** re-reads stale states: a 120 s sweep (`device_state_service.js:41-46`) over dirty states only, one Get every
  15 s (`config.json device_state_poll_interval_sec` = the pause), a state dirty after `dirtyAfterSeconds × 2^retries`
  ≤ 3600 s without a report (`device-states.js:364-388`). `generic_client_set` uses flags=1 (Silabs
  "response required" → acked Set); devices answer only with the group publication (~1 s), which explains command
  confirmation latency.
- Time: `publish_time_interval_minutes = 0` → the gateway never publishes Time; only the phone sets device clocks.
- Sequence number persisted hourly (`seq_number_service.js`), 0x9FC000 (June) → 0xA68000 (Aug) → ~0xB0D6xx (Sep) =
  9–18 k msgs/day; IV-update request (< 128 reboots-worth left) is ~6–12 months out.
- Security: the app obtains the gateway's API token / IP / TLS fingerprint **over the mesh** from vendor props
  `0xC001–0xC003` (`btmesh_property_service.js:38-45,151-170`) readable by anyone holding AppKey 0; `GET /project/cdb`
  returns the CDB including keys — and so does `GET /project/junghome` (its `network` field is the same CDB, Base64).
- Cross-mapping to the sibling project: gateway function = one mesh element; group `id` = `"id"` + decimal group
  address; scene `value` = mesh scene number; `GET /project/junghome` (API 1.5.0+, i.e. gateway firmware 2.1.x)
  exposes node UUID / MAC / unicast / locations.
- Matter: nothing implemented in this firmware (`sdb2/opt/matter-interface/` empty).
- `GET /devices/?verbose=true` (probed 2026-09-16) returns the raw middleware device objects: per-state
  `statistics.reachable` (a cached snapshot from the state's last value change, not live), per-device `property` incl.
  `software_revision` (2.2.0.2 where read — only 2 of 20 buttons; 18 `null`), `key_mode` and, on `SocketEnergy`,
  `total_device_energy_use` in Wh (re-read hourly) — see `docs/gateway-rest-api.md`.
- Identity across a real device-firmware update (June 13 vs August 1 dumps, app 2.1.0 → 2.2.0): 0 of 26 surviving
  nodes re-provisioned (UUID and MAC kept), 0 datapoint-suffix or function-type changes on the 30 kept labels; the
  8 kept-label id changes were labels moved between nodes (7) or onto swapped hardware (1 of 4 new nodes); 6 renames.
  Names are not on the air: a 15 s BLE scan (2026-09-16) saw 28 mesh proxies, all nameless, all broadcasting the
  same 9-byte Network ID beacon — the only MAC → name source is the app's project on the gateway (`meta.devices[]`
  `mac_address`/`name`/`node_id`; CDB `nodes[].name`), which the middleware itself turns into function labels
  (`MW/services/devices_service.js:227`, renamed from the meta at `:128-149`).

---

## 2. Bugs

All nine landed on 2026-09-15/16 — the reauth reload for a never-loaded entry (`4c1ccea`, with the "decide who
reloads before the update" rule), event-entity availability on the WebSocket + capability-reload debounce + pinned
pruner guards (`33845e6`), scene registry removal + light optimistic writes + hub diagnostics leak (`66c9a8e`), the
outage-clock repair issue + reply correlation by frame type (`09b5daa`), and `command_rejected` with the gateway's
reason (`79f327c`). The git history carries the per-item rationale.

## 3. Improvements

Landed: per-device duplicate suppression + derived `click`/`hold` gestures (`be74dff`), `via_device_id` (#207),
hardware identity from the project export (`2448564`), sensor descriptions with translation keys (`79f327c`), mypy
config in-repo, the "10 consecutive polls" wording, the capture tool's cover caveat (`48e80d0`), and the id-rationale
rewording in every code comment (2026-09-16). `manifest.json` `loggers` was declined (it is for library loggers; the
package has none) and the logbook's English "was recalled" is a documented limitation (the logbook API has no
translation hook). Still open:

- [x] **Verify the gesture rebuild on hardware** — done 2026-09-16 on three rocker elements and two single-key
  elements (table in `docs/gateway-websocket.md`); the copied key-element hold was captured once in four and
  `event.py` now completes such a hold with the copy's release. *Still open:* the upstream report (a one-line counter
  dedupe in the gateway's `btmesh_property_service.js`, which ignores the `0x5012` counter byte) — tracked in
  `CLAUDE.md` → Backlog.
- [x] Scene `value` (the mesh scene number) as a join key for tooling — it is in every `scenes` frame and the
  diagnostics dump keeps `coordinator.scenes` raw, `value` included; nothing more to build (the label-keyed
  `unique_id`s stay).
- [x] Renames followed (2026-09-16): `coordinator.follow_renames` pairs a vanished label with a new one on the same
  element (function id, or node MAC + element location from the export) and rewrites the registry in place — the
  one churn the June→August measurement found. Per-device availability from the verbose endpoint was closed instead
  (`CLAUDE.md` → Settled decisions: `reachable` is "last request answered", false on every push button).
- [ ] Share code with the sibling project long-term: `jhmesh` `Metadata`/`CDB.parse`, and the cover/climate/trigger/
  logbook/diagnostics boilerplate. HA ≥ 2026.8 binds a device to one config entry, so one integration with two
  transports is the only way to get one device page.

## 4. Documentation corrections

All landed in `887e0f8` (2026-09-15): `docs/bt-mesh-direct.md` (GATT-proxy route, own unicast/sequence counter,
real vendor opcode table, CT range, acked-Set behaviour), `docs/gateway-websocket.md` + `CLAUDE.md` (the §1.1
mechanism, the KeyMode table, `trigger_request` as a descriptor promise), `docs/gateway-architecture.md` (bt_tunnel,
node role), `docs/gateway-rest-api.md` (the app's own API use, token/IP/fingerprint over the mesh, `/project/cdb`),
`docs/matter-bridge.md` / `docs/README.md` (the gateway-free route), and the cover backlog wording. The 2026-09-16
review added the TLS certificate's persistence (it survives a factory reset and firmware updates) to the REST doc.

## 5. Captures that would close open questions

- [ ] Simultaneous ms-resolution capture: `tools/ws-capture/capture_ws.py` + the sibling's mesh sniffer — match each WS
  pair to one mesh copy (expect press at copy arrival, release at +0.4–0.5 s, second pair at copy 2).
- [x] Same on a **key** element (event 5) to confirm the alternating side, and on a **rocker** element (events 0/1) —
  confirmed on the WebSocket side 2026-09-16 (key taps alternate, rocker taps repeat the side); the mesh half of the
  simultaneous capture above is what is still missing.
- [ ] Whether `Scene Recall` from a scene key is doubled (same TID) — settles `gateway-websocket.md:88-92`.
- [ ] Toggle `status_led` from HA while sniffing — confirm `0x5013` via User Property Status.
- [ ] A moving blind (cover backlog) — none exists in this network.
- [ ] Real RTR composition and the numeric ids behind `LBC_PROP_RTR_SCHEDULER_ENABLE_ID` / `_HVACMODE_DISPLAY_ID`
  (firmware table says `0x1246` / `0x120B`).
