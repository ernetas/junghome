# JUNG HOME Gateway — WebSocket protocol

The gateway pushes live state and accepts commands over a WebSocket. This is
what the integration's coordinator uses.

## Connecting

```
wss://<gateway>/ws
```

nginx proxies `/ws` to the internal WebSocket server (`127.0.0.1:8080`) with a
24-hour idle timeout. Authentication uses the **same token as the REST API**,
sent as the `token` header (or a `token` cookie) on the upgrade request. A bad
token is rejected with `401 Unauthorized`.

Every frame is a JSON object of the form:

```jsonc
{ "type": "<string>", "data": <any>, "message_id": "<optional>" }
```

## Handshake (server → client, on connect)

In order:

1. `{ "type": "message", "data": "Hello from JUNG HOME Gateway" }`
2. `{ "type": "version", "data": "1.5.0" }`
3. After ~1 s, the current state is pushed:
   - `{ "type": "functions", "data": [ ...all functions... ] }`
   - `{ "type": "groups", "data": [ ...all groups... ] }`
   - `{ "type": "scenes", "data": [ ...all scenes... ] }`

(The `functions` payload matches `GET /functions/` — see
[gateway-rest-api.md](gateway-rest-api.md).)

## Server → client message types

| `type` | `data` | Meaning |
|--------|--------|---------|
| `message` | string | Info / error text. Errors look like `"error: <reason>"`. |
| `version` | string | Gateway API version. |
| `functions` | array | Full list of functions (sent on connect and on change). |
| `datapoint` | object | A single datapoint changed (broadcast to all clients), **or** the reply to a client `datapoint` command. |
| `scene` | object | A scene was recalled. |
| `groups` / `groups-new` / `groups-deleted` | array | Full groups list / added / removed. |
| `scenes` / `scenes-new` / `scenes-deleted` | array | Full scenes list / added / removed. |
| `devices` / `devices-new` / `devices-deleted` | array | Lower-level device list / added / removed. |
| `config` | object | Configuration (currently not emitted). |

The `*-new` / `*-deleted` variants are how the gateway signals that nodes,
groups, or scenes were added or removed at runtime (e.g. provisioning a new
device in the app).

A pushed `datapoint` frame carries the updated datapoint object, e.g.:

```jsonc
{ "type": "datapoint",
  "data": {
    "id": "id5f09764942a70ce-001",
    "type": "switch",
    "values": [ { "key": "switch", "value": "1" } ]
  } }
```

## Client → server commands

Send a JSON frame with a `type`. An optional `message_id` is echoed back on the
matching reply.

### Set / get a datapoint

```jsonc
{ "type": "datapoint",
  "data": {
    "id": "id5f09764942a70ce-001",
    "values": [ { "key": "switch", "value": "1" } ]
  },
  "message_id": "abc"            // optional
}
```

- If `data.values` is present, the gateway **sets** those values, then re-reads.
- The gateway replies with the fresh datapoint:
  `{ "type": "datapoint", "data": { ...datapoint... }, "message_id": "abc" }`.
- `data.id` is required. (A `type` field inside `data` is ignored by the gateway,
  which looks the datapoint up by `id`; the integration includes one anyway.)

Common `values` keys by device type:

| Device | key / value |
|--------|-------------|
| Switch / light on-off | `switch` = `"0"` / `"1"` |
| Dimmer | `brightness` = `"0".."100"` (device scale) |
| Tunable white | `color_temperature` = Kelvin, e.g. `"2700"` |
| Status LED (rocker) | `status_led` = `"0"` / `"1"` |
| Rocker press (read-only events) | `up_request` / `down_request` / `trigger_request` = `"1"` |

### Other command types

| `type` | Behaviour |
|--------|-----------|
| `message`, `version` | Logged by the gateway; no reply. |
| `api_version` | Reserved / not implemented. |
| `scene`, `functions`, `get_devices` | **Not implemented** — return `{"type":"message","data":"error: ... not implemented ..."}`. Use the REST API for scenes. |
| anything else | `{"type":"message","data":"error: ...message type is unknown"}` |

### Errors

Any failure is returned as a `message` frame:

```json
{ "type": "message", "data": "error: could not set datapoint (id...-001) value, ..." }
```

## Notes for the integration

- State updates arrive as `datapoint` broadcasts; the coordinator matches them to
  entities. Commands are sent as `datapoint` set frames.
- Reconnect on drop: the gateway sends the full `functions`/`groups`/`scenes`
  snapshot again on every new connection, so re-syncing is automatic.
- Watch `functions` / `*-new` / `*-deleted` frames to pick up devices added or
  removed at runtime (the integration also rediscovers on each coordinator
  refresh).
