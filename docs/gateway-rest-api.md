# JUNG HOME Gateway — REST API

Base: `https://<gateway>/api/junghome` (TLS, self-signed cert). `<gateway>` can
be the IP or `junghome.local`. API version 1.5.0.

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
| GET  | `/version/` | API version (no auth). |
| GET  | `/apidoc` | Full OpenAPI spec (no auth). |
| POST | `/register` | Request a token via app approval (no auth). |
| POST | `/register/by-password` | Request a token via password (no auth). |
| GET  | `/healthstatus/` | Gateway/mesh health. |
| GET  | `/functions/` | All functions (devices as the app/integration sees them). |
| GET  | `/functions/{function_id}` | One function. |
| GET  | `/functions/{function_id}/datapoints` | A function's datapoints. |
| GET  | `/functions/{function_id}/datapoints/{datapoint_id}` | One datapoint. |
| PATCH| `/functions/{function_id}/datapoints/{datapoint_id}` | **Set** a datapoint (control). Body `{"data":[{"key":"switch","value":"1"}]}`. |
| GET  | `/devices/` | All devices (lower-level device view). |
| GET  | `/devices/{device_id}` | One device. |
| GET  | `/devices/states/{state_id}` | One device state. |
| PATCH| `/devices/states/{state_id}` | Set a device state. |
| GET  | `/groups/`, `/groups/{group_id}` | Groups (BT-Mesh group addresses). |
| GET  | `/scenes/`, `/scenes/{scene_id}` | Scenes. |
| POST | `/scenes/{scene_id}` | Trigger / recall a scene. |
| GET  | `/types/functions`, `/types/function_versions`, `/types/datapoints`, `/types/datapoint_versions` | Type/template catalog. |
| GET  | `/config/`, `/config/types`, `/config/parameter/{parameter}`, `/config/topic/{topic}` | Gateway configuration. |
| POST | `/config/` | Update configuration. |
| GET  | `/products/`, `/products/{uuid}` | Product catalog *(fw 1.5.0+)*. |
| GET  | `/project/cdb`, `/project/junghome` ; PATCH `/project` | Project / mesh DB export *(fw 1.5.0+)*. |
| GET  | `/log/...` | Diagnostic snapshots (system, kernel, middleware, api-server, jungremote-client, bt_mesh_project, jung_home_project). |

> Endpoint set grows with firmware. `products/*` and `project/*` exist on 1.5.0
> but not on the 1.4.1 build seen on disk. Always confirm against `/apidoc`.

## `functions` payload (what the integration uses)

```jsonc
[
  {
    "id": "id5f09764942a70ce",          // volatile; regenerated on firmware update
    "type": "OnOff",                     // OnOff | ColorLight | Socket | RockerSwitch | ...
    "label": "Ernesto balkonas",         // user-set, stable across updates
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
(`-001`, `-00e`, …) because `id` changes on firmware updates. See
[gateway-architecture.md](gateway-architecture.md).

## Control via REST vs WebSocket

You can set a datapoint with `PATCH /functions/{id}/datapoints/{dpid}` (body
`{"data":[{"key":"switch","value":"1"}]}`), but this integration uses the
WebSocket for both state updates and commands — see
[gateway-websocket.md](gateway-websocket.md).
