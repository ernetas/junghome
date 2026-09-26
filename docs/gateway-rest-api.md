# JUNG HOME Gateway — REST API

Base: `https://<gateway>/api/junghome` (TLS, self-signed cert). `<gateway>` can
be the IP or the mDNS name the gateway announces, `junghome-<mac>.local`
(`junghome.local` is only the certificate's CN). API version 1.5.0.

## Authentication

All endpoints require a token **except** `version`, `register`,
`register/by-password`, and `apidoc`. Pass the token in the `token` request
header (a cookie named `token` is also accepted):

```
token: <jwt>
```

The token is an HS256 JWT whose payload is `{"user_id":"<8 hex>"}`. It is signed
with a per-user secret stored on the gateway (`api-server/res/tokens/<id>.tkn`).
An invalid/missing token returns `401 {"error":"Unauthorized"}`.

## `GET /devices/?verbose=true` — the raw device objects

Marked *deprecated / experimental* in the OpenAPI, but live and read-only on
2.1.3 (2840). Without `verbose` the endpoint returns the reduced DTO
(`device_id`, `device_type`, `label`, `groups`, `states` with `state_id` /
`state_type` / `value` / `mode`); with `verbose=true` it returns the middleware's
`JungHomeDevice` objects verbatim (`04_devices-controller.js:60-70`), i.e.
everything `/functions/` drops. Probed 2026-09-16 (49 devices, 187 KB):

| field | what it carries |
|---|---|
| `device_type` | the middleware's type (`OnOffLight`, `TuneableWhiteLight`, `SocketEnergy`, `PushButton`, …), finer than the function type |
| `states[*].statistics` | `reachable`, `last_seen` (s), `latest_request` (ms), `retry_attempts`, `connection_quality` (0–100), `not_supported`. **Not live**: the api-server answers from its cache, which takes a state's object only when its *value* changes (or the whole device list is republished) — `jung-device-service.js:249-276` — and the middleware serialises that object *before* the mesh answer is counted (`device_state_service.js:249-253`: `communicateToAPI`, then `notify_received`). So the flags are a snapshot from before the state's last change was acknowledged: 162 states/properties of the probe held a value yet read `reachable: false`, `last_seen: 0`. The live flag would not help either: `reachable` means "the last request for this state was answered" (`models/device-states.js:596-619`, false after `state_acceptable_request_fails` failures, which also resets the value to `NaN`), and push buttons never answer requests for their key states. Not an availability signal |
| `states[*].profile` | `index` (the datapoint suffix, in hex: `input_power` index 16 ↔ `-010`), `range`, `unit`, `readable`/`writeable`/`visible`, `dirtyAfterSeconds` — how long after its last report or request a state counts as stale and is re-read (300 for most states, 3600 for the energy counter and `software_revision`; see the polling model in [bt-mesh-direct.md](bt-mesh-direct.md)) |
| `states[*].model` | the mesh binding: `address` (element unicast), `server`/`client` model ids, `publish` (group), `bind`, `category` |
| `property` | device *properties* — never states, so never datapoints: `software_revision` (`[2, 2, 0, 2]` = device firmware 2.2.0.2 — a *node* property, filled on each node's main-element function and `null` on its other functions: in the probe all 27 main-element functions carried it and all 22 others were `null` — 18 of the 20 push-button functions, 4 lights; filled: lights 18 × 2.2.0.2, 5 × 2.2.0.1, 2 buttons 2.2.0.2, both sockets 2.2.0.1; why, in the caveats below), `key_mode` (0..6, see the WebSocket doc), `switch_operation_mode`, `enforced_output`, `device_key_lock`; on `SocketEnergy` additionally **`total_device_energy_use` in Wh** (e.g. 209655) — re-read from the device only **hourly** (`dirtyAfterSeconds` 3600, `device_property_states/TotalDeviceEnergyUse.js:65` `POLL_60MIN`) — and `total_device_power_on_time` in h (300) — the cumulative energy the function list lacks |

The integration reads it (`models.parse_devices_verbose`): the full list once
after the first refresh (and again only when a function appears that the last
answer did not list — one the endpoint omits is not asked for again; a missing
endpoint or a failed read is retried every interval, one small request), then
`GET /devices/{device_id}?verbose=true` — the same object for one device,
~8 KB — every five minutes for each device that has an energy counter (the
counter itself only moves when the gateway re-reads it, about hourly, so the
sensor steps once an hour; the cheap re-read just keeps the lag short). That
feeds the `total_energy` sensor (Wh, `total_increasing`) and exempts buttons
whose `software_revision` is known to predate 2.2.0 from duplicate-press
suppression. Caveats: the endpoint is declared subject to change (the sensor
simply disappears on firmware without it); `property` values read `null`
until the middleware has read them, and again after repeated unanswered
re-reads (which reset the value and push the reset to the cache,
`device_state_service.js:235-240`). That second case is why a node's
non-main functions read `software_revision` `null` — a gateway bug, not a
read failure on the device: the middleware means to skip polling a node
property on those functions and to copy it from the main-element function
(`[POLL-SKIP]`/`[PROP-REDIRECT]` and `[PROP-SYNC]`,
`services/device_state_service.js:388-393`, `:406-425`, `:436-465`), but
`Product.isDeviceAtMainElement` (`models/product.js:41-43`) also counts the
function's *properties*, and every function's property states sit at the
node's element 0 — so every function passes as the main one and polls the
revision itself. The node answers, but the answer is matched to the first
function with a *state* at that address (`handler/bt_event_handler.js:469`),
so the other function's request times out every hour (~850–900 `no answer
from …-017` warnings per such function in the June–July middleware log) and
its value is reset (`models/device-states.js:611-624` `onResponseFail`)
between `[PROP-SYNC]` copies, which happen only when the main function's
value changes (`device_state_service.js:254-257`). `statistics` cannot tell a stale
counter from a fresh one (the second reference socket's counter held 53150 Wh
with `last_seen: 0` — the cache snapshot described above); no cover exists in the reference network, so
`move_operation_mode` (the awning hint) is still unobserved. Labels are in it
— keep captures out of issues.

## `GET /healthstatus/` — the gateway's health log

Derived from the v2.1.3 (2840) implementation (`disk_dump/jung-20260801/sdb2/opt`;
no live capture yet). Behind `auth()` with no role (`api-server/dist/server.js:128`),
so any registered token reads it; a missing/unknown token gets `401`, never
`403`. The controller (`controllers/10_healthstatus-controller.js`) returns the
list the middleware last published over its IPC `gateway_errors` topic,
replaced wholesale on each publish (`services/jung-healthstatus-service.js`).
The WebSocket push of it is commented out (`websocket-server-service.js`
`_on_error_event`: "DO not send Healthstatus over websocket"), so it can only be
polled.

```json
[
  {"level": "INFO", "time": "2026-09-26T08:03:12.345Z",
   "description": "Your JUNG HOME Gateway is up and running",
   "details": "You now have access to all the features without additional waiting time"},
  {"level": "ERROR", "time": "2026-09-26T08:01:40.002Z",
   "description": "out of sequence numbers",
   "details": "Your JUNG HOME Gateway may lost its abillity to communicate to the Bluetooth Mesh Network. In Case you face problems you may reset your Gateway."}
]
```

- **An append-only log since the middleware started, newest first**
  (`middleware/dist/services/health_status_service.js`: every `debug` / `info`
  / `warn` / `error` call pushes an entry and republishes `[...list].reverse()`).
  Nothing removes an entry: a condition "clears" only on a gateway restart
  (empty list) or when a later entry supersedes it. It is also not
  deduplicated — a repeating trigger logs again each time.
- `level` is `DEBUG` / `INFO` / `WARN` / `ERROR`. `time` is
  `Date.toISOString()` — **ISO-8601 UTC**, not the "European String" the
  api-server DTO comment and the `/apidoc` `HealthStatusList` schema promise.
  `details` is a string (the caller's text, or the fixed default "No further
  detailed description available. You may be able to resolve or clear an
  existing error by restarting your JUNG HOME gateway."), except that the two
  startup failures in `startup.js:151,181` pass a caught error through, which
  can arrive as a JSON object (`{}`) — the schema's `string` is a promise.
- Besides the explicit calls, every `state_db` flag with `info_level`
  `error`/`warn` logs an entry each time it is set `true`
  (`configuration_service.js:196-207`), described by its key with the
  **first** underscore replaced (`String.replace("_", " ")`): `time error`,
  `btmesh error`, `project not_uploaded`, `btmesh device_not_available`,
  `cloud error`, …, with the flag's `state_db` description as `details`.

The messages the current build can emit (`middleware/dist`):

| level | description | source | meaning |
|---|---|---|---|
| ERROR | `JUNG HOME Gateway Bluetooth Chip start failure` | `services/ncp_service.js:68` | mesh node init failed at chip boot |
| ERROR | `was not able to start bluetooth adapter, details: ` | `startup.js:151` | the Bluetooth adapter did not start |
| ERROR | `was not able to start gateway correctly, details: ` | `startup.js:181` | startup did not complete |
| ERROR | `out of sequence numbers` | `services/ncp_service.js:192` | chip answered `503 BT_MESH_LIMIT_REACHED`: the gateway can no longer send on the mesh |
| ERROR | `JUNG HOME Gateway Time Sync Error` | `handler/sys_event_handler.js:154` | a failed sync more than 24 h after the last good one (`details`: "Last successful sync was N hours ago") |
| WARN | `JUNG HOME Project missing` | `services/project_file_service.js:156` | no project 5 min after provisioning (`btmesh.no_projectfile_timeout` 300000 ms) |
| WARN | `JUNG HOME project is incomplete` | `services/project_file_service.js:202` | the stored project has no JUNG HOME `meta` (names, rooms, scenes); checked at middleware start |
| WARN | `JUNG HOME project has no devices` | `services/devices_service.js:64` | provisioned, but the project has no devices |
| WARN | `JUNG HOME Devices are unreachable` / INFO `All JUNG HOME Devices are reachable` | `services/devices_service.js:174,178` | `isDeviceOnline` over all devices, logged when the count changes; `details` lists the **labels**. Reads working push buttons as unreachable (see the verbose section) |
| WARN | `JUNG HOME Devices have poor signal quality` / INFO `… signal quality improved` | `services/devices_service.js:186,191` | any state's `connection_quality` < 10; `details` lists labels |
| WARN | `no info about linked devices` | `services/devices_service.js:346` | the project's metadata has no connection group for the gateway (its text: push buttons may not be exposed correctly) |
| WARN / ERROR | `<topic> <rest_of_key>` flag entries | `services/configuration_service.js:203,207` | see above; `time error` fires on **every** failed NTP sync |
| INFO | `New Bluetooth Mesh Project` | `handler/ip_event_handler.js:528,585` | a project import succeeded (after the project flags were reset) |
| INFO | provisioning / factory reset / cloud / `New User Permission` / `Your JUNG HOME Gateway is up and running` | `bt_event_handler.js`, `sys_event_handler.js:187`, `cloud_connection_service.js`, `api_access_service.js`, `startup.js:220` | informational; the cloud one quotes the **myJUNG user name**, `New User Permission` the **client name** |
| DEBUG | `Update History`, `Ethernet Speed Configuration: …`, `high cpu load`, `SN error read from flash`, `IV Update …`, `project upload failed`, `Provisioning of Gateway failed` | `startup.js`, `cpu_load_service.js`, `ncp_service.js`, `ip_event_handler.js` | diagnostics |

The integration polls it (`coordinator.async_fetch_health_status`, `health.py`)
once after the first refresh and every 15 minutes, best-effort (any non-200 —
401 included — or transport error changes nothing), and raises one repair
issue per condition: Bluetooth failure (the first two rows), out of sequence
numbers, time sync (withdrawn as soon as `GET /config/parameter/time_error`
reads `false` — the log has no "recovered" entry), project missing (`JUNG HOME
Project missing` or `project not_uploaded`) and project incomplete (both
withdrawn by a newer `New Bluetooth Mesh Project`). The others — the
unreachable list above all — go to diagnostics only, with the myJUNG user
and client names masked.

## Discovering the full spec

The complete OpenAPI 3.0 document is served **unauthenticated** at:

```
GET https://<gateway>/api/junghome/apidoc
```

(The Swagger *UI* at `/api/junghome/swagger` may 404 on some firmware — its HTML
redirect target is missing — but `/apidoc` returns the raw spec regardless.)

```sh
curl -sk https://<gateway>/api/junghome/apidoc | jq .
```

## Registering a client (getting a token)

Two methods, both returning `200 {"token":"<jwt>"}`:

### A. By app approval (used by this integration)

```
POST /api/junghome/register
Content-Type: application/json

{ "user_name": "Home Assistant" }
```

The request **blocks for up to 180 s** (`register_timeout_ms`) while the user
approves it in the JUNG HOME app under **Settings → Gateway → Access Permissions
→ Open Requests** (it appears there as `"<id>. <user_name>"`). On approval the
gateway creates a client and returns the token. Re-POSTing the same
`user_name` + client IP reuses the existing pending request. On timeout it
returns `400 {"error":"Error during register."}`. The field is `user_name`
(not `name`/`user`).

### B. By password (instant)

```
POST /api/junghome/register/by-password
Content-Type: application/json

{ "password": "<network key password>" }
```

Returns a token immediately, or `401` if the password is wrong. The password is
the gateway's network-key password.

## Endpoints

`{...}` are path params. Auth required unless noted.

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/version/` | `api_version` (the API contract, "1.5.0") **plus** `version_release` / `version_build` — the gateway's software version, readable without a token (`01_version-controller.js:32-42`). |
| GET  | `/apidoc` | Full OpenAPI spec (no auth). |
| POST | `/register` | Request a token via app approval (no auth). |
| POST | `/register/by-password` | Request a token via password (no auth). |
| GET  | `/healthstatus/` | The gateway's health log since the middleware started, newest first — see the section above. |
| GET  | `/functions/` | All functions (devices as the app/integration sees them). |
| GET  | `/functions/{function_id}` | One function. |
| GET  | `/functions/{function_id}/datapoints` | A function's datapoints. |
| GET  | `/functions/{function_id}/datapoints/{datapoint_id}` | One datapoint. |
| PATCH| `/functions/{function_id}/datapoints/{datapoint_id}` | **Set** a datapoint (control). Body `{"data":[{"key":"switch","value":"1"}]}`. |
| GET  | `/devices/` | All devices (lower-level device view); `?verbose=true` returns the raw middleware objects — reachability, properties, energy counters — see the section above. |
| GET  | `/devices/{device_id}` | One device. |
| GET  | `/devices/states/{state_id}` | One device state. |
| PATCH| `/devices/states/{state_id}` | Set a device state. |
| GET  | `/groups/`, `/groups/{group_id}` | Groups (BT-Mesh group addresses). |
| GET  | `/scenes/`, `/scenes/{scene_id}` | Scenes. |
| POST | `/scenes/{scene_id}` | Trigger / recall a scene. |
| GET  | `/types/functions`, `/types/function_versions`, `/types/datapoints`, `/types/datapoint_versions` | Type/template catalog. |
| GET  | `/config/`, `/config/types`, `/config/parameter/{parameter}`, `/config/topic/{topic}` | Gateway configuration. `parameter/system_serial` returns the hardware serial as a raw JSON string — the same cpuinfo-derived value the mDNS TXT record advertises (`serial=`); read-only, populated by the middleware shortly after boot (empty string until then), 404 on firmware without it. The integration keys config entries on it. `parameter/version_release` and `parameter/version_build` return the gateway's **software** version, e.g. `"2.1.3"` and `"2840"` — the middleware populates them from the board controller's `MSG_SW_VERSION_IND` (raw form `"2.1.3 Release (2840)"`, split on the parentheses) and ships the declared defaults `"0.0.0"` / `"0"` until it has answered. Do **not** use the WebSocket `version` frame for this: that is the API version. |
| POST | `/config/` | Update configuration — this is the app's main write channel; see [How the JUNG HOME app uses the API](#how-the-jung-home-app-uses-the-api). |
| GET  | `/products/`, `/products/{uuid}` | Product catalog *(API 1.5.0+, i.e. gateway fw 2.1.x)*. |
| GET  | `/project/cdb`, `/project/junghome` ; PATCH `/project` | Project / mesh DB export *(API 1.5.0+, i.e. gateway fw 2.1.x)*. The integration parses `/project/junghome` into `NodeIdentity` (`models.parse_project_export`) at setup; a function id is `"id" + md5(UUID upper-case with dashes + 4-hex element location)[:15]` (`util/project_file_helper_methods.js:19-31`, location parsed as hex from `elements[0]` in `services/devices_service.js:211`), `models.function_id_for`. **`/project/cdb` returns the Bluetooth Mesh CDB including NetKey, AppKeys and every device key**; `/project/junghome` is the app's `ExportDto` (node UUID / MAC / unicast / locations per device — the hardware identity the `functions` payload lacks) **and carries the same keys**: it returns the stored upload whole, keys converted to camelCase (`03_project-file-controller.js:13-27,47-61`, `project_file_service.js:88-99`), whose `network` field is that CDB, Base64-encoded. Treat both as secrets. |
| GET  | `/log/...` | Diagnostic snapshots (system, kernel, middleware, api-server, jungremote-client, bt_mesh_project, jung_home_project). |

> Endpoint set grows with firmware. `products/*` and `project/*` exist on 1.5.0
> but not on the previous partition's 1.1.0 build (the api-server version that
> ships with gateway firmware v2.0.0). Always confirm against `/apidoc`.

## `functions` payload (what the integration uses)

```jsonc
[
  {
    "id": "id5f09764942a70ce",          // "id" + md5(node UUID + hex(location))[:15] — changes on re-provisioning / re-enumeration (not on a device-firmware update: all 26 surviving nodes kept theirs across app 2.1.0 → 2.2.0)
    "type": "OnOff",                     // OnOff | ColorLight | Socket | RockerSwitch | ...
    "label": "Balcony Light",            // user-set, stable across updates
    "parent_groups": ["id49186"],
    "datapoints": [
      { "id": "id5f09764942a70ce-001",   // "<device_id>-<suffix>"; suffix is stable
        "type": "switch",
        "values": [ { "key": "switch", "value": "0" } ] }
    ]
  }
]
```

The integration derives stable entity IDs from `label` + the datapoint **suffix**
(`-001`, `-00e`, …) because `id` has changed across app-driven firmware
updates (it is derived from the node UUID and location, so a re-provisioned or
re-enumerated node gets a new one). A function is one mesh element; a group
`id` is `"id"` + the decimal group address (`id49186` = `0xC022`); a scene
`value` is the mesh scene number. See
[gateway-architecture.md](gateway-architecture.md).

## Control via REST vs WebSocket

You can set a datapoint with `PATCH /functions/{id}/datapoints/{dpid}` (body
`{"data":[{"key":"switch","value":"1"}]}`), but this integration uses the
WebSocket for both state updates and commands — see
[gateway-websocket.md](gateway-websocket.md).

## How the JUNG HOME app uses the API

The app is just another API client. The shapes below come from the
Bluetooth-direct sibling project's Android app analysis
(`docs/android/network-logic.md`, `docs/gap-analysis/network-features.md`
there), cross-checked with the middleware where noted; confirm exact bodies
against `/apidoc` before relying on them.

- **`GET /config/`** returns the `GatewayConfigDTO` the app polls every 5 s for
  its status page: `version_release`, `version_build`, `system_serial`,
  `ip_address`, `ip_subnet`, `ip_dns`, `ip_gateway`, `ip_mac`, `ip_dhcp`,
  `project_file`, `cloud_register`, `cloud_connect`,
  `btmesh_device_not_available`, `btmesh_error`, `cloud_error`, `ip_error`,
  `api_clients[]`, `api_client_name_asking[]`.
- **`POST /config/`** is the app's write channel:

| Body | Purpose |
|---|---|
| `{"data": {"project_file": <ExportDto>}}` | Uploads the whole mesh project (nodes, keys, groups, scenes, locations). **This is how the gateway gets the network keys**: the middleware installs its own node from it (`services/ncp_service.js:299-351`) and re-runs self-configuration. The app sends it automatically after every change it makes (2 retries 15 s apart, Wi-Fi only) — a gateway-free client that changes the network must do the same or the gateway drifts. |
| cloud credentials (`GatewayLoginDTO{userName, userPassword, cloudRegister}`) | Pairs the gateway with the JUNG cloud for the `jungremote-client` link. |
| `{"api_client_accept": "<name>"}` | Approves a pending `POST /register` request (how a third-party client such as this integration gets its token). |
| `{"api_client_reset": true}` | Revokes all third-party clients. |
| `{"ip_dhcp", "ip_address", "ip_subnet", "ip_dns"}` (strings, `null` when DHCP) | Network settings. |

The gateway signals a pending registration back to the app **over the mesh**:
vendor property `0xC000 api_status` is one byte with bit 0 = API available and
bit 1 = "a client is asking for a name"
(`services/btmesh_property_service.js:161-167`; the same
`api_client_name_asking` the DTO lists).

### Security notes

- **Bootstrap over the mesh.** Before it can call the API the app reads the
  gateway's **API token, IPv4 address and TLS certificate fingerprint** from
  vendor properties hosted on the gateway's own element — `0xC001 api_token`,
  `0xC002 ip_v4`, `0xC003 fingerprint_sha256` (ASCII;
  `services/btmesh_property_service.js:38-45,151-170` — the token is the
  "smartphone" token the middleware keeps in memory) — with a vendor
  **Manufacturer Property Get** (`C8 27 05`) at app start and again on any
  HTTP 401/404/503 (sibling's app analysis). These are ordinary vendor
  property Gets answered under AppKey 0, so **anyone holding AppKey 0 can read
  a valid API token off the mesh**. The mesh keys are the real credential; the
  JWT is derived access.
- **`GET /project/cdb` exposes the mesh keys.** Any authenticated API client —
  including one approved for a third-party integration — can download the CDB
  with NetKey, AppKeys and all device keys, i.e. everything needed to control
  (or reconfigure) every device without the gateway. Treat an API token as
  equivalent to the network keys, redact both in diagnostics, and never
  commit either.
- The TLS certificate is self-signed; the app pins it with a custom trust
  manager that accepts only the certificate whose SHA-256 matches the
  fingerprint it read over the mesh. A client that merely skips verification
  is open to on-path interception of the token — and, worse, to redirection:
  the mDNS TXT record carries `serial`/`mac`/`version` but **no
  fingerprint**, and the serial is public, so a forged `_junghome._tcp`
  announcement naming a configured serial could point a client at any
  address. This integration therefore pins too, without the mesh: it learns
  the fingerprint on first contact (trust on first use — a bare TLS
  handshake pinned to an impossible digest, whose `ServerFingerprintMismatch`
  reports the real one; no request is sent) and passes
  `ssl=aiohttp.Fingerprint(...)` on every request and WebSocket upgrade over
  Home Assistant's `verify_ssl=False` session, so a mismatch aborts at the
  handshake before the `token` header exists. A changed certificate is
  surfaced as a repair issue and re-pinned only after the user confirms;
  discovery moves an entry's address only when the entry cannot reach its
  gateway and the new address presents the pinned certificate
  (`custom_components/junghome/tls.py`, `config_flow.py`, `repairs.py`).
  The certificate itself is stable: `etc/nginx/generate_ssl_key.sh` (run as
  `nginx.service` `ExecStartPre`) writes `svs.key`/`svs.crt`/`fingerprint.txt`
  into `/data/etc/nginx/` only when one is missing, unparseable or
  mismatched, and the factory-reset sequence in `board_ctrl` wipes only the
  four `res` trees (`/data/{middleware,api-server,jungremote-client}/res*`,
  `ncp_ctrl/res`) — the imaged gateway's certificate dates from 2023 and is
  still in service. A changed fingerprint therefore means a replaced gateway
  or a re-imaged card, never a factory reset or a firmware update.
